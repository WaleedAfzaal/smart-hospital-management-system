"""In-app notifications for the Hospital Management System.

Step 12 of the improvement plan: before this, the dashboard bell only
counted pending appointments and unpaid bills — there was no record of
what actually happened (a booking, a cancellation, a reschedule, a
status change) and no reminder for tomorrow's appointments. Now every
such event writes a notification, and the bell shows the real unread
ones.

Notifications live in a ``notifications`` table in the same SQLite
file (``hospital.db``) as everything else, so they survive a restart —
but like the users, bills, status-history and availability tables,
they are managed only here; ``Database.save()`` never touches them.
With no database path the store is purely in-memory (used by the unit
tests).

What is stored
--------------
* ``type`` — booking, cancellation, reschedule, status, reminder or
  schedule (see NOTIFICATION_TYPES).
* ``title`` / ``message`` — short plain text, e.g. "Appointment
  booked" / "APT-0001 (N-1): Ali Khan with Dr. Tariq on 2026-11-29
  10:30 — General, Pending."
* ``apt_id`` — the appointment it is about (None for hospital-wide
  ones, e.g. a holiday), so the dashboard can open its details page.
* ``doctor_id`` — the doctor it concerns (None = hospital-wide, e.g.
  a holiday). This is what the Doctor role filter uses: a Doctor
  linked to one doctor only sees notifications about their own queue
  plus hospital-wide ones; Admin and Receptionist see everything.
* ``is_read`` — 0 unread, 1 read.
* ``dedupe_key`` — when given, only ONE notification with that key
  ever exists (tomorrow reminders: one per appointment, however often
  they are generated).

Notifications are a record of what happened: they are only ever
added or marked read, never edited or deleted — not even when the
appointment is undone or the patient is deleted.
"""

import datetime
import os
import sqlite3
import threading

NOTIFICATION_TYPES = ("booking", "cancellation", "reschedule",
                      "status", "reminder", "schedule")

TYPE_LABELS = {
    "booking": "Booking",
    "cancellation": "Cancellation",
    "reschedule": "Reschedule",
    "status": "Status",
    "reminder": "Reminder",
    "schedule": "Schedule change",
}

_NOTIFICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    apt_id TEXT,
    doctor_id TEXT,
    created_at TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT
);
"""


def _now():
    """Local timestamp, second precision — when the event happened."""
    return datetime.datetime.now().isoformat(timespec="seconds")


class Notifications:
    """Stored in-app notifications.

    One persistent connection (``check_same_thread=False``) guarded by
    a lock, like AuthStore, Billing, StatusHistory and Availability —
    the threaded web server shares one store.
    """

    def __init__(self, db_path=None):
        self.path = os.path.abspath(db_path) if db_path else None
        self._lock = threading.Lock()
        self._conn = None
        self._rows = []  # in-memory rows when there is no database file
        self._next_id = 1
        if self.path is not None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            with self._lock:
                self._conn.executescript(_NOTIFICATIONS_SCHEMA)
                self._conn.commit()
                self._next_id = self._max_id() + 1

    def _max_id(self):
        row = self._conn.execute(
            "SELECT MAX(id) FROM notifications").fetchone()
        return row[0] or 0 if row else 0

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------ add
    def add(self, ntype, title, message, apt_id=None, doctor_id=None,
            dedupe_key=None):
        """Store one notification and return it.

        ``ntype`` must be one of NOTIFICATION_TYPES. When
        ``dedupe_key`` is given and a notification with that key
        already exists, the existing one is returned and nothing new
        is stored — tomorrow reminders use this so each appointment
        is reminded about exactly once, however often the reminders
        are generated.
        """
        if ntype not in NOTIFICATION_TYPES:
            raise ValueError(
                "Notification type must be one of: "
                + ", ".join(NOTIFICATION_TYPES) + ".")
        title = str(title or "").strip()
        message = str(message or "").strip()
        if not title:
            raise ValueError("Notification title is required.")
        apt_id = str(apt_id).strip() if apt_id else None
        doctor_id = str(doctor_id or "").strip().upper() or None
        dedupe_key = str(dedupe_key).strip() if dedupe_key else None
        with self._lock:
            if dedupe_key is not None:
                existing = self._find_dedupe_locked(dedupe_key)
                if existing is not None:
                    return dict(existing)
            entry = {
                "type": ntype, "title": title, "message": message,
                "apt_id": apt_id, "doctor_id": doctor_id,
                "created_at": _now(), "is_read": 0,
                "dedupe_key": dedupe_key,
            }
            if self._conn is None:
                stored = dict(entry, id=self._next_id)
                self._next_id += 1
                self._rows.append(stored)
                return dict(stored)
            cur = self._conn.execute(
                "INSERT INTO notifications"
                "(type, title, message, apt_id, doctor_id, created_at,"
                " is_read, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ntype, title, message, apt_id, doctor_id,
                 entry["created_at"], 0, dedupe_key),
            )
            self._conn.commit()
            entry["id"] = cur.lastrowid
            return dict(entry)

    def _find_dedupe_locked(self, dedupe_key):
        """The stored notification with this dedupe key, or None.

        Caller must hold the lock.
        """
        if self._conn is None:
            for row in self._rows:
                if row.get("dedupe_key") == dedupe_key:
                    return row
            return None
        row = self._conn.execute(
            "SELECT id, type, title, message, apt_id, doctor_id,"
            " created_at, is_read, dedupe_key FROM notifications"
            " WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row):
        return {"id": row[0], "type": row[1], "title": row[2],
                "message": row[3], "apt_id": row[4],
                "doctor_id": row[5], "created_at": row[6],
                "is_read": row[7], "dedupe_key": row[8]}

    # ------------------------------------------------------------ read
    def _visible(self, rows, doctor_id):
        """Only the rows a user may see.

        ``doctor_id`` None (Admin / Receptionist, or a Doctor with
        no linked doctor) sees everything; a linked Doctor sees
        their own doctor's rows plus hospital-wide ones
        (``doctor_id`` NULL — e.g. a holiday).
        """
        if not doctor_id:
            return list(rows)
        doctor_id = str(doctor_id).strip().upper()
        return [r for r in rows
                if r.get("doctor_id") in (None, doctor_id)]

    def list(self, doctor_id=None, ntype=None, unread_only=False,
             limit=100):
        """Notifications, newest first — all, or only the ones a
        Doctor user may see (see _visible).

        ``ntype`` filters by type; ``unread_only`` keeps only unread
        ones; ``limit`` caps the list (default 100).
        """
        if ntype is not None and ntype not in NOTIFICATION_TYPES:
            raise ValueError(
                "Notification type must be one of: "
                + ", ".join(NOTIFICATION_TYPES) + ".")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError("Limit must be a whole number.")
        limit = min(1000, max(1, limit))
        with self._lock:
            if self._conn is None:
                rows = [dict(r) for r in self._rows]
            else:
                rows = [self._row_to_dict(r) for r in self._conn.execute(
                    "SELECT id, type, title, message, apt_id, doctor_id,"
                    " created_at, is_read, dedupe_key FROM notifications"
                    " ORDER BY id DESC")]
        rows = self._visible(rows, doctor_id)
        if ntype is not None:
            rows = [r for r in rows if r["type"] == ntype]
        if unread_only:
            rows = [r for r in rows if not r["is_read"]]
        return rows[:limit]

    def unread_count(self, doctor_id=None):
        """How many unread notifications this user has."""
        return len(self.list(doctor_id=doctor_id, unread_only=True,
                             limit=1000))

    # ------------------------------------------------------------ read / unread
    def mark_read(self, notification_id, doctor_id=None):
        """Mark one notification read.

        Returns True when it was marked; False when it does not
        exist or is not visible to this user (a Doctor may only mark
        their own queue's notifications).
        """
        try:
            notification_id = int(notification_id)
        except (TypeError, ValueError):
            raise ValueError("Notification ID must be a number.")
        with self._lock:
            if self._conn is None:
                row = next((r for r in self._rows
                            if r["id"] == notification_id), None)
                if row is None:
                    return False
                if not self._visible([row], doctor_id):
                    return False
                row["is_read"] = 1
                return True
            row = self._conn.execute(
                "SELECT doctor_id FROM notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                return False
            if not self._visible([{"doctor_id": row[0]}], doctor_id):
                return False
            self._conn.execute(
                "UPDATE notifications SET is_read = 1 WHERE id = ?",
                (notification_id,),
            )
            self._conn.commit()
            return True

    def mark_all_read(self, doctor_id=None):
        """Mark every visible notification read. Returns how many
        were marked."""
        with self._lock:
            if self._conn is None:
                visible = self._visible(self._rows, doctor_id)
                count = sum(1 for r in visible if not r["is_read"])
                for r in visible:
                    r["is_read"] = 1
                return count
            rows = self._conn.execute(
                "SELECT id, doctor_id FROM notifications"
                " WHERE is_read = 0").fetchall()
            visible_ids = [r[0] for r in rows
                           if self._visible([{"doctor_id": r[1]}],
                                            doctor_id)]
            if visible_ids:
                self._conn.executemany(
                    "UPDATE notifications SET is_read = 1 WHERE id = ?",
                    [(i,) for i in visible_ids])
                self._conn.commit()
            return len(visible_ids)

    def has_dedupe(self, dedupe_key):
        """True when a notification with this dedupe key exists."""
        with self._lock:
            return self._find_dedupe_locked(dedupe_key) is not None

    # ------------------------------------------------------------ reminders
    def generate_tomorrow_reminders(self, hospital, today=None):
        """One reminder per appointment scheduled for TOMORROW.

        Only appointments still Pending or Confirmed are reminded
        about (a Cancelled / No Show / Completed one needs no
        reminder). The dedupe key is the appointment's APT ID (or
        doctor + token for rows booked before Step 8), so calling
        this twice — or on every dashboard load — still creates each
        reminder exactly once. Returns how many NEW reminders were
        created. ``hospital`` is the Hospital to read appointments
        from (duck-typed: it only needs ``all_appointments()``).
        """
        if today is None:
            today = datetime.date.today()
        elif isinstance(today, str):
            today = datetime.date.fromisoformat(today)
        tomorrow = (today + datetime.timedelta(days=1)).isoformat()
        created = 0
        for appt in hospital.all_appointments():
            if appt.get("appt_date") != tomorrow:
                continue
            if appt.get("status") not in ("Pending", "Confirmed"):
                continue
            ref = appt.get("apt_id") or \
                f"{appt['doctor_id']}:{appt['token']}"
            key = f"reminder:{ref}"
            if self.has_dedupe(key):
                continue
            when = tomorrow + (f" {appt['appt_time']}"
                              if appt.get("appt_time") else "")
            self.add(
                "reminder", "Appointment tomorrow",
                f"Reminder: {appt['patient_name']} with "
                f"{appt['doctor_name']} tomorrow {when} "
                f"({ref}, {appt['status']}).",
                apt_id=appt.get("apt_id"), doctor_id=appt["doctor_id"],
                dedupe_key=key)
            created += 1
        return created

    def count(self):
        """Total stored notifications (tests only)."""
        with self._lock:
            if self._conn is None:
                return len(self._rows)
            return self._conn.execute(
                "SELECT COUNT(*) FROM notifications").fetchone()[0]
