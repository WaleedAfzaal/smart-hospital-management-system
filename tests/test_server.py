"""End-to-end API tests for server.py backed by hospital.Hospital."""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self) -> None:
        server.hospital = Hospital()

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
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


if __name__ == "__main__":
    unittest.main()
