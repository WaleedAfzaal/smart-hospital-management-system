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

Step 8 (Book Appointment form): patients also carry Father Name,
Phone and Gender; a booking carries an auto-generated unique
Appointment ID (APT-0001, sequential, counter kept in the database),
a Type (General / Follow-up / Emergency — Emergency means an E-
token, the others an N- token), a Reason for Visit, Additional Notes,
and a Status chosen at booking (Pending or Confirmed). The heap row
grew to 14 fields: (priority, seq, token, pid, name, fee,
booked_date, appt_date, appt_time, status, apt_id, appt_type,
reason, notes). Old rows load with the new fields empty — their
APT ID is None and their type reads as General.

Step 9 (Appointments table + details page): ``list_appointments``
lists every appointment with server-side search (patient name, APT
ID, token), filters (date, doctor, department, status), sorting and
pagination; ``get_appointment_by_id`` / ``appointment_detail`` find
one appointment by its APT ID (rows booked before Step 8 have none —
"D1:N-1", doctor + token, works for those) with the patient, the
doctor, the payment status from billing and the status history;
``edit_appointment`` changes date/time/type/reason/notes — a
reschedule re-runs the Step 6 double-booking guard, and a type
change that flips the priority (Emergency <-> the others) changes
the priority in place and re-orders the queue — the token, the
APT ID, the booked date, the status and the history all stay put. The table exports as CSV (``appointments_to_csv``)
and as a PDF (see pdf_export.py) — both respect the active filters.

Step 10 (Appointment Calendar): ``calendar_appointments`` lists every
appointment in an inclusive date range (doctor/department filters,
untimed rows included, undated rows excluded); the CLI month grid and
the web Day/Week/Month views read from it.

Step 11 (Doctor Availability & Scheduling): each doctor has a working
pattern — working days, daily hours, breaks, consultation duration
(see availability.py) — plus per-doctor leave and hospital-wide
holidays. ``check_availability`` refuses a booking outside them
(holiday, leave, non-working day, outside hours, inside a break, or
an overlapping appointment — the Step 6 guard is now duration-aware,
so a 30-minute appointment at 09:00 also blocks 09:15) and is
enforced by ``book_appointment`` and by reschedules in
``edit_appointment``. A doctor with no pattern set has no hours to
enforce — any time is allowed, and the booking form says so plainly.
``availability_slots`` lists the bookable start times for a doctor on
a date (working hours minus breaks, in duration steps, booked ones
marked). Admin manages patterns and holidays; a Doctor may add or
delete only their OWN leave.
"""

import csv
import datetime
import getpass
import heapq
import io
import os
import re

from auth import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME, AuthStore, can
from availability import (Availability, WEEKDAY_NAMES, minutes_to_time,
                          time_to_minutes, weekday_name)
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
# Statuses that may be chosen AT BOOKING time (Step 8) — anything
# else (Checked In, In Progress, Completed, Cancelled, No Show) only
# makes sense as a later update, never as a fresh booking.
BOOKING_STATUSES = ("Pending", "Confirmed")
# Step 8: appointment types. "Emergency" books an E- token (first in
# the queue); General and Follow-up book an N- token.
APPOINTMENT_TYPES = ("General", "Follow-up", "Emergency")
# Step 8: patient genders.
GENDERS = ("Male", "Female", "Other")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]*$")


def validate_phone(phone):
    """Clean a phone number, or raise ValueError.

    Empty/None means "no phone" and comes back as "". Otherwise only
    digits, spaces, dashes, brackets and a leading + are allowed —
    "0300-1234567" and "+92 300 1234567" are fine, letters are not.
    """
    if phone in (None, ""):
        return ""
    phone = str(phone).strip()
    if not _PHONE_RE.match(phone) or not any(c.isdigit() for c in phone):
        raise ValueError(
            "Phone number may only contain digits, spaces, dashes "
            "and a leading + (e.g. 0300-1234567).")
    return phone


def validate_gender(gender):
    """Clean a gender value, or raise ValueError.

    Empty/None means "not given" and comes back as "". Otherwise it
    must be Male, Female or Other (any case — "male" becomes "Male").
    """
    if gender in (None, ""):
        return ""
    gender = str(gender).strip().title()
    if gender not in GENDERS:
        raise ValueError("Gender must be Male, Female or Other.")
    return gender


def validate_patient_details(name, age, problem):
    """Check a new patient's name/age/problem, or raise ValueError.

    Used by the booking flow (CLI + API) when a patient is registered
    inline — the same rules the Add Patient form already enforces.
    Returns (name, age, problem) cleaned up.
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("Name cannot be empty.")
    problem = str(problem or "").strip()
    if not problem:
        raise ValueError("Problem cannot be empty.")
    if isinstance(age, bool) or not isinstance(age, int) \
            or not 0 <= age <= 150:
        raise ValueError("Age must be a whole number between 0 and 150.")
    return name, age, problem
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
    both use, so the status is the same everywhere.

    Step 8 fields (11th–14th): the auto-generated Appointment ID
    (None for rows booked before Step 8), the type, the reason for
    visit and the notes. Rows built before Step 8 simply have none of
    these — the type reads as General and the rest as empty.
    """
    return {
        "token": row[2],
        "doctor_id": doc["id"],
        "doctor_name": doc["name"],
        "department": doc["spec"],
        "patient_id": row[3],
        "patient_name": row[4],
        "fee": row[5],
        "emergency": row[0] == 1,
        "booked_date": row[6] if len(row) > 6 else None,
        "appt_date": row[7] if len(row) > 7 else None,
        "appt_time": row[8] if len(row) > 8 else None,
        "status": _row_status(row),
        "apt_id": row[10] if len(row) > 10 else None,
        "appt_type": row[11] if len(row) > 11 and row[11] else "General",
        "reason": row[12] if len(row) > 12 and row[12] else "",
        "notes": row[13] if len(row) > 13 and row[13] else "",
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
            "notes": notes, "registered": pat.get("registered"),
            # Step 8: Father Name, Phone and Gender (""/missing for
            # patients registered before Step 8).
            "father_name": pat.get("father_name") or "",
            "phone": pat.get("phone") or "",
            "gender": pat.get("gender") or ""}


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

    def enqueue(self, is_emg, pid, name, fee, appt_date=None, appt_time=None,
                apt_id=None, appt_type=None, reason=None, notes=None,
                status="Pending"):
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
        # appointment starts Pending (or Confirmed when chosen at
        # booking, Step 8).
        # Step 8: the Appointment ID, type, reason and notes ride as
        # the 11th–14th fields. Rows built before Step 8 simply have
        # none of these (they save/load as NULL/empty).
        self.heap.append((1 if is_emg else 2, self.count, token, pid, name,
                          fee, datetime.date.today().isoformat(),
                          appt_date, appt_time, status, apt_id,
                          appt_type or "General", reason or "", notes or ""))
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
        # Step 8: the Appointment ID counter — the next number to use
        # (APT-0001, APT-0002, ...). The database load overwrites it
        # with the saved value, so IDs never repeat after a restart.
        self.apt_counter = 1
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
        # Step 11: doctor availability, leave and holidays live in
        # their own store (same file when a db is given, pure
        # in-memory otherwise — see availability.py). Database.save()
        # never touches them.
        self.availability = Availability(db_path)
        if self.db is not None:
            self.db.load(self)

    def save(self):
        """Write the current state to the database file (no-op in memory)."""
        if self.db is not None:
            self.db.save(self)

    def _persist(self):
        """Save after every change, so a restart never loses data."""
        self.save()

    def add_patient(self, pid, name, age, problem, father_name=None,
                    phone=None, gender=None, save_undo=True):
        """Register a patient.

        Step 8: Father Name, Phone and Gender are optional (empty when
        not given). A bad phone or gender raises ValueError; a
        duplicate ID just prints and adds nothing (as before — the
        test for duplicate IDs relies on it).
        """
        if pid in self.patients_map:
            return print("  ID already exists.")
        phone = validate_phone(phone)
        gender = validate_gender(gender)
        # Step 3: remember the registration date for the daily report.
        pat = {"id": pid, "name": name, "age": age, "problem": problem,
               "visits": 0, "notes": LinkedList(),
               "registered": datetime.date.today().isoformat(),
               "father_name": (father_name or "").strip(),
               "phone": phone, "gender": gender}
        self.patients_map[pid] = pat
        self.patients_list.append(pat)
        if save_undo:
            self.undo_stack.push(("ADD_PATIENT", pid))
        self._persist()
        print("  Patient added.")

    def next_patient_id(self):
        """The next free patient ID — one above the highest ID in use
        (101 when the register is empty). Used when a new patient is
        registered inside the booking flow, so nobody has to invent
        an ID number."""
        return max(self.patients_map, default=100) + 1

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
    def search_patients(self, query):
        """Find patients by ID, name or phone — one search box for all
        three (Step 8).

        * a query that is all digits matches a patient with that exact
          ID, and any patient whose phone contains those digits;
        * any other query matches patients whose NAME contains it
          (partial, case-insensitive, as before) or whose phone
          contains its digits ("0300" finds "0300-1234567").
        Returns public copies sorted by ID. An empty query matches
        nothing — it is not "show everyone" — so the caller gets an
        empty list and can say so plainly instead of dumping the whole
        register.
        """
        raw = (query or "")
        text = str(raw).strip()
        if not text:
            return []
        digits = re.sub(r"\D", "", text)
        is_id = text.isdigit()
        query_lower = text.lower()
        out = []
        for pat in sorted(self.patients_list, key=lambda p: p["id"]):
            pat_digits = re.sub(r"\D", "", pat.get("phone") or "")
            if is_id and pat["id"] == int(text):
                out.append(_public_patient(pat))
            elif query_lower in pat["name"].lower():
                out.append(_public_patient(pat))
            elif digits and pat_digits and digits in pat_digits:
                out.append(_public_patient(pat))
        return out

    def edit_patient(self, pid, name=None, age=None, problem=None,
                     father_name=None, phone=None, gender=None):
        """Change a patient's name, age and/or problem. The ID never changes.

        Only the fields you pass are changed. Raises ValueError if the
        patient does not exist or a new value is invalid (empty name /
        problem, age outside 0–150). A renamed patient's queued
        appointments show the new name too; bills keep the name they
        were made with (they are financial records). Returns the
        updated patient.

        Step 8: Father Name, Phone and Gender can be changed the same
        way (pass "" to clear one, None to leave it alone). A bad
        phone or gender raises ValueError and nothing changes.
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
        if father_name is not None:
            father_name = str(father_name).strip()
        if phone is not None:
            phone = validate_phone(phone)
        if gender is not None:
            gender = validate_gender(gender)
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
        if father_name is not None:
            pat["father_name"] = father_name
        if phone is not None:
            pat["phone"] = phone
        if gender is not None:
            pat["gender"] = gender
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

    # ------------------------------------------------ Step 8: booking form helpers
    def departments(self):
        """Every department — the distinct doctor specializations,
        alphabetically. There is no separate Departments module yet;
        a doctor's specialization IS their department."""
        return sorted({doc["spec"] for doc in self.doctors_map.values()})

    def doctors_in_department(self, department):
        """Doctors in one department (specialization), by ID."""
        dept = str(department or "").strip()
        return [doc for doc in sorted(self.doctors_map.values(),
                                      key=lambda d: d["id"])
                if doc["spec"] == dept]

    def next_apt_id(self):
        """The next Appointment ID (APT-0001, APT-0002, ...) and move
        the counter on. The counter is saved in the database, so an ID
        is never handed out twice — not even after a restart. Only
        call this when a booking is really about to happen."""
        apt_id = f"APT-{self.apt_counter:04d}"
        self.apt_counter += 1
        return apt_id

    def doctor_booked_times(self, doctor_id, date):
        """The times one doctor already has booked on ``date`` — the
        availability the booking form shows, so taken slots are
        visible before a time is picked.

        Every timed appointment that day is listed (time, token,
        status, patient name), sorted by time. Cancelled and No Show
        appointments are listed too but do NOT block their slot — the
        Step 6 guard only counts ACTIVE ones. Untimed appointments
        never appear here (they have no slot to show).
        """
        if date is None:
            date = datetime.date.today().isoformat()
        else:
            date, _ = validate_appt_datetime(date, None)
        doc = self.doctors_map.get(str(doctor_id or "").upper())
        if not doc:
            raise ValueError("Doctor not found.")
        out = []
        for row in doc["queue"].heap:
            appt_date = row[7] if len(row) > 7 else None
            appt_time = row[8] if len(row) > 8 else None
            if appt_date == date and appt_time:
                out.append({
                    "time": appt_time,
                    "token": row[2],
                    "status": _row_status(row),
                    "patient_name": row[4],
                })
        out.sort(key=lambda b: b["time"])
        return out

    def book_appointment(self, doc_id, pid, is_emg=False, appt_date=None,
                         appt_time=None, appt_type=None, reason=None,
                         notes=None, status="Pending"):
        """Book an appointment and return its token (None on failure).

        Step 4: ``appt_date`` (YYYY-MM-DD, empty = today) and
        ``appt_time`` (HH:MM 24-hour, empty = no fixed time) say WHEN
        the patient should come. A bad date/time just prints the reason
        and books nothing — the CLI never crashes on it.
        Step 6: a double booking is refused — the same doctor cannot
        have two ACTIVE appointments at the same date and time. The
        new appointment starts with status Pending.
        Step 11: the booking is also refused outside the doctor's
        availability — on a hospital holiday, on a leave day, on a
        non-working day, outside working hours, inside a break, or
        overlapping an ACTIVE appointment (the guard is now
        duration-aware: a 30-minute appointment at 09:00 also blocks
        09:15). A doctor with no availability set has no hours to
        enforce, so any time is allowed.
        Step 8: ``appt_type`` is General, Follow-up or Emergency. The
        type decides the queue priority — Emergency books an E- token,
        the others an N- token (when no type is given, the old
        ``is_emg`` flag decides, so old callers keep working).
        ``reason`` and ``notes`` are stored on the appointment, and
        ``status`` may be Pending (the default) or Confirmed. The
        booking gets a unique Appointment ID (APT-0001, ...) — read it
        back with get_appointment(token). A bad type, status, reason
        or note just prints the reason and books nothing.
        """
        doc, pat = self.doctors_map.get(doc_id.upper()), self.patients_map.get(pid)
        if not doc or not pat:
            print("  Invalid doctor or patient ID.")
            return None
        if appt_type is None:
            appt_type = "Emergency" if is_emg else "General"
        else:
            appt_type = str(appt_type).strip()
            if appt_type not in APPOINTMENT_TYPES:
                print("  Type must be General, Follow-up or Emergency.")
                return None
            is_emg = appt_type == "Emergency"
        status = str(status or "Pending").strip()
        if status not in BOOKING_STATUSES:
            print("  Status at booking must be Pending or Confirmed.")
            return None
        reason = (reason or "").strip()
        notes = (notes or "").strip()
        try:
            appt_date, appt_time = validate_appt_datetime(appt_date, appt_time)
        except ValueError as exc:
            print(f"  {exc}")
            return None
        # Step 11: availability — holiday, leave, working day/hours,
        # break, and the (now duration-aware) double-booking guard.
        # A refusal prints the plain reason and books nothing.
        block_reason = self.check_availability(
            doc["id"], appt_date, appt_time)
        if block_reason is not None:
            print(f"  {block_reason}")
            return None
        apt_id = self.next_apt_id()
        try:
            token = doc["queue"].enqueue(
                is_emg, pid, pat["name"], doc["fee"], appt_date, appt_time,
                apt_id=apt_id, appt_type=appt_type, reason=reason,
                notes=notes, status=status)
        except ValueError as exc:
            print(f"  {exc}")
            return None
        pat["visits"] += 1
        self.undo_stack.push(("BOOK_APPT", doc["id"], token, pid))
        self._persist()
        row = next((r for r in doc["queue"].heap if r[2] == token), None)
        when = _appt_when(row) if row is not None else ""
        print(f"  Booked {apt_id} ({token}) for {pat['name']} with {doc['name']} | Fee: {doc['fee']}"
              f"{' | ' + when if when else ''} | {appt_type} | {status}")
        return token

    # ------------------------------------------------ Step 6: statuses
    def find_conflicting_appointment(self, doc_id, appt_date, appt_time,
                                     duration=None, exclude_token=None):
        """The ACTIVE appointment that already occupies ``appt_date`` +
        ``appt_time`` for ``doc_id`` — or None when the slot is free.

        Only timed appointments can collide: a booking with no fixed
        time never blocks (or is blocked by) anything, and an empty
        date/time simply has no slot to check. Cancelled and No Show
        appointments no longer occupy their slot.

        Step 11: ``duration`` (minutes) turns the check into an
        interval check — a 30-minute appointment at 09:00 also blocks
        a 09:15 booking (it runs until 09:30), while a booking at
        09:30 is fine. Without a duration it is the old exact-minute
        check. ``exclude_token`` skips one appointment — a reschedule
        never blocks its own slot.
        """
        if not appt_date or not appt_time:
            return None
        doc = self.doctors_map.get(str(doc_id or "").upper())
        if not doc:
            return None
        start = time_to_minutes(appt_time)
        for row in doc["queue"].heap:
            row_date = row[7] if len(row) > 7 else None
            row_time = row[8] if len(row) > 8 else None
            if row_date != appt_date or not row_time:
                continue
            if _row_status(row) in _INACTIVE_STATUSES:
                continue
            if exclude_token and row[2] == exclude_token:
                continue
            if duration is None:
                if row_time == appt_time:
                    return _public_appointment(doc, row)
            else:
                other_start = time_to_minutes(row_time)
                if start < other_start + duration \
                        and other_start < start + duration:
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
        # heap stays valid — only the 10th field changes. Everything
        # after it (Step 8: Appointment ID, type, reason, notes) is
        # kept exactly as it was.
        doc["queue"].heap[idx] = row[:9] + (new_status,) + row[10:]
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

    # ------------------------------------------------ Step 11: availability & scheduling
    def check_availability(self, doc_id, appt_date, appt_time,
                           exclude_token=None):
        """Why a booking at ``appt_date`` + ``appt_time`` for ``doc_id``
        is NOT allowed — or None when it is allowed.

        Step 11, in order: (a) the date is a hospital holiday, (b) the
        doctor is on leave that day, (c) the date is not one of the
        doctor's working days, (d) the time is outside the working
        hours, (e) the time falls inside a break, (f) the slot
        overlaps an ACTIVE appointment — the Step 6 guard, now
        duration-aware (with a 30-minute consultation a 09:00
        appointment also blocks 09:15). When the doctor has no
        availability set, only the holiday/leave checks apply and any
        time is allowed — the booking form says so plainly. An untimed
        booking (no fixed time) only runs the date checks (holiday,
        leave, working day). ``exclude_token`` skips one appointment
        (a reschedule never blocks its own slot). Raises ValueError
        for an unknown doctor or a bad date.
        """
        doc = self.doctors_map.get(str(doc_id or "").upper())
        if not doc:
            raise ValueError("Doctor not found.")
        appt_date, appt_time = validate_appt_datetime(appt_date, appt_time)
        holiday = self.availability.holiday_on(appt_date)
        if holiday is not None:
            return (f"Hospital is closed on {appt_date} ({holiday}) — "
                    "no appointments that day.")
        if self.availability.is_on_leave(doc["id"], appt_date):
            return f"{doc['name']} is on leave on {appt_date}."
        avail = self.availability.get(doc["id"])
        duration = None
        if avail is not None:
            duration = avail["duration_minutes"]
            day = weekday_name(appt_date)
            if day not in avail["working_days"]:
                return (f"{doc['name']} does not work on "
                        f"{WEEKDAY_NAMES[day]}s ({appt_date}).")
            if appt_time:
                start = time_to_minutes(appt_time)
                work_start = time_to_minutes(avail["start_time"])
                work_end = time_to_minutes(avail["end_time"])
                if start < work_start or start + duration > work_end:
                    return (f"{doc['name']} works {avail['start_time']}–"
                            f"{avail['end_time']} — {appt_time} is outside "
                            "working hours.")
                for bs, be in avail["breaks"]:
                    if start < time_to_minutes(be) \
                            and time_to_minutes(bs) < start + duration:
                        return (f"{appt_time} falls in {doc['name']}'s break "
                                f"({bs}–{be}).")
        if appt_time:
            conflict = self.find_conflicting_appointment(
                doc["id"], appt_date, appt_time, duration=duration,
                exclude_token=exclude_token)
            if conflict is not None:
                if duration is not None:
                    end = minutes_to_time(
                        time_to_minutes(conflict["appt_time"]) + duration)
                    return (f"{doc['name']} already has an appointment at "
                            f"{appt_date} {appt_time} (token "
                            f"{conflict['token']}, {conflict['status']}) — "
                            f"it runs until {end}. Pick another time.")
                return (f"{doc['name']} already has an appointment at "
                        f"{appt_date} {appt_time} (token {conflict['token']}, "
                        f"{conflict['status']}). Pick another time.")
        return None

    def availability_slots(self, doctor_id, date=None):
        """The bookable slots for one doctor on one date (today by
        default) — Step 11.

        Returns {"doctor_id", "date", "availability", "slots",
        "booked", "unavailable_reason"}: "availability" is the
        doctor's working pattern (None when not set yet — then any
        time is allowed and "slots" is empty); "slots" is every start
        time from the working hours minus breaks, in
        consultation-duration steps, each marked available or booked
        (with the blocking token); "booked" is the same booked-times
        list the booking form already shows; "unavailable_reason"
        says plainly why there are no slots (holiday, leave, not a
        working day) — None when slots exist or availability is
        simply not set. Raises ValueError for an unknown doctor or a
        bad date.
        """
        doc = self.doctors_map.get(str(doctor_id or "").upper())
        if not doc:
            raise ValueError("Doctor not found.")
        date, _ = validate_appt_datetime(date, None)
        booked = self.doctor_booked_times(doc["id"], date)
        avail = self.availability.get(doc["id"])
        # With no time, check_availability runs only the date checks
        # (holiday / leave / working day) — exactly what decides
        # whether ANY slot exists that day.
        reason = self.check_availability(doc["id"], date, None)
        slots = []
        if avail is not None and reason is None:
            start = time_to_minutes(avail["start_time"])
            end = time_to_minutes(avail["end_time"])
            duration = avail["duration_minutes"]
            breaks = [(time_to_minutes(bs), time_to_minutes(be))
                      for bs, be in avail["breaks"]]
            t = start
            while t + duration <= end:
                in_break = any(t < be and bs < t + duration
                               for bs, be in breaks)
                if not in_break:
                    taken = self.find_conflicting_appointment(
                        doc["id"], date, minutes_to_time(t),
                        duration=duration)
                    slots.append({
                        "time": minutes_to_time(t),
                        "available": taken is None,
                        "token": taken["token"] if taken else None,
                    })
                t += duration
        return {"doctor_id": doc["id"], "date": date,
                "availability": avail, "slots": slots,
                "booked": booked, "unavailable_reason": reason}

    def set_doctor_availability(self, doctor_id, working_days, start_time,
                                end_time, duration_minutes, breaks=None,
                                role="Admin"):
        """Save a doctor's working pattern (Admin only).

        ``working_days`` is e.g. ["Mon", "Tue", "Wed", "Thu", "Fri"];
        ``breaks`` is e.g. ["13:00-14:00"]. Returns the saved record.
        Raises PermissionError when the role is not Admin, ValueError
        for an unknown doctor or a bad pattern.
        """
        if role != "Admin":
            raise PermissionError(
                "Only Admin can set doctor availability.")
        doc = self.doctors_map.get(str(doctor_id or "").strip().upper())
        if not doc:
            raise ValueError("Doctor not found.")
        return self.availability.set(
            doc["id"], working_days, start_time, end_time,
            duration_minutes, breaks)

    def get_doctor_availability(self, doctor_id):
        """One doctor's working pattern, or None when not set yet."""
        doc = self.doctors_map.get(str(doctor_id or "").strip().upper())
        if not doc:
            raise ValueError("Doctor not found.")
        return self.availability.get(doc["id"])

    def clear_doctor_availability(self, doctor_id, role="Admin"):
        """Remove a doctor's working pattern (Admin only) — back to
        "not set", so any time is allowed again. Returns True when a
        pattern was actually removed."""
        if role != "Admin":
            raise PermissionError(
                "Only Admin can clear doctor availability.")
        doc = self.doctors_map.get(str(doctor_id or "").strip().upper())
        if not doc:
            raise ValueError("Doctor not found.")
        return self.availability.clear(doc["id"])

    def add_doctor_leave(self, doctor_id, start_date, end_date, reason="",
                         role="Admin", user_doctor_id=None):
        """Record leave for a doctor (Admin: any doctor; Doctor: only
        themself — the linked doctor_id must match). Returns the saved
        leave row (with its id). Raises PermissionError or ValueError.
        """
        if role == "Doctor":
            if user_doctor_id is None or user_doctor_id.upper() \
                    != str(doctor_id or "").strip().upper():
                raise PermissionError(
                    "Doctors can only add leave for themselves.")
        elif role != "Admin":
            raise PermissionError(
                "Only Admin can add leave for a doctor.")
        doc = self.doctors_map.get(str(doctor_id or "").strip().upper())
        if not doc:
            raise ValueError("Doctor not found.")
        return self.availability.add_leave(
            doc["id"], start_date, end_date, reason)

    def list_doctor_leaves(self, doctor_id=None, role="Admin",
                           user_doctor_id=None):
        """Leave rows, oldest first — all doctors, one doctor, or (for
        a Doctor user) only their own leave. Raises ValueError for an
        unknown doctor."""
        if role == "Doctor" and user_doctor_id:
            doctor_id = user_doctor_id
        if doctor_id:
            doc = self.doctors_map.get(str(doctor_id).strip().upper())
            if not doc:
                raise ValueError("Doctor not found.")
            doctor_id = doc["id"]
        return self.availability.leaves(doctor_id)

    def delete_doctor_leave(self, leave_id, role="Admin",
                            user_doctor_id=None):
        """Delete one leave row (Admin: any; Doctor: only their own).

        Raises ValueError when the leave does not exist.
        """
        try:
            leave_id = int(leave_id)
        except (TypeError, ValueError):
            raise ValueError("Leave ID must be a number.")
        row = next((r for r in self.availability.leaves()
                    if r["id"] == leave_id), None)
        if row is None:
            raise ValueError("Leave not found.")
        if role == "Doctor":
            if user_doctor_id is None \
                    or user_doctor_id.upper() != row["doctor_id"]:
                raise PermissionError(
                    "Doctors can only delete their own leave.")
        elif role != "Admin":
            raise PermissionError(
                "Only Admin can delete leave.")
        return self.availability.delete_leave(leave_id)

    def add_holiday(self, date, name, role="Admin"):
        """Add a hospital holiday (Admin only). Returns the saved row
        (with its id). Raises PermissionError or ValueError (bad date,
        empty name, or a date that is already a holiday)."""
        if role != "Admin":
            raise PermissionError("Only Admin can add holidays.")
        return self.availability.add_holiday(date, name)

    def list_holidays(self):
        """Every hospital holiday, earliest first."""
        return self.availability.holidays()

    def delete_holiday(self, holiday_id, role="Admin"):
        """Delete one holiday (Admin only). Raises ValueError when it
        does not exist."""
        if role != "Admin":
            raise PermissionError("Only Admin can delete holidays.")
        try:
            holiday_id = int(holiday_id)
        except (TypeError, ValueError):
            raise ValueError("Holiday ID must be a number.")
        if not self.availability.delete_holiday(holiday_id):
            raise ValueError("Holiday not found.")
        return True

    # ------------------------------------------------ Step 9: appointments table + details
    def all_appointments(self, doctor_id=None):
        """Every appointment as a public dict (all doctors, or one).

        The table, the exports and the dashboard all read from here,
        so the numbers are the same everywhere.
        """
        out = []
        for doc in self.doctors_map.values():
            if doctor_id is not None and doc["id"] != str(doctor_id).upper():
                continue
            for row in doc["queue"].heap:
                out.append(_public_appointment(doc, row))
        return out

    # ------------------------------------------------ Step 10: calendar
    def calendar_appointments(self, from_date=None, to_date=None,
                              doctor_id=None, department=None):
        """Every appointment scheduled in [from_date, to_date]
        (both inclusive), time-ordered within each day.

        Defaults: from today to 30 days ahead. ``doctor_id`` limits
        the list to one doctor; ``department`` (a doctor
        specialization) limits it to that department. Rows with no
        scheduled date (booked before Step 4) have no day to sit on
        and never appear in a dated view — same as Schedule. Rows
        with a date but no fixed time DO appear (time sorts last).
        Cancelled / No Show rows are included — the calendar shows
        the day as it really is. Raises ValueError on a bad date or
        when from_date is after to_date.
        """
        if from_date in (None, ""):
            from_date = datetime.date.today().isoformat()
        else:
            from_date, _ = validate_appt_datetime(from_date, None)
        if to_date in (None, ""):
            to_date = (datetime.date.today()
                       + datetime.timedelta(days=30)).isoformat()
        else:
            to_date, _ = validate_appt_datetime(to_date, None)
        if from_date > to_date:
            raise ValueError(
                "The start date must not be after the end date.")
        if doctor_id:
            doctor_id = str(doctor_id).strip().upper()
            if doctor_id not in self.doctors_map:
                raise ValueError("Doctor not found.")
        if department:
            department = str(department).strip()
        out = []
        for a in self.all_appointments(doctor_id):
            if not a["appt_date"]:
                continue
            if not (from_date <= a["appt_date"] <= to_date):
                continue
            if department and a["department"] != department:
                continue
            out.append(a)
        out.sort(key=lambda a: (a["appt_date"],
                                a["appt_time"] or "99:99",
                                0 if a["emergency"] else 1,
                                a["token"]))
        return out

    def get_appointment_by_id(self, apt_id):
        """(doctor dict, public appointment) for an Appointment ID.

        Returns (None, None) when there is no such appointment. Rows
        booked before Step 8 have no APT ID — for those, "D1:N-1"
        (doctor ID, colon, token) also works, so old appointments
        stay viewable.
        """
        ref = str(apt_id or "").strip()
        if not ref:
            return None, None
        for doc in self.doctors_map.values():
            for row in doc["queue"].heap:
                if len(row) > 10 and row[10] and row[10] == ref:
                    return doc, _public_appointment(doc, row)
        if ":" in ref:
            doc_id, _, token = ref.partition(":")
            doc_id = doc_id.strip().upper()
            doc = self.doctors_map.get(doc_id)
            if doc is not None:
                appt = self.get_appointment(doc_id, token.strip())
                if appt is not None:
                    return doc, appt
        return None, None

    def appointment_detail(self, apt_id):
        """Everything the details page needs for one appointment.

        The appointment, its patient (with Father Name / Phone /
        Gender), its doctor, the payment status from billing (a bill
        is found by doctor + token — a token is billed only once) and
        the recorded status history. None when the ID is unknown.
        """
        doc, appt = self.get_appointment_by_id(apt_id)
        if doc is None:
            return None
        pat = self.patients_map.get(appt["patient_id"])
        if pat is not None:
            patient = _public_patient(pat)
        else:
            # The patient record is gone (a deleted patient's
            # appointments go with them, so this should not happen)
            # — show what the appointment row still remembers.
            patient = {"id": appt["patient_id"],
                       "name": appt["patient_name"], "age": None,
                       "problem": "", "visits": 0, "notes": [],
                       "registered": None, "father_name": "",
                       "phone": "", "gender": ""}
        bill = self.billing.find_bill_by_token(doc["id"], appt["token"])
        if bill is not None:
            # get_bill adds the computed total (the stored bill only
            # carries its line items).
            bill = self.billing.get_bill(bill["id"])
        payment = {"billed": bill is not None,
                   "bill_id": bill["id"] if bill else None,
                   "status": bill["status"] if bill else None,
                   "total": bill["total"] if bill else None}
        return {
            "appointment": appt,
            "patient": patient,
            "doctor": {"id": doc["id"], "name": doc["name"],
                       "spec": doc["spec"], "fee": doc["fee"]},
            "payment": payment,
            "history": self.appointment_history(doc["id"], appt["token"]),
        }

    def filter_appointments(self, search=None, date=None, doctor_id=None,
                            department=None, status=None, sort="date",
                            order="asc"):
        """Every appointment that matches the filters, sorted.

        * ``search`` matches part of the patient name, the APT ID or
          the token (case-insensitive).
        * ``date`` is an exact appointment date — rows with no
          scheduled date never match a date filter.
        * ``sort`` is date, time, patient_name, doctor_name or
          status; ``order`` is asc or desc. Rows with no date/time
          sort last when ascending (first when descending).
        Raises ValueError on a bad sort/order/date/doctor/status.
        The list endpoint paginates this; the exports take it whole.
        """
        if sort not in ("date", "time", "patient_name", "doctor_name",
                        "status"):
            raise ValueError(
                "Sort must be date, time, patient_name, doctor_name or status.")
        if order not in ("asc", "desc"):
            raise ValueError("Order must be asc or desc.")
        if date:
            date, _ = validate_appt_datetime(date, None)
        if doctor_id:
            doctor_id = str(doctor_id).strip().upper()
            if doctor_id not in self.doctors_map:
                raise ValueError("Doctor not found.")
        if department:
            department = str(department).strip()
        if status:
            status = str(status).strip()
            if status not in APPOINTMENT_STATUSES:
                raise ValueError(
                    "Status must be one of: "
                    + ", ".join(APPOINTMENT_STATUSES) + ".")
        search = str(search or "").strip().lower()
        out = []
        for a in self.all_appointments(doctor_id):
            if search and not (search in a["patient_name"].lower()
                               or search in (a["apt_id"] or "").lower()
                               or search in a["token"].lower()):
                continue
            if date and a["appt_date"] != date:
                continue
            if department and a["department"] != department:
                continue
            if status and a["status"] != status:
                continue
            out.append(a)

        def _key(a):
            if sort == "date":
                return (a["appt_date"] or "9999-12-31",
                        a["appt_time"] or "99:99", a["token"])
            if sort == "time":
                return (a["appt_time"] or "99:99",
                        a["appt_date"] or "9999-12-31", a["token"])
            if sort == "patient_name":
                return (a["patient_name"].lower(),
                        a["appt_date"] or "9999-12-31", a["token"])
            if sort == "doctor_name":
                return (a["doctor_name"].lower(),
                        a["appt_date"] or "9999-12-31", a["token"])
            return (a["status"], a["appt_date"] or "9999-12-31",
                    a["token"])

        out.sort(key=_key, reverse=(order == "desc"))
        return out

    def list_appointments(self, search=None, date=None, doctor_id=None,
                          department=None, status=None, sort="date",
                          order="asc", page=1, per_page=10):
        """One page of the filtered, sorted appointments + the counts.

        ``page`` starts at 1 and is clamped to the last page;
        ``per_page`` is clamped to 1–100 (default 10). Bad page /
        per_page values raise ValueError; bad filters raise the same
        ValueError as ``filter_appointments``.
        """
        try:
            page = int(page)
        except (TypeError, ValueError):
            raise ValueError("Page must be a whole number.")
        try:
            per_page = int(per_page)
        except (TypeError, ValueError):
            raise ValueError("Per page must be a whole number.")
        page = max(1, page)
        per_page = min(100, max(1, per_page))
        rows = self.filter_appointments(
            search=search, date=date, doctor_id=doctor_id,
            department=department, status=status, sort=sort, order=order)
        total = len(rows)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        start = (page - 1) * per_page
        return {"appointments": rows[start:start + per_page],
                "total": total, "page": page,
                "per_page": per_page, "pages": pages}

    def edit_appointment(self, apt_id, date=None, time=None, appt_type=None,
                         reason=None, notes=None, role="Admin",
                         user_doctor_id=None):
        """Change an appointment's date/time/type/reason/notes.

        Only the fields you pass change — None means "leave it".
        An empty date also leaves it (an appointment always has a
        date); an empty time clears it to "no fixed time"; an empty
        reason/note clears that field. Returns the updated
        appointment. Raises ValueError for an unknown appointment, a
        bad value, or a reschedule onto a slot the doctor already
        has taken (the Step 6 guard, re-run here — the appointment
        itself never blocks its own slot), and PermissionError when
        the role may not edit details: only Admin and Receptionist
        may — a Doctor may only update the consultation status
        (Step 6) and never edits an appointment's details. Step 11:
        a reschedule also re-runs ALL the availability checks
        (holiday, leave, working day/hours, break, duration-aware
        overlap), so an appointment can never be moved outside the
        doctor's working hours.

        A type change that flips the queue priority (Emergency <->
        General/Follow-up) changes the priority field in place and
        re-orders the queue — the token stays the same, so the bill
        and the status history (both keyed by doctor + token) stay
        linked to the appointment. The APT ID, the status and the
        history are never touched by an edit.
        """
        if role not in ("Admin", "Receptionist"):
            raise PermissionError(
                "Doctors cannot edit appointment details — they can only "
                "update the consultation status.")
        doc, appt = self.get_appointment_by_id(apt_id)
        if doc is None:
            raise ValueError("Appointment not found.")
        row = next((r for r in doc["queue"].heap
                    if r[2] == appt["token"]), None)
        if row is None:
            raise ValueError("Appointment not found.")
        new_date = appt["appt_date"]
        if date:
            new_date, _ = validate_appt_datetime(date, None)
        new_time = appt["appt_time"]
        if time is not None:
            if time == "":
                new_time = None
            else:
                _, new_time = validate_appt_datetime(None, time)
        new_type = appt["appt_type"]
        if appt_type:
            new_type = str(appt_type).strip()
            if new_type not in APPOINTMENT_TYPES:
                raise ValueError(
                    "Type must be General, Follow-up or Emergency.")
        new_reason = appt["reason"] if reason is None else str(reason).strip()
        new_notes = appt["notes"] if notes is None else str(notes).strip()
        # ---- Step 11: a reschedule re-runs ALL the availability
        # checks (holiday, leave, working day/hours, break, and the
        # duration-aware double-booking guard) — but never against
        # the appointment itself. A reason/notes/type-only edit is
        # not a reschedule and skips the checks. An appointment with
        # no scheduled date (booked before Step 4) has no date to
        # check, so it skips them too.
        if (date or time is not None) and new_date is not None:
            block_reason = self.check_availability(
                doc["id"], new_date, new_time,
                exclude_token=appt["token"])
            if block_reason is not None:
                raise ValueError(block_reason)
        new_is_emg = new_type == "Emergency"
        # Same priority — rebuild the row in place (heap order only
        # depends on fields 0/1, so the heap stays valid). A priority
        # flip (Emergency <-> the others) changes field 0, so the
        # queue is re-ordered with heapify — the token stays the
        # same, so the bill and the status history (both keyed by
        # doctor + token) stay linked. Old rows (shorter than 14
        # fields) are padded out properly here.
        new_row = (
            1 if new_is_emg else 2, row[1], row[2], row[3], row[4],
            row[5],
            row[6] if len(row) > 6 else None,
            new_date, new_time, _row_status(row),
            row[10] if len(row) > 10 else None,
            new_type, new_reason, new_notes,
        )
        idx = next((i for i, r in enumerate(doc["queue"].heap)
                    if r[2] == appt["token"]), None)
        doc["queue"].heap[idx] = new_row
        if new_is_emg != (row[0] == 1):
            heapq.heapify(doc["queue"].heap)
        self._persist()
        return _public_appointment(doc, new_row)

    def appointments_to_csv(self, rows):
        """The given appointment dicts as a CSV string — stdlib csv only.

        One row per appointment, all columns. Opens in Excel.
        """
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Appointment ID", "Token", "Patient Name",
                         "Patient ID", "Doctor", "Department", "Date",
                         "Time", "Type", "Status", "Fee (Rs)", "Reason"])
        for a in rows:
            writer.writerow([a.get("apt_id") or "", a.get("token", ""),
                             a.get("patient_name", ""),
                             a.get("patient_id", ""),
                             a.get("doctor_name", ""),
                             a.get("department", ""),
                             a.get("appt_date") or "",
                             a.get("appt_time") or "",
                             a.get("appt_type", ""), a.get("status", ""),
                             a.get("fee", ""), a.get("reason", "")])
        return out.getvalue()

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

    def dashboard_data(self, today=None):
        """Everything the Step 7 dashboard needs — from LIVE data only.

        No number here is a sample: the counts, the money, the monthly
        series, the department split, today's list and the activity
        feed all come from the patients, doctors, appointments, bills
        and status history actually stored. An empty hospital reports
        honest zeros. A monthly chart counts an appointment in the
        month it is scheduled for (or the month it was booked, when it
        has no scheduled date); rows with no date at all are left out
        of the chart but still count everywhere else.
        """
        if today is None:
            today = datetime.date.today().isoformat()
        year, month = int(today[:4]), int(today[5:7])

        rows = []  # (doctor dict, heap row) for every appointment
        for doc in self.doctors_map.values():
            for row in doc["queue"].heap:
                rows.append((doc, row))

        def _appt_month(row):
            d = (row[7] if len(row) > 7 else None) or \
                (row[6] if len(row) > 6 else None)
            return d[:7] if d else None  # "YYYY-MM"

        # ---- Monthly series: this year (Jan–Dec) + rolling last 12
        months_year = [f"{year}-{m:02d}" for m in range(1, 13)]
        counts_year = {m: 0 for m in months_year}
        rolling = []
        y, m = year, month
        for _ in range(12):
            rolling.append(f"{y}-{m:02d}")
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        rolling.reverse()
        counts_roll = {m: 0 for m in rolling}
        for _, row in rows:
            key = _appt_month(row)
            if key in counts_year:
                counts_year[key] += 1
            if key in counts_roll:
                counts_roll[key] += 1
        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        monthly_year = [
            {"label": month_names[int(k[5:7]) - 1],
             "count": counts_year[k]} for k in months_year]
        monthly_last12 = [
            {"label": f"{month_names[int(k[5:7]) - 1]} '{k[2:4]}",
             "count": counts_roll[k]} for k in rolling]

        # ---- Departments: appointments per doctor specialization
        dept_counts = {}
        for doc, _row in rows:
            dept_counts[doc["spec"]] = dept_counts.get(doc["spec"], 0) + 1
        departments = [
            {"name": name, "count": c,
             "percent": round(100 * c / len(rows)) if rows else 0}
            for name, c in sorted(dept_counts.items(),
                                  key=lambda kv: -kv[1])]

        # ---- Today's appointments (time, then Emergency-first)
        todays = self.appointments_for_date(today)
        for a in todays:
            a["department"] = self.doctors_map[a["doctor_id"]]["spec"]

        # ---- Money + pending items (the bell badge counts these)
        bills = self.billing.list_bills()
        total_revenue = sum(b["total"] for b in bills
                            if b["status"] == "Paid")
        pending_bills = [b for b in bills if b["status"] == "Unpaid"]
        pending_appts = []
        for doc, row in rows:
            a = _public_appointment(doc, row)
            if a["status"] == "Pending":
                pending_appts.append(a)

        # ---- Recent activities: status changes, registrations, bills,
        # bookings — newest first, latest 8. Items with only a date
        # sort as the START of that day, so a status change stamped
        # this morning still comes before a registration dated today
        # (whose exact time was never recorded), while anything from
        # yesterday still comes after anything dated today.
        activities = []
        for h in self.status_history.recent(10):
            appt = self.get_appointment(h["doctor_id"], h["token"])
            who = appt["patient_name"] if appt else h["token"]
            activities.append({
                "kind": "status",
                "text": f"Status changed: {h['token']} ({who}) — "
                        f"{h['old_status'] or '—'} → {h['new_status']} "
                        f"by {h['changed_by']}",
                "when": h["changed_at"], "sort": h["changed_at"],
            })
        for pat in sorted(self.patients_list, key=lambda p: p["id"],
                          reverse=True)[:5]:
            reg = pat.get("registered") or today
            activities.append({
                "kind": "patient",
                "text": f"Patient registered: {pat['name']} (#{pat['id']})",
                "when": reg, "sort": reg + "T00:00:00",
            })
        for b in sorted(bills, key=lambda b: b["id"], reverse=True)[:5]:
            activities.append({
                "kind": "bill",
                "text": f"Bill #{b['id']} — {b['patient_name']}: "
                        f"Rs {b['total']} ({b['status']})",
                "when": b["date"], "sort": b["date"] + "T00:00:00",
            })
        for doc, row in sorted(
                rows, key=lambda dr: ((dr[1][6] or ""), dr[1][2]),
                reverse=True)[:5]:
            a = _public_appointment(doc, row)
            booked = a["booked_date"] or today
            activities.append({
                "kind": "appointment",
                "text": f"Appointment booked: {a['patient_name']} with "
                        f"{a['doctor_name']} ({a['token']})",
                "when": booked, "sort": booked + "T00:00:00",
            })
        activities.sort(key=lambda x: x["sort"], reverse=True)
        activities = activities[:8]
        for a in activities:
            del a["sort"]

        return {
            "date": today,
            "total_patients": len(self.patients_map),
            "total_doctors": len(self.doctors_map),
            "todays_appointments": len(todays),
            "available_beds": sum(1 for b in self.beds if b == 0),
            "total_beds": self.NUM_BEDS,
            "total_revenue": total_revenue,
            "pending_bills": len(pending_bills),
            "pending_bills_total": sum(b["total"]
                                       for b in pending_bills),
            "pending_appointments": len(pending_appts),
            "notification_count": len(pending_appts) + len(pending_bills),
            "monthly_year": monthly_year,
            "monthly_last12": monthly_last12,
            "departments": departments,
            "todays_list": todays,
            "activities": activities,
            "pending_items": {
                "appointments": [{
                    "token": a["token"], "doctor_id": a["doctor_id"],
                    "doctor_name": a["doctor_name"],
                    "patient_name": a["patient_name"],
                    "appt_date": a["appt_date"],
                    "appt_time": a["appt_time"],
                } for a in pending_appts[:10]],
                "bills": [{
                    "id": b["id"], "patient_name": b["patient_name"],
                    "total": b["total"],
                } for b in pending_bills[:10]],
            },
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


def appointments_table_menu(hosp, role, user):
    """All Appointments (Step 9, menu 21): the appointments table on
    the command line — search + filters, paged 10 at a time, then
    pick one by Appointment ID for the full details, edit/reschedule
    or cancel. Doctors only ever see their own queue; only
    Admin/Receptionist may edit or cancel."""
    doctor_filter = None
    if role == "Doctor" and user.get("doctor_id"):
        doctor_filter = user["doctor_id"]
    print("\n  --- All Appointments ---")
    search = input("  Search (patient name / APT ID / token, empty = all): ").strip() or None
    date = input("  Date (YYYY-MM-DD, empty = all dates): ").strip() or None
    if doctor_filter:
        doc_id = doctor_filter
        print(f"  Doctor: {doctor_filter} (your queue)")
    else:
        doc_id = input("  Doctor ID (empty = all doctors): ").strip().upper() or None
    dept = input("  Department (empty = all): ").strip() or None
    status = input("  Status (empty = all): ").strip() or None
    sort = input("  Sort by (date/time/patient_name/doctor_name/status, empty = date): ").strip() or "date"
    page = 1
    while True:
        try:
            result = hosp.list_appointments(
                search=search, date=date, doctor_id=doc_id,
                department=dept, status=status, sort=sort,
                page=page, per_page=10)
        except ValueError as exc:
            return print(f"  {exc}")
        rows = result["appointments"]
        print(f"\n  --- Appointments — page {result['page']} of {result['pages']} ({result['total']} total) ---")
        if not rows:
            print("  No appointments match.")
        for a in rows:
            when = (a["appt_date"] or "no date") \
                + (f" {a['appt_time']}" if a["appt_time"] else "")
            print(f"  {a['apt_id'] or '—':8} | {a['patient_name']} (#{a['patient_id']}) | "
                  f"{a['doctor_name']} ({a['department']}) | {when} | "
                  f"{a['appt_type']} | {a['status']}")
        choice = input("  [N]ext page / [P]revious / APT ID to open / [Enter] back: ").strip()
        if not choice:
            return
        if choice.upper() == "N":
            if page < result["pages"]:
                page += 1
            else:
                print("  Already on the last page.")
            continue
        if choice.upper() == "P":
            if page > 1:
                page -= 1
            else:
                print("  Already on the first page.")
            continue
        _appointment_detail_menu(hosp, role, user, choice)


def calendar_menu(hosp, role, user):
    """Appointment Calendar (Step 10, menu 22): a month grid with
    appointment counts per day, then pick a day to see that day's
    appointments time-ordered (with status), then pick one by
    Appointment ID for the full details (Step 9 flow). Doctors only
    ever see their own queue; the doctor/department filters work
    the same as the table's."""
    doctor_filter = None
    if role == "Doctor" and user.get("doctor_id"):
        doctor_filter = user["doctor_id"]
    print("\n  --- Appointment Calendar ---")
    month_raw = input("  Month (YYYY-MM, empty = this month): ").strip()
    if month_raw:
        try:
            year, month = (int(x) for x in month_raw.split("-", 1))
            first = datetime.date(year, month, 1)
        except (ValueError, TypeError):
            return print("  Month must look like YYYY-MM (e.g. 2026-11).")
    else:
        today = datetime.date.today()
        first = datetime.date(today.year, today.month, 1)
    if doctor_filter:
        doc_id = doctor_filter
        print(f"  Doctor: {doctor_filter} (your queue)")
    else:
        doc_id = input("  Doctor ID (empty = all doctors): ").strip().upper() or None
    dept = input("  Department (empty = all): ").strip() or None
    if first.month == 12:
        next_first = datetime.date(first.year + 1, 1, 1)
    else:
        next_first = datetime.date(first.year, first.month + 1, 1)
    last = next_first - datetime.timedelta(days=1)
    try:
        rows = hosp.calendar_appointments(
            first.isoformat(), last.isoformat(),
            doctor_id=doc_id, department=dept)
    except ValueError as exc:
        return print(f"  {exc}")
    counts = {}
    for a in rows:
        counts[a["appt_date"]] = counts.get(a["appt_date"], 0) + 1
    today = datetime.date.today()
    print(f"\n  --- {first.strftime('%B %Y')} ({len(rows)} appointment"
          f"{'s' if len(rows) != 1 else ''}) ---")
    print("  " + "".join(f"{w:<7}" for w in
                         ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")))
    cells = ["       "] * first.weekday()  # Monday-first grid
    for day in range(1, last.day + 1):
        d = datetime.date(first.year, first.month, day)
        n = counts.get(d.isoformat(), 0)
        mark = "*" if d == today else " "
        cells.append(f"{day:2}{mark}" + (f"({n}) " if n else "    "))
    while len(cells) % 7:
        cells.append("       ")
    for i in range(0, len(cells), 7):
        print("  " + "".join(cells[i:i + 7]))
    print("  * = today. A number in brackets is that day's appointments.")
    while True:
        choice = input("  Day number to open (empty = back): ").strip()
        if not choice:
            return
        try:
            picked = datetime.date(first.year, first.month, int(choice))
        except ValueError:
            print("  Pick a day in this month.")
            continue
        day_rows = hosp.appointments_for_date(picked.isoformat(), doc_id)
        if dept:
            day_rows = [a for a in day_rows if a["department"] == dept]
        print(f"\n  --- {picked.isoformat()} "
              f"({len(day_rows)} appointment"
              f"{'s' if len(day_rows) != 1 else ''}) ---")
        if not day_rows:
            print("  No appointments on this date.")
            continue
        for a in day_rows:
            when = a["appt_time"] or "no fixed time"
            print(f"  {when} | {a['apt_id'] or '—'} ({a['token']}) | "
                  f"{a['patient_name']} (#{a['patient_id']}) | "
                  f"{a['doctor_name']} | {a['appt_type']} | {a['status']}")
        ref = input("  APT ID to open (empty = back to the month): ").strip()
        if not ref:
            continue
        _appointment_detail_menu(hosp, role, user, ref)


def availability_menu(hosp, role, user):
    """Doctor Availability (Step 11, menu 23): a doctor's working
    pattern and their slots for a date; Admin sets or clears it. A
    Doctor only ever sees their own availability."""
    doctor_filter = None
    if role == "Doctor" and user.get("doctor_id"):
        doctor_filter = user["doctor_id"]
    if doctor_filter:
        doc_id = doctor_filter
        print(f"  Doctor: {doctor_filter} (your availability)")
    else:
        if not hosp.doctors_map:
            return print("  No doctors yet — ask Admin to add one first.")
        for doc in hosp.doctors_map.values():
            print(f"  {doc['id']} | {doc['name']} | {doc['spec']}")
        doc_id = input("  Doctor ID: ").strip().upper()
        if doc_id not in hosp.doctors_map:
            return print("  Doctor not found.")
    doc = hosp.doctors_map[doc_id]
    avail = hosp.get_doctor_availability(doc_id)
    if avail is None:
        print(f"  {doc['name']} — no availability set yet "
              "(any time is allowed).")
    else:
        print(f"  {doc['name']} — {', '.join(avail['working_days'])}, "
              f"{avail['start_time']}–{avail['end_time']}, "
              f"{avail['duration_minutes']} min per appointment")
        if avail["breaks"]:
            print("  Breaks: " + ", ".join(
                f"{bs}–{be}" for bs, be in avail["breaks"]))
        else:
            print("  Breaks: none")
    date = input("  Show slots for date (YYYY-MM-DD, empty = today): ").strip() or None
    try:
        slots = hosp.availability_slots(doc_id, date)
    except ValueError as exc:
        return print(f"  {exc}")
    if slots["availability"] is None:
        print("  No availability set — slots are not listed; any time is allowed.")
    elif slots["unavailable_reason"]:
        print(f"  {slots['unavailable_reason']}")
    else:
        print(f"  Slots on {slots['date']}:")
        if not slots["slots"]:
            print("    (no slots)")
        for s in slots["slots"]:
            tag = "free" if s["available"] else f"booked ({s['token']})"
            print(f"    {s['time']} — {tag}")
    if role != "Admin":
        return print("  Only Admin can change availability.")
    choice = input("  [S]et availability / [C]lear availability / [Enter] back: ").strip().upper()
    if choice == "C":
        if hosp.clear_doctor_availability(doc_id, role=role):
            print("  Availability cleared — any time is allowed again.")
        else:
            print("  No availability was set.")
        return
    if choice != "S":
        return
    days_raw = input("  Working days (e.g. Mon,Tue,Wed,Thu,Fri): ").strip()
    working_days = [d.strip() for d in days_raw.split(",") if d.strip()]
    start = input("  Working hours start (HH:MM, e.g. 09:00): ").strip()
    end = input("  Working hours end (HH:MM, e.g. 17:00): ").strip()
    try:
        duration = int(input("  Consultation duration in minutes (e.g. 30): ").strip())
    except ValueError:
        return print("  Duration must be a whole number of minutes.")
    breaks_raw = input("  Breaks (e.g. 13:00-14:00,16:00-16:15 — empty = none): ").strip()
    breaks = [b.strip() for b in breaks_raw.split(",") if b.strip()] \
        if breaks_raw else []
    try:
        record = hosp.set_doctor_availability(
            doc_id, working_days, start, end, duration, breaks, role=role)
    except (ValueError, PermissionError) as exc:
        return print(f"  {exc}")
    print(f"  Availability saved for {doc['name']}: "
          f"{', '.join(record['working_days'])}, "
          f"{record['start_time']}–{record['end_time']}, "
          f"{record['duration_minutes']} min.")


def _leave_menu(hosp, role, user):
    """Doctor leave: list, add, delete. A Doctor only ever touches
    their OWN leave; everyone may view."""
    doctor_filter = None
    if role == "Doctor" and user.get("doctor_id"):
        doctor_filter = user["doctor_id"]
    if doctor_filter:
        doc_id = doctor_filter
        print(f"  Doctor: {doctor_filter} (your leave)")
    else:
        if not hosp.doctors_map:
            return print("  No doctors yet.")
        doc_id = input("  Doctor ID: ").strip().upper()
        if doc_id not in hosp.doctors_map:
            return print("  Doctor not found.")
    leaves = hosp.list_doctor_leaves(
        doc_id, role=role, user_doctor_id=user.get("doctor_id"))
    if not leaves:
        print("  No leave recorded for this doctor.")
    for lv in leaves:
        print(f"  #{lv['id']} | {lv['start_date']} to {lv['end_date']}"
              + (f" | {lv['reason']}" if lv["reason"] else ""))
    choice = input("  [A]dd leave  [D]elete leave  [Enter] back: ").strip().upper()
    if choice == "A":
        if role not in ("Admin", "Doctor"):
            return print("  Only Admin or the doctor themself can add leave.")
        start = input("  Leave from (YYYY-MM-DD): ").strip()
        end = input("  Leave to (YYYY-MM-DD): ").strip()
        reason = input("  Reason (optional): ").strip()
        try:
            row = hosp.add_doctor_leave(
                doc_id, start, end, reason, role=role,
                user_doctor_id=user.get("doctor_id"))
        except (ValueError, PermissionError) as exc:
            return print(f"  {exc}")
        print(f"  Leave #{row['id']} added: "
              f"{row['start_date']} to {row['end_date']}.")
    elif choice == "D":
        if role not in ("Admin", "Doctor"):
            return print("  Only Admin or the doctor themself can delete leave.")
        try:
            leave_id = int(input("  Leave ID to delete: ").strip())
        except ValueError:
            return print("  Leave ID must be a number.")
        try:
            hosp.delete_doctor_leave(
                leave_id, role=role, user_doctor_id=user.get("doctor_id"))
        except (ValueError, PermissionError) as exc:
            return print(f"  {exc}")
        print("  Leave deleted.")
    elif choice == "":
        return
    else:
        print("  Pick A, D or Enter.")


def _holidays_menu(hosp, role):
    """Hospital holidays: list, add, delete. Holidays are Admin only
    to change; everyone may view."""
    holidays = hosp.list_holidays()
    if not holidays:
        print("  No holidays recorded.")
    for h in holidays:
        print(f"  #{h['id']} | {h['date']} — {h['name']}")
    choice = input("  [A]dd holiday  [D]elete holiday  [Enter] back: ").strip().upper()
    if choice == "A":
        if role != "Admin":
            return print("  Only Admin can add holidays.")
        date = input("  Holiday date (YYYY-MM-DD): ").strip()
        name = input("  Holiday name (e.g. Eid-ul-Fitr): ").strip()
        try:
            row = hosp.add_holiday(date, name, role=role)
        except (ValueError, PermissionError) as exc:
            return print(f"  {exc}")
        print(f"  Holiday #{row['id']} added: {row['date']} — {row['name']}.")
    elif choice == "D":
        if role != "Admin":
            return print("  Only Admin can delete holidays.")
        try:
            holiday_id = int(input("  Holiday ID to delete: ").strip())
        except ValueError:
            return print("  Holiday ID must be a number.")
        try:
            hosp.delete_holiday(holiday_id, role=role)
        except (ValueError, PermissionError) as exc:
            return print(f"  {exc}")
        print("  Holiday deleted.")
    elif choice == "":
        return
    else:
        print("  Pick A, D or Enter.")


def leave_holidays_menu(hosp, role, user):
    """Doctor leave + hospital holidays (Step 11, menu 24)."""
    print("\n  --- Leave & Holidays ---")
    print("  1. Doctor Leave")
    print("  2. Hospital Holidays")
    choice = input("  Pick (1-2, empty = back): ").strip()
    if choice == "1":
        _leave_menu(hosp, role, user)
    elif choice == "2":
        _holidays_menu(hosp, role)
    elif choice == "":
        return
    else:
        print("  Pick 1 or 2.")


def _appointment_detail_menu(hosp, role, user, apt_ref):
    """Full details for one appointment (Step 9) — everything the
    details page shows, plus edit/reschedule and cancel for
    Admin/Receptionist. Returns when the user goes back."""
    detail = hosp.appointment_detail(apt_ref)
    if detail is None:
        return print("  Appointment not found.")
    a, pat, doc, pay = (detail["appointment"], detail["patient"],
                        detail["doctor"], detail["payment"])
    if role == "Doctor" and user.get("doctor_id") \
            and user["doctor_id"].upper() != a["doctor_id"]:
        return print("  Doctors can only view appointments in their own queue.")
    print(f"\n  --- Appointment {a['apt_id'] or a['token']} ---")
    print(f"  Patient: {pat['name']} (#{pat['id']}) | Age {pat.get('age')} | {pat.get('problem') or ''}")
    if pat.get("father_name") or pat.get("phone") or pat.get("gender"):
        print(f"  Father: {pat.get('father_name') or '—'} | Phone: {pat.get('phone') or '—'} | Gender: {pat.get('gender') or '—'}")
    print(f"  Doctor: {doc['name']} ({doc['spec']}) | Fee Rs {doc['fee']}")
    when = (a["appt_date"] or "no date") \
        + (f" {a['appt_time']}" if a["appt_time"] else "")
    print(f"  When: {when} | Type: {a['appt_type']} | Status: {a['status']}")
    print(f"  Reason: {a['reason'] or '—'} | Notes: {a['notes'] or '—'}")
    print(f"  Booked: {a['booked_date'] or '—'} | Token: {a['token']}")
    if pay["billed"]:
        print(f"  Payment: Bill #{pay['bill_id']} — Rs {pay['total']} ({pay['status']})")
    else:
        print("  Payment: Not billed yet.")
    history = detail["history"]
    if history:
        print("  History:")
        for h in history:
            print(f"    {h['changed_at']} | {h['old_status'] or '—'} -> {h['new_status']} by {h['changed_by']}")
    else:
        print("  No status changes yet.")
    can_edit = role in ("Admin", "Receptionist")
    while True:
        prompt = "  [B]ack" if not can_edit \
            else "  [E]dit / Reschedule  [C]ancel appointment  [B]ack"
        choice = input(prompt + ": ").strip().upper()
        if choice in ("B", ""):
            return
        if not can_edit:
            print("  Only Admin / Receptionist can edit or cancel.")
            continue
        if choice == "E":
            date_raw = input(f"  New date [{a['appt_date']}] (Enter = keep): ").strip()
            time_raw = input(f"  New time [{a['appt_time'] or 'no fixed time'}] (Enter = keep, 'none' = no fixed time): ").strip()
            type_raw = input(f"  New type [{a['appt_type']}] (General/Follow-up/Emergency, Enter = keep): ").strip()
            reason = input(f"  New reason [{a['reason']}] (Enter = keep): ").strip()
            notes = input(f"  New notes [{a['notes']}] (Enter = keep): ").strip()
            kwargs = {}
            if date_raw:
                kwargs["date"] = date_raw
            if time_raw:
                kwargs["time"] = "" if time_raw.lower() == "none" else time_raw
            if type_raw:
                kwargs["appt_type"] = type_raw.title()
            if reason:
                kwargs["reason"] = reason
            if notes:
                kwargs["notes"] = notes
            if not kwargs:
                print("  Nothing changed.")
                continue
            try:
                updated = hosp.edit_appointment(
                    a["apt_id"] or f"{a['doctor_id']}:{a['token']}",
                    role=role, **kwargs)
            except (ValueError, PermissionError) as exc:
                print(f"  {exc}")
                continue
            print(f"  Updated: {updated['apt_id']} ({updated['token']}) | "
                  f"{updated['appt_date']}"
                  f"{(' ' + updated['appt_time']) if updated['appt_time'] else ''} | "
                  f"{updated['appt_type']} | {updated['status']}")
            detail = hosp.appointment_detail(
                updated["apt_id"] or f"{updated['doctor_id']}:{updated['token']}")
            a = detail["appointment"]
            continue
        if choice == "C":
            if input("  Cancel this appointment? Type YES to confirm: ").strip().upper() != "YES":
                print("  Not cancelled.")
                continue
            try:
                updated = hosp.update_status(
                    a["doctor_id"], a["token"], "Cancelled",
                    changed_by=user["username"], role=role)
            except (ValueError, PermissionError) as exc:
                print(f"  {exc}")
                continue
            print(f"  Cancelled: {updated['token']} is now {updated['status']}.")
            return
        print("  Pick E, C or B.")


def book_appointment_menu(hosp):
    """Book Appointment (Step 8, menu 6): the full booking form on the
    command line — search an existing patient (ID / name / phone) or
    register a new one inline, pick department then doctor, see that
    doctor's booked times for the day, then date/time/type/reason/
    notes/status. A confirmation summary is shown before booking, and
    after booking the user is offered a bill for the appointment
    (Step 3 flow — only when they ask, never automatic).
    """
    print("\n  --- Book Appointment ---")
    # ---- 1. Patient: search, or register a new one inline
    print("  1. Patient")
    query = input("  Search patient by ID / name / phone (empty = new patient): ").strip()
    pat = None
    if query:
        matches = hosp.search_patients(query)
        if not matches:
            print("  No patient found — register a new patient below.")
        else:
            for i, p in enumerate(matches, 1):
                print(f"    {i}. ID {p['id']} | {p['name']} | Age {p['age']} | {p['problem']}"
                      + (f" | {p['phone']}" if p.get("phone") else ""))
            try:
                pick = int(input("  Pick a patient (number, 0 = new patient): ").strip())
            except ValueError:
                return print("  Pick a number.")
            if pick == 0:
                pass
            elif 1 <= pick <= len(matches):
                pat = hosp.patients_map[matches[pick - 1]["id"]]
            else:
                return print("  Pick a number from the list.")
    if pat is None:
        pid = hosp.next_patient_id()
        print(f"  New patient — ID will be {pid}.")
        name = input("  Full Name: ").strip()
        try:
            age = int(input("  Age: ").strip())
        except ValueError:
            return print("  Age must be a number.")
        problem = input("  Problem / Symptoms: ").strip()
        father_name = input("  Father Name (optional): ").strip()
        phone = input("  Phone (optional): ").strip()
        gender = input("  Gender (Male/Female/Other, optional): ").strip()
        try:
            name, age, problem = validate_patient_details(name, age, problem)
            phone = validate_phone(phone)
            gender = validate_gender(gender)
        except ValueError as exc:
            return print(f"  {exc}")
        hosp.add_patient(pid, name, age, problem, father_name=father_name,
                         phone=phone, gender=gender)
        pat = hosp.patients_map[pid]
        print(f"  Patient {name} (#{pid}) registered.")
    # ---- 2. Department, then doctor
    print("  2. Doctor")
    depts = hosp.departments()
    if not depts:
        return print("  No doctors yet — ask Admin to add one first.")
    for i, dept in enumerate(depts, 1):
        print(f"    {i}. {dept}")
    try:
        pick = int(input("  Pick a department (number): ").strip())
        dept = depts[pick - 1]
    except (ValueError, IndexError):
        return print("  Pick a number from the list.")
    docs = hosp.doctors_in_department(dept)
    for i, doc in enumerate(docs, 1):
        print(f"    {i}. {doc['id']} | {doc['name']} | Fee Rs {doc['fee']}")
    try:
        pick = int(input("  Pick a doctor (number): ").strip())
        doc = docs[pick - 1]
    except (ValueError, IndexError):
        return print("  Pick a number from the list.")
    # ---- 3. Date, then that doctor's booked times, then time
    print(f"  3. Date & Time — {doc['name']} ({doc['spec']}), Fee Rs {doc['fee']}")
    date_raw = input("  Date (YYYY-MM-DD, empty = today): ").strip() or None
    try:
        appt_date, _ = validate_appt_datetime(date_raw, None)
    except ValueError as exc:
        return print(f"  {exc}")
    try:
        booked = hosp.doctor_booked_times(doc["id"], appt_date)
    except ValueError as exc:
        return print(f"  {exc}")
    if booked:
        print(f"  Already booked on {appt_date}:")
        for b in booked:
            tag = "TAKEN" if b["status"] not in _INACTIVE_STATUSES else "free again"
            print(f"    {b['time']} | {b['token']} ({b['patient_name']}) | {b['status']} — {tag}")
    else:
        print(f"  Nothing booked on {appt_date} yet — all times free.")
    # Step 11: the doctor's available slots for the day (working
    # hours minus breaks, in consultation-duration steps) — or the
    # plain reason when there are none (holiday, leave, not a
    # working day), or the note that no availability is set yet and
    # any time is allowed.
    try:
        slots = hosp.availability_slots(doc["id"], appt_date)
    except ValueError as exc:
        return print(f"  {exc}")
    if slots["availability"] is None:
        print("  No availability set for this doctor yet — any time is allowed.")
    elif slots["unavailable_reason"]:
        print(f"  {slots['unavailable_reason']}")
    else:
        free = [s["time"] for s in slots["slots"] if s["available"]]
        taken = [s["time"] for s in slots["slots"] if not s["available"]]
        print(f"  Available slots on {appt_date}: "
              f"{', '.join(free) if free else 'none'}")
        if taken:
            print(f"  Already booked: {', '.join(taken)}")
    appt_time = input("  Time (HH:MM, empty = no fixed time): ").strip() or None
    # ---- 4. Type, reason, notes, status
    print("  4. Appointment details")
    print("  Type: 1. General  2. Follow-up  3. Emergency (E- token, first in queue)")
    type_raw = input("  Pick a type (1-3, empty = General): ").strip()
    appt_type = {"1": "General", "2": "Follow-up",
                 "3": "Emergency"}.get(type_raw or "1")
    if appt_type is None:
        return print("  Pick 1, 2 or 3.")
    reason = input("  Reason for Visit: ").strip()
    notes = input("  Additional Notes (optional): ").strip()
    status_raw = input("  Status (Pending / Confirmed, empty = Pending): ").strip().title()
    status = status_raw if status_raw in BOOKING_STATUSES else "Pending"
    # ---- 5. Confirm, then book
    print("\n  --- Confirm ---")
    print(f"  Patient: {pat['name']} (#{pat['id']})")
    print(f"  Doctor: {doc['name']} ({doc['spec']}) — Fee Rs {doc['fee']}")
    print(f"  When: {appt_date}"
          + (f" {appt_time}" if appt_time else " (no fixed time)"))
    print(f"  Type: {appt_type} | Status: {status}")
    if reason:
        print(f"  Reason: {reason}")
    if input("  Book this appointment? (Y/N): ").strip().upper() != "Y":
        return print("  Cancelled — nothing booked.")
    token = hosp.book_appointment(doc["id"], pat["id"], False, appt_date,
                                  appt_time, appt_type=appt_type,
                                  reason=reason, notes=notes, status=status)
    if token is None:
        return print("  Booking failed — see the message above.")
    appt = hosp.get_appointment(doc["id"], token)
    print(f"  Booked! Appointment ID {appt['apt_id']} — Token {token} "
          f"— Fee Rs {doc['fee']} — {status}")
    # ---- 6. Offer the bill (Step 3 flow — only when the user asks)
    if input("  Generate a bill for this appointment now? (Y/N): ").strip().upper() == "Y":
        try:
            bill = hosp.create_bill(pat["id"], doc["id"], token)
        except ValueError as exc:
            return print(f"  {exc}")
        print(f"  Bill #{bill['id']} created — Total Rs {bill['total']} (Unpaid).")


def add_patient_menu(hosp):
    """Add Patient (menu 1, Step 8): ID + name + age + problem, plus
    the optional Father Name, Phone and Gender."""
    try:
        pid = int(input("ID: "))
    except ValueError:
        return print("  ID must be a number.")
    name = input("Name: ").strip()
    try:
        age = int(input("Age: "))
    except ValueError:
        return print("  Age must be a number.")
    problem = input("Problem: ").strip()
    father_name = input("Father Name (optional): ").strip()
    phone = input("Phone (optional): ").strip()
    gender = input("Gender (Male/Female/Other, optional): ").strip()
    try:
        name, age, problem = validate_patient_details(name, age, problem)
        phone = validate_phone(phone)
        gender = validate_gender(gender)
    except ValueError as exc:
        return print(f"  {exc}")
    hosp.add_patient(pid, name, age, problem, father_name=father_name,
                     phone=phone, gender=gender)


def find_patient_menu(hosp):
    """Find a patient — by ID (binary search) or by name / phone / ID
    (Step 8: one search matches all three)."""
    opt = input("  1. By ID  2. Search (name / phone / ID): ").strip()
    if opt == "1":
        try:
            pid = int(input("  ID: "))
        except ValueError:
            return print("  ID must be a number.")
        hosp.find_patient(pid)
    elif opt == "2":
        query = input("  Search (name, phone or ID): ").strip()
        matches = hosp.search_patients(query)
        if not matches:
            return print("  No patient found.")
        for p in matches:
            print(f"  ID {p['id']} | {p['name']} | Age {p['age']} | "
                  f"{p['problem']} | Visits: {p['visits']}"
                  + (f" | {p['phone']}" if p.get("phone") else ""))
    else:
        print("  Invalid choice.")


def edit_patient_menu(hosp):
    """Edit a patient (Step 5): change name, age and/or problem.
    The ID never changes; Enter keeps the current value.
    Step 8: Father Name, Phone and Gender can be changed the same way.
    """
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
    father_name = input(f"  New father name [{pat.get('father_name') or ''}]: ").strip() or None
    phone = input(f"  New phone [{pat.get('phone') or ''}]: ").strip() or None
    gender = input(f"  New gender [{pat.get('gender') or ''}]: ").strip() or None
    age = None
    if age_raw:
        try:
            age = int(age_raw)
        except ValueError:
            return print("  Age must be a number.")
    try:
        updated = hosp.edit_patient(pid, name=name, age=age, problem=problem,
                                    father_name=father_name, phone=phone,
                                    gender=gender)
    except ValueError as exc:
        return print(f"  {exc}")
    print(f"  Updated: ID {updated['id']} | {updated['name']} | "
          f"Age {updated['age']} | {updated['problem']}"
          + (f" | Phone {updated['phone']}" if updated.get("phone") else ""))


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
    ("21", "All Appointments", "view"),  # Step 9: table + details
    ("22", "Calendar", "view"),  # Step 10: day/week/month calendar
    ("23", "Availability", "view_availability"),  # Step 11: doctor availability
    ("24", "Leave & Holidays", "view_availability"),  # Step 11: leave + holidays
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
            add_patient_menu(hosp)
        elif choice == "2":
            hosp.show_sorted("age" if input("Sort by (id/age): ").strip() == "age" else "id")
        elif choice == "3":
            find_patient_menu(hosp)
        elif choice == "4":
            hosp.add_note(int(input("ID: ")), input("Note: "))
        elif choice == "5":
            hosp.doctors_menu(role)
        elif choice == "6":
            # Step 8: the full booking form — search or register a
            # patient inline, pick department/doctor, date/time/type.
            book_appointment_menu(hosp)
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
        elif choice == "21":
            appointments_table_menu(hosp, role, user)
        elif choice == "22":
            calendar_menu(hosp, role, user)
        elif choice == "23":
            availability_menu(hosp, role, user)
        elif choice == "24":
            leave_holidays_menu(hosp, role, user)


if __name__ == "__main__":
    main()