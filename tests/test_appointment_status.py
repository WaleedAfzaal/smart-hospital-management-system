"""Step 6 tests: appointment statuses, status history, and the
no-double-booking guard (plus the server endpoints for both).

Statuses: Pending, Confirmed, Checked In, In Progress, Completed,
Cancelled, No Show. New appointments start Pending; every change is
written to the status-history table with the user and the time; a
Doctor may only move their OWN queue's appointments to Checked In,
In Progress or Completed.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth import AuthStore
from hospital import APPOINTMENT_STATUSES, DOCTOR_STATUSES, Hospital
from status_history import StatusHistory


class TestDefaultStatus(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.h.add_patient(101, "Ayesha", 30, "Fever")
        self.h.add_doctor("D1", "Dr. Ali", "Physician", 1000)

    def test_new_booking_starts_pending(self):
        token = self.h.book_appointment("D1", 101, False)
        appt = self.h.get_appointment("D1", token)
        self.assertEqual(appt["status"], "Pending")
        self.assertEqual(len(self.h.appointment_history("D1", token)), 0)

    def test_all_seven_statuses_exist(self):
        self.assertEqual(
            list(APPOINTMENT_STATUSES),
            ["Pending", "Confirmed", "Checked In", "In Progress",
             "Completed", "Cancelled", "No Show"])
        self.assertEqual(
            list(DOCTOR_STATUSES),
            ["Checked In", "In Progress", "Completed"])


class TestStatusTransitionsAndHistory(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.h.add_patient(101, "Ayesha", 30, "Fever")
        self.h.add_doctor("D1", "Dr. Ali", "Physician", 1000)
        self.token = self.h.book_appointment("D1", 101, False)

    def test_transition_records_history_with_user_and_time(self):
        appt = self.h.update_status(
            "D1", self.token, "Confirmed", changed_by="reception1",
            role="Receptionist")
        self.assertEqual(appt["status"], "Confirmed")
        history = self.h.appointment_history("D1", self.token)
        self.assertEqual(len(history), 1)
        entry = history[0]
        self.assertEqual(entry["old_status"], "Pending")
        self.assertEqual(entry["new_status"], "Confirmed")
        self.assertEqual(entry["changed_by"], "reception1")
        self.assertTrue(entry["changed_at"])  # a real timestamp
        self.assertEqual(entry["doctor_id"], "D1")
        self.assertEqual(entry["token"], self.token)

    def test_full_journey_keeps_every_change(self):
        for status in ("Confirmed", "Checked In", "In Progress",
                       "Completed"):
            self.h.update_status("D1", self.token, status,
                                 changed_by="admin", role="Admin")
        history = self.h.appointment_history("D1", self.token)
        self.assertEqual([h["new_status"] for h in history],
                         ["Confirmed", "Checked In", "In Progress",
                          "Completed"])
        self.assertEqual([h["old_status"] for h in history],
                         ["Pending", "Confirmed", "Checked In",
                          "In Progress"])

    def test_same_status_is_a_noop(self):
        self.h.update_status("D1", self.token, "Pending",
                             changed_by="admin", role="Admin")
        self.assertEqual(self.h.appointment_history("D1", self.token), [])

    def test_bad_status_and_missing_token_raise(self):
        with self.assertRaises(ValueError):
            self.h.update_status("D1", self.token, "Waiting",
                                 changed_by="admin", role="Admin")
        with self.assertRaises(ValueError):
            self.h.update_status("D1", "N-99", "Confirmed",
                                 changed_by="admin", role="Admin")
        with self.assertRaises(ValueError):
            self.h.update_status("D9", self.token, "Confirmed",
                                 changed_by="admin", role="Admin")

    def test_heap_order_unchanged_by_status(self):
        self.h.add_patient(102, "Bilal", 40, "Cough")
        token2 = self.h.book_appointment("D1", 102, True)
        self.h.update_status("D1", self.token, "Confirmed",
                             changed_by="admin", role="Admin")
        rows = self.h.doctors_map["D1"]["queue"].heap
        # Emergency (N-2) still serves before Normal (N-1).
        self.assertEqual(rows[0][2], token2)
        self.assertEqual(rows[1][2], self.token)

    def test_schedule_view_shows_status(self):
        self.h.update_status("D1", self.token, "Confirmed",
                             changed_by="admin", role="Admin")
        rows = self.h.appointments_for_date()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "Confirmed")


class TestMigrationToPending(unittest.TestCase):
    """Rows booked before Step 6 (9 fields, no status) load as
    Pending — tested by building a raw old-style database by hand
    and loading it, plus a database with a NULL status column."""

    def test_old_table_without_status_column_loads_as_pending(self):
        # A faithful pre-Step 6 database: the real schema MINUS the
        # status column, with the initialized marker. _connect runs
        # CREATE TABLE IF NOT EXISTS (other tables appear empty) and
        # _migrate adds the status column; the old row then loads as
        # Pending.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE meta(
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE patients(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    age INTEGER NOT NULL, problem TEXT NOT NULL,
                    visits INTEGER NOT NULL DEFAULT 0,
                    position INTEGER NOT NULL, registered TEXT);
                CREATE TABLE doctors(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    spec TEXT NOT NULL, fee INTEGER NOT NULL,
                    queue_count INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE appointments(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doctor_id TEXT NOT NULL, heap_pos INTEGER NOT NULL,
                    priority INTEGER NOT NULL, seq INTEGER NOT NULL,
                    token TEXT NOT NULL, patient_id INTEGER NOT NULL,
                    patient_name TEXT NOT NULL, fee INTEGER NOT NULL,
                    date TEXT, appt_date TEXT, appt_time TEXT);
            """)
            conn.execute("INSERT INTO meta VALUES ('initialized', '1')")
            conn.execute(
                "INSERT INTO patients VALUES "
                "(201, 'Ayesha', 30, 'Fever', 1, 0, NULL)")
            conn.execute(
                "INSERT INTO doctors VALUES "
                "('D1', 'Dr. Ali', 'Physician', 1000, 1)")
            conn.execute(
                "INSERT INTO appointments(doctor_id, heap_pos, priority,"
                " seq, token, patient_id, patient_name, fee, date,"
                " appt_date, appt_time)"
                " VALUES ('D1', 0, 2, 1, 'N-1', 201, 'Ayesha', 1000,"
                " '2026-11-27', '2026-11-28', '10:00')")
            conn.commit()
            conn.close()
            h = Hospital(db_path=path)
            self.assertEqual(
                h.get_appointment("D1", "N-1")["status"], "Pending")

    def test_null_status_in_new_table_loads_as_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "null.db")
            h = Hospital(db_path=path)
            h.add_patient(101, "Ayesha", 30, "Fever")
            h.add_doctor("D1", "Dr. Ali", "Physician", 1000)
            h.book_appointment("D1", 101, False, "2026-11-28", "10:00")
            # Blank the status the way an old file would have it.
            conn = sqlite3.connect(path)
            conn.execute("UPDATE appointments SET status = NULL")
            conn.commit()
            conn.close()
            h2 = Hospital(db_path=path)
            self.assertEqual(
                h2.get_appointment("D1", "N-1")["status"], "Pending")


class TestDoubleBooking(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.h.add_patient(101, "Ayesha", 30, "Fever")
        self.h.add_patient(102, "Bilal", 40, "Cough")
        self.h.add_doctor("D1", "Dr. Ali", "Physician", 1000)
        self.h.add_doctor("D2", "Dr. Sara", "Cardiologist", 2000)

    def test_same_doctor_same_date_time_refused(self):
        token = self.h.book_appointment("D1", 101, False,
                                        "2026-11-28", "10:00")
        self.assertIsNotNone(token)
        clash = self.h.find_conflicting_appointment(
            "D1", "2026-11-28", "10:00")
        self.assertIsNotNone(clash)
        self.assertEqual(clash["token"], token)
        # book_appointment refuses it and returns None.
        self.assertIsNone(self.h.book_appointment(
            "D1", 102, False, "2026-11-28", "10:00"))
        self.assertEqual(len(self.h.doctors_map["D1"]["queue"].heap), 1)

    def test_different_doctor_allowed(self):
        self.h.book_appointment("D1", 101, False, "2026-11-28", "10:00")
        token = self.h.book_appointment("D2", 102, False,
                                        "2026-11-28", "10:00")
        self.assertIsNotNone(token)

    def test_different_time_allowed(self):
        self.h.book_appointment("D1", 101, False, "2026-11-28", "10:00")
        token = self.h.book_appointment("D1", 102, False,
                                        "2026-11-28", "11:00")
        self.assertIsNotNone(token)

    def test_different_date_allowed(self):
        self.h.book_appointment("D1", 101, False, "2026-11-28", "10:00")
        token = self.h.book_appointment("D1", 102, False,
                                        "2026-11-29", "10:00")
        self.assertIsNotNone(token)

    def test_cancelled_or_no_show_frees_the_slot(self):
        token = self.h.book_appointment("D1", 101, False,
                                        "2026-11-28", "10:00")
        self.h.update_status("D1", token, "Cancelled",
                             changed_by="admin", role="Admin")
        self.assertIsNone(self.h.find_conflicting_appointment(
            "D1", "2026-11-28", "10:00"))
        self.assertIsNotNone(self.h.book_appointment(
            "D1", 102, False, "2026-11-28", "10:00"))
        # A No Show frees the slot as well.
        token2 = self.h.book_appointment("D1", 101, False,
                                         "2026-11-28", "11:00")
        self.h.update_status("D1", token2, "No Show",
                             changed_by="admin", role="Admin")
        self.assertIsNotNone(self.h.book_appointment(
            "D1", 102, False, "2026-11-28", "11:00"))

    def test_completed_still_blocks_the_slot(self):
        token = self.h.book_appointment("D1", 101, False,
                                        "2026-11-28", "10:00")
        self.h.update_status("D1", token, "Completed",
                             changed_by="admin", role="Admin")
        self.assertIsNotNone(self.h.find_conflicting_appointment(
            "D1", "2026-11-28", "10:00"))

    def test_untimed_appointments_never_collide(self):
        self.h.book_appointment("D1", 101, False)  # no date/time
        self.assertIsNotNone(self.h.book_appointment("D1", 102, False))
        self.assertIsNotNone(
            self.h.book_appointment("D1", 101, False, "2026-11-28"))
        # A timed booking next to untimed ones is fine too.
        self.assertIsNotNone(
            self.h.book_appointment("D1", 102, False, "2026-11-28",
                                    "10:00"))


class TestRoleEnforcement(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.h.add_patient(101, "Ayesha", 30, "Fever")
        self.h.add_doctor("D1", "Dr. Ali", "Physician", 1000)
        self.h.add_doctor("D2", "Dr. Sara", "Cardiologist", 2000)
        self.t1 = self.h.book_appointment("D1", 101, False)
        self.t2 = self.h.book_appointment("D2", 101, False)

    def test_admin_and_receptionist_can_set_any_status(self):
        for role in ("Admin", "Receptionist"):
            appt = self.h.update_status(
                "D1", self.t1, "Cancelled", changed_by=role.lower(),
                role=role)
            self.assertEqual(appt["status"], "Cancelled")
            self.h.update_status("D1", self.t1, "Pending",
                                 changed_by=role.lower(), role=role)

    def test_doctor_can_only_move_own_queue_to_three_statuses(self):
        appt = self.h.update_status(
            "D1", self.t1, "Checked In", changed_by="dr.ali",
            role="Doctor", user_doctor_id="D1")
        self.assertEqual(appt["status"], "Checked In")

    def test_doctor_cannot_cancel_or_confirm(self):
        for status in ("Cancelled", "Confirmed", "Pending", "No Show"):
            with self.assertRaises(PermissionError):
                self.h.update_status(
                    "D1", self.t1, status, changed_by="dr.ali",
                    role="Doctor", user_doctor_id="D1")

    def test_doctor_cannot_touch_another_doctors_queue(self):
        with self.assertRaises(PermissionError):
            self.h.update_status(
                "D2", self.t2, "Checked In", changed_by="dr.ali",
                role="Doctor", user_doctor_id="D1")

    def test_doctor_with_no_link_can_use_any_queue(self):
        # A Doctor user not linked to any doctor may move any queue —
        # but still only to the three consultation statuses.
        appt = self.h.update_status(
            "D2", self.t2, "Completed", changed_by="dr.x",
            role="Doctor", user_doctor_id=None)
        self.assertEqual(appt["status"], "Completed")
        with self.assertRaises(PermissionError):
            self.h.update_status(
                "D2", self.t2, "Cancelled", changed_by="dr.x",
                role="Doctor", user_doctor_id=None)


class TestStatusHistoryStore(unittest.TestCase):
    def test_in_memory_history_survives_only_in_that_store(self):
        store = StatusHistory()
        store.record("D1", "N-1", None, "Pending", "admin")
        store.record("D1", "N-1", "Pending", "Confirmed", "admin")
        rows = store.history("D1", "N-1")
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["old_status"])
        self.assertEqual(rows[1]["new_status"], "Confirmed")

    def test_history_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.db")
            h = Hospital(db_path=path)
            h.add_patient(101, "Ayesha", 30, "Fever")
            h.add_doctor("D1", "Dr. Ali", "Physician", 1000)
            token = h.book_appointment("D1", 101, False,
                                       "2026-11-28", "10:00")
            h.update_status("D1", token, "Confirmed",
                            changed_by="admin", role="Admin")
            h.update_status("D1", token, "Checked In",
                            changed_by="admin", role="Admin")
            h2 = Hospital(db_path=path)
            self.assertEqual(h2.get_appointment("D1", token)["status"],
                             "Checked In")
            history = h2.appointment_history("D1", token)
            self.assertEqual([x["new_status"] for x in history],
                             ["Confirmed", "Checked In"])
            self.assertEqual([x["changed_by"] for x in history],
                             ["admin", "admin"])

    def test_history_survives_patient_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.db")
            h = Hospital(db_path=path)
            h.add_patient(101, "Ayesha", 30, "Fever")
            h.add_doctor("D1", "Dr. Ali", "Physician", 1000)
            token = h.book_appointment("D1", 101, False)
            h.update_status("D1", token, "Confirmed",
                            changed_by="admin", role="Admin")
            h.delete_patient(101)
            self.assertEqual(len(h.appointment_history("D1", token)), 1)


class TestStatusServer(unittest.TestCase):
    """End-to-end: status + history endpoints, role 403s, and the
    double-booking 400 — through the real HTTP server, the same way
    tests/test_server.py boots it (server.hospital / server.auth_store
    + HospitalRequestHandler on an ephemeral port).

    Every test books its OWN appointment at its own time, so the
    tests never depend on each other's order or on shared state.
    """

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer

        import server

        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "server.db")
        h = Hospital(db_path=db_path)
        h.add_patient(101, "Ayesha", 30, "Fever")
        h.add_patient(102, "Bilal", 40, "Cough")
        h.add_doctor("D1", "Dr. Ali", "Physician", 1000)
        h.add_doctor("D2", "Dr. Sara", "Cardiologist", 2000)
        cls.h = h
        cls.auth_db = os.path.join(cls.tmp.name, "auth.db")
        cls.auth = AuthStore(cls.auth_db)
        # "admin" already exists — AuthStore seeds the default admin.
        cls.auth.create_user("rec", "rec123", "Receptionist")
        cls.auth.create_user("doc", "doc123", "Doctor", doctor_id="D1")
        server.hospital = h
        server.auth_store = cls.auth
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.HospitalRequestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def _login(self, user, password):
        import json as _json
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/login",
            data=_json.dumps(
                {"username": user, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req) as resp:
            return _json.load(resp)["token"]

    def _call(self, method, path, token=None, payload=None):
        import json as _json
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=_json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"}
                        if token else {})},
            method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, _json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, _json.load(exc)

    def _book(self, token, doctor_id, patient_id, time):
        status, body = self._call(
            "POST", "/api/appointments", token,
            {"doctor_id": doctor_id, "patient_id": patient_id,
             "date": "2026-11-28", "time": time})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "Pending")
        return body["token"]

    def test_status_update_and_history_endpoints(self):
        token = self._login("admin", "admin123")
        appt = self._book(token, "D1", 101, "10:00")
        status, body = self._call(
            "POST", f"/api/appointments/D1/{appt}/status", token,
            {"status": "Confirmed"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["appointment"]["status"], "Confirmed")
        self.assertEqual(len(body["history"]), 1)
        self.assertEqual(body["history"][0]["old_status"], "Pending")
        self.assertEqual(body["history"][0]["new_status"], "Confirmed")
        self.assertEqual(body["history"][0]["changed_by"], "admin")
        self.assertTrue(body["history"][0]["changed_at"])
        status, body = self._call(
            "GET", f"/api/appointments/D1/{appt}/history", token)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["history"]), 1)

    def test_doctor_cannot_cancel_own_queue(self):
        token = self._login("doc", "doc123")
        appt = self._book(self._login("admin", "admin123"),
                          "D1", 101, "11:00")
        status, body = self._call(
            "POST", f"/api/appointments/D1/{appt}/status", token,
            {"status": "Cancelled"})
        self.assertEqual(status, 403, body)
        # And the appointment is still Pending.
        status, body = self._call(
            "GET", f"/api/appointments/D1/{appt}/history",
            self._login("admin", "admin123"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["appointment"]["status"], "Pending")

    def test_doctor_can_check_in_own_queue(self):
        token = self._login("doc", "doc123")
        appt = self._book(self._login("admin", "admin123"),
                          "D1", 101, "12:00")
        status, body = self._call(
            "POST", f"/api/appointments/D1/{appt}/status", token,
            {"status": "Checked In"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["appointment"]["status"], "Checked In")
        self.assertEqual(body["history"][0]["changed_by"], "doc")

    def test_receptionist_can_set_any_status(self):
        token = self._login("rec", "rec123")
        appt = self._book(self._login("admin", "admin123"),
                          "D1", 101, "13:00")
        status, body = self._call(
            "POST", f"/api/appointments/D1/{appt}/status", token,
            {"status": "No Show"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["appointment"]["status"], "No Show")

    def test_double_booking_returns_400(self):
        token = self._login("admin", "admin123")
        self._book(token, "D1", 101, "14:00")
        status, body = self._call(
            "POST", "/api/appointments", token,
            {"doctor_id": "D1", "patient_id": 102,
             "date": "2026-11-28", "time": "14:00"})
        self.assertEqual(status, 400, body)
        self.assertIn("already has an appointment", body["error"])

    def test_state_and_schedule_include_status(self):
        token = self._login("admin", "admin123")
        appt = self._book(token, "D2", 102, "15:00")
        self._call("POST", f"/api/appointments/D2/{appt}/status", token,
                   {"status": "Confirmed"})
        status, body = self._call("GET", "/api/state", token)
        self.assertEqual(status, 200, body)
        d2 = next(d for d in body["state"]["doctors"] if d["id"] == "D2")
        row = next(q for q in d2["queue"] if q["token"] == appt)
        self.assertEqual(row["status"], "Confirmed")
        status, body = self._call(
            "GET", "/api/appointments?date=2026-11-28", token)
        self.assertEqual(status, 200, body)
        # Tokens are per-doctor (D1 and D2 both have an N-1), so match
        # the doctor too.
        row = next(a for a in body["appointments"]
                   if a["token"] == appt and a["doctor_id"] == "D2")
        self.assertEqual(row["status"], "Confirmed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
