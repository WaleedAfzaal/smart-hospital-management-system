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
from hospital import (DEFAULT_DB_PATH, Hospital, PriorityQueue,
                      binary_search, bubble_sort, validate_appt_datetime)

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
    """Read the session token from the request's Cookie header."""
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
    }


def _serialize_doctor(doc: Dict[str, Any]) -> Dict[str, Any]:
    # heap rows: (priority, count, token, pid, name, fee, booked date,
    # appt_date, appt_time). Sort a copy so the displayed queue is in
    # real service order (Emergency first).
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
            }
            for row in rows
        ],
    }


def _shifts() -> List[str]:
    q = hospital.shift_q
    return [q.items[(q.front + i) % q.size] for i in range(q.total)]


def _dashboard() -> Dict[str, Any]:
    free_beds = sum(1 for b in hospital.beds if b == 0)
    waiting = sum(len(doc["queue"].heap) for doc in hospital.doctors_map.values())
    # Step 3: today's money at a glance (bills dated today).
    today = datetime.date.today().isoformat()
    money = hospital.billing.summary_for_date(today)
    return {
        "patients": len(hospital.patients_map),
        "doctors": len(hospital.doctors_map),
        "waiting": waiting,
        "free_beds": free_beds,
        "total_beds": hospital.NUM_BEDS,
        "billed_today": money["billed"],
        "collected_today": money["collected"],
        "unpaid_total": hospital.billing.unpaid_total(),
    }


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


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR allows two processes to bind the same port at once,
    # causing requests to hit a stale background server. Disable it on Windows.
    allow_reuse_address = os.name != "nt"


# ---------------------------------------------------------------- handler

class HospitalRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"  [HTTP] {self.command} {self.path} -> "
              f"{args[1] if len(args) > 1 else ''}")

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
        if path in ("/", "/index.html") and _session_user(self) is None:
            self.path = "/login.html"
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
        data = self._read_json_body()

        # ---- Login / logout need no session (they make or break one).
        if path == "/api/login":
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
                 "user": user},
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
                if pid in hospital.patients_map:
                    return self._send_json(
                        {"ok": False, "error": "ID already exists."}, 400)
                hospital.add_patient(pid, name, age, problem)
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

            # ---- Book Appointment (Step 4: date/time — date empty =
            # today, time empty = no fixed time; bad values get a 400)
            if path == "/api/appointments":
                if not can(role, "book_appointment"):
                    return self._forbidden(role)
                doc_id = str(data.get("doctor_id", "")).strip().upper()
                pid = _parse_int(data.get("patient_id"))
                if pid is None:
                    return self._send_json(
                        {"ok": False, "error": "Patient ID must be a number."}, 400)
                is_emg = bool(data.get("emergency"))
                try:
                    appt_date, appt_time = validate_appt_datetime(
                        data.get("date"), data.get("time"))
                except ValueError as exc:
                    return self._send_json(
                        {"ok": False, "error": str(exc)}, 400)
                doc = hospital.doctors_map.get(doc_id)
                pat = hospital.patients_map.get(pid)
                if not doc or not pat:
                    return self._send_json(
                        {"ok": False, "error": "Invalid doctor or patient ID."}, 400)
                token = hospital.book_appointment(
                    doc_id, pid, is_emg, appt_date, appt_time)
                if token is None:
                    return self._send_json(
                        {"ok": False, "error": "Could not book appointment."}, 400)
                when = appt_date + (f" {appt_time}" if appt_time else "")
                return self._send_json({
                    "ok": True,
                    "message": f"Booked {token} for {pat['name']} ({when}).",
                    "token": token, "doctor": doc["name"],
                    "patient": pat["name"], "fee": doc["fee"],
                    "emergency": is_emg,
                    "date": appt_date, "time": appt_time,
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Hospital Management System")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = _ReusableThreadingHTTPServer((args.host, args.port), HospitalRequestHandler)
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
