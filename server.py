"""Hospital Management System — web server.

Serves the single-page dashboard in web/ and a JSON API backed by the
Hospital class from hospital.py (Ali's file — kept as-is, no changes).

Hospital's methods print instead of returning, so success is determined
from state before/after each call (e.g. pid in patients_map), and the
booking token is computed from the doctor's queue count after enqueue.

Step 1 (SQLite): state is saved to hospital.db on every change and loaded
on startup, so restarting the server no longer resets everything. The
undo stack is session-only and starts empty after a restart.
"""

import argparse
import json
import os
import threading
import urllib.parse
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from hospital import DEFAULT_DB_PATH, Hospital, PriorityQueue, binary_search, bubble_sort

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
HOST = "127.0.0.1"
PORT = 8000

# Step 1 (SQLite): the server shares the same hospital.db file as the
# CLI, so data added in one shows up in the other and survives restarts.
hospital = Hospital(db_path=DEFAULT_DB_PATH)
state_lock = threading.Lock()


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
    # heap rows: (priority, count, token, pid, name, fee). Sort a copy so
    # the displayed queue is in real service order (Emergency first).
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
    return {
        "patients": len(hospital.patients_map),
        "doctors": len(hospital.doctors_map),
        "waiting": waiting,
        "free_beds": free_beds,
        "total_beds": hospital.NUM_BEDS,
    }


def _undo_last() -> Optional[List[Any]]:
    top = hospital.undo_stack.top
    return list(top.val) if top is not None else None


def _state_payload() -> Dict[str, Any]:
    return {
        "patients": [_serialize_patient(p) for p in hospital.patients_list],
        "doctors": [_serialize_doctor(d) for d in hospital.doctors_map.values()],
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

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

        with state_lock:
            if path == "/api/state":
                return self._send_json({"ok": True, "state": _state_payload()})

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
                return self._send_json({
                    "ok": True,
                    "doctors": [_serialize_doctor(d)
                                for d in hospital.doctors_map.values()],
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

            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return

            if path.startswith("/api/"):
                # Unknown API route: answer JSON, never an HTML 404 page —
                # the frontend's res.json() would throw on HTML and the
                # whole page would die silently with no error message.
                return self._send_json(
                    {"ok": False, "error": "Unknown endpoint."}, 404)

        return super().do_GET()

    # ----------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        data = self._read_json_body()

        with state_lock:
            # ---- Add Patient
            if path == "/api/patients":
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

            # ---- Book Appointment
            if path == "/api/appointments":
                doc_id = str(data.get("doctor_id", "")).strip().upper()
                pid = _parse_int(data.get("patient_id"))
                if pid is None:
                    return self._send_json(
                        {"ok": False, "error": "Patient ID must be a number."}, 400)
                is_emg = bool(data.get("emergency"))
                doc = hospital.doctors_map.get(doc_id)
                pat = hospital.patients_map.get(pid)
                if not doc or not pat:
                    return self._send_json(
                        {"ok": False, "error": "Invalid doctor or patient ID."}, 400)
                hospital.book_appointment(doc_id, pid, is_emg)
                # Token is f"{E|N}-{queue.count}" — computed from the
                # doctor's queue count after enqueue.
                token = f"{'E' if is_emg else 'N'}-{doc['queue'].count}"
                return self._send_json({
                    "ok": True, "message": f"Booked {token} for {pat['name']}.",
                    "token": token, "doctor": doc["name"],
                    "patient": pat["name"], "fee": doc["fee"],
                    "emergency": is_emg,
                })

            # ---- Beds: assign / free
            if path == "/api/beds/assign":
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
                token = str(data.get("token", "")).strip()
                if not token:
                    return self._send_json(
                        {"ok": False, "error": "Token or Name is required."}, 400)
                hospital.pharmacy_q.enqueue(token)
                hospital.save()
                return self._send_json(
                    {"ok": True, "message": f"Token '{token}' added to pharmacy queue."})

            if path == "/api/pharmacy/serve":
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
                now = hospital.shift_q.rotate()
                if now is not None:
                    hospital.save()
                if now is None:
                    return self._send_json(
                        {"ok": False, "error": "No shifts to rotate."}, 400)
                return self._send_json(
                    {"ok": True, "message": f"Now on duty: {now}", "on_duty": now})

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
