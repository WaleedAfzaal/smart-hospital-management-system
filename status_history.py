"""Appointment status history for the Hospital Management System.

Step 6 of the improvement plan: every appointment has a status
(Pending, Confirmed, Checked In, In Progress, Completed, Cancelled,
No Show), and every status change is written down — who changed it,
from what to what, and when — so the full story of an appointment can
be read back later.

History lives in an ``appointment_status_history`` table in the same
SQLite file (``hospital.db``) as everything else, so it survives a
restart — but like the users and bills tables, it is managed only
here; ``Database.save()`` never touches it (it only rewrites the
tables it owns). With no database path the store is purely in-memory
(used by the unit tests).

History is an audit trail, not live state: rows are only ever
appended, never edited or deleted — not even when the patient is
deleted or the appointment is undone. The appointment itself is
identified by its doctor + token (the stable key until a global
appointment ID arrives in Step 8).
"""

import datetime
import os
import sqlite3
import threading

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS appointment_status_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id TEXT NOT NULL,
    token TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
"""


def _now():
    """Local timestamp, second precision — when the change happened."""
    return datetime.datetime.now().isoformat(timespec="seconds")


class StatusHistory:
    """Append-only record of appointment status changes.

    One persistent connection (``check_same_thread=False``) guarded by
    a lock, like AuthStore and Billing — the threaded web server
    shares one store.
    """

    def __init__(self, db_path=None):
        self.path = os.path.abspath(db_path) if db_path else None
        self._lock = threading.Lock()
        self._conn = None
        self._rows = []  # in-memory rows when there is no database file
        if self.path is not None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            with self._lock:
                self._conn.executescript(_HISTORY_SCHEMA)
                self._conn.commit()

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def record(self, doctor_id, token, old_status, new_status, changed_by):
        """Append one change and return the stored entry.

        ``old_status`` is None for the first recorded change of an
        appointment that had no status before (it never happens for
        bookings — new appointments start Pending and only explicit
        updates are recorded).
        """
        entry = {
            "doctor_id": doctor_id,
            "token": token,
            "old_status": old_status,
            "new_status": new_status,
            "changed_by": changed_by,
            "changed_at": _now(),
        }
        with self._lock:
            if self._conn is None:
                stored = dict(entry, id=len(self._rows) + 1)
                self._rows.append(stored)
                return dict(stored)
            cur = self._conn.execute(
                "INSERT INTO appointment_status_history"
                "(doctor_id, token, old_status, new_status, changed_by,"
                " changed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (doctor_id, token, old_status, new_status, changed_by,
                 entry["changed_at"]),
            )
            self._conn.commit()
            entry["id"] = cur.lastrowid
            return dict(entry)

    def history(self, doctor_id, token):
        """All recorded changes for one appointment, oldest first."""
        with self._lock:
            if self._conn is None:
                return [dict(r) for r in self._rows
                        if r["doctor_id"] == doctor_id
                        and r["token"] == token]
            rows = self._conn.execute(
                "SELECT id, doctor_id, token, old_status, new_status,"
                " changed_by, changed_at"
                " FROM appointment_status_history"
                " WHERE doctor_id = ? AND token = ? ORDER BY id",
                (doctor_id, token),
            ).fetchall()
        return [{"id": r[0], "doctor_id": r[1], "token": r[2],
                 "old_status": r[3], "new_status": r[4],
                 "changed_by": r[5], "changed_at": r[6]} for r in rows]
