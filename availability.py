"""Doctor availability, leave and holidays for the Hospital Management
System.

Step 11 of the improvement plan: before this, a booking only checked
the date/time format and whether the exact minute was already taken —
there was no idea of when a doctor actually works. Now each doctor
has a working pattern (working days, daily hours, breaks, consultation
duration), plus per-doctor leave and hospital-wide holidays, and
bookings are refused outside them.

Availability lives in three tables in the same SQLite file
(``hospital.db``) as everything else, so it survives a restart — but
like the users, bills and status-history tables, it is managed only
here; ``Database.save()`` never touches it (it only rewrites the
tables it owns). With no database path the store is purely in-memory
(used by the unit tests).

What is stored
--------------
* ``doctor_availability`` — one row per doctor: working days (e.g.
  "Mon,Tue,Wed,Thu,Fri"), working hours (start/end, HH:MM 24-hour),
  consultation duration in minutes, and breaks as a JSON list of
  [start, end] pairs. A doctor with NO row has no availability set:
  that is not an error — it means "any time is allowed" and the
  booking form says so plainly (Step 8 behaviour, unchanged).
* ``doctor_leave`` — one row per leave: doctor, start date, end date
  (both inclusive), optional reason.
* ``holidays`` — one row per hospital holiday: date + name. A holiday
  closes the whole hospital — no doctor can be booked that day.
"""

import datetime
import json
import os
import re
import sqlite3
import threading

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_NAMES = {
    "Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday",
    "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday",
    "Sun": "Sunday",
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_AVAILABILITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS doctor_availability(
    doctor_id TEXT PRIMARY KEY,
    working_days TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    breaks TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doctor_leave(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_id TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS holidays(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);
"""


def validate_time(value):
    """Check an HH:MM 24-hour time, or raise ValueError."""
    value = str(value or "").strip()
    if not _TIME_RE.match(value):
        raise ValueError(
            "Time must look like HH:MM in 24-hour format (e.g. 09:00).")
    return value


def time_to_minutes(value):
    """"09:30" -> 570."""
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def minutes_to_time(minutes):
    """570 -> "09:30"."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def validate_date(value):
    """Check a YYYY-MM-DD date, or raise ValueError."""
    try:
        return datetime.date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise ValueError(
            "Date must look like YYYY-MM-DD (e.g. 2026-11-28).")


def weekday_name(date_str):
    """'Mon' .. 'Sun' for a YYYY-MM-DD date."""
    return WEEKDAYS[datetime.date.fromisoformat(date_str).weekday()]


class Availability:
    """Working patterns, leave and holidays.

    One persistent connection (``check_same_thread=False``) guarded by
    a lock, like AuthStore, Billing and StatusHistory — the threaded
    web server shares one store.
    """

    def __init__(self, db_path=None):
        self.path = os.path.abspath(db_path) if db_path else None
        self._lock = threading.Lock()
        self._conn = None
        self._avail = {}   # doctor_id -> record (in-memory when no db)
        self._leaves = []  # list of leave dicts
        self._holidays = []  # list of holiday dicts
        self._next_leave_id = 1
        self._next_holiday_id = 1
        if self.path is not None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            with self._lock:
                self._conn.executescript(_AVAILABILITY_SCHEMA)
                self._conn.commit()
                self._load()

    def close(self):
        if self._conn is not None:
            with self._lock:
                self._conn.close()

    def _load(self):
        """Read the saved rows back (database mode only)."""
        for doc_id, days, start, end, duration, breaks in self._conn.execute(
            "SELECT doctor_id, working_days, start_time, end_time,"
            " duration_minutes, breaks FROM doctor_availability"
        ):
            self._avail[doc_id] = {
                "doctor_id": doc_id,
                "working_days": days.split(",") if days else [],
                "start_time": start, "end_time": end,
                "duration_minutes": duration,
                "breaks": json.loads(breaks) if breaks else [],
            }
        for lid, doc_id, start, end, reason in self._conn.execute(
            "SELECT id, doctor_id, start_date, end_date, reason"
            " FROM doctor_leave ORDER BY start_date"
        ):
            self._leaves.append({"id": lid, "doctor_id": doc_id,
                                 "start_date": start, "end_date": end,
                                 "reason": reason or ""})
            self._next_leave_id = max(self._next_leave_id, lid + 1)
        for hid, date, name in self._conn.execute(
            "SELECT id, date, name FROM holidays ORDER BY date"
        ):
            self._holidays.append({"id": hid, "date": date, "name": name})
            self._next_holiday_id = max(self._next_holiday_id, hid + 1)

    # ------------------------------------------------------------ working pattern
    @staticmethod
    def _validate_schedule(working_days, start_time, end_time,
                           duration_minutes, breaks):
        """Check a working pattern and return it cleaned up.

        Raises ValueError with a plain message on: no working days (or
        a day that is not Mon..Sun), hours that do not start before
        they end, a duration outside 5–120 minutes, a break outside
        the working hours, or breaks that overlap each other. Breaks
        may be given as "13:00-14:00" strings or [start, end] pairs.
        """
        days = [str(d).strip().title() for d in (working_days or [])]
        if not days or any(d not in WEEKDAYS for d in days):
            raise ValueError(
                "Working days must be Mon, Tue, Wed, Thu, Fri, Sat or Sun "
                "(e.g. Mon,Tue,Wed,Thu,Fri).")
        if len(set(days)) != len(days):
            raise ValueError("A working day must not be listed twice.")
        start = validate_time(start_time)
        end = validate_time(end_time)
        if time_to_minutes(start) >= time_to_minutes(end):
            raise ValueError(
                "Working hours must start before they end (e.g. 09:00–17:00).")
        if isinstance(duration_minutes, bool) \
                or not isinstance(duration_minutes, int) \
                or not 5 <= duration_minutes <= 120:
            raise ValueError(
                "Consultation duration must be a whole number of minutes "
                "between 5 and 120.")
        parsed = []
        for br in (breaks or []):
            if isinstance(br, (list, tuple)) and len(br) == 2:
                bs, be = validate_time(br[0]), validate_time(br[1])
            elif isinstance(br, str) and "-" in br:
                bs, be = (validate_time(p)
                          for p in br.split("-", 1))
            else:
                raise ValueError(
                    "Each break must look like HH:MM-HH:MM "
                    "(e.g. 13:00-14:00).")
            if time_to_minutes(bs) >= time_to_minutes(be):
                raise ValueError("A break must start before it ends.")
            if time_to_minutes(bs) < time_to_minutes(start) \
                    or time_to_minutes(be) > time_to_minutes(end):
                raise ValueError(
                    "Breaks must be inside the working hours.")
            parsed.append([bs, be])
        parsed.sort()
        for i in range(1, len(parsed)):
            if time_to_minutes(parsed[i][0]) \
                    < time_to_minutes(parsed[i - 1][1]):
                raise ValueError("Breaks must not overlap each other.")
        return days, start, end, duration_minutes, parsed

    def set(self, doctor_id, working_days, start_time, end_time,
            duration_minutes, breaks=None):
        """Save a doctor's working pattern (replaces any old one).

        Returns the saved record. Raises ValueError on a bad pattern
        (see _validate_schedule).
        """
        doctor_id = str(doctor_id or "").strip().upper()
        if not doctor_id:
            raise ValueError("Doctor ID is required.")
        days, start, end, duration, parsed = self._validate_schedule(
            working_days, start_time, end_time, duration_minutes, breaks)
        record = {"doctor_id": doctor_id, "working_days": days,
                  "start_time": start, "end_time": end,
                  "duration_minutes": duration, "breaks": parsed}
        with self._lock:
            self._avail[doctor_id] = record
            if self._conn is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO doctor_availability"
                    "(doctor_id, working_days, start_time, end_time,"
                    " duration_minutes, breaks)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (doctor_id, ",".join(days), start, end, duration,
                     json.dumps(parsed)),
                )
                self._conn.commit()
        return self.get(doctor_id)

    def get(self, doctor_id):
        """One doctor's working pattern, or None when not set yet."""
        doctor_id = str(doctor_id or "").strip().upper()
        rec = self._avail.get(doctor_id)
        if rec is None:
            return None
        return {"doctor_id": rec["doctor_id"],
                "working_days": list(rec["working_days"]),
                "start_time": rec["start_time"],
                "end_time": rec["end_time"],
                "duration_minutes": rec["duration_minutes"],
                "breaks": [list(b) for b in rec["breaks"]]}

    def clear(self, doctor_id):
        """Remove a doctor's working pattern (back to "not set").

        Returns True when a pattern was actually removed.
        """
        doctor_id = str(doctor_id or "").strip().upper()
        with self._lock:
            existed = doctor_id in self._avail
            self._avail.pop(doctor_id, None)
            if self._conn is not None:
                self._conn.execute(
                    "DELETE FROM doctor_availability WHERE doctor_id = ?",
                    (doctor_id,),
                )
                self._conn.commit()
        return existed

    # ------------------------------------------------------------ leave
    def add_leave(self, doctor_id, start_date, end_date, reason=""):
        """Record leave for a doctor (both dates inclusive).

        Returns the saved leave row (with its id). Raises ValueError
        on a bad date or when the leave ends before it starts.
        """
        doctor_id = str(doctor_id or "").strip().upper()
        if not doctor_id:
            raise ValueError("Doctor ID is required.")
        start = validate_date(start_date)
        end = validate_date(end_date)
        if start > end:
            raise ValueError("Leave must start on or before it ends.")
        reason = str(reason or "").strip()
        with self._lock:
            if self._conn is not None:
                cur = self._conn.execute(
                    "INSERT INTO doctor_leave"
                    "(doctor_id, start_date, end_date, reason)"
                    " VALUES (?, ?, ?, ?)",
                    (doctor_id, start, end, reason),
                )
                self._conn.commit()
                lid = cur.lastrowid
            else:
                lid = self._next_leave_id
                self._next_leave_id += 1
            row = {"id": lid, "doctor_id": doctor_id,
                   "start_date": start, "end_date": end, "reason": reason}
            self._leaves.append(row)
        return dict(row)

    def leaves(self, doctor_id=None):
        """Leave rows, oldest first — all doctors, or one doctor."""
        rows = self._leaves
        if doctor_id:
            doctor_id = str(doctor_id).strip().upper()
            rows = [r for r in rows if r["doctor_id"] == doctor_id]
        return [dict(r) for r in sorted(
            rows, key=lambda r: (r["start_date"], r["doctor_id"]))]

    def delete_leave(self, leave_id):
        """Delete one leave row. Returns False when it does not exist."""
        try:
            leave_id = int(leave_id)
        except (TypeError, ValueError):
            raise ValueError("Leave ID must be a number.")
        with self._lock:
            if not any(r["id"] == leave_id for r in self._leaves):
                return False
            self._leaves = [r for r in self._leaves
                            if r["id"] != leave_id]
            if self._conn is not None:
                self._conn.execute(
                    "DELETE FROM doctor_leave WHERE id = ?", (leave_id,))
                self._conn.commit()
        return True

    def is_on_leave(self, doctor_id, date):
        """True when the doctor has leave covering ``date``."""
        doctor_id = str(doctor_id or "").strip().upper()
        date = validate_date(date)
        return any(r["doctor_id"] == doctor_id
                   and r["start_date"] <= date <= r["end_date"]
                   for r in self._leaves)

    # ------------------------------------------------------------ holidays
    def add_holiday(self, date, name):
        """Add a hospital holiday. Returns the saved row (with its id).

        Raises ValueError on a bad date, an empty name, or a date
        that is already a holiday.
        """
        date = validate_date(date)
        name = str(name or "").strip()
        if not name:
            raise ValueError("Holiday name is required.")
        with self._lock:
            if any(h["date"] == date for h in self._holidays):
                raise ValueError(f"{date} is already a holiday.")
            if self._conn is not None:
                cur = self._conn.execute(
                    "INSERT INTO holidays(date, name) VALUES (?, ?)",
                    (date, name),
                )
                self._conn.commit()
                hid = cur.lastrowid
            else:
                hid = self._next_holiday_id
                self._next_holiday_id += 1
            row = {"id": hid, "date": date, "name": name}
            self._holidays.append(row)
        return dict(row)

    def holidays(self):
        """Every holiday, earliest first."""
        return [dict(h) for h in sorted(self._holidays,
                                        key=lambda h: h["date"])]

    def delete_holiday(self, holiday_id):
        """Delete one holiday. Returns False when it does not exist."""
        try:
            holiday_id = int(holiday_id)
        except (TypeError, ValueError):
            raise ValueError("Holiday ID must be a number.")
        with self._lock:
            if not any(h["id"] == holiday_id for h in self._holidays):
                return False
            self._holidays = [h for h in self._holidays
                              if h["id"] != holiday_id]
            if self._conn is not None:
                self._conn.execute(
                    "DELETE FROM holidays WHERE id = ?", (holiday_id,))
                self._conn.commit()
        return True

    def holiday_on(self, date):
        """The holiday name on ``date``, or None when it is a normal day."""
        date = validate_date(date)
        for h in self._holidays:
            if h["date"] == date:
                return h["name"]
        return None
