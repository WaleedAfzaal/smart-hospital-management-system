"""End-to-end API tests for server.py backed by hospital.Hospital.

Step 2 (Login): the API needs a login, so setUpClass creates a
throwaway AuthStore (a temp .db file — never the real hospital.db),
logs in as the default admin, and every request carries that session
cookie. Tests for the auth rules themselves live in test_auth.py and
in TestServerAuth below.
"""

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

    def test_state_and_starter_doctors(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
