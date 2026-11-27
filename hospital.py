"""Hospital Management System — core logic (data structures + operations).

Step 1 (SQLite): the Hospital can now be opened with a database file
(``Hospital(db_path="hospital.db")``) — every change is saved at once and
loaded back on the next start, so data survives a restart. With no
``db_path`` it works exactly as before: fully in memory, nothing written.
The undo stack is session-only and starts empty after a restart.
"""

import getpass
import os

from auth import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME, AuthStore, can
from database import Database

# SQLite file used by the CLI and the web server (same file, so both
# see the same patients, appointments and beds).
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "hospital.db")


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

    def enqueue(self, is_emg, pid, name, fee):
        self.count += 1
        token = f"{'E' if is_emg else 'N'}-{self.count}"
        self.heap.append((1 if is_emg else 2, self.count, token, pid, name, fee))
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
    STARTER_DOCTORS = [
        ("D1", "Dr. Tariq", "Neurosurgeon", 2500),
        ("D2", "Dr. Ayesha", "Cardiologist", 2000),
        ("D3", "Dr. Bilal", "Orthopedic", 1800),
        ("D4", "Dr. Sana", "Physician", 1200),
        ("D5", "Dr. Kamran", "Pediatrician", 1500),
    ]

    def __init__(self, db_path=None):
        self.patients_map, self.patients_list, self.doctors_map = {}, [], {}
        self.beds = [0] * self.NUM_BEDS
        self.undo_stack, self.pharmacy_q, self.shift_q = Stack(), Queue(), CircularQueue(20)
        for doc_id, name, spec, fee in self.STARTER_DOCTORS:
            self.doctors_map[doc_id] = {"id": doc_id, "name": name, "spec": spec, "fee": fee, "queue": PriorityQueue()}
            self.shift_q.enqueue(f"{doc_id} - {name}")
        # Persistence (Step 1): with a db file, load saved state (or seed
        # the file with this fresh starter state on first run). Without
        # one, everything stays in memory as before.
        self.db_path = db_path
        self.db = Database(db_path) if db_path else None
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
        pat = {"id": pid, "name": name, "age": age, "problem": problem, "visits": 0, "notes": LinkedList()}
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
            print(f"  {row[2]} | Patient {row[3]} ({row[4]})"
                  f"{' [EMERGENCY]' if row[0] == 1 else ''}")

    def doctors_menu(self, role="Admin"):
        opt = input("  1. View Doctors  2. Add Doctor: ").strip()
        if opt == "1":
            for doc in self.doctors_map.values():
                print(f"  {doc['id']} | {doc['name']} | {doc['spec']} | Fee: {doc['fee']}")
        elif opt == "2":
            if not can(role, "add_doctor"):
                return print("  Only Admin can add a doctor.")
            doc_id = input("  Doctor ID: ").strip().upper()
            if doc_id in self.doctors_map:
                return print("  Doctor ID already exists.")
            name, spec = input("  Name: ").strip(), input("  Specialty: ").strip()
            fee = int(input("  Token Fee: "))
            self.doctors_map[doc_id] = {"id": doc_id, "name": name, "spec": spec, "fee": fee, "queue": PriorityQueue()}
            self.shift_q.enqueue(f"{doc_id} - {name}")
            self._persist()
            print("  Doctor added.")

    def book_appointment(self, doc_id, pid, is_emg):
        doc, pat = self.doctors_map.get(doc_id.upper()), self.patients_map.get(pid)
        if not doc or not pat:
            return print("  Invalid doctor or patient ID.")
        pat["visits"] += 1
        token = doc["queue"].enqueue(is_emg, pid, pat["name"], doc["fee"])
        self.undo_stack.push(("BOOK_APPT", doc["id"], token, pid))
        self._persist()
        print(f"  Booked {token} for {pat['name']} with {doc['name']} | Fee: {doc['fee']}")

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
            hosp.find_patient(int(input("ID: ")))
        elif choice == "4":
            hosp.add_note(int(input("ID: ")), input("Note: "))
        elif choice == "5":
            hosp.doctors_menu(role)
        elif choice == "6":
            hosp.book_appointment(input("Doctor ID: "), int(input("Patient ID: ")), input("Emergency (E) or Normal (N): ").strip().upper() == "E")
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


if __name__ == "__main__":
    main()