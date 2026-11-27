"""End-to-end API tests for server.py backed by hospital.Hospital.

Step 2 (Login): the API needs a login, so setUpClass creates a
throwaway AuthStore (a temp .db file — never the real hospital.db),
logs in as the default admin, and every request carries that session
cookie. Tests for the auth rules themselves live in test_auth.py and
in TestServerAuth below.
"""

import datetime
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import AuthStore  # noqa: E402
from hospital import Hospital  # noqa: E402
import server  # noqa: E402


def _add_five_doctors(h):
    """The doctors the API tests need, added explicitly — a fresh
    Hospital now starts with zero doctors."""
    h.add_doctor("D1", "Dr. Tariq", "Neurosurgeon", 2500)
    h.add_doctor("D2", "Dr. Ayesha", "Cardiologist", 2000)
    h.add_doctor("D3", "Dr. Bilal", "Orthopedic", 1800)
    h.add_doctor("D4", "Dr. Sana", "Physician", 1200)
    h.add_doctor("D5", "Dr. Kamran", "Pediatrician", 1500)


class TestServerAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        server.hospital = Hospital()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.HospitalRequestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        # Step 2: throwaway users database + admin login for all requests.
        fd, cls.auth_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(cls.auth_db)
        server.auth_store = AuthStore(cls.auth_db)
        cls.cookie = cls._login("admin", "admin123")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        if os.path.exists(cls.auth_db):
            os.remove(cls.auth_db)

    @classmethod
    def _login(cls, username: str, password: str) -> str:
        """Log in and return the session cookie (empty string on failure)."""
        url = f"http://127.0.0.1:{cls.port}/api/login"
        data = json.dumps({"username": username, "password": password}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                cookie = resp.headers.get("Set-Cookie", "") or ""
                return cookie.split(";")[0] if cookie else ""
        except urllib.error.HTTPError:
            return ""

    def setUp(self) -> None:
        server.hospital = Hospital()
        _add_five_doctors(server.hospital)

    def _request(self, method: str, path: str, body: dict | None = None,
                 cookie: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie is None:
            cookie = self.cookie
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_state_and_doctors(self) -> None:
        status, payload = self._request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        stats = payload["state"]["stats"]
        self.assertEqual(stats["patients"], 0)
        self.assertEqual(stats["doctors"], 5)
        self.assertEqual(stats["waiting"], 0)
        self.assertEqual(stats["free_beds"], 10)

    def test_patient_validation_and_crud(self) -> None:
        bad_cases = [
            {"id": 0, "name": "Ali", "age": 20, "problem": "Fever"},
            {"id": -1, "name": "Ali", "age": 20, "problem": "Fever"},
            {"id": True, "name": "Ali", "age": 20, "problem": "Fever"},
            {"id": 101, "name": "Ali", "age": -5, "problem": "Fever"},
            {"id": 101, "name": "Ali", "age": 200, "problem": "Fever"},
            {"id": 101, "name": "Ali", "age": False, "problem": "Fever"},
            {"id": 101, "name": "  ", "age": 20, "problem": "Fever"},
            {"id": 101, "name": "Ali", "age": 20, "problem": ""},
        ]
        for body in bad_cases:
            status, payload = self._request("POST", "/api/patients", body)
            self.assertEqual(status, 400)
            self.assertFalse(payload["ok"])

        status, payload = self._request(
            "POST",
            "/api/patients",
            {"id": 101, "name": "Ayesha Malik", "age": 19, "problem": "Allergy"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["patient"]["visits"], 0)

        status, payload = self._request("GET", "/api/patients/101")
        self.assertEqual(status, 200)
        self.assertEqual(payload["patient"]["name"], "Ayesha Malik")

        status, payload = self._request(
            "POST", "/api/patients/101/notes", {"note": "Prescribed antihistamine"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["patient"]["visits"], 1)
        self.assertEqual(payload["patient"]["notes"], ["Prescribed antihistamine"])

    def test_doctors_appointments_beds_pharmacy_shifts_undo(self) -> None:
        self._request(
            "POST",
            "/api/patients",
            {"id": 101, "name": "Ali Khan", "age": 32, "problem": "Fever"},
        )

        # Negative fee rejected
        status, payload = self._request(
            "POST",
            "/api/doctors",
            {"id": "D6", "name": "Dr. Test", "spec": "ENT", "fee": -100},
        )
        self.assertEqual(status, 400)

        # Book normal then emergency appointment
        status, payload = self._request(
            "POST",
            "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101, "emergency": False},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["token"], "N-1")

        status, payload = self._request(
            "POST",
            "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101, "emergency": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["token"], "E-2")

        # Undo last booking
        status, payload = self._request("POST", "/api/undo", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["undid"][0], "BOOK_APPT")

        # Assign and free bed
        status, _ = self._request("POST", "/api/beds/assign", {"bed": 1, "patient_id": 101})
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/api/beds/free", {"bed": 1})
        self.assertEqual(status, 200)

        # Pharmacy FIFO
        self._request("POST", "/api/pharmacy", {"token": "T-1"})
        status, payload = self._request("POST", "/api/pharmacy/serve", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["served"], "T-1")

        # Shifts rotate
        status, payload = self._request("POST", "/api/shifts/rotate", {})
        self.assertEqual(status, 200)
        self.assertIn("D2", payload["on_duty"])

        # Unknown API route returns JSON 404
        status, payload = self._request("GET", "/api/does-not-exist")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])


class TestServerAuth(TestServerAPI):
    """Step 2: login gate + role rules on the HTTP API."""

    def test_unauthenticated_api_calls_are_rejected(self) -> None:
        for method, path in [("GET", "/api/state"), ("GET", "/api/me"),
                             ("POST", "/api/patients")]:
            status, payload = self._request(method, path, {}, cookie="")
            self.assertEqual(status, 401, path)
            self.assertFalse(payload["ok"])

    def test_wrong_password_and_unknown_user_rejected(self) -> None:
        self.assertEqual(self._login("admin", "wrong-password"), "")
        self.assertEqual(self._login("nobody", "whatever"), "")

    def test_me_returns_the_logged_in_user(self) -> None:
        status, payload = self._request("GET", "/api/me")
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["username"], "admin")
        self.assertEqual(payload["user"]["role"], "Admin")

    def test_receptionist_cannot_add_doctor_or_undo(self) -> None:
        server.auth_store.create_user("recep1", "pw123", "Receptionist")
        cookie = self._login("recep1", "pw123")
        self.assertTrue(cookie)
        status, payload = self._request(
            "POST", "/api/doctors",
            {"id": "D9", "name": "Dr. X", "spec": "ENT", "fee": 100},
            cookie=cookie)
        self.assertEqual(status, 403)
        status, payload = self._request("POST", "/api/undo", {}, cookie=cookie)
        self.assertEqual(status, 403)

    def test_doctor_cannot_add_patient_but_can_add_note(self) -> None:
        server.auth_store.create_user("doc1", "pw123", "Doctor", "D1")
        cookie = self._login("doc1", "pw123")
        self.assertTrue(cookie)
        status, payload = self._request(
            "POST", "/api/patients",
            {"id": 201, "name": "Test", "age": 30, "problem": "Fever"},
            cookie=cookie)
        self.assertEqual(status, 403)
        # Admin adds the patient first, then the Doctor adds a note.
        self._request(
            "POST", "/api/patients",
            {"id": 201, "name": "Test", "age": 30, "problem": "Fever"})
        status, payload = self._request(
            "POST", "/api/patients/201/notes", {"note": "Checked by doctor"},
            cookie=cookie)
        self.assertEqual(status, 200)

    def test_doctor_sees_only_own_queue(self) -> None:
        server.auth_store.create_user("doc2", "pw123", "Doctor", "D2")
        cookie = self._login("doc2", "pw123")
        self._request(
            "POST", "/api/patients",
            {"id": 202, "name": "Pat", "age": 40, "problem": "Cough"})
        self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 202, "emergency": False})
        self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D2", "patient_id": 202, "emergency": False})
        status, payload = self._request("GET", "/api/doctors", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual([d["id"] for d in payload["doctors"]], ["D2"])
        self.assertEqual(payload["doctors"][0]["waiting"], 1)

    def test_admin_can_create_user_who_can_log_in(self) -> None:
        status, payload = self._request(
            "POST", "/api/users",
            {"username": "recep2", "password": "pw123",
             "role": "Receptionist", "doctor_id": ""})
        self.assertEqual(status, 200)
        cookie = self._login("recep2", "pw123")
        self.assertTrue(cookie)
        status, payload = self._request("GET", "/api/me", cookie=cookie)
        self.assertEqual(payload["user"]["role"], "Receptionist")

    def test_logout_invalidates_the_session(self) -> None:
        cookie = self._login("admin", "admin123")
        status, payload = self._request("POST", "/api/logout", {}, cookie=cookie)
        self.assertEqual(status, 200)
        status, payload = self._request("GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 401)


class TestServerBilling(TestServerAPI):
    """Step 3: billing + reports on the HTTP API."""

    def _raw_request(self, method: str, path: str, body: dict | None = None,
                     cookie: str | None = None):
        """Like _request, but returns (status, content-type, raw text) —
        the CSV export is not JSON."""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie is None:
            cookie = self.cookie
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return (resp.status,
                        resp.headers.get("Content-Type", ""),
                        resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return (err.code, err.headers.get("Content-Type", ""),
                    err.read().decode("utf-8"))

    def _booked_patient(self, pid: int = 101) -> None:
        self._request(
            "POST", "/api/patients",
            {"id": pid, "name": "Ali Khan", "age": 32, "problem": "Fever"})
        status, _ = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": pid, "emergency": False})
        self.assertEqual(status, 200)

    def test_unauthenticated_billing_calls_rejected(self) -> None:
        for method, path in [("GET", "/api/bills"), ("GET", "/api/reports"),
                             ("GET", "/api/bills/export"),
                             ("POST", "/api/bills")]:
            status, payload = self._request(method, path, {}, cookie="")
            self.assertEqual(status, 401, path)

    def test_create_bill_charge_pay_flow(self) -> None:
        self._booked_patient()
        status, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["bill"]["total"], 2500)
        self.assertEqual(payload["bill"]["status"], "Unpaid")
        bill_id = payload["bill"]["id"]

        status, payload = self._request(
            "POST", f"/api/bills/{bill_id}/charges",
            {"label": "Medicine — Paracetamol", "amount": 350})
        self.assertEqual(status, 200)
        self.assertEqual(payload["bill"]["total"], 2850)

        status, payload = self._request("GET", f"/api/bills/{bill_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["bill"]["items"]), 2)

        status, payload = self._request(
            "POST", f"/api/bills/{bill_id}/pay", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["bill"]["status"], "Paid")

        status, payload = self._request(
            "POST", f"/api/bills/{bill_id}/unpay", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["bill"]["status"], "Unpaid")

    def test_create_bill_bad_token_and_duplicate_rejected(self) -> None:
        self._booked_patient()
        status, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-9"})
        self.assertEqual(status, 400)
        self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"})
        status, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"})
        self.assertEqual(status, 400)

    def test_bad_charge_rejected(self) -> None:
        self._booked_patient()
        _, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"})
        bill_id = payload["bill"]["id"]
        for body in [{"label": "Medicine", "amount": 0},
                     {"label": "Medicine", "amount": -5},
                     {"label": "", "amount": 100},
                     {"label": "Medicine", "amount": "abc"}]:
            status, payload = self._request(
                "POST", f"/api/bills/{bill_id}/charges", body)
            self.assertEqual(status, 400, body)
        status, payload = self._request(
            "POST", "/api/bills/999/charges",
            {"label": "Medicine", "amount": 100})
        self.assertEqual(status, 400)

    def test_doctor_read_only_and_receptionist_can_bill(self) -> None:
        self._booked_patient()
        server.auth_store.create_user("doc1", "pw123", "Doctor", "D1")
        doc_cookie = self._login("doc1", "pw123")
        status, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"},
            cookie=doc_cookie)
        self.assertEqual(status, 403)
        status, payload = self._request("GET", "/api/bills", cookie=doc_cookie)
        self.assertEqual(status, 200)
        status, payload = self._request("GET", "/api/reports", cookie=doc_cookie)
        self.assertEqual(status, 200)

        server.auth_store.create_user("recep1", "pw123", "Receptionist")
        recep_cookie = self._login("recep1", "pw123")
        status, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"},
            cookie=recep_cookie)
        self.assertEqual(status, 200)
        status, payload = self._request(
            "POST", f"/api/bills/{payload['bill']['id']}/pay", {},
            cookie=recep_cookie)
        self.assertEqual(status, 200)

    def test_reports_endpoint_matches_data(self) -> None:
        self._booked_patient()
        _, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"})
        bill_id = payload["bill"]["id"]
        self._request("POST", f"/api/bills/{bill_id}/charges",
                      {"label": "Medicine", "amount": 500})
        self._request("POST", f"/api/bills/{bill_id}/pay", {})
        status, payload = self._request("GET", "/api/reports")
        self.assertEqual(status, 200)
        rep = payload["report"]
        self.assertEqual(rep["patients_today"], 1)
        self.assertEqual(rep["appointments_today"], 1)
        self.assertEqual(rep["billed_today"], 3000)
        self.assertEqual(rep["collected_today"], 3000)
        self.assertEqual(rep["unpaid_total"], 0)
        self.assertEqual(rep["per_doctor"][0]["doctor_id"], "D1")
        self.assertEqual(rep["per_doctor"][0]["appointments_today"], 1)

        status, payload = self._request("GET", "/api/reports?date=2020-01-01")
        self.assertEqual(status, 200)
        self.assertEqual(payload["report"]["billed_today"], 0)
        status, payload = self._request("GET", "/api/reports?date=not-a-date")
        self.assertEqual(status, 400)

    def test_csv_export_download(self) -> None:
        self._booked_patient()
        _, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"})
        status, content_type, text = self._raw_request(
            "GET", "/api/bills/export")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", content_type)
        self.assertIn("Bill ID", text)
        self.assertIn("Ali Khan", text)
        self.assertIn("2500", text)
        status, _, _ = self._raw_request("GET", "/api/bills/export", cookie="")
        self.assertEqual(status, 401)

    def test_dashboard_stats_include_today_money(self) -> None:
        self._booked_patient()
        self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"})
        status, payload = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(payload["stats"]["billed_today"], 2500)
        self.assertEqual(payload["stats"]["collected_today"], 0)
        self.assertEqual(payload["stats"]["unpaid_total"], 2500)


class TestServerAppointments(TestServerAPI):
    """Step 4: booking with date/time + the schedule endpoint."""

    def _book(self, doctor_id="D1", pid=101, emergency=False,
              date=None, time=None):
        body = {"doctor_id": doctor_id, "patient_id": pid,
                "emergency": emergency}
        if date is not None:
            body["date"] = date
        if time is not None:
            body["time"] = time
        return self._request("POST", "/api/appointments", body)

    def test_book_with_date_and_time(self) -> None:
        self._request(
            "POST", "/api/patients",
            {"id": 101, "name": "Ali Khan", "age": 32, "problem": "Fever"})
        status, payload = self._book(date="2026-11-28", time="10:30")
        self.assertEqual(status, 200)
        self.assertEqual(payload["token"], "N-1")
        self.assertEqual(payload["date"], "2026-11-28")
        self.assertEqual(payload["time"], "10:30")

        # The queue shows the scheduled date/time too.
        status, payload = self._request("GET", "/api/doctors")
        queue = payload["doctors"][0]["queue"]
        self.assertEqual(queue[0]["appt_date"], "2026-11-28")
        self.assertEqual(queue[0]["appt_time"], "10:30")

    def test_book_without_date_defaults_to_today(self) -> None:
        self._request(
            "POST", "/api/patients",
            {"id": 101, "name": "Ali Khan", "age": 32, "problem": "Fever"})
        status, payload = self._book()
        self.assertEqual(status, 200)
        self.assertEqual(payload["date"],
                         datetime.date.today().isoformat())
        self.assertIsNone(payload["time"])

    def test_book_with_bad_date_or_time_rejected(self) -> None:
        self._request(
            "POST", "/api/patients",
            {"id": 101, "name": "Ali Khan", "age": 32, "problem": "Fever"})
        for body in ({"date": "2026-13-01", "time": "10:30"},
                     {"date": "2026-11-28", "time": "25:00"},
                     {"date": "28-11-2026"},
                     {"date": "2026-02-30", "time": "10:30"}):
            status, payload = self._book(**body)
            self.assertEqual(status, 400, body)
            self.assertFalse(payload["ok"])
        # Nothing was booked by the rejected attempts.
        status, payload = self._request("GET", "/api/doctors")
        self.assertEqual(payload["doctors"][0]["waiting"], 0)

    def test_schedule_endpoint_filters_by_date(self) -> None:
        self._request(
            "POST", "/api/patients",
            {"id": 101, "name": "Ali Khan", "age": 32, "problem": "Fever"})
        self._book(date="2026-11-28", time="10:30")
        self._book(date="2026-11-29", time="09:00")

        status, payload = self._request(
            "GET", "/api/appointments?date=2026-11-28")
        self.assertEqual(status, 200)
        self.assertEqual(payload["date"], "2026-11-28")
        self.assertEqual(len(payload["appointments"]), 1)
        self.assertEqual(payload["appointments"][0]["token"], "N-1")
        self.assertEqual(payload["appointments"][0]["appt_time"], "10:30")

        status, payload = self._request(
            "GET", "/api/appointments?date=2026-11-29")
        self.assertEqual(len(payload["appointments"]), 1)
        self.assertEqual(payload["appointments"][0]["token"], "N-2")

        status, payload = self._request(
            "GET", "/api/appointments?date=2020-01-01")
        self.assertEqual(payload["appointments"], [])

        # Bad date on the schedule endpoint is a 400.
        status, payload = self._request(
            "GET", "/api/appointments?date=not-a-date")
        self.assertEqual(status, 400)

    def test_schedule_endpoint_filters_by_doctor(self) -> None:
        self._request(
            "POST", "/api/patients",
            {"id": 101, "name": "Ali Khan", "age": 32, "problem": "Fever"})
        self._book(doctor_id="D1", date="2026-11-28", time="10:30")
        self._book(doctor_id="D2", date="2026-11-28", time="09:00")

        status, payload = self._request(
            "GET", "/api/appointments?date=2026-11-28&doctor_id=D2")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["appointments"]), 1)
        self.assertEqual(payload["appointments"][0]["doctor_id"], "D2")

        status, payload = self._request(
            "GET", "/api/appointments?date=2026-11-28&doctor_id=D9")
        self.assertEqual(status, 404)

    def test_schedule_endpoint_needs_login(self) -> None:
        status, payload = self._request(
            "GET", "/api/appointments?date=2026-11-28", cookie="")
        self.assertEqual(status, 401)

    def test_doctor_sees_only_own_schedule(self) -> None:
        self._request(
            "POST", "/api/patients",
            {"id": 101, "name": "Ali Khan", "age": 32, "problem": "Fever"})
        self._book(doctor_id="D1", date="2026-11-28", time="10:30")
        self._book(doctor_id="D2", date="2026-11-28", time="09:00")
        server.auth_store.create_user("doc2", "pw123", "Doctor", "D2")
        cookie = self._login("doc2", "pw123")
        self.assertTrue(cookie)
        # Asking for D1 still only shows D2's appointments.
        status, payload = self._request(
            "GET", "/api/appointments?date=2026-11-28&doctor_id=D1",
            cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["appointments"]), 1)
        self.assertEqual(payload["appointments"][0]["doctor_id"], "D2")

    def test_billing_still_works_from_dated_token(self) -> None:
        self._request(
            "POST", "/api/patients",
            {"id": 101, "name": "Ali Khan", "age": 32, "problem": "Fever"})
        self._book(date="2026-11-28", time="10:30")
        status, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["bill"]["total"], 2500)


class TestServerPatients(TestServerAPI):
    """Step 5: patient search by name, edit, and delete on the HTTP API."""

    def _add(self, pid, name, age=30, problem="Fever"):
        status, _ = self._request(
            "POST", "/api/patients",
            {"id": pid, "name": name, "age": age, "problem": problem})
        self.assertEqual(status, 200)

    def test_search_by_name_partial_and_case_insensitive(self) -> None:
        self._add(101, "Ahmed Khan")
        self._add(102, "Ayesha Malik")
        self._add(103, "Ali Ahmed")
        status, payload = self._request("GET", "/api/patients?name=ahm")
        self.assertEqual(status, 200)
        self.assertEqual([p["id"] for p in payload["patients"]], [101, 103])
        status, payload = self._request("GET", "/api/patients?name=AYESHA")
        self.assertEqual([p["id"] for p in payload["patients"]], [102])

    def test_search_no_match_returns_empty_list(self) -> None:
        self._add(101, "Ahmed Khan")
        status, payload = self._request("GET", "/api/patients?name=Zubair")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["patients"], [])

    def test_search_needs_login(self) -> None:
        status, payload = self._request(
            "GET", "/api/patients?name=ah", cookie="")
        self.assertEqual(status, 401)

    def test_edit_patient_partial_update(self) -> None:
        self._add(101, "Ahmed Khan", 30, "Fever")
        status, payload = self._request(
            "POST", "/api/patients/101", {"age": 31})
        self.assertEqual(status, 200)
        self.assertEqual(payload["patient"]["age"], 31)
        self.assertEqual(payload["patient"]["name"], "Ahmed Khan")
        self.assertEqual(payload["patient"]["problem"], "Fever")
        # And the change shows up on a later read.
        status, payload = self._request("GET", "/api/patients/101")
        self.assertEqual(payload["patient"]["age"], 31)

    def test_edit_patient_bad_values_rejected(self) -> None:
        self._add(101, "Ahmed Khan")
        for body in ({"name": ""}, {"name": "   "}, {"problem": ""},
                     {"age": -1}, {"age": 200}, {"age": "old"}):
            status, payload = self._request(
                "POST", "/api/patients/101", body)
            self.assertEqual(status, 400, body)
        status, payload = self._request(
            "POST", "/api/patients/999", {"age": 31})
        self.assertEqual(status, 404)

    def test_edit_patient_role_rules(self) -> None:
        self._add(101, "Ahmed Khan")
        server.auth_store.create_user("doc-edit", "pw123", "Doctor", "D1")
        doc_cookie = self._login("doc-edit", "pw123")
        self.assertTrue(doc_cookie)
        status, _ = self._request(
            "POST", "/api/patients/101", {"age": 31}, cookie=doc_cookie)
        self.assertEqual(status, 403)
        server.auth_store.create_user("recep-edit", "pw123", "Receptionist")
        recep_cookie = self._login("recep-edit", "pw123")
        self.assertTrue(recep_cookie)
        status, payload = self._request(
            "POST", "/api/patients/101", {"age": 31}, cookie=recep_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload["patient"]["age"], 31)

    def test_delete_patient_admin_only(self) -> None:
        self._add(101, "Ahmed Khan")
        server.auth_store.create_user("recep-del", "pw123", "Receptionist")
        recep_cookie = self._login("recep-del", "pw123")
        status, _ = self._request(
            "DELETE", "/api/patients/101", cookie=recep_cookie)
        self.assertEqual(status, 403)
        server.auth_store.create_user("doc-del", "pw123", "Doctor", "D1")
        doc_cookie = self._login("doc-del", "pw123")
        status, _ = self._request(
            "DELETE", "/api/patients/101", cookie=doc_cookie)
        self.assertEqual(status, 403)
        # And without a login at all.
        status, _ = self._request("DELETE", "/api/patients/101", cookie="")
        self.assertEqual(status, 401)
        # The patient is still there after the rejected attempts.
        status, payload = self._request("GET", "/api/patients/101")
        self.assertEqual(status, 200)

    def test_delete_patient_removes_record_notes_appointments_keeps_bills(self) -> None:
        self._add(101, "Ahmed Khan")
        self._add(102, "Sara")
        self._request(
            "POST", "/api/patients/101/notes", {"note": "First visit"})
        self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101, "emergency": False})
        self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 102, "emergency": False})
        _, payload = self._request(
            "POST", "/api/bills",
            {"patient_id": 101, "doctor_id": "D1", "token": "N-1"})
        bill_id = payload["bill"]["id"]
        self._request(
            "POST", "/api/beds/assign", {"bed": 3, "patient_id": 101})

        status, payload = self._request("DELETE", "/api/patients/101")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["deleted"]["notes_removed"], 1)
        self.assertEqual(len(payload["deleted"]["appointments_removed"]), 1)
        self.assertEqual(payload["deleted"]["beds_freed"], [3])

        status, payload = self._request("GET", "/api/patients/101")
        self.assertEqual(status, 404)
        # The other patient and their appointment are untouched.
        status, payload = self._request("GET", "/api/patients/102")
        self.assertEqual(status, 200)
        status, payload = self._request("GET", "/api/doctors")
        self.assertEqual(payload["doctors"][0]["waiting"], 1)
        # The bed is free again.
        status, payload = self._request("GET", "/api/beds")
        self.assertEqual(payload["beds"][2]["status"], "Free")
        # The bill is kept.
        status, payload = self._request("GET", f"/api/bills/{bill_id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["bill"]["patient_name"], "Ahmed Khan")

    def test_delete_missing_patient_is_404(self) -> None:
        status, payload = self._request("DELETE", "/api/patients/999")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
