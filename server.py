"""Hospital Management System — web server.

Serves the single-page dashboard in web/ and a JSON API backed by the
Hospital class from hospital.py (Ali's file — kept as-is, no changes).

Hospital's methods print instead of returning, so success is determined
from state before/after each call (e.g. pid in patients_map), and the
booking token is computed from the doctor's queue count after enqueue.

Step 1 (SQLite): state is saved to hospital.db on every change and loaded
on startup, so restarting the server no longer resets everything. The
undo stack is session-only and starts empty after a restart.

Step 2 (Login + roles): every API call needs a login (cookie session).
Users live in the same hospital.db file (``users`` table, passwords
hashed — see auth.py). Roles: Admin (everything), Doctor (patients +
own appointments + notes), Receptionist (patients, bookings, beds,
pharmacy). The dashboard page itself ("/" and "/index.html") shows
the login page until you log in.

Step 3 (Billing + Reports): bills are created from booked appointments
(consultation fee first, then extra charges), and the daily report
counts patients/appointments/money for a date. Admin and Receptionist
make and change bills; Doctors can view bills and reports (read-only).
Bills export as CSV from ``GET /api/bills/export``.

Step 4 (Appointment Date & Time): booking takes a date (YYYY-MM-DD,
empty = today) and a time (HH:MM 24-hour, empty = no fixed time) —
both validated, bad values get a 400 with a plain message. Queues show
the scheduled date/time, and ``GET /api/appointments?date=...`` lists
that day's appointments (a Doctor sees only their own doctor's).

Step 5 (Patient Search / Edit / Delete): ``GET /api/patients?name=...``
finds patients by part of their name (case-insensitive, empty list
when nothing matches); ``POST /api/patients/<id>`` edits name/age/
problem (Admin + Receptionist); ``DELETE /api/patients/<id>`` deletes
a patient — Admin only — removing their record, notes and queued
appointments and freeing their bed, while their bills are kept.

Step 6 (Appointment Statuses + History): every appointment carries a
status (Pending by default). ``POST /api/appointments/<doctor>/<token>/status``
changes it (Admin/Receptionist: any status; Doctor: only their own
queue, only Checked In / In Progress / Completed — otherwise 403);
``GET /api/appointments/<doctor>/<token>/history`` reads the recorded
changes (old -> new, who, when). Booking refuses a double booking —
the same doctor, same date, same time, while the existing appointment
is still active — with a 400 that says so plainly. Queue items and
schedule rows now include each appointment's status.

Step 7 (New Dashboard): ``GET /api/dashboard`` now returns the full
dashboard payload — counts (patients, doctors, today's appointments,
free beds, total revenue from Paid bills, pending bills), the monthly
appointment series (this year + rolling last 12 months), the
department split (appointments per doctor specialization), today's
appointments with real statuses, the recent-activity feed (status
changes, registrations, bills, bookings) and the notification count
(pending appointments + unpaid bills). Every number is live data from
hospital.db — nothing is hardcoded.

Step 8 (Book Appointment form): patients carry Father Name, Phone and
Gender (``POST /api/patients`` and the edit endpoint accept them;
``GET /api/patients?name=...`` now matches by ID, name or phone).
Booking takes a Type (General / Follow-up / Emergency — Emergency
books an E- token), a Reason, Notes and a Status (Pending or
Confirmed), and accepts either ``patient_id`` or a ``new_patient``
object to register inline. Every booking gets a unique Appointment
ID (APT-0001, ...) returned with the appointment, and
``GET /api/appointments/slots?doctor_id=...&date=...`` lists that
doctor's booked times for the day.

Step 9 (Appointments table + details page): ``GET
/api/appointments/list`` lists every appointment with server-side
search (patient name, APT ID, token), filters (date, doctor,
department, status), sorting and pagination; ``GET
/api/appointments/by-id/<apt_id>`` returns one appointment with its
patient, doctor, payment status and status history (rows booked
before Step 8 have no APT ID — "D1:N-1", doctor + token, works for
those); ``POST /api/appointments/by-id/<apt_id>/edit`` changes
date/time/type/reason/notes (Admin/Receptionist only — a reschedule
re-runs the double-booking guard, and a type change that flips the
priority changes it in place and re-orders the queue, keeping the
token, the APT ID and the history); the table
exports as CSV (``GET /api/appointments/export.csv``) and as a PDF
(``GET /api/appointments/export.pdf``, pure-Python generator, no
dependencies) — both respect the active filters. A Doctor user linked
to one doctor only ever sees that doctor's appointments.

Step 10 (Appointment Calendar): ``GET /api/appointments/calendar``
lists every appointment in an inclusive date range (defaults: this
month), with doctor/department filters; a Doctor user linked to one
doctor only ever sees that doctor's appointments.

Step 11 (Doctor Availability & Scheduling): ``GET /api/availability``
reads a doctor's working pattern + leave; ``POST /api/availability``
saves it (Admin only) and ``POST /api/availability/clear`` removes
it; ``POST /api/leaves`` adds leave (Admin any doctor, Doctor only
themself) and ``POST /api/leaves/<id>/delete`` removes it; ``GET
/api/holidays`` lists hospital holidays and ``POST /api/holidays``
adds one (Admin only) with ``POST /api/holidays/<id>/delete`` to
remove it. ``GET /api/appointments/slots`` now also returns the
doctor's ``availability`` and the day's ``slots`` (each marked
available or booked) plus an ``unavailable_reason`` when there are no
slots (holiday, leave, not a working day). Booking refuses a slot
outside the doctor's availability with a 400 that says why — checked
before a new patient is registered, so a refused booking never
leaves a stray patient behind.
"""

import argparse
import datetime
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from auth import AuthStore, can
from hospital import (APPOINTMENT_TYPES, BOOKING_STATUSES, DEFAULT_DB_PATH,
                      Hospital, PriorityQueue, binary_search, bubble_sort,
                      validate_appt_datetime, validate_gender,
                      validate_patient_details, validate_phone)
from pdf_export import appointments_pdf

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
HOST = "127.0.0.1"
PORT = 8000

# Step 1 (SQLite): the server shares the same hospital.db file as the
# CLI, so data added in one shows up in the other and survives restarts.
hospital = Hospital(db_path=DEFAULT_DB_PATH)
state_lock = threading.Lock()

# Step 2 (Login): users live in the same hospital.db file. Sessions are
# random tokens kept in memory (server restart logs everyone out) and
# sent as an HttpOnly cookie. No expiry besides that — this is a
# university project, not production.
auth_store = AuthStore(DEFAULT_DB_PATH)
SESSION_COOKIE = "hms_session"
SESSION_MAX_AGE = 12 * 3600  # 12 hours
_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def _new_session(user: Dict[str, Any]) -> str:
    """Create a login session token for ``user`` and remember it."""
    token = secrets.token_hex(32)
    with _sessions_lock:
        _sessions[token] = {
            "username": user["username"],
            "role": user["role"],
            "doctor_id": user.get("doctor_id"),
            "created": time.time(),
        }
    return token


def _drop_session(token: Optional[str]) -> None:
    if token:
        with _sessions_lock:
            _sessions.pop(token, None)


def _session_token(handler) -> Optional[str]:
    """Read the session token from Authorization header, query string, or Cookie."""
    # 1. Bearer header
    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        val = auth_header[7:].strip()
        if val:
            return val

    # 2. Query parameter (?token=...)
    parsed = urllib.parse.urlparse(getattr(handler, "path", ""))
    query = urllib.parse.parse_qs(parsed.query)
    if "token" in query and query["token"]:
        val = query["token"][0].strip()
        if val:
            return val

    # 3. Cookie header
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith(SESSION_COOKIE + "="):
            return part[len(SESSION_COOKIE) + 1:]
    return None


def _session_user(handler) -> Optional[Dict[str, Any]]:
    """The logged-in user for this request, or None if not logged in.

    Returns only the user fields (username, role, doctor_id) — the
    session's internal ``created`` timestamp is never exposed."""
    token = _session_token(handler)
    if not token:
        return None
    with _sessions_lock:
        session = _sessions.get(token)
        if session is None:
            return None
        if time.time() - session["created"] > SESSION_MAX_AGE:
            _sessions.pop(token, None)
            return None
        return {"username": session["username"],
                "role": session["role"],
                "doctor_id": session["doctor_id"]}


# ---------------------------------------------------------------- helpers

def _notes_of(patient: Dict[str, Any]) -> List[str]:
    """Walk a patient's notes LinkedList and return the texts."""
    out: List[str] = []
    node = patient["notes"].head
    while node is not None:
        out.append(node.val)
        node = node.next
    return out


def _queue_items(queue) -> List[str]:
    """Walk a linked-list Queue and return its values in order."""
    out: List[str] = []
    node = queue.front
    while node is not None:
        out.append(node.val)
        node = node.next
    return out


def _serialize_patient(patient: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": patient["id"],
        "name": patient["name"],
        "age": patient["age"],
        "problem": patient["problem"],
        "visits": patient["visits"],
        "notes": _notes_of(patient),
        # Step 8: Father Name, Phone and Gender ("" when not given).
        "father_name": patient.get("father_name") or "",
        "phone": patient.get("phone") or "",
        "gender": patient.get("gender") or "",
    }


def _serialize_doctor(doc: Dict[str, Any]) -> Dict[str, Any]:
    # heap rows: (priority, count, token, pid, name, fee, booked date,
    # appt_date, appt_time, status). Sort a copy so the displayed queue
    # is in real service order (Emergency first).
    rows = sorted(doc["queue"].heap, key=lambda r: (r[0], r[1]))
    return {
        "id": doc["id"],
        "name": doc["name"],
        "spec": doc["spec"],
        "fee": doc["fee"],
        "waiting": len(doc["queue"].heap),
        "queue": [
            {
                "token": row[2],
                "patient_id": row[3],
                "patient_name": row[4],
                "fee": row[5],
                "emergency": row[0] == 1,
                "booked_date": row[6] if len(row) > 6 else None,
                "appt_date": row[7] if len(row) > 7 else None,
                "appt_time": row[8] if len(row) > 8 else None,
                # Step 6: every appointment carries its status
                # ("Pending" for rows built before Step 6).
                "status": row[9] if len(row) > 9 and row[9] else "Pending",
                # Step 8: Appointment ID, type, reason, notes
                # (None/"" for rows booked before Step 8).
                "apt_id": row[10] if len(row) > 10 else None,
                "appt_type": row[11] if len(row) > 11 and row[11] else "General",
                "reason": row[12] if len(row) > 12 and row[12] else "",
                "notes": row[13] if len(row) > 13 and row[13] else "",
            }
            for row in rows
        ],
    }


def _shifts() -> List[str]:
    q = hospital.shift_q
    return [q.items[(q.front + i) % q.size] for i in range(q.total)]


def _dashboard() -> Dict[str, Any]:
    """Live stats for the dashboard. The small counts are the same
    keys the old stats strip used (kept so /api/dashboard stays
    compatible); the Step 7 payload — monthly chart series,
    department split, today's list, activity feed, notification
    count — comes from Hospital.dashboard_data() and is all real
    data, never sample numbers."""
    free_beds = sum(1 for b in hospital.beds if b == 0)
    waiting = sum(len(doc["queue"].heap) for doc in hospital.doctors_map.values())
    # Step 3: today's money at a glance (bills dated today).
    today = datetime.date.today().isoformat()
    money = hospital.billing.summary_for_date(today)
    out = {
        "patients": len(hospital.patients_map),
        "doctors": len(hospital.doctors_map),
        "waiting": waiting,
        "free_beds": free_beds,
        "total_beds": hospital.NUM_BEDS,
        "billed_today": money["billed"],
        "collected_today": money["collected"],
        "unpaid_total": hospital.billing.unpaid_total(),
    }
    # Step 7: full dashboard payload (see Hospital.dashboard_data).
    out.update(hospital.dashboard_data(today))
    return out


def _undo_last() -> Optional[List[Any]]:
    top = hospital.undo_stack.top
    return list(top.val) if top is not None else None


def _state_payload(doctor_id: Optional[str] = None) -> Dict[str, Any]:
    """Everything the UI needs. When ``doctor_id`` is given (a Doctor
    user linked to one doctor), only that doctor's queue is included —
    a Doctor sees their own appointments, not everyone else's. Stats
    always cover the whole hospital."""
    doctors = hospital.doctors_map.values()
    if doctor_id is not None:
        doctors = [d for d in doctors if d["id"] == doctor_id]
    return {
        "patients": [_serialize_patient(p) for p in hospital.patients_list],
        "doctors": [_serialize_doctor(d) for d in doctors],
        "beds": [
            {"bed": i + 1, "patient_id": pid if pid != 0 else None,
             "status": "Free" if pid == 0 else "Occupied"}
            for i, pid in enumerate(hospital.beds)
        ],
        "pharmacy": _queue_items(hospital.pharmacy_q),
        "shifts": _shifts(),
        "on_duty": _shifts()[0] if hospital.shift_q.total else None,
        "undo_last": _undo_last(),
        "stats": _dashboard(),
    }


def _parse_int(value: Any) -> Optional[int]:
    """Parse an integer while rejecting booleans (since bool is a subclass of int)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _appt_list_params(query: Dict[str, List[str]]) -> Dict[str, Any]:
    """Filter/sort/page params shared by the Step 9 list and export
    routes — only the keys the caller actually sent."""
    params: Dict[str, Any] = {}
    for key in ("search", "date", "doctor_id", "department", "status",
                "sort", "order", "page", "per_page"):
        value = query.get(key, [None])[0]
        if value not in (None, ""):
            params[key] = value
    return params


def _filters_note(params: Dict[str, Any], total: int) -> str:
    """One line for the PDF subtitle: which filters were applied and
    how many appointments matched, plus today's date."""
    bits = [f"{key}={params[key]}" for key in
            ("search", "date", "doctor_id", "department", "status")
            if params.get(key)]
    note = "All appointments" if not bits else "Filters: " + ", ".join(bits)
    return (f"{note}  •  {total} appointment(s)  •  "
            f"{datetime.date.today().isoformat()}")


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR allows two processes to bind the same port at once,
    # causing requests to hit a stale background server. Disable it on Windows.
    allow_reuse_address = os.name != "nt"


# ---------------------------------------------------------------- handler

class HospitalRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def end_headers(self) -> None:
        # Prevent caching of HTML pages and API routes so login transitions smoothly
        path = getattr(self, "path", "")
        if path in ("/", "/index.html", "/login.html") or path.endswith(".html") or path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"  [HTTP] {self.command} {self.path} -> "
              f"{args[1] if len(args) > 1 else ''}", flush=True)

    def _send_json(self, payload: Dict[str, Any], status: int = 200,
                   extra_headers: Optional[List[tuple]] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str,
                   filename: Optional[str] = None) -> None:
        """Plain-text response (used for the CSV export)."""
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str,
                    filename: Optional[str] = None) -> None:
        """Binary download response (used for the PDF export)."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _forbidden(self, role: str) -> None:
        """403 answer when a logged-in role tries something it may not do."""
        self._send_json(
            {"ok": False, "error": f"Not allowed for your role ({role})."}, 403)

    def _doctor_filter(self) -> Optional[str]:
        """Doctor id to filter queues by, or None (Admin/Receptionist,
        or a Doctor with no linked doctor record)."""
        user = getattr(self, "_user", None) or {}
        if user.get("role") == "Doctor":
            return user.get("doctor_id")
        return None

    def _read_json_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if path.startswith("/api/"):
            # Every API call needs a login — no session, no data.
            user = _session_user(self)
            if user is None:
                return self._send_json(
                    {"ok": False, "error": "Please log in first."}, 401)
            self._user = user
            return self._api_get(path, query)

        # Static pages: the dashboard itself needs a login — without
        # one, serve the login page instead. (CSS/JS/favicon stay
        # public so the login page can load.)
        if path == "/login.html":
            user = _session_user(self)
            if user is not None:
                token = _session_token(self)
                target = f"/index.html?token={token}" if token else "/index.html"
                self.send_response(303)
                self.send_header("Location", target)
                self.end_headers()
                return

        if path in ("/", "/index.html"):
            if _session_user(self) is None:
                self.path = "/login.html"
            else:
                self.path = "/index.html"
        return super().do_GET()

    def _api_get(self, path: str, query: Dict[str, List[str]]) -> None:
        """Authenticated GET /api/* routes (called only after login)."""
        with state_lock:
            if path == "/api/me":
                return self._send_json({"ok": True, "user": self._user})

            if path == "/api/state":
                return self._send_json(
                    {"ok": True,
                     "state": _state_payload(self._doctor_filter())})

            if path == "/api/dashboard":
                return self._send_json({"ok": True, "stats": _dashboard()})

            if path == "/api/patients":
                field = query.get("sort", ["id"])[0]
                field = "age" if field == "age" else "id"
                # Step 5: ?name= finds patients by part of their name
                # (case-insensitive) — an empty result is a plain empty
                # list, not an error. Step 8: the same search also
                # matches an exact patient ID and a phone number
                # ("0300" finds a patient whose phone is 0300-...).
                name = query.get("name", [None])[0]
                if name is not None:
                    matches = hospital.search_patients(name)
                    return self._send_json({
                        "ok": True, "name": name,
                        "patients": [_serialize_patient(
                            hospital.patients_map[p["id"]])
                            for p in matches],
                    })
                ordered = bubble_sort(hospital.patients_list, field)
                return self._send_json({
                    "ok": True, "sort": field,
                    "patients": [_serialize_patient(p) for p in ordered],
                })

            if path.startswith("/api/patients/"):
                raw = path.removeprefix("/api/patients/").strip("/")
                try:
                    pid = int(raw)
                except ValueError:
                    return self._send_json(
                        {"ok": False, "error": "Patient ID must be a number."}, 400)
                found = binary_search(
                    bubble_sort(hospital.patients_list, "id"), pid)
                if not found:
                    return self._send_json(
                        {"ok": False, "error": "Patient not found."}, 404)
                return self._send_json(
                    {"ok": True, "patient": _serialize_patient(found)})

            if path == "/api/doctors":
                docs = hospital.doctors_map.values()
                doctor_id = self._doctor_filter()
                if doctor_id is not None:
                    docs = [d for d in docs if d["id"] == doctor_id]
                return self._send_json({
                    "ok": True,
                    "doctors": [_serialize_doctor(d) for d in docs],
                })

            # ---- Step 4: appointments scheduled for a date (today by
            # default). A Doctor user linked to one doctor only ever
            # sees that doctor's appointments, whatever they ask for.
            if path == "/api/appointments":
                date = query.get("date", [None])[0] or \
                    datetime.date.today().isoformat()
                try:
                    date, _ = validate_appt_datetime(date, None)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                doctor_id = self._doctor_filter()
                if doctor_id is None:
                    doctor_id = query.get("doctor_id", [None])[0]
                if doctor_id is not None:
                    doctor_id = doctor_id.strip().upper()
                    if doctor_id not in hospital.doctors_map:
                        return self._send_json(
                            {"ok": False, "error": "Doctor not found."}, 404)
                return self._send_json({
                    "ok": True, "date": date,
                    "appointments": hospital.appointments_for_date(
                        date, doctor_id),
                })

            # ---- Step 8: booked times for one doctor on one day —
            # the availability the booking form shows, so taken slots
            # are visible before a time is picked. A Doctor user
            # linked to one doctor only ever sees their own day.
            # Step 11: the reply also carries the doctor's working
            # pattern ("availability", None when not set yet) and the
            # day's bookable "slots" (each marked available or
            # booked), plus an "unavailable_reason" when there are no
            # slots (holiday, leave, not a working day).
            if path == "/api/appointments/slots":
                doctor_id = query.get("doctor_id", [None])[0]
                date = query.get("date", [None])[0] or \
                    datetime.date.today().isoformat()
                doctor_filter = self._doctor_filter()
                if doctor_filter is not None:
                    doctor_id = doctor_filter
                if not doctor_id:
                    return self._send_json(
                        {"ok": False,
                         "error": "doctor_id is required."}, 400)
                doctor_id = doctor_id.strip().upper()
                if doctor_id not in hospital.doctors_map:
                    return self._send_json(
                        {"ok": False, "error": "Doctor not found."}, 404)
                try:
                    slots = hospital.availability_slots(doctor_id, date)
                    date, _ = validate_appt_datetime(date, None)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True, "doctor_id": doctor_id, "date": date,
                    "booked": slots["booked"],
                    "availability": slots["availability"],
                    "slots": slots["slots"],
                    "unavailable_reason": slots["unavailable_reason"],
                })

            # ---- Step 11: a doctor's availability + leave. A Doctor
            # user linked to one doctor only ever sees their own.
            if path == "/api/availability":
                doctor_id = self._doctor_filter()
                if doctor_id is None:
                    doctor_id = query.get("doctor_id", [None])[0]
                if not doctor_id:
                    return self._send_json(
                        {"ok": False,
                         "error": "doctor_id is required."}, 400)
                doctor_id = doctor_id.strip().upper()
                try:
                    avail = hospital.get_doctor_availability(doctor_id)
                    leaves = hospital.list_doctor_leaves(
                        doctor_id, role=self._user["role"],
                        user_doctor_id=self._user.get("doctor_id"))
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True, "doctor_id": doctor_id,
                    "availability": avail, "leaves": leaves,
                })

            # ---- Step 11: hospital holidays (every role may view).
            if path == "/api/holidays":
                return self._send_json({
                    "ok": True, "holidays": hospital.list_holidays(),
                })

            # ---- Step 10: the appointment calendar — every
            # appointment in a date range (both ends inclusive).
            # Defaults: this month. A Doctor user linked to one
            # doctor only ever sees that doctor's appointments,
            # whatever they ask for.
            if path == "/api/appointments/calendar":
                start = query.get("from", [None])[0]
                end = query.get("to", [None])[0]
                if start in (None, ""):
                    start = datetime.date.today().replace(day=1).isoformat()
                if end in (None, ""):
                    today = datetime.date.today()
                    if today.month == 12:
                        next_first = datetime.date(today.year + 1, 1, 1)
                    else:
                        next_first = datetime.date(
                            today.year, today.month + 1, 1)
                    end = (next_first
                           - datetime.timedelta(days=1)).isoformat()
                doctor_id = self._doctor_filter()
                if doctor_id is None:
                    doctor_id = query.get("doctor_id", [None])[0]
                department = query.get("department", [None])[0]
                try:
                    start, _ = validate_appt_datetime(start, None)
                    end, _ = validate_appt_datetime(end, None)
                    rows = hospital.calendar_appointments(
                        start, end, doctor_id=doctor_id,
                        department=department)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True, "from": start, "to": end,
                    "appointments": rows,
                })

            # ---- Step 9: the appointments table — server-side
            # search, filters, sorting and pagination. A Doctor user
            # linked to one doctor only ever sees that doctor's
            # appointments, whatever they ask for.
            if path == "/api/appointments/list":
                params = _appt_list_params(query)
                doctor_filter = self._doctor_filter()
                if doctor_filter is not None:
                    params["doctor_id"] = doctor_filter
                try:
                    result = hospital.list_appointments(**params)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({"ok": True, **result})

            # ---- Step 9: the same filtered table as a download —
            # CSV (opens in Excel) or PDF (pure-Python generator).
            # No pagination here: every matching row is exported.
            if path == "/api/appointments/export.csv":
                params = _appt_list_params(query)
                doctor_filter = self._doctor_filter()
                if doctor_filter is not None:
                    params["doctor_id"] = doctor_filter
                params.pop("page", None)
                params.pop("per_page", None)
                try:
                    rows = hospital.filter_appointments(**params)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_text(
                    hospital.appointments_to_csv(rows),
                    "text/csv; charset=utf-8", "appointments.csv")

            if path == "/api/appointments/export.pdf":
                params = _appt_list_params(query)
                doctor_filter = self._doctor_filter()
                if doctor_filter is not None:
                    params["doctor_id"] = doctor_filter
                params.pop("page", None)
                params.pop("per_page", None)
                try:
                    rows = hospital.filter_appointments(**params)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_bytes(
                    appointments_pdf(
                        rows, "Appointments — City Care Hospital",
                        _filters_note(params, len(rows))),
                    "application/pdf", "appointments.pdf")

            # ---- Step 9: one appointment's full details — patient,
            # doctor, payment status and status history. A Doctor
            # linked to one doctor may only open their own queue's.
            if path.startswith("/api/appointments/by-id/"):
                raw = path.removeprefix("/api/appointments/by-id/").strip("/")
                if not raw or "/" in raw:
                    return self._send_json(
                        {"ok": False, "error": "Unknown endpoint."}, 404)
                apt_id = urllib.parse.unquote(raw)
                detail = hospital.appointment_detail(apt_id)
                if detail is None:
                    return self._send_json(
                        {"ok": False, "error": "Appointment not found."}, 404)
                doctor_filter = self._doctor_filter()
                if doctor_filter is not None \
                        and doctor_filter != detail["appointment"]["doctor_id"]:
                    return self._forbidden(self._user["role"])
                return self._send_json({"ok": True, **detail})

            # ---- Step 6: status history for one appointment
            # (doctor + token). Every role may read it; a Doctor
            # linked to one doctor may only read their own queue's.
            if path.startswith("/api/appointments/") and path.endswith("/history"):
                parts = path.removeprefix("/api/appointments/").removesuffix("/history").strip("/").split("/")
                if len(parts) != 2 or not parts[0] or not parts[1]:
                    return self._send_json(
                        {"ok": False, "error": "Unknown endpoint."}, 404)
                doc_id = parts[0].strip().upper()
                token = parts[1].strip().upper()
                doctor_filter = self._doctor_filter()
                if doctor_filter is not None and doctor_filter != doc_id:
                    return self._forbidden(self._user["role"])
                if doc_id not in hospital.doctors_map:
                    return self._send_json(
                        {"ok": False, "error": "Doctor not found."}, 404)
                appt = hospital.get_appointment(doc_id, token)
                if appt is None:
                    return self._send_json(
                        {"ok": False, "error": "Appointment not found."}, 404)
                return self._send_json({
                    "ok": True, "appointment": appt,
                    "history": hospital.appointment_history(doc_id, token),
                })

            if path == "/api/beds":
                return self._send_json(
                    {"ok": True, "beds": _state_payload()["beds"]})

            if path == "/api/pharmacy":
                return self._send_json(
                    {"ok": True, "queue": _queue_items(hospital.pharmacy_q)})

            if path == "/api/shifts":
                shifts = _shifts()
                return self._send_json({
                    "ok": True, "shifts": shifts,
                    "on_duty": shifts[0] if shifts else None,
                })

            if path == "/api/users":
                if not can(self._user["role"], "manage_users"):
                    return self._forbidden(self._user["role"])
                return self._send_json(
                    {"ok": True, "users": auth_store.list_users()})

            # ---- Step 3: Billing + Reports (read-only for every role)
            if path == "/api/bills":
                pid = None
                if query.get("patient_id"):
                    pid = _parse_int(query.get("patient_id")[0])
                    if pid is None:
                        return self._send_json(
                            {"ok": False,
                             "error": "Patient ID must be a number."}, 400)
                status = query.get("status", [None])[0]
                if status not in (None, "Paid", "Unpaid"):
                    return self._send_json(
                        {"ok": False,
                         "error": "Status must be Paid or Unpaid."}, 400)
                return self._send_json({
                    "ok": True,
                    "bills": hospital.billing.list_bills(
                        patient_id=pid, status=status),
                })

            if path == "/api/bills/export":
                # CSV download — a plain link, the browser sends the
                # session cookie with it, so it stays behind the login.
                return self._send_text(
                    hospital.billing.to_csv(),
                    "text/csv; charset=utf-8", "bills.csv")

            if path.startswith("/api/bills/"):
                raw = path.removeprefix("/api/bills/").strip("/")
                try:
                    bill_id = int(raw)
                except ValueError:
                    return self._send_json(
                        {"ok": False, "error": "Bill ID must be a number."}, 400)
                bill = hospital.billing.get_bill(bill_id)
                if bill is None:
                    return self._send_json(
                        {"ok": False, "error": "Bill not found."}, 404)
                return self._send_json({"ok": True, "bill": bill})

            if path == "/api/reports":
                date = query.get("date", [None])[0] or \
                    datetime.date.today().isoformat()
                try:
                    datetime.date.fromisoformat(date)
                except ValueError:
                    return self._send_json(
                        {"ok": False,
                         "error": "Date must look like 2026-11-27."}, 400)
                return self._send_json(
                    {"ok": True, "report": hospital.daily_report(date)})

            # Unknown API route: answer JSON, never an HTML 404 page —
            # the frontend's res.json() would throw on HTML and the
            # whole page would die silently with no error message.
            return self._send_json(
                {"ok": False, "error": "Unknown endpoint."}, 404)

    # ----------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Support native HTML form POST to /login or /api/login
        ctype = self.headers.get("Content-Type", "")
        if path in ("/login", "/api/login") and "application/x-www-form-urlencoded" in ctype:
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                length = 0
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
            params = urllib.parse.parse_qs(raw)
            username = params.get("username", [""])[0].strip()
            password = params.get("password", [""])[0].strip()
            user = auth_store.authenticate(username, password)
            if user is None:
                self.send_response(303)
                self.send_header("Location", "/login.html?error=Wrong%20username%20or%20password")
                self.end_headers()
                return
            token = _new_session(user)
            cookie = (f"{SESSION_COOKIE}={token}; Path=/;"
                      f" SameSite=Lax; Max-Age={SESSION_MAX_AGE}")
            self.send_response(303)
            self.send_header("Set-Cookie", cookie)
            self.send_header("Location", f"/index.html?token={token}")
            self.end_headers()
            return

        data = self._read_json_body()

        # ---- Login / logout need no session (they make or break one).
        if path in ("/login", "/api/login"):
            username = str(data.get("username", "")).strip()
            password = str(data.get("password", ""))
            user = auth_store.authenticate(username, password)
            if user is None:
                return self._send_json(
                    {"ok": False, "error": "Wrong username or password."}, 401)
            token = _new_session(user)
            cookie = (f"{SESSION_COOKIE}={token}; HttpOnly; Path=/;"
                      f" SameSite=Lax; Max-Age={SESSION_MAX_AGE}")
            return self._send_json(
                {"ok": True, "message": f"Logged in as {user['username']}.",
                 "user": user, "token": token},
                extra_headers=[("Set-Cookie", cookie)])

        if path == "/api/logout":
            _drop_session(_session_token(self))
            return self._send_json(
                {"ok": True, "message": "Logged out."},
                extra_headers=[("Set-Cookie",
                                f"{SESSION_COOKIE}=; HttpOnly; Path=/;"
                                " SameSite=Lax; Max-Age=0")])

        # ---- Everything else needs a login.
        if not path.startswith("/api/"):
            return self._send_json(
                {"ok": False, "error": "Unknown endpoint."}, 404)
        user = _session_user(self)
        if user is None:
            return self._send_json(
                {"ok": False, "error": "Please log in first."}, 401)
        self._user = user
        return self._api_post(path, data)

    def _api_post(self, path: str, data: Dict[str, Any]) -> None:
        """Authenticated POST /api/* routes (called only after login)."""
        role = self._user["role"]

        with state_lock:
            # ---- Change my own password (every role)
            if path == "/api/change-password":
                current = str(data.get("current_password", ""))
                new = str(data.get("new_password", ""))
                again = str(data.get("new_password_again", new))
                if new != again:
                    return self._send_json(
                        {"ok": False,
                         "error": "New passwords do not match."}, 400)
                try:
                    ok = auth_store.change_password(
                        self._user["username"], current, new)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                if not ok:
                    return self._send_json(
                        {"ok": False, "error": "Wrong current password."}, 400)
                return self._send_json(
                    {"ok": True, "message": "Password changed."})

            # ---- Users (Admin only)
            if path == "/api/users":
                if not can(role, "manage_users"):
                    return self._forbidden(role)
                username = str(data.get("username", "")).strip()
                password = str(data.get("password", ""))
                new_role = str(data.get("role", "")).strip().title()
                doctor_id = str(data.get("doctor_id", "")).strip()
                try:
                    created = auth_store.create_user(
                        username, password, new_role, doctor_id)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json(
                    {"ok": True, "message": f"User {username} created.",
                     "user": created})

            if path.startswith("/api/users/") and path.endswith("/reset"):
                if not can(role, "manage_users"):
                    return self._forbidden(role)
                username = path.removeprefix("/api/users/").removesuffix("/reset").strip("/")
                new_password = str(data.get("new_password", ""))
                try:
                    auth_store.reset_password(username, new_password)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json(
                    {"ok": True,
                     "message": f"Password reset for {username}."})

            # ---- Add Patient
            if path == "/api/patients":
                if not can(role, "add_patient"):
                    return self._forbidden(role)
                pid = _parse_int(data.get("id"))
                age = _parse_int(data.get("age"))
                if pid is None or age is None:
                    return self._send_json(
                        {"ok": False, "error": "ID and Age must be numbers."}, 400)
                if pid <= 0:
                    return self._send_json(
                        {"ok": False, "error": "Patient ID must be a positive number."}, 400)
                if not (0 <= age <= 150):
                    return self._send_json(
                        {"ok": False, "error": "Age must be between 0 and 150."}, 400)
                name = str(data.get("name", "")).strip()
                problem = str(data.get("problem", "")).strip()
                if not name or not problem:
                    return self._send_json(
                        {"ok": False, "error": "Name and Problem are required."}, 400)
                # Step 8: Father Name, Phone and Gender are optional —
                # a bad phone or gender is a 400 with a plain message.
                father_name = str(data.get("father_name", "") or "").strip()
                try:
                    phone = validate_phone(data.get("phone", ""))
                    gender = validate_gender(data.get("gender", ""))
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                if pid in hospital.patients_map:
                    return self._send_json(
                        {"ok": False, "error": "ID already exists."}, 400)
                hospital.add_patient(pid, name, age, problem,
                                     father_name=father_name, phone=phone,
                                     gender=gender)
                if pid not in hospital.patients_map:
                    return self._send_json(
                        {"ok": False, "error": "Could not add patient."}, 400)
                return self._send_json({
                    "ok": True, "message": "Patient added.",
                    "patient": _serialize_patient(hospital.patients_map[pid]),
                })

            # ---- Add Note
            if path.startswith("/api/patients/") and path.endswith("/notes"):
                if not can(role, "add_note"):
                    return self._forbidden(role)
                raw = path.removeprefix("/api/patients/").removesuffix("/notes").strip("/")
                try:
                    pid = int(raw)
                except ValueError:
                    return self._send_json(
                        {"ok": False, "error": "Patient ID must be a number."}, 400)
                patient = hospital.patients_map.get(pid)
                if not patient:
                    return self._send_json(
                        {"ok": False, "error": "Patient not found."}, 404)
                note = str(data.get("note", "")).strip()
                if not note:
                    return self._send_json(
                        {"ok": False, "error": "Note text is required."}, 400)
                before = len(_notes_of(patient))
                hospital.add_note(pid, note)
                if len(_notes_of(patient)) != before + 1:
                    return self._send_json(
                        {"ok": False, "error": "Could not add note."}, 400)
                return self._send_json({
                    "ok": True, "message": "Note added.",
                    "patient": _serialize_patient(patient),
                })

            # ---- Edit Patient (Step 5): POST /api/patients/<id> with
            # any of {name, age, problem} — only the given fields
            # change, the ID never changes. Admin + Receptionist only.
            # Step 8: father_name, phone and gender can be changed the
            # same way ("" clears one, missing leaves it alone).
            if path.startswith("/api/patients/"):
                if not can(role, "edit_patient"):
                    return self._forbidden(role)
                raw = path.removeprefix("/api/patients/").strip("/")
                if "/" in raw:
                    return self._send_json(
                        {"ok": False, "error": "Unknown endpoint."}, 404)
                try:
                    pid = int(raw)
                except ValueError:
                    return self._send_json(
                        {"ok": False, "error": "Patient ID must be a number."}, 400)
                if pid not in hospital.patients_map:
                    return self._send_json(
                        {"ok": False, "error": "Patient not found."}, 404)
                name = data.get("name")
                problem = data.get("problem")
                age = data.get("age")
                if age is not None:
                    age = _parse_int(age)
                    if age is None:
                        return self._send_json(
                            {"ok": False, "error": "Age must be a number."}, 400)
                if name is not None:
                    name = str(name).strip()
                    if not name:
                        return self._send_json(
                            {"ok": False, "error": "Name cannot be empty."}, 400)
                if problem is not None:
                    problem = str(problem).strip()
                    if not problem:
                        return self._send_json(
                            {"ok": False, "error": "Problem cannot be empty."}, 400)
                father_name = data.get("father_name")
                phone = data.get("phone")
                gender = data.get("gender")
                if father_name is not None:
                    father_name = str(father_name).strip()
                try:
                    if phone is not None:
                        phone = validate_phone(phone)
                    if gender is not None:
                        gender = validate_gender(gender)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                try:
                    updated = hospital.edit_patient(
                        pid, name=name, age=age, problem=problem,
                        father_name=father_name, phone=phone, gender=gender)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True, "message": "Patient updated.",
                    "patient": _serialize_patient(
                        hospital.patients_map[pid]),
                })

            # ---- Add Doctor
            if path == "/api/doctors":
                if not can(role, "add_doctor"):
                    return self._forbidden(role)
                doc_id = str(data.get("id", "")).strip().upper()
                name = str(data.get("name", "")).strip()
                spec = str(data.get("spec", "")).strip()
                fee = _parse_int(data.get("fee"))
                if fee is None:
                    return self._send_json(
                        {"ok": False, "error": "Fee must be a number."}, 400)
                if fee < 0:
                    return self._send_json(
                        {"ok": False, "error": "Fee cannot be negative."}, 400)
                if not doc_id or not name or not spec:
                    return self._send_json(
                        {"ok": False, "error": "ID, Name and Specialty are required."}, 400)
                if doc_id in hospital.doctors_map:
                    return self._send_json(
                        {"ok": False, "error": "Doctor ID already exists."}, 400)
                hospital.doctors_map[doc_id] = {
                    "id": doc_id, "name": name, "spec": spec, "fee": fee,
                    "queue": PriorityQueue(),
                }
                hospital.shift_q.enqueue(f"{doc_id} - {name}")
                hospital.save()
                return self._send_json({
                    "ok": True, "message": "Doctor added.",
                    "doctor": _serialize_doctor(hospital.doctors_map[doc_id]),
                })

            # ---- Step 11: save a doctor's availability (Admin only).
            # working_days is e.g. ["Mon","Tue","Wed","Thu","Fri"];
            # breaks is e.g. ["13:00-14:00"].
            if path == "/api/availability":
                if not can(role, "manage_availability"):
                    return self._forbidden(role)
                doc_id = str(data.get("doctor_id", "")).strip().upper()
                duration = _parse_int(data.get("duration_minutes"))
                if duration is None:
                    return self._send_json(
                        {"ok": False,
                         "error": "Consultation duration must be a number of minutes."}, 400)
                try:
                    record = hospital.set_doctor_availability(
                        doc_id, data.get("working_days"),
                        data.get("start_time"), data.get("end_time"),
                        duration, data.get("breaks"), role=role)
                except PermissionError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 403)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True, "message": "Availability saved.",
                    "availability": record,
                })

            # ---- Step 11: clear a doctor's availability (Admin only)
            # — back to "not set", so any time is allowed again.
            if path == "/api/availability/clear":
                if not can(role, "manage_availability"):
                    return self._forbidden(role)
                doc_id = str(data.get("doctor_id", "")).strip().upper()
                try:
                    cleared = hospital.clear_doctor_availability(
                        doc_id, role=role)
                except PermissionError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 403)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True,
                    "message": "Availability cleared."
                    if cleared else "No availability was set.",
                    "cleared": cleared,
                })

            # ---- Step 11: add leave for a doctor — Admin for any
            # doctor, a Doctor only for themself (Hospital enforces
            # the fine rule — 403 otherwise).
            if path == "/api/leaves":
                if not can(role, "manage_leave"):
                    return self._forbidden(role)
                doc_id = str(data.get("doctor_id", "")).strip().upper()
                try:
                    row = hospital.add_doctor_leave(
                        doc_id, data.get("start_date"),
                        data.get("end_date"), data.get("reason", ""),
                        role=role,
                        user_doctor_id=self._user.get("doctor_id"))
                except PermissionError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 403)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True, "message": "Leave added.", "leave": row,
                })

            # ---- Step 11: delete one leave row (Admin any, Doctor
            # only their own).
            if path.startswith("/api/leaves/") and path.endswith("/delete"):
                if not can(role, "manage_leave"):
                    return self._forbidden(role)
                raw = path.removeprefix("/api/leaves/").removesuffix("/delete").strip("/")
                if not raw or "/" in raw:
                    return self._send_json(
                        {"ok": False, "error": "Unknown endpoint."}, 404)
                try:
                    hospital.delete_doctor_leave(
                        raw, role=role,
                        user_doctor_id=self._user.get("doctor_id"))
                except PermissionError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 403)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True, "message": "Leave deleted.",
                })

            # ---- Step 11: add a hospital holiday (Admin only).
            if path == "/api/holidays":
                if not can(role, "manage_holidays"):
                    return self._forbidden(role)
                try:
                    row = hospital.add_holiday(
                        data.get("date"), data.get("name"), role=role)
                except PermissionError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 403)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True, "message": "Holiday added.",
                    "holiday": row,
                })

            # ---- Step 11: delete one holiday (Admin only).
            if path.startswith("/api/holidays/") and path.endswith("/delete"):
                if not can(role, "manage_holidays"):
                    return self._forbidden(role)
                raw = path.removeprefix("/api/holidays/").removesuffix("/delete").strip("/")
                if not raw or "/" in raw:
                    return self._send_json(
                        {"ok": False, "error": "Unknown endpoint."}, 404)
                try:
                    hospital.delete_holiday(raw, role=role)
                except PermissionError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 403)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True, "message": "Holiday deleted.",
                })

            # ---- Book Appointment (Step 4: date/time — date empty =
            # today, time empty = no fixed time; bad values get a 400)
            # Step 8: a Type (General / Follow-up / Emergency —
            # Emergency books an E- token), a Reason, Notes and a
            # Status (Pending or Confirmed), and either "patient_id"
            # for an existing patient or a "new_patient" object to
            # register one inline (its ID is auto-assigned when left
            # out). The reply carries the full appointment, including
            # its auto-generated Appointment ID (APT-0001, ...).
            if path == "/api/appointments":
                if not can(role, "book_appointment"):
                    return self._forbidden(role)
                doc_id = str(data.get("doctor_id", "")).strip().upper()
                # ---- Patient: existing, or register one inline.
                new_patient = data.get("new_patient")
                pid = None
                if isinstance(new_patient, dict):
                    try:
                        np_age = _parse_int(new_patient.get("age"))
                        if np_age is None:
                            raise ValueError(
                                "Age must be a number.")
                        np_name, np_age, np_problem = validate_patient_details(
                            new_patient.get("name"), np_age,
                            new_patient.get("problem"))
                        np_phone = validate_phone(
                            new_patient.get("phone", ""))
                        np_gender = validate_gender(
                            new_patient.get("gender", ""))
                    except ValueError as exc:
                        return self._send_json(
                            {"ok": False, "error": str(exc)}, 400)
                    np_id = _parse_int(new_patient.get("id"))
                    if np_id is not None:
                        if np_id <= 0:
                            return self._send_json(
                                {"ok": False,
                                 "error": "Patient ID must be a positive number."}, 400)
                        if np_id in hospital.patients_map:
                            return self._send_json(
                                {"ok": False,
                                 "error": "ID already exists."}, 400)
                        pid = np_id
                    else:
                        pid = hospital.next_patient_id()
                    np_father = str(
                        new_patient.get("father_name", "") or "").strip()
                else:
                    pid = _parse_int(data.get("patient_id"))
                    if pid is None:
                        return self._send_json(
                            {"ok": False,
                             "error": "Patient ID must be a number, or send new_patient."}, 400)
                # ---- Type (decides the token), status, reason, notes.
                # When no type is given, the old "emergency" flag
                # still decides it (Emergency -> E- token) so older
                # callers keep working.
                if data.get("type") not in (None, ""):
                    appt_type = str(data.get("type")).strip()
                    if appt_type not in APPOINTMENT_TYPES:
                        return self._send_json(
                            {"ok": False,
                             "error": "Type must be General, Follow-up or Emergency."}, 400)
                else:
                    appt_type = "Emergency" if bool(
                        data.get("emergency")) else "General"
                is_emg = appt_type == "Emergency"
                status = str(
                    data.get("status", "Pending") or "Pending").strip()
                if status not in BOOKING_STATUSES:
                    return self._send_json(
                        {"ok": False,
                         "error": "Status at booking must be Pending or Confirmed."}, 400)
                reason = str(data.get("reason", "") or "").strip()
                notes = str(data.get("notes", "") or "").strip()
                try:
                    appt_date, appt_time = validate_appt_datetime(
                        data.get("date"), data.get("time"))
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                doc = hospital.doctors_map.get(doc_id)
                if not doc:
                    return self._send_json(
                        {"ok": False, "error": "Invalid doctor ID."}, 400)
                # Everything is checked BEFORE the new patient is
                # registered, so a failed booking never leaves a stray
                # patient behind.
                # ---- Step 11: availability — holiday, leave, working
                # day/hours, break, and the (now duration-aware)
                # double-booking guard. The 400 says exactly why.
                try:
                    block_reason = hospital.check_availability(
                        doc_id, appt_date, appt_time)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                if block_reason is not None:
                    return self._send_json(
                        {"ok": False, "error": block_reason}, 400)
                if isinstance(new_patient, dict):
                    if pid not in hospital.patients_map:
                        hospital.add_patient(
                            pid, np_name, np_age, np_problem,
                            father_name=np_father, phone=np_phone,
                            gender=np_gender)
                    pat = hospital.patients_map[pid]
                else:
                    pat = hospital.patients_map.get(pid)
                    if not pat:
                        return self._send_json(
                            {"ok": False, "error": "Invalid patient ID."}, 400)
                # ---- Step 11: the availability was already checked
                # above (before the new patient was registered), and
                # book_appointment checks it again — a refused booking
                # never leaves a stray patient behind.
                token = hospital.book_appointment(
                    doc_id, pid, is_emg, appt_date, appt_time,
                    appt_type=appt_type, reason=reason, notes=notes,
                    status=status)
                if token is None:
                    return self._send_json(
                        {"ok": False, "error": "Could not book appointment."}, 400)
                appt = hospital.get_appointment(doc_id, token)
                when = appt_date + (f" {appt_time}" if appt_time else "")
                return self._send_json({
                    "ok": True,
                    "message": f"Booked {appt['apt_id']} ({token}) for {pat['name']} ({when}).",
                    "token": token, "apt_id": appt["apt_id"],
                    "appointment": appt,
                    "doctor": doc["name"],
                    "department": doc["spec"],
                    "patient": pat["name"], "fee": doc["fee"],
                    "emergency": is_emg,
                    "date": appt_date, "time": appt_time,
                    "status": appt["status"],
                })

            # ---- Step 6: change an appointment's status.
            # Admin/Receptionist: any status, any queue. Doctor: only
            # their own queue, only Checked In / In Progress /
            # Completed — Hospital.update_status enforces it (403).
            if path.startswith("/api/appointments/") and path.endswith("/status"):
                if not can(role, "update_appointment_status"):
                    return self._forbidden(role)
                parts = path.removeprefix("/api/appointments/").removesuffix("/status").strip("/").split("/")
                if len(parts) != 2 or not parts[0] or not parts[1]:
                    return self._send_json(
                        {"ok": False, "error": "Unknown endpoint."}, 404)
                doc_id = parts[0].strip().upper()
                token = parts[1].strip().upper()
                new_status = str(data.get("status", "")).strip()
                if not new_status:
                    return self._send_json(
                        {"ok": False, "error": "Status is required."}, 400)
                try:
                    updated = hospital.update_status(
                        doc_id, token, new_status,
                        changed_by=self._user["username"], role=role,
                        user_doctor_id=self._user.get("doctor_id"))
                except PermissionError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 403)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True,
                    "message": f"Status updated: {token} is now {updated['status']}.",
                    "appointment": updated,
                    "history": hospital.appointment_history(doc_id, token),
                })

            # ---- Step 9: edit an appointment's date/time/type/
            # reason/notes. Admin/Receptionist only (a Doctor gets
            # 403 — they update the consultation status, never the
            # details). Only the keys sent change; a reschedule
            # re-runs the double-booking guard, and a type change
            # that flips the priority changes it in place and
            # re-orders the queue (token, APT ID and history kept).
            if path.startswith("/api/appointments/by-id/") and path.endswith("/edit"):
                if not can(role, "edit_appointment"):
                    return self._forbidden(role)
                raw = path.removeprefix("/api/appointments/by-id/").removesuffix("/edit").strip("/")
                if not raw or "/" in raw:
                    return self._send_json(
                        {"ok": False, "error": "Unknown endpoint."}, 404)
                apt_id = urllib.parse.unquote(raw)
                mapping = {"date": "date", "time": "time",
                           "type": "appt_type", "reason": "reason",
                           "notes": "notes"}
                kwargs = {param: data[key] for key, param in mapping.items()
                          if key in data}
                # Unknown APT ID is a 404, not a 400 (the same rule as
                # the details endpoint).
                if hospital.get_appointment_by_id(apt_id)[0] is None:
                    return self._send_json(
                        {"ok": False, "error": "Appointment not found."}, 404)
                try:
                    updated = hospital.edit_appointment(
                        apt_id, role=role,
                        user_doctor_id=self._user.get("doctor_id"), **kwargs)
                except PermissionError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 403)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True,
                    "message": f"Appointment {updated['apt_id'] or updated['token']} updated.",
                    "appointment": updated,
                })

            # ---- Beds: assign / free
            if path == "/api/beds/assign":
                if not can(role, "manage_beds"):
                    return self._forbidden(role)
                bed_no = _parse_int(data.get("bed"))
                pid = _parse_int(data.get("patient_id"))
                if bed_no is None or pid is None:
                    return self._send_json(
                        {"ok": False, "error": "Bed and Patient ID must be numbers."}, 400)
                idx = bed_no - 1
                if not 0 <= idx < hospital.NUM_BEDS:
                    return self._send_json(
                        {"ok": False, "error": f"Bed must be 1-{hospital.NUM_BEDS}."}, 400)
                if hospital.beds[idx] != 0:
                    return self._send_json(
                        {"ok": False, "error": "That bed is already occupied."}, 400)
                if pid not in hospital.patients_map:
                    return self._send_json(
                        {"ok": False, "error": "Patient not found."}, 404)
                hospital.beds[idx] = pid
                hospital.undo_stack.push(("BED", idx, 0))
                hospital.save()
                return self._send_json({
                    "ok": True, "message": f"Bed {bed_no} assigned to patient {pid}."})

            if path == "/api/beds/free":
                if not can(role, "manage_beds"):
                    return self._forbidden(role)
                bed_no = _parse_int(data.get("bed"))
                if bed_no is None:
                    return self._send_json(
                        {"ok": False, "error": "Bed must be a number."}, 400)
                idx = bed_no - 1
                if not 0 <= idx < hospital.NUM_BEDS:
                    return self._send_json(
                        {"ok": False, "error": f"Bed must be 1-{hospital.NUM_BEDS}."}, 400)
                if hospital.beds[idx] == 0:
                    return self._send_json(
                        {"ok": False, "error": "That bed is already free."}, 400)
                old_pid = hospital.beds[idx]
                hospital.beds[idx] = 0
                hospital.undo_stack.push(("BED", idx, old_pid))
                hospital.save()
                return self._send_json({
                    "ok": True, "message": f"Bed {bed_no} freed (was patient {old_pid})."})

            # ---- Undo
            if path == "/api/undo":
                if not can(role, "undo"):
                    return self._forbidden(role)
                last = _undo_last()
                if not last:
                    return self._send_json(
                        {"ok": False, "error": "Nothing to undo."}, 400)
                hospital.undo()
                return self._send_json({
                    "ok": True, "message": f"Undid {last[0]}.",
                    "undid": last,
                })

            # ---- Pharmacy: add token / serve
            if path == "/api/pharmacy":
                if not can(role, "pharmacy"):
                    return self._forbidden(role)
                token = str(data.get("token", "")).strip()
                if not token:
                    return self._send_json(
                        {"ok": False, "error": "Token or Name is required."}, 400)
                hospital.pharmacy_q.enqueue(token)
                hospital.save()
                return self._send_json(
                    {"ok": True, "message": f"Token '{token}' added to pharmacy queue."})

            if path == "/api/pharmacy/serve":
                if not can(role, "pharmacy"):
                    return self._forbidden(role)
                served = hospital.pharmacy_q.dequeue()
                if served is not None:
                    hospital.save()
                if served is None:
                    return self._send_json(
                        {"ok": False, "error": "Pharmacy queue is empty."}, 400)
                return self._send_json(
                    {"ok": True, "message": f"Served: {served}", "served": served})

            # ---- Shifts: rotate
            if path == "/api/shifts/rotate":
                if not can(role, "rotate_shift"):
                    return self._forbidden(role)
                now = hospital.shift_q.rotate()
                if now is not None:
                    hospital.save()
                if now is None:
                    return self._send_json(
                        {"ok": False, "error": "No shifts to rotate."}, 400)
                return self._send_json(
                    {"ok": True, "message": f"Now on duty: {now}", "on_duty": now})

            # ---- Step 3: Billing (create/change: Admin + Receptionist;
            # reading is open to every role — see _api_get above)
            if path == "/api/bills":
                if not can(role, "create_bill"):
                    return self._forbidden(role)
                doc_id = str(data.get("doctor_id", "")).strip().upper()
                token = str(data.get("token", "")).strip().upper()
                pid = _parse_int(data.get("patient_id"))
                if pid is None:
                    return self._send_json(
                        {"ok": False, "error": "Patient ID must be a number."}, 400)
                if not doc_id or not token:
                    return self._send_json(
                        {"ok": False,
                         "error": "Doctor ID and Token are required."}, 400)
                try:
                    bill = hospital.create_bill(pid, doc_id, token)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True,
                    "message": f"Bill #{bill['id']} created — Rs {bill['total']}.",
                    "bill": bill,
                })

            if path.startswith("/api/bills/") and path.endswith("/charges"):
                if not can(role, "manage_bills"):
                    return self._forbidden(role)
                raw = path.removeprefix("/api/bills/").removesuffix("/charges").strip("/")
                try:
                    bill_id = int(raw)
                except ValueError:
                    return self._send_json(
                        {"ok": False, "error": "Bill ID must be a number."}, 400)
                label = str(data.get("label", "")).strip()
                amount = _parse_int(data.get("amount"))
                if not label:
                    return self._send_json(
                        {"ok": False, "error": "Charge label is required."}, 400)
                if amount is None:
                    return self._send_json(
                        {"ok": False, "error": "Amount must be a number."}, 400)
                try:
                    bill = hospital.billing.add_charge(bill_id, label, amount)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True,
                    "message": f"Charge added. Bill #{bill_id} total is now Rs {bill['total']}.",
                    "bill": bill,
                })

            if path.startswith("/api/bills/") and (
                    path.endswith("/pay") or path.endswith("/unpay")):
                if not can(role, "manage_bills"):
                    return self._forbidden(role)
                raw = path.removeprefix("/api/bills/")
                raw = raw.removesuffix("/pay").removesuffix("/unpay").strip("/")
                try:
                    bill_id = int(raw)
                except ValueError:
                    return self._send_json(
                        {"ok": False, "error": "Bill ID must be a number."}, 400)
                try:
                    bill = (hospital.billing.mark_paid(bill_id)
                            if path.endswith("/pay")
                            else hospital.billing.mark_unpaid(bill_id))
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                return self._send_json({
                    "ok": True,
                    "message": f"Bill #{bill_id} is now {bill['status']}.",
                    "bill": bill,
                })

        return self._send_json(
            {"ok": False, "error": "Unknown endpoint."}, 404)


    # --------------------------------------------------------- DELETE
    def do_DELETE(self) -> None:  # noqa: N802
        """DELETE /api/patients/<id> (Step 5, Admin only).

        The dashboard asks for confirmation before calling this — the
        server itself just deletes: patient record + notes + queued
        appointments go away, their bed is freed, their bills are KEPT.
        """
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            return self._send_json(
                {"ok": False, "error": "Unknown endpoint."}, 404)
        user = _session_user(self)
        if user is None:
            return self._send_json(
                {"ok": False, "error": "Please log in first."}, 401)
        self._user = user
        with state_lock:
            if path.startswith("/api/patients/"):
                if not can(user["role"], "delete_patient"):
                    return self._forbidden(user["role"])
                raw = path.removeprefix("/api/patients/").strip("/")
                if "/" in raw:
                    return self._send_json(
                        {"ok": False, "error": "Unknown endpoint."}, 404)
                try:
                    pid = int(raw)
                except ValueError:
                    return self._send_json(
                        {"ok": False, "error": "Patient ID must be a number."}, 400)
                try:
                    removed = hospital.delete_patient(pid)
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 404)
                return self._send_json({
                    "ok": True,
                    "message": f"Patient {removed['name']} (#{pid}) deleted.",
                    "deleted": removed,
                })
        return self._send_json(
            {"ok": False, "error": "Unknown endpoint."}, 404)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hospital Management System")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    try:
        server = _ReusableThreadingHTTPServer((args.host, args.port), HospitalRequestHandler)
    except OSError as err:
        print(f"\n[ERROR] Port {args.port} pehle se use ho raha hai (already in use).")
        print("Hal (Solutions):")
        print(f"  1. Pehle chalte hue terminal ko Ctrl+C daba kar band karein.")
        print(f"  2. Ya doosre port par chalayein: python server.py --port 8080\n")
        return
    url = f"http://{args.host}:{args.port}"
    print(f"Hospital Management System running at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
