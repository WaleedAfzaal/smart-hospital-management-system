"""Hospital Management System — core logic (data structures + operations).

Step 1 (SQLite): the Hospital can now be opened with a database file
(``Hospital(db_path="hospital.db")``) — every change is saved at once and
loaded back on the next start, so data survives a restart. With no
``db_path`` it works exactly as before: fully in memory, nothing written.
The undo stack is session-only and starts empty after a restart.

Step 3 (Billing + Reports): patients remember the date they registered
and appointments remember the date they were booked, so the daily
report can count "today" honestly. Bills live in ``hospital.billing``
(see billing.py) and are created from a booked appointment — the first
line item is that appointment's consultation fee.

Step 4 (Appointment Date & Time): every appointment also carries the
date and time it is SCHEDULED for (fields 8 and 9 of the heap row).
The booking date (field 7) is when the token was issued; the
appointment date/time is when the patient should come. The queue order
is unchanged — Emergency still comes first; date/time is for
scheduling and display only. ``appointments_for_date`` / ``doctor_schedule``
list appointments for a given day, sorted by time, then priority.

Step 5 (Patient Search / Edit / Delete): ``search_patients`` finds
patients by part of their name (case-insensitive); ``edit_patient``
changes name/age/problem (the ID never changes, and a renamed
patient's queued appointments show the new name); ``delete_patient``
removes the record + notes + queued appointments and frees their bed,
while their bills are KEPT as financial records.

Step 6 (Appointment Statuses + History): every appointment carries a
status — Pending, Confirmed, Checked In, In Progress, Completed,
Cancelled or No Show — as the 10th field of the heap row. New
appointments start Pending; old rows (before Step 6) load as Pending.
Every status change is written to the status-history table (see
status_history.py) with the old/new status, the user who changed it
and the time — the history is an audit trail and is never deleted,
even when the patient or the appointment goes away. Booking refuses
a double booking: the same doctor cannot have two ACTIVE
appointments (anything except Cancelled / No Show) at the same date
and time. Doctors may only move their OWN queue's appointments to
Checked In, In Progress or Completed; Admin and Receptionist may set
any status.
"""

import datetime
import getpass
import os
import re

from auth import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME, AuthStore, can
from billing import Billing
from database import Database
from status_history import StatusHistory

# Step 6: appointment statuses. "Pending" is the default for a new
# booking; old rows (booked before Step 6) load as Pending too.
APPOINTMENT_STATUSES = ("Pending", "Confirmed", "Checked In",
                        "In Progress", "Completed", "Cancelled", "No Show")
# Statuses a Doctor may set — only on their OWN queue (Admin and
# Receptionist may set any status on any doctor's queue).
DOCTOR_STATUSES = ("Checked In", "In Progress", "Completed")
# Appointments in these statuses no longer occupy their time slot —
# a new booking at the same date/time is allowed after one of these.
_INACTIVE_STATUSES = ("Cancelled", "No Show")


def _row_status(row):
    """An appointment heap row's status — "Pending" when the row was
    built before Step 6 (9 fields) or its stored status is empty."""
    if len(row) > 9 and row[9]:
        return row[9]
    return "Pending"


def _public_appointment(doc, row):
    """A plain dict for one heap row — the shape the API and the CLI
    both use, so the status is the same everywhere."""
    return {
        "token": row[2],
        "doctor_id": doc["id"],
        "doctor_name": doc["name"],
        "patient_id": row[3],
        "patient_name": row[4],
        "fee": row[5],
        "emergency": row[0] == 1,
        "booked_date": row[6] if len(row) > 6 else None,
        "appt_date": row[7] if len(row) > 7 else None,
        "appt_time": row[8] if len(row) > 8 else None,
        "status": _row_status(row),
    }

# SQLite file used by the CLI and the web server (same file, so both
# see the same patients, appointments and beds).
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "hospital.db")


# ------------------------------------------------ Step 4: date & time
_APPT_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate_appt_datetime(appt_date, appt_time):
    """Check an appointment date/time and return (date, time) cleaned up.

    Date: ``YYYY-MM-DD``; empty/None means today. Time: ``HH:MM`` in
    24-hour format; empty/None means "no fixed time". Raises
    ValueError with a plain message on anything else (a wrong month,
    30 February, 25:00, 10:60, or a date written any other way).
    """
    if appt_date in (None, ""):
        date_str = datetime.date.today().isoformat()
    else:
        try:
            date_str = datetime.date.fromisoformat(str(appt_date).strip()).isoformat()
        except ValueError:
            raise ValueError(
                "Date must look like YYYY-MM-DD (e.g. 2026-11-28).")
    if appt_time in (None, ""):
        time_str = None
    else:
        time_str = str(appt_time).strip()
        if not _APPT_TIME_RE.match(time_str):
            raise ValueError(
                "Time must look like HH:MM in 24-hour format (e.g. 10:30).")
    return date_str, time_str


def _appt_when(row):
    """'2026-11-28 10:30' / '2026-11-28' / '' for a heap row.

    Rows booked before Step 4 have no appointment date/time (fields 8
    and 9 are missing or None) — they show nothing, they never crash.
    """
    appt_date = row[7] if len(row) > 7 else None
    appt_time = row[8] if len(row) > 8 else None
    if not appt_date:
        return ""
    return f"{appt_date} {appt_time}" if appt_time else appt_date


def _public_patient(pat):
    """A plain dict copy of a patient (notes as a list) — safe to hand
    out to callers and the API without exposing the internal LinkedList.
    """
    notes = []
    node = pat["notes"].head
    while node is not None:
        notes.append(node.val)
        node = node.next
    return {"id": pat["id"], "name": pat["name"], "age": pat["age"],
            "problem": pat["problem"], "visits": pat["visits"],
            "notes": notes, "registered": pat.get("registered")}


class Node:
    def __init__(self, val):
        self.val, self.next = val, None


class LinkedList:
    def __init__(self):
        self.head = None

    def add_last(self, val):
        node = Node(val)
        if not self.head:
            self.head = node
            return
        curr = self.head
        while curr.next:
            curr = curr.next
        curr.next = node

    def remove_last(self):
        if not self.head:
            return None
        if not self.head.next:
            val, self.head = self.head.val, None
            return val
        curr = self.head
        while curr.next.next:
            curr = curr.next
        val, curr.next = curr.next.val, None
        return val

    def show(self):
        curr, i = self.head, 1
        while curr:
            print(f"  {i}. {curr.val}")
            curr, i = curr.next, i + 1


class Stack:
    def __init__(self):
        self.top = None

    def push(self, val):
        node = Node(val)
        node.next, self.top = self.top, node

    def pop(self):
        if not self.top:
            return None
        val, self.top = self.top.val, self.top.next
        return val


class Queue:
    def __init__(self):
        self.front = self.rear = None

    def enqueue(self, val):
        node = Node(val)
        if not self.rear:
            self.front = self.rear = node
        else:
            self.rear.next = self.rear = node

    def dequeue(self):
        if not self.front:
            return None
        val, self.front = self.front.val, self.front.next
        if not self.front:
            self.rear = None
        return val

    def show(self):
        curr, i = self.front, 1
        while curr:
            print(f"  {i}. {curr.val}")
            curr, i = curr.next, i + 1


class CircularQueue:
    def __init__(self, size):
        self.size, self.items = size, [None] * size
        self.front = self.rear = self.total = 0

    def enqueue(self, item):
        if self.total == self.size:
            return False
        self.items[self.rear] = item
        self.rear = (self.rear + 1) % self.size
        self.total += 1
        return True

    def rotate(self):
        if self.total <= 1:
            return self.items[self.front] if self.total else None
        first = self.items[self.front]
        self.front = (self.front + 1) % self.size
        if self.total < self.size:
            self.items[self.rear] = first
            self.rear = (self.rear + 1) % self.size
        else:
            self.rear = self.front
        return self.items[self.front]

    def show(self):
        for i in range(self.total):
            pos = (self.front + i) % self.size
            tag = " [ON DUTY]" if i == 0 else ""
            print(f"  {i + 1}. {self.items[pos]}{tag}")


class PriorityQueue:
    def __init__(self):
        self.heap, self.count = [], 0

    def _comes_first(self, a, b):
        return (a[0], a[1]) < (b[0], b[1])

    def _move_up(self, i):
        while i > 0:
            p = (i - 1) // 2
            if self._comes_first(self.heap[i], self.heap[p]):
                self.heap[i], self.heap[p] = self.heap[p], self.heap[i]
                i = p
            else:
                break

    def _move_down(self, i):
        n = len(self.heap)
        while True:
            best, l, r = i, 2 * i + 1, 2 * i + 2
            if l < n and self._comes_first(self.heap[l], self.heap[best]):
                best = l
            if r < n and self._comes_first(self.heap[r], self.heap[best]):
                best = r
            if best == i:
                break
            self.heap[i], self.heap[best] = self.heap[best], self.heap[i]
            i = best

    def enqueue(self, is_emg, pid, name, fee, appt_date=None, appt_time=None):
        self.count += 1
        token = f"{'E' if is_emg else 'N'}-{self.count}"
        # Step 3: the booking date rides along as the 7th field, so the
        # daily report can count appointments booked "today". Readers
        # that only need the first six fields are unaffected.
        # Step 4: the SCHEDULED date/time are the 8th and 9th fields.
        # Empty date means today, empty time means "no fixed time".
        # Bad values raise ValueError (see validate_appt_datetime).
        appt_date, appt_time = validate_appt_datetime(appt_date, appt_time)
        # Step 6: the status rides as the 10th field — every new
        # appointment starts Pending.
        self.heap.append((1 if is_emg else 2, self.count, token, pid, name,
                          fee, datetime.date.today().isoformat(),
                          appt_date, appt_time, "Pending"))
        self._move_up(len(self.heap) - 1)
        return token

    def remove_token(self, token):
        for i, row in enumerate(self.heap):
            if row[2] == token:
                removed = self.heap[i]
                last = self.heap.pop()
                if i < len(self.heap):
                    self.heap[i] = last
                    self._move_up(i)
                    self._move_down(i)
                return removed
        return None


def bubble_sort(items, field):
    copy = items[:]
    for i in range(len(copy)):
        for j in range(len(copy) - i - 1):
            if copy[j][field] > copy[j + 1][field]:
                copy[j], copy[j + 1] = copy[j + 1], copy[j]
    return copy


def binary_search(sorted_items, pid):
    low, high = 0, len(sorted_items) - 1
    while low <= high:
        mid = (low + high) // 2
        if sorted_items[mid]["id"] == pid:
            return sorted_items[mid]
        if sorted_items[mid]["id"] < pid:
            low = mid + 1
        else:
            high = mid - 1
    return None


class Hospital:
    NUM_BEDS = 10

    def __init__(self, db_path=None):
        # A fresh hospital starts with ZERO doctors: the old 5 starter
        # doctors were removed (Nov 2026) — staff add their own doctors
        # with add_doctor (CLI menu or web dashboard).
        self.patients_map, self.patients_list, self.doctors_map = {}, [], {}
        self.beds = [0] * self.NUM_BEDS
        self.undo_stack, self.pharmacy_q, self.shift_q = Stack(), Queue(), CircularQueue(20)
        # Persistence (Step 1): with a db file, load saved state (or
        # save this fresh empty state to the file on first run). Without
        # one, everything stays in memory as before.
        self.db_path = db_path
        self.db = Database(db_path) if db_path else None
        # Step 3: bills live in their own store (same file when a db is
        # given, pure in-memory otherwise — see billing.py).
        self.billing = Billing(db_path)
        # Step 6: status history lives in its own store (same file when
        # a db is given, pure in-memory otherwise — see
        # status_history.py). It is an audit trail: only appended to,
        # never rewritten by Database.save().
        self.status_history = StatusHistory(db_path)
        if self.db is not None:
            self.db.load(self)

    def save(self):
        """Write the current state to the database file (no-op in memory)."""
        if self.db is not None:
            self.db.save(self)

    def _persist(self):
        """Save after every change, so a restart never loses data."""
        self.save()

    def add_patient(self, pid, name, age, problem, save_undo=True):
        if pid in self.patients_map:
            return print("  ID already exists.")
        # Step 3: remember the registration date for the daily report.
        pat = {"id": pid, "name": name, "age": age, "problem": problem,
               "visits": 0, "notes": LinkedList(),
               "registered": datetime.date.today().isoformat()}
        self.patients_map[pid] = pat
        self.patients_list.append(pat)
        if save_undo:
            self.undo_stack.push(("ADD_PATIENT", pid))
        self._persist()
        print("  Patient added.")

    def show_sorted(self, field):
        for p in bubble_sort(self.patients_list, field):
            print(f"  ID {p['id']} | {p['name']} | Age {p['age']} | {p['problem']} | Visits: {p['visits']}")

    def find_patient(self, pid):
        p = self.patients_map.get(pid)
        if not p:
            return print("  Patient not found.")
        found = binary_search(bubble_sort(self.patients_list, "id"), pid)
        print(f"  ID {p['id']} | {p['name']} | Age {p['age']} | {p['problem']} | Visits: {p['visits']} | Found: {bool(found)}")
        p["notes"].show()

    # ------------------------------------------------ Step 5: search / edit / delete
    def search_patients(self, name):
        """Patients whose name contains ``name`` (partial, case-insensitive).

        Returns public copies sorted by ID. An empty query matches
        nothing — it is not "show everyone" — so the caller gets an
        empty list and can say so plainly instead of dumping the whole
        register.
        """
        query = (name or "").strip().lower()
        if not query:
            return []
        return [_public_patient(p) for p in
                sorted(self.patients_list, key=lambda p: p["id"])
                if query in p["name"].lower()]

    def edit_patient(self, pid, name=None, age=None, problem=None):
        """Change a patient's name, age and/or problem. The ID never changes.

        Only the fields you pass are changed. Raises ValueError if the
        patient does not exist or a new value is invalid (empty name /
        problem, age outside 0–150). A renamed patient's queued
        appointments show the new name too; bills keep the name they
        were made with (they are financial records). Returns the
        updated patient.
        """
        pat = self.patients_map.get(pid)
        if pat is None:
            raise ValueError("Patient not found.")
        if name is not None:
            name = str(name).strip()
            if not name:
                raise ValueError("Name cannot be empty.")
        if problem is not None:
            problem = str(problem).strip()
            if not problem:
                raise ValueError("Problem cannot be empty.")
        if age is not None:
            if isinstance(age, bool) or not isinstance(age, int) \
                    or not 0 <= age <= 150:
                raise ValueError(
                    "Age must be a whole number between 0 and 150.")
        if name is not None and name != pat["name"]:
            # The name rides on the queued appointment rows too — keep
            # them in sync (heap order is untouched: priority/seq never
            # change, so the heap stays valid).
            for doc in self.doctors_map.values():
                for i, row in enumerate(doc["queue"].heap):
                    if row[3] == pid:
                        doc["queue"].heap[i] = row[:4] + (name,) + row[5:]
            pat["name"] = name
        if age is not None:
            pat["age"] = age
        if problem is not None:
            pat["problem"] = problem
        self._persist()
        return _public_patient(pat)

    def delete_patient(self, pid):
        """Delete a patient (Admin only — enforced by the callers).

        Removes the patient record + their notes + their queued
        appointments (their tokens leave every doctor's queue) and
        frees any bed they occupy. Bills for the patient are KEPT —
        they are financial records and keep the patient name they were
        made with. Returns a summary of what was removed. Raises
        ValueError if the patient does not exist.
        """
        pat = self.patients_map.get(pid)
        if pat is None:
            raise ValueError("Patient not found.")
        notes_count = len(_public_patient(pat)["notes"])
        removed_tokens = []
        for doc in self.doctors_map.values():
            tokens = [row[2] for row in doc["queue"].heap if row[3] == pid]
            for token in tokens:
                doc["queue"].remove_token(token)
                removed_tokens.append(f"{doc['id']} {token}")
        freed_beds = [i + 1 for i, b in enumerate(self.beds) if b == pid]
        for bed_no in freed_beds:
            self.beds[bed_no - 1] = 0
        self.patients_map.pop(pid)
        self.patients_list = [p for p in self.patients_list
                              if p["id"] != pid]
        self._persist()
        return {"id": pid, "name": pat["name"],
                "notes_removed": notes_count,
                "appointments_removed": removed_tokens,
                "beds_freed": freed_beds}

    def add_note(self, pid, note_text):
        p = self.patients_map.get(pid)
        if not p:
            return print("  Patient not found.")
        p["notes"].add_last(note_text)
        p["visits"] += 1
        self.undo_stack.push(("ADD_NOTE", pid))
        self._persist()
        print("  Note added.")

    def show_doctor_queue(self, doc_id):
        """Print one doctor's appointment queue in service order
        (Emergency first). Used by the Doctor role's "My Appointments"."""
        doc = self.doctors_map.get(doc_id.upper())
        if not doc:
            return print("  Doctor not found.")
        rows = sorted(doc["queue"].heap, key=lambda r: (r[0], r[1]))
        print(f"  {doc['name']} ({doc['spec']}) — {len(rows)} waiting:")
        if not rows:
            print("  Queue is empty.")
        for row in rows:
            when = _appt_when(row)
            print(f"  {row[2]} | Patient {row[3]} ({row[4]})"
                  f"{' | ' + when if when else ''}"
                  f" | {_row_status(row)}"
                  f"{' [EMERGENCY]' if row[0] == 1 else ''}")

    def add_doctor(self, doc_id, name, spec, fee):
        """Add one doctor and join them to the shift rotation.

        Raises ValueError on missing ID/name/specialty, a bad fee, or
        a duplicate doctor ID. Returns the new doctor record.
        """
        doc_id = str(doc_id or "").strip().upper()
        name = str(name or "").strip()
        spec = str(spec or "").strip()
        if not doc_id or not name or not spec:
            raise ValueError("Doctor ID, name and specialty are required.")
        if isinstance(fee, bool) or not isinstance(fee, int) or fee < 0:
            raise ValueError("Fee must be a whole number of rupees (0 or more).")
        if doc_id in self.doctors_map:
            raise ValueError("Doctor ID already exists.")
        doc = {"id": doc_id, "name": name, "spec": spec, "fee": fee,
               "queue": PriorityQueue()}
        self.doctors_map[doc_id] = doc
        self.shift_q.enqueue(f"{doc_id} - {name}")
        self._persist()
        return doc

    def doctors_menu(self, role="Admin"):
        opt = input("  1. View Doctors  2. Add Doctor: ").strip()
        if opt == "1":
            for doc in self.doctors_map.values():
                print(f"  {doc['id']} | {doc['name']} | {doc['spec']} | Fee: {doc['fee']}")
        elif opt == "2":
            if not can(role, "add_doctor"):
                return print("  Only Admin can add a doctor.")
            doc_id = input("  Doctor ID: ").strip().upper()
            name, spec = input("  Name: ").strip(), input("  Specialty: ").strip()
            fee = int(input("  Token Fee: "))
            try:
                self.add_doctor(doc_id, name, spec, fee)
            except ValueError as exc:
                return print(f"  {exc}")
            print("  Doctor added.")

    def book_appointment(self, doc_id, pid, is_emg, appt_date=None, appt_time=None):
        """Book an appointment and return its token (None on failure).

        Step 4: ``appt_date`` (YYYY-MM-DD, empty = today) and
        ``appt_time`` (HH:MM 24-hour, empty = no fixed time) say WHEN
        the patient should come. A bad date/time just prints the reason
        and books nothing — the CLI never crashes on it.
        Step 6: a double booking is refused — the same doctor cannot
        have two ACTIVE appointments at the same date and time. The
        new appointment starts with status Pending.
        """
        doc, pat = self.doctors_map.get(doc_id.upper()), self.patients_map.get(pid)
        if not doc or not pat:
            print("  Invalid doctor or patient ID.")
            return None
        try:
            appt_date, appt_time = validate_appt_datetime(appt_date, appt_time)
        except ValueError as exc:
            print(f"  {exc}")
            return None
        conflict = self.find_conflicting_appointment(
            doc["id"], appt_date, appt_time)
        if conflict is not None:
            print(f"  {doc['name']} already has an appointment at "
                  f"{appt_date} {appt_time} (token {conflict['token']}, "
                  f"{conflict['status']}). Pick another time.")
            return None
        try:
            token = doc["queue"].enqueue(
                is_emg, pid, pat["name"], doc["fee"], appt_date, appt_time)
        except ValueError as exc:
            print(f"  {exc}")
            return None
        pat["visits"] += 1
        self.undo_stack.push(("BOOK_APPT", doc["id"], token, pid))
        self._persist()
        row = next((r for r in doc["queue"].heap if r[2] == token), None)
        when = _appt_when(row) if row is not None else ""
        print(f"  Booked {token} for {pat['name']} with {doc['name']} | Fee: {doc['fee']}"
              f"{' | ' + when if when else ''} | Pending")
        return token

    # ------------------------------------------------ Step 6: statuses
    def find_conflicting_appointment(self, doc_id, appt_date, appt_time):
        """The ACTIVE appointment that already occupies ``appt_date`` +
        ``appt_time`` for ``doc_id`` — or None when the slot is free.

        Only timed appointments can collide: a booking with no fixed
        time never blocks (or is blocked by) anything, and an empty
        date/time simply has no slot to check. Cancelled and No Show
        appointments no longer occupy their slot.
        """
        if not appt_date or not appt_time:
            return None
        doc = self.doctors_map.get(str(doc_id or "").upper())
        if not doc:
            return None
        for row in doc["queue"].heap:
            row_date = row[7] if len(row) > 7 else None
            row_time = row[8] if len(row) > 8 else None
            if (row_date == appt_date and row_time == appt_time
                    and _row_status(row) not in _INACTIVE_STATUSES):
                return _public_appointment(doc, row)
        return None

    def get_appointment(self, doc_id, token):
        """One appointment as a public dict, or None when the doctor
        or the token does not exist."""
        doc = self.doctors_map.get(str(doc_id or "").upper())
        if not doc:
            return None
        token = str(token or "").strip().upper()
        for row in doc["queue"].heap:
            if row[2] == token:
                return _public_appointment(doc, row)
        return None

    def appointment_history(self, doc_id, token):
        """Recorded status changes for one appointment, oldest first."""
        return self.status_history.history(
            str(doc_id or "").upper(), str(token or "").strip().upper())

    def update_status(self, doc_id, token, new_status, changed_by="system",
                      role="Admin", user_doctor_id=None):
        """Change an appointment's status and record the change.

        Returns the updated appointment. Raises ValueError for an
        unknown doctor/token or a status that is not one of the seven,
        and PermissionError when the role may not make this change:
        a Doctor may only move appointments in their OWN queue (their
        linked doctor; a Doctor with no link may use any queue) to
        Checked In, In Progress or Completed — Admin and Receptionist
        may set any status on any queue. Setting the status it already
        has is a no-op (no history row). The heap order never changes
        (priority/seq are untouched), and the change is saved at once.
        """
        if new_status not in APPOINTMENT_STATUSES:
            raise ValueError(
                "Status must be one of: "
                + ", ".join(APPOINTMENT_STATUSES) + ".")
        doc = self.doctors_map.get(str(doc_id or "").upper())
        if not doc:
            raise ValueError("Doctor not found.")
        token = str(token or "").strip().upper()
        idx = next((i for i, r in enumerate(doc["queue"].heap)
                    if r[2] == token), None)
        if idx is None:
            raise ValueError(f"Token {token} not found for {doc['name']}.")
        if role == "Doctor":
            if (user_doctor_id is not None
                    and user_doctor_id.upper() != doc["id"]):
                raise PermissionError(
                    "Doctors can only update appointments in their own queue.")
            if new_status not in DOCTOR_STATUSES:
                raise PermissionError(
                    "Doctors can only set Checked In, In Progress or Completed.")
        elif role not in ("Admin", "Receptionist"):
            raise PermissionError(
                f"Role {role} cannot update appointment statuses.")
        row = doc["queue"].heap[idx]
        old_status = _row_status(row)
        if old_status == new_status:
            return _public_appointment(doc, row)
        # Heap order is untouched (priority/seq never change), so the
        # heap stays valid — only the 10th field changes.
        base = row[:9] if len(row) > 9 else row
        doc["queue"].heap[idx] = base + (new_status,)
        self.status_history.record(
            doc["id"], token, old_status, new_status, changed_by)
        self._persist()
        return _public_appointment(doc, doc["queue"].heap[idx])

    def appointments_for_date(self, date=None, doctor_id=None):
        """All appointments scheduled for ``date`` (today by default).

        Each item is a dict with token, doctor, patient, fee, emergency,
        booking date and the scheduled date/time. Sorted by time first
        (no fixed time goes last), then Emergency-first, then token
        order. Appointments booked before Step 4 have no scheduled date
        and never appear in a dated view — they still show in the
        doctor's queue. ``doctor_id`` limits the list to one doctor.
        """
        if date is None:
            date = datetime.date.today().isoformat()
        else:
            date, _ = validate_appt_datetime(date, None)
        out = []
        for doc in self.doctors_map.values():
            if doctor_id is not None and doc["id"] != doctor_id.upper():
                continue
            for row in doc["queue"].heap:
                appt_date = row[7] if len(row) > 7 else None
                if appt_date != date:
                    continue
                out.append(_public_appointment(doc, row))
        out.sort(key=lambda a: (a["appt_time"] or "99:99",
                                0 if a["emergency"] else 1,
                                a["token"]))
        return out

    def doctor_schedule(self, doctor_id, date=None):
        """One doctor's appointments for ``date`` (today by default).

        Raises ValueError if the doctor does not exist.
        """
        if doctor_id.upper() not in self.doctors_map:
            raise ValueError("Doctor not found.")
        return self.appointments_for_date(date, doctor_id)

    def beds_menu(self):
        for i, bed in enumerate(self.beds, 1):
            print(f"  Bed {i}: {'Free' if bed == 0 else f'Patient {bed}'}")
        opt = input("  1. Assign  2. Free  3. Back: ").strip()
        if opt == "1":
            idx, pid = int(input(f"  Bed (1-{self.NUM_BEDS}): ")) - 1, int(input("  Patient ID: "))
            if 0 <= idx < self.NUM_BEDS and self.beds[idx] == 0 and pid in self.patients_map:
                self.beds[idx] = pid
                self.undo_stack.push(("BED", idx, 0))
                self._persist()
                print("  Bed assigned.")
        elif opt == "2":
            idx = int(input(f"  Bed (1-{self.NUM_BEDS}): ")) - 1
            if 0 <= idx < self.NUM_BEDS and self.beds[idx] != 0:
                old_pid, self.beds[idx] = self.beds[idx], 0
                self.undo_stack.push(("BED", idx, old_pid))
                self._persist()
                print("  Bed freed.")

    def undo(self):
        act = self.undo_stack.pop()
        if not act:
            return print("  Nothing to undo.")
        kind = act[0]
        if kind == "ADD_PATIENT":
            self.patients_map.pop(act[1], None)
            self.patients_list = [p for p in self.patients_list if p["id"] != act[1]]
        elif kind == "ADD_NOTE" and self.patients_map.get(act[1]):
            p = self.patients_map[act[1]]
            p["notes"].remove_last()
            p["visits"] = max(0, p["visits"] - 1)
        elif kind == "BOOK_APPT":
            self.doctors_map[act[1]]["queue"].remove_token(act[2])
            if self.patients_map.get(act[3]):
                self.patients_map[act[3]]["visits"] = max(0, self.patients_map[act[3]]["visits"] - 1)
        elif kind == "BED":
            self.beds[act[1]] = act[2]
        self._persist()
        print(f"  Undid {kind}.")

    def pharmacy_menu(self):
        opt = input("  1. Add Token  2. Serve  3. View: ").strip()
        if opt == "1":
            self.pharmacy_q.enqueue(input("  Token or Name: ").strip())
            self._persist()
        elif opt == "2":
            print("  Served:", self.pharmacy_q.dequeue())
            self._persist()
        elif opt == "3":
            self.pharmacy_q.show()

    def shift_menu(self):
        opt = input("  1. View Shifts  2. Rotate Shift: ").strip()
        if opt == "1":
            self.shift_q.show()
        elif opt == "2":
            print("  Now on duty:", self.shift_q.rotate())
            self._persist()

    def dashboard(self):
        free_beds = sum(1 for b in self.beds if b == 0)
        waiting = sum(len(doc["queue"].heap) for doc in self.doctors_map.values())
        print(f"  Patients: {len(self.patients_map)} | Doctors: {len(self.doctors_map)} | Waiting: {waiting} | Free Beds: {free_beds}/{self.NUM_BEDS}")

    # ------------------------------------------------ Step 3: billing
    def create_bill(self, patient_id, doctor_id, token):
        """Create a bill from a booked appointment. Returns the bill dict.

        The first line item is that appointment's consultation fee. Raises
        ValueError if the patient/doctor/token is wrong, the token
        belongs to another patient, or the token was already billed.
        Unlike the older print-style methods, this one RETURNS its result
        so the web server can use it too (see the refactor note in
        ideas.md).
        """
        doc = self.doctors_map.get(doctor_id.upper())
        if not doc:
            raise ValueError("Doctor not found.")
        pat = self.patients_map.get(patient_id)
        if not pat:
            raise ValueError("Patient not found.")
        row = next((r for r in doc["queue"].heap if r[2] == token), None)
        if row is None:
            raise ValueError(f"Token {token} not found for {doc['name']}.")
        if row[3] != patient_id:
            raise ValueError(f"Token {token} belongs to another patient.")
        return self.billing.create_bill(
            patient_id, pat["name"], doc["id"], doc["name"], token, row[5])

    def daily_report(self, date=None):
        """The daily report for ``date`` (today by default).

        Counts come from live data, not stored numbers: patients whose
        registration date is that day, appointments booked that day,
        money billed/collected that day (from bills), per-doctor
        appointments, bed occupancy, and unpaid bills overall.
        Appointments booked before Step 3 have no date and are not
        counted for any day — they still show in the waiting counts.
        """
        if date is None:
            date = datetime.date.today().isoformat()
        patients_today = sum(
            1 for p in self.patients_list if p.get("registered") == date)
        appointments_today = 0
        per_doctor = []
        for doc in self.doctors_map.values():
            today_rows = [r for r in doc["queue"].heap
                          if len(r) > 6 and r[6] == date]
            appointments_today += len(today_rows)
            per_doctor.append({
                "doctor_id": doc["id"], "name": doc["name"],
                "appointments_today": len(today_rows),
                "waiting": len(doc["queue"].heap),
            })
        money = self.billing.summary_for_date(date)
        occupied = sum(1 for b in self.beds if b != 0)
        return {
            "date": date,
            "patients_today": patients_today,
            "appointments_today": appointments_today,
            "billed_today": money["billed"],
            "collected_today": money["collected"],
            "bills_today": money["bills"],
            "paid_today": money["paid_count"],
            "unpaid_today": money["unpaid_count"],
            "unpaid_total": self.billing.unpaid_total(),
            "unpaid_count": len(self.billing.list_bills(status="Unpaid")),
            "beds_occupied": occupied,
            "beds_total": self.NUM_BEDS,
            "beds_free": self.NUM_BEDS - occupied,
            "per_doctor": per_doctor,
        }


def login_prompt(auth):
    """Ask for username/password until they are right.

    Returns the logged-in user dict {"username", "role", "doctor_id"},
    or None if the user quits (empty username). Wrong credentials just
    print a message and ask again — nothing is locked out.
    """
    print("\n  === Login ===")
    print(f"  (First run? Use {DEFAULT_ADMIN_USERNAME} / "
          f"{DEFAULT_ADMIN_PASSWORD} — change it after login.)")
    while True:
        username = input("  Username (empty to quit): ").strip()
        if not username:
            return None
        password = getpass.getpass("  Password: ")
        user = auth.authenticate(username, password)
        if user:
            print(f"  Logged in as {user['username']} ({user['role']}).")
            return user
        print("  Wrong username or password — try again.")


def users_menu(auth):
    """Admin only: add a user, list users, reset a password."""
    while True:
        print("\n  --- Users (Admin) ---")
        print("  1. List Users  2. Add User  3. Reset Password  4. Back")
        choice = input("  Choice: ").strip()
        if choice == "1":
            for u in auth.list_users():
                link = f" -> Doctor {u['doctor_id']}" if u["doctor_id"] else ""
                print(f"  {u['username']} | {u['role']}{link}")
        elif choice == "2":
            username = input("  New username: ").strip()
            password = getpass.getpass("  Password: ")
            role = input("  Role (Admin/Doctor/Receptionist): ").strip().title()
            doctor_id = ""
            if role == "Doctor":
                doctor_id = input("  Linked Doctor ID (e.g. D1, empty = all queues): ").strip()
            try:
                auth.create_user(username, password, role, doctor_id)
                print(f"  User {username} ({role}) created.")
            except ValueError as exc:
                print(f"  {exc}")
        elif choice == "3":
            username = input("  Username: ").strip()
            new_password = getpass.getpass("  New password: ")
            try:
                auth.reset_password(username, new_password)
                print(f"  Password reset for {username}.")
            except ValueError as exc:
                print(f"  {exc}")
        elif choice == "4":
            return
        else:
            print("  Invalid choice.")


def billing_menu(hosp, role):
    """Billing (Step 3): create bills from appointments, add charges,
    view/list bills, mark paid/unpaid, export CSV.

    Creating and changing bills needs Admin/Receptionist; every role may
    view bills and export the CSV (Doctor read-only).
    """
    can_edit = can(role, "manage_bills")
    while True:
        print("\n  --- Billing ---")
        print("  1. Create Bill (from appointment)")
        if can_edit:
            print("  2. Add Charge to Bill")
        print("  3. View Bill")
        print("  4. List Bills")
        if can_edit:
            print("  5. Mark Bill Paid / Unpaid")
        print("  6. Export Bills to CSV")
        print("  7. Back")
        choice = input("  Choice: ").strip()
        if choice == "1":
            if not can(role, "create_bill"):
                print("  Only Admin / Receptionist can create bills.")
                continue
            try:
                pid = int(input("  Patient ID: "))
            except ValueError:
                print("  Patient ID must be a number.")
                continue
            try:
                bill = hosp.create_bill(
                    pid, input("  Doctor ID: ").strip(),
                    input("  Token (e.g. N-1): ").strip().upper())
            except ValueError as exc:
                print(f"  {exc}")
                continue
            print(f"  Bill #{bill['id']} created for {bill['patient_name']} — "
                  f"Total Rs {bill['total']} ({bill['status']}).")
        elif choice == "2":
            if not can_edit:
                print("  Only Admin / Receptionist can add charges.")
                continue
            try:
                bill_id = int(input("  Bill ID: "))
                amount = int(input("  Amount (Rs): "))
            except ValueError:
                print("  Bill ID and Amount must be numbers.")
                continue
            try:
                bill = hosp.billing.add_charge(
                    bill_id,
                    input("  Charge (e.g. Medicine — Paracetamol): ").strip(),
                    amount)
            except ValueError as exc:
                print(f"  {exc}")
                continue
            print(f"  Charge added. Bill #{bill['id']} total is now Rs {bill['total']}.")
        elif choice == "3":
            try:
                bill = hosp.billing.get_bill(int(input("  Bill ID: ")))
            except ValueError:
                print("  Bill ID must be a number.")
                continue
            if bill is None:
                print("  Bill not found.")
                continue
            _print_bill(bill)
        elif choice == "4":
            pid_raw = input("  Patient ID (empty = all bills): ").strip()
            try:
                pid = int(pid_raw) if pid_raw else None
            except ValueError:
                print("  Patient ID must be a number.")
                continue
            bills = hosp.billing.list_bills(patient_id=pid)
            if not bills:
                print("  No bills yet.")
                continue
            for bill in bills:
                print(f"  #{bill['id']} | {bill['date']} | {bill['patient_name']} "
                      f"(#{bill['patient_id']}) | {bill['doctor_name']} {bill['token']} | "
                      f"Rs {bill['total']} | {bill['status']}")
        elif choice == "5":
            if not can_edit:
                print("  Only Admin / Receptionist can change bill status.")
                continue
            try:
                bill_id = int(input("  Bill ID: "))
            except ValueError:
                print("  Bill ID must be a number.")
                continue
            status = input("  Mark (P)aid or (U)npaid: ").strip().upper()
            try:
                bill = (hosp.billing.mark_paid(bill_id) if status == "P"
                        else hosp.billing.mark_unpaid(bill_id) if status == "U"
                        else None)
            except ValueError as exc:
                print(f"  {exc}")
                continue
            if bill is None:
                print("  Type P for Paid or U for Unpaid.")
                continue
            print(f"  Bill #{bill['id']} is now {bill['status']}.")
        elif choice == "6":
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "bills.csv")
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(hosp.billing.to_csv())
            print(f"  Bills exported to {path}")
        elif choice == "7":
            return
        else:
            print("  Invalid choice.")


def _print_bill(bill):
    """Print one bill with its line items (CLI view)."""
    print(f"  Bill #{bill['id']} | {bill['date']} | {bill['status']}")
    print(f"  Patient: {bill['patient_name']} (#{bill['patient_id']})")
    print(f"  Doctor: {bill['doctor_name']} | Token {bill['token']}")
    for item in bill["items"]:
        print(f"    - {item['label']}: Rs {item['amount']}")
    print(f"  Total: Rs {bill['total']}")


def reports_menu(hosp):
    """Daily report (Step 3): today by default, or any date you type."""
    date = input("  Date (YYYY-MM-DD, empty = today): ").strip()
    if not date:
        date = datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        return print("  Date must look like 2026-11-27.")
    rep = hosp.daily_report(date)
    print(f"\n  --- Daily Report — {rep['date']} ---")
    print(f"  Patients registered today: {rep['patients_today']}")
    print(f"  Appointments booked today: {rep['appointments_today']}")
    print(f"  Billed today: Rs {rep['billed_today']} ({rep['bills_today']} bills)")
    print(f"  Collected today: Rs {rep['collected_today']} "
          f"({rep['paid_today']} paid, {rep['unpaid_today']} unpaid)")
    print(f"  Unpaid bills (all time): {rep['unpaid_count']} bills, "
          f"Rs {rep['unpaid_total']} owed")
    print(f"  Beds: {rep['beds_occupied']}/{rep['beds_total']} occupied")
    print("  Appointments per doctor:")
    for doc in rep["per_doctor"]:
        print(f"    {doc['name']} ({doc['doctor_id']}): "
              f"{doc['appointments_today']} today, {doc['waiting']} waiting")


def schedule_menu(hosp):
    """Appointments for a date (Step 4): today by default, or any date
    you type; one doctor or all of them. Sorted by time, then
    Emergency-first. Every role may view it."""
    date = input("  Date (YYYY-MM-DD, empty = today): ").strip()
    if not date:
        date = datetime.date.today().isoformat()
    doc_id = input("  Doctor ID (empty = all doctors): ").strip().upper()
    try:
        rows = hosp.appointments_for_date(date, doc_id or None)
    except ValueError as exc:
        return print(f"  {exc}")
    print(f"\n  --- Appointments — {date} ---")
    if not rows:
        return print("  No appointments on this date.")
    for row in rows:
        when = row["appt_time"] or "no fixed time"
        print(f"  {when} | {row['token']} | {row['patient_name']} "
              f"(#{row['patient_id']}) | {row['doctor_name']} | {row['status']}"
              f"{' [EMERGENCY]' if row['emergency'] else ''}")


def appointment_status_menu(hosp, role, user):
    """Change an appointment's status (Step 6) and show its history.

    Shows the current status and every recorded change (old -> new,
    who, when), then lets the user pick one of the seven statuses.
    Doctors may only move their OWN queue's appointments to Checked
    In, In Progress or Completed — Admin and Receptionist may set any
    status. The server enforces the same rules (403 otherwise).
    """
    doc_id = input("  Doctor ID: ").strip().upper()
    token = input("  Token (e.g. N-1): ").strip().upper()
    appt = hosp.get_appointment(doc_id, token)
    if appt is None:
        return print("  Appointment not found.")
    when = appt["appt_date"] or ""
    if appt["appt_time"]:
        when += f" {appt['appt_time']}"
    print(f"  {appt['token']} | {appt['patient_name']} "
          f"(#{appt['patient_id']}) with {appt['doctor_name']}"
          f"{' | ' + when if when else ''}"
          f" | Status: {appt['status']}")
    history = hosp.appointment_history(doc_id, token)
    if history:
        print("  History:")
        for h in history:
            old = h["old_status"] or "—"
            print(f"    {h['changed_at']} | {old} -> {h['new_status']} "
                  f"by {h['changed_by']}")
    else:
        print("  No status changes yet.")
    print("  Statuses:")
    for i, status in enumerate(APPOINTMENT_STATUSES, 1):
        print(f"    {i}. {status}")
    try:
        choice = int(input("  New status (number 1-7): ").strip())
        new_status = APPOINTMENT_STATUSES[choice - 1]
    except (ValueError, IndexError):
        return print("  Pick a number 1-7.")
    try:
        updated = hosp.update_status(
            doc_id, token, new_status, changed_by=user["username"],
            role=role, user_doctor_id=user.get("doctor_id"))
    except (ValueError, PermissionError) as exc:
        return print(f"  {exc}")
    print(f"  Status updated: {token} is now {updated['status']}.")


def find_patient_menu(hosp):
    """Find a patient — by ID (binary search) or by name (Step 5:
    partial, case-insensitive)."""
    opt = input("  1. By ID  2. By Name: ").strip()
    if opt == "1":
        try:
            pid = int(input("  ID: "))
        except ValueError:
            return print("  ID must be a number.")
        hosp.find_patient(pid)
    elif opt == "2":
        name = input("  Name (or part of it): ").strip()
        matches = hosp.search_patients(name)
        if not matches:
            return print("  No patient found with that name.")
        for p in matches:
            print(f"  ID {p['id']} | {p['name']} | Age {p['age']} | "
                  f"{p['problem']} | Visits: {p['visits']}")
    else:
        print("  Invalid choice.")


def edit_patient_menu(hosp):
    """Edit a patient (Step 5): change name, age and/or problem.
    The ID never changes; Enter keeps the current value."""
    try:
        pid = int(input("  Patient ID to edit: "))
    except ValueError:
        return print("  Patient ID must be a number.")
    pat = hosp.patients_map.get(pid)
    if pat is None:
        return print("  Patient not found.")
    print(f"  Editing {pat['name']} (#{pid}) — press Enter to keep a value.")
    name = input(f"  New name [{pat['name']}]: ").strip() or None
    age_raw = input(f"  New age [{pat['age']}]: ").strip()
    problem = input(f"  New problem [{pat['problem']}]: ").strip() or None
    age = None
    if age_raw:
        try:
            age = int(age_raw)
        except ValueError:
            return print("  Age must be a number.")
    try:
        updated = hosp.edit_patient(pid, name=name, age=age, problem=problem)
    except ValueError as exc:
        return print(f"  {exc}")
    print(f"  Updated: ID {updated['id']} | {updated['name']} | "
          f"Age {updated['age']} | {updated['problem']}")


def delete_patient_menu(hosp):
    """Delete a patient (Step 5, Admin only) — with a confirmation step.

    Shows exactly what will go away (notes, queued appointments, beds)
    and only deletes when the user types YES. Bills are kept.
    """
    try:
        pid = int(input("  Patient ID to delete: "))
    except ValueError:
        return print("  Patient ID must be a number.")
    pat = hosp.patients_map.get(pid)
    if pat is None:
        return print("  Patient not found.")
    tokens = []
    for doc in hosp.doctors_map.values():
        tokens += [f"{doc['id']} {row[2]}" for row in doc["queue"].heap
                   if row[3] == pid]
    beds = [i + 1 for i, b in enumerate(hosp.beds) if b == pid]
    notes = _public_patient(pat)["notes"]
    print(f"  Delete {pat['name']} (#{pid})? This removes:")
    print(f"    - the patient record and {len(notes)} note(s)")
    print(f"    - {len(tokens)} queued appointment(s)"
          + (f": {', '.join(tokens)}" if tokens else ""))
    if beds:
        print(f"    - bed(s) {', '.join(map(str, beds))} will be freed")
    print("    - bills for this patient are KEPT (financial records).")
    if input("  Type YES to confirm: ").strip().upper() != "YES":
        return print("  Cancelled — patient NOT deleted.")
    removed = hosp.delete_patient(pid)
    print(f"  Patient {removed['name']} (#{pid}) deleted.")


def change_password_prompt(auth, user):
    """Let the logged-in user change their own password."""
    current = getpass.getpass("  Current password: ")
    new = getpass.getpass("  New password: ")
    again = getpass.getpass("  New password again: ")
    if new != again:
        return print("  New passwords do not match.")
    try:
        ok = auth.change_password(user["username"], current, new)
    except ValueError as exc:
        return print(f"  {exc}")
    print("  Password changed." if ok else "  Wrong current password.")


# Menu choices and the permission each one needs. "view" actions are
# open to every role; anything not listed here is Admin-only.
MENU = [
    ("1", "Add Patient", "add_patient"),
    ("2", "View Sorted", "view"),
    ("3", "Find Patient", "view"),
    ("4", "Add Note", "add_note"),
    ("5", "Doctors", "view"),
    ("6", "Book Appt", "book_appointment"),
    ("7", "Beds", "manage_beds"),
    ("8", "Undo", "undo"),
    ("9", "Pharmacy Q", "pharmacy"),
    ("10", "Shifts", "view"),
    ("11", "Dashboard & Exit", "view"),
    ("12", "My Appointments", "view"),   # Doctor only (own queue)
    ("13", "Users (Admin)", "manage_users"),
    ("14", "Change My Password", "view"),
    ("15", "Billing", "view_bills"),
    ("16", "Reports", "view_reports"),
    ("17", "Schedule", "view"),   # Step 4: appointments by date
    ("18", "Edit Patient", "edit_patient"),    # Step 5
    ("19", "Delete Patient", "delete_patient"),  # Step 5, Admin only
    ("20", "Appointment Status", "update_appointment_status"),  # Step 6
]


def main():
    hosp = Hospital(db_path=DEFAULT_DB_PATH)
    auth = AuthStore(DEFAULT_DB_PATH)
    user = login_prompt(auth)
    if user is None:
        print("  Bye.")
        return
    role = user["role"]
    print(f"  Role: {role} — you can only use the options shown below.")

    while True:
        print("\n--- Menu ---")
        for num, label, action in MENU:
            if num == "12" and role != "Doctor":
                continue  # My Appointments is only for Doctors
            if not can(role, action):
                continue
            print(f"  {num}. {label}")
        choice = input("Choice: ").strip()
        allowed = {num: action for num, _, action in MENU}
        if choice == "12" and role != "Doctor":
            print("  Only Doctors have 'My Appointments'.")
            continue
        if choice not in allowed or not can(role, allowed[choice]):
            print(f"  Not allowed for your role ({role}).")
            continue
        if choice == "1":
            hosp.add_patient(int(input("ID: ")), input("Name: "), int(input("Age: ")), input("Problem: "))
        elif choice == "2":
            hosp.show_sorted("age" if input("Sort by (id/age): ").strip() == "age" else "id")
        elif choice == "3":
            find_patient_menu(hosp)
        elif choice == "4":
            hosp.add_note(int(input("ID: ")), input("Note: "))
        elif choice == "5":
            hosp.doctors_menu(role)
        elif choice == "6":
            # Step 4: ask WHEN the appointment is for — date empty means
            # today, time empty means no fixed time. A bad date/time
            # just prints the reason (book_appointment handles it).
            hosp.book_appointment(
                input("Doctor ID: "), int(input("Patient ID: ")),
                input("Emergency (E) or Normal (N): ").strip().upper() == "E",
                input("Date (YYYY-MM-DD, empty = today): ").strip() or None,
                input("Time (HH:MM, empty = no fixed time): ").strip() or None)
        elif choice == "7":
            hosp.beds_menu()
        elif choice == "8":
            hosp.undo()
        elif choice == "9":
            hosp.pharmacy_menu()
        elif choice == "10":
            hosp.shift_menu()
        elif choice == "11":
            hosp.dashboard()
            break
        elif choice == "12":
            if user["doctor_id"]:
                hosp.show_doctor_queue(user["doctor_id"])
            else:
                print("  No doctor linked to your account — ask Admin to link one.")
                print("  Showing all queues for now:")
                for doc_id in hosp.doctors_map:
                    hosp.show_doctor_queue(doc_id)
        elif choice == "13":
            users_menu(auth)
        elif choice == "14":
            change_password_prompt(auth, user)
        elif choice == "15":
            billing_menu(hosp, role)
        elif choice == "16":
            reports_menu(hosp)
        elif choice == "17":
            schedule_menu(hosp)
        elif choice == "18":
            edit_patient_menu(hosp)
        elif choice == "19":
            delete_patient_menu(hosp)
        elif choice == "20":
            appointment_status_menu(hosp, role, user)


if __name__ == "__main__":
    main()