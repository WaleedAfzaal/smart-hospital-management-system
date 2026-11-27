"""SQLite persistence for the Hospital Management System.

Step 1 of the improvement plan: data used to live only in memory, so every
restart wiped patients, appointments, beds and the pharmacy queue. This
module saves the whole Hospital state into one SQLite file (default
``hospital.db`` next to ``hospital.py``) and loads it back on startup.

Only the Python standard library is used (``sqlite3``) — no new
dependencies. The undo stack is deliberately NOT saved: undo is a
session thing, it starts empty after a restart (this is noted in the
docs). Everything else survives: patients + notes, doctors, appointment
queues (with their token numbering), beds, pharmacy queue, shift rotation.

Design notes
------------
* One short-lived connection per save/load — safe under the server's
  threading lock and avoids stale-connection problems.
* Save rewrites all tables inside a single transaction, so the file is
  never left half-written.
* A ``meta`` row ``initialized=1`` marks a database that already holds
  real state. A fresh/empty file is seeded with the Hospital's starter
  state (5 starter doctors, empty everything else) on first load.
* Appointment heap rows are stored in heap order (``heap_pos``) and put
  back in the same order, so the binary heap keeps working unchanged.
  Each doctor's token counter (``queue_count``) is stored too, so token
  numbers keep counting up after a restart (no duplicate E-1/N-1).
* Shift rotation is stored in duty order (front of the circular queue
  first) and rebuilt by enqueueing in that order.
"""

import os
import sqlite3

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patients(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    age INTEGER NOT NULL,
    problem TEXT NOT NULL,
    visits INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS patient_notes(
    patient_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (patient_id, position)
);
CREATE TABLE IF NOT EXISTS doctors(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    spec TEXT NOT NULL,
    fee INTEGER NOT NULL,
    queue_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS appointments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id TEXT NOT NULL,
    heap_pos INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    token TEXT NOT NULL,
    patient_id INTEGER NOT NULL,
    patient_name TEXT NOT NULL,
    fee INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS beds(
    bed_no INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pharmacy(
    position INTEGER PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shifts(
    position INTEGER PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _notes_of(patient):
    """Walk a patient's notes LinkedList and return the texts in order."""
    out = []
    node = patient["notes"].head
    while node is not None:
        out.append(node.val)
        node = node.next
    return out


def _queue_items(queue):
    """Walk a linked-list Queue and return its values in order."""
    out = []
    node = queue.front
    while node is not None:
        out.append(node.val)
        node = node.next
    return out


def _shifts_in_order(hospital):
    """Shift rotation in duty order (front of the circular queue first)."""
    q = hospital.shift_q
    return [q.items[(q.front + i) % q.size] for i in range(q.total)]


class Database:
    """Saves and loads a Hospital object's state to a SQLite file."""

    def __init__(self, path):
        self.path = os.path.abspath(path)

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        return conn

    # ------------------------------------------------------------ save
    def save(self, hospital):
        """Write the whole Hospital state to the database (one transaction)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM patient_notes")
            conn.execute("DELETE FROM patients")
            conn.execute("DELETE FROM appointments")
            conn.execute("DELETE FROM doctors")
            conn.execute("DELETE FROM beds")
            conn.execute("DELETE FROM pharmacy")
            conn.execute("DELETE FROM shifts")

            for pos, pat in enumerate(hospital.patients_list):
                conn.execute(
                    "INSERT INTO patients(id, name, age, problem, visits, position)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (pat["id"], pat["name"], pat["age"], pat["problem"],
                     pat["visits"], pos),
                )
                for npos, note in enumerate(_notes_of(pat)):
                    conn.execute(
                        "INSERT INTO patient_notes(patient_id, position, note)"
                        " VALUES (?, ?, ?)",
                        (pat["id"], npos, note),
                    )

            for doc in hospital.doctors_map.values():
                conn.execute(
                    "INSERT INTO doctors(id, name, spec, fee, queue_count)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (doc["id"], doc["name"], doc["spec"], doc["fee"],
                     doc["queue"].count),
                )
                for hpos, row in enumerate(doc["queue"].heap):
                    # heap row: (priority, count, token, pid, name, fee)
                    conn.execute(
                        "INSERT INTO appointments(doctor_id, heap_pos, priority,"
                        " seq, token, patient_id, patient_name, fee)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (doc["id"], hpos, row[0], row[1], row[2], row[3],
                         row[4], row[5]),
                    )

            for i, pid in enumerate(hospital.beds):
                conn.execute(
                    "INSERT INTO beds(bed_no, patient_id) VALUES (?, ?)",
                    (i + 1, pid),
                )

            for pos, value in enumerate(_queue_items(hospital.pharmacy_q)):
                conn.execute(
                    "INSERT INTO pharmacy(position, value) VALUES (?, ?)",
                    (pos, value),
                )

            for pos, value in enumerate(_shifts_in_order(hospital)):
                conn.execute(
                    "INSERT INTO shifts(position, value) VALUES (?, ?)",
                    (pos, value),
                )

            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('initialized', '1')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value)"
                " VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    # ------------------------------------------------------------ load
    def load(self, hospital):
        """Rebuild a Hospital object's state from the database.

        If the file is fresh (no ``initialized`` marker), the Hospital
        keeps the starter state it was just built with and that state is
        saved, so the next start loads it back.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'initialized'"
            ).fetchone()
            if not row:
                self.save(hospital)
                return False

            from hospital import CircularQueue, LinkedList, PriorityQueue

            # ---- patients (+ notes), in original insertion order
            hospital.patients_map, hospital.patients_list = {}, []
            for pid, name, age, problem, visits in conn.execute(
                "SELECT id, name, age, problem, visits FROM patients"
                " ORDER BY position"
            ):
                notes = LinkedList()
                for (note,) in conn.execute(
                    "SELECT note FROM patient_notes WHERE patient_id = ?"
                    " ORDER BY position",
                    (pid,),
                ):
                    notes.add_last(note)
                pat = {"id": pid, "name": name, "age": age,
                       "problem": problem, "visits": visits, "notes": notes}
                hospital.patients_map[pid] = pat
                hospital.patients_list.append(pat)

            # ---- doctors + their appointment heaps
            hospital.doctors_map = {}
            for doc_id, name, spec, fee, count in conn.execute(
                "SELECT id, name, spec, fee, queue_count FROM doctors"
            ):
                queue = PriorityQueue()
                queue.count = count
                queue.heap = [
                    (priority, seq, token, pat_id, pat_name, fee_)
                    for priority, seq, token, pat_id, pat_name, fee_ in conn.execute(
                        "SELECT priority, seq, token, patient_id, patient_name, fee"
                        " FROM appointments WHERE doctor_id = ? ORDER BY heap_pos",
                        (doc_id,),
                    )
                ]
                hospital.doctors_map[doc_id] = {
                    "id": doc_id, "name": name, "spec": spec, "fee": fee,
                    "queue": queue,
                }

            # ---- beds (10 slots, 0 = free)
            hospital.beds = [0] * hospital.NUM_BEDS
            for bed_no, pid in conn.execute(
                "SELECT bed_no, patient_id FROM beds"
            ):
                if 1 <= bed_no <= hospital.NUM_BEDS:
                    hospital.beds[bed_no - 1] = pid

            # ---- pharmacy queue (FIFO order)
            for (value,) in conn.execute(
                "SELECT value FROM pharmacy ORDER BY position"
            ):
                hospital.pharmacy_q.enqueue(value)

            # ---- shift rotation (duty order). The fresh Hospital already
            # seeded this queue with the starter doctors in __init__, so
            # reset it first or the shifts would appear twice.
            hospital.shift_q = CircularQueue(20)
            for (value,) in conn.execute(
                "SELECT value FROM shifts ORDER BY position"
            ):
                hospital.shift_q.enqueue(value)

            return True
