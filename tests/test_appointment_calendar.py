"""Step 10 tests: appointment calendar.

Covers Hospital.calendar_appointments (date range inclusive,
doctor + department filters, status on every entry, untimed
appointments included, undated rows excluded, bad dates), the HTTP
endpoint (defaults, filters, Doctor scoping, 400s, 401) and the
CLI month grid + day view + details (menu 22).
"""

import datetime
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import AuthStore  # noqa: E402
from hospital import Hospital  # noqa: E402
import server  # noqa: E402


def _add_doctors(h):
    h.add_doctor("D1", "Dr. Tariq", "Cardiologist", 2500)
    h.add_doctor("D2", "Dr. Ayesha", "Orthopedic", 1800)
    h.add_doctor("D3", "Dr. Sana", "General Medicine", 1200)


def _seed(h):
    """3 doctors, 3 patients, appointments across 2026-11-28..30
    (timed + one untimed + one Cancelled + one old-style row) plus
    one undated pre-Step-4 row that has no day to sit on."""
    _add_doctors(h)
    h.add_patient(101, "Ahmed Khan", 30, "Fever",
                  father_name="Muhammad Khan", phone="0300-1111111",
                  gender="Male")
    h.add_patient(102, "Sara Ahmed", 25, "Cold",
                  phone="0300-2222222", gender="Female")
    h.add_patient(103, "Bilal Raza", 40, "Back pain")
    # APT-0001: D1, 2026-11-28 10:00, General, Pending.
    t1 = h.book_appointment("D1", 101, False, "2026-11-28", "10:00",
                            appt_type="General", reason="Checkup")
    # Bill for APT-0001, paid — the details page shows it.
    bill1 = h.create_bill(101, "D1", t1)
    h.billing.mark_paid(bill1["id"])
    # APT-0002: D1, 2026-11-28 11:00, Follow-up, Confirmed.
    h.book_appointment("D1", 102, False, "2026-11-28", "11:00",
                       appt_type="Follow-up", status="Confirmed")
    # APT-0003: D2, 2026-11-29 09:30, Emergency, Completed (E-1).
    h.book_appointment("D2", 103, False, "2026-11-29", "09:30",
                       appt_type="Emergency", reason="Fracture")
    h.update_status("D2", "E-1", "Completed", changed_by="admin")
    # APT-0004: D3, 2026-11-28 10:00, General, Cancelled (N-1).
    h.book_appointment("D3", 101, False, "2026-11-28", "10:00",
                       appt_type="General")
    h.update_status("D3", "N-1", "Cancelled", changed_by="admin")
    # APT-0005: D1, 2026-11-30, no fixed time, General, Pending.
    h.book_appointment("D1", 103, False, "2026-11-30", None,
                       appt_type="General")
    # Old-style row: booked before Step 8, so no APT ID (D2, N-2).
    h.doctors_map["D2"]["queue"].enqueue(
        False, 101, "Ahmed Khan", 1800, "2026-11-28", "14:00")
    # Undated pre-Step-4 row: only 7 fields, no scheduled date.
    h.doctors_map["D2"]["queue"].heap.append(
        (2, 99, "N-99", 101, "Ahmed Khan", 1800, "2026-11-20"))


class TestCalendarAppointments(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)

    def test_range_inclusive_both_ends(self):
        rows = self.h.calendar_appointments("2026-11-28", "2026-11-29")
        ids = [a["apt_id"] for a in rows]
        self.assertEqual(len(rows), 5)
        self.assertIn("APT-0001", ids)
        self.assertIn("APT-0003", ids)
        self.assertNotIn("APT-0005", ids)  # 2026-11-30 is outside

    def test_single_day_range(self):
        rows = self.h.calendar_appointments("2026-11-29", "2026-11-29")
        self.assertEqual([a["apt_id"] for a in rows], ["APT-0003"])

    def test_doctor_filter(self):
        rows = self.h.calendar_appointments(
            "2026-11-28", "2026-11-30", doctor_id="D1")
        self.assertEqual(
            [a["apt_id"] for a in rows], ["APT-0001", "APT-0002", "APT-0005"])
        self.assertTrue(all(a["doctor_id"] == "D1" for a in rows))

    def test_department_filter(self):
        rows = self.h.calendar_appointments(
            "2026-11-28", "2026-11-30", department="Orthopedic")
        self.assertEqual(len(rows), 2)  # APT-0003 + old-style D2 row
        self.assertTrue(all(a["department"] == "Orthopedic" for a in rows))

    def test_status_present_on_every_entry(self):
        rows = self.h.calendar_appointments("2026-11-28", "2026-11-30")
        self.assertEqual(len(rows), 6)
        by_id = {a["apt_id"]: a["status"] for a in rows}
        self.assertEqual(by_id["APT-0001"], "Pending")
        self.assertEqual(by_id["APT-0002"], "Confirmed")
        self.assertEqual(by_id["APT-0003"], "Completed")
        self.assertEqual(by_id["APT-0004"], "Cancelled")

    def test_untimed_included_and_sorts_last(self):
        rows = self.h.calendar_appointments("2026-11-30", "2026-11-30")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["apt_id"], "APT-0005")
        self.assertIsNone(rows[0]["appt_time"])

    def test_undated_row_never_appears(self):
        rows = self.h.calendar_appointments("2026-11-01", "2026-12-31")
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(a["token"] != "N-99" for a in rows))

    def test_time_order_within_a_day(self):
        rows = self.h.calendar_appointments("2026-11-28", "2026-11-28")
        self.assertEqual(
            [a["appt_time"] for a in rows],
            ["10:00", "10:00", "11:00", "14:00"])

    def test_bad_dates_and_reversed_range(self):
        with self.assertRaises(ValueError):
            self.h.calendar_appointments("28-11-2026", "2026-11-30")
        with self.assertRaises(ValueError):
            self.h.calendar_appointments("2026-11-28", "2026-13-01")
        with self.assertRaises(ValueError):
            self.h.calendar_appointments("2026-11-30", "2026-11-28")
        with self.assertRaises(ValueError):
            self.h.calendar_appointments(
                "2026-11-28", "2026-11-30", doctor_id="D9")

    def test_defaults_cover_today_to_30_days(self):
        today = datetime.date.today()
        far = today + datetime.timedelta(days=31)
        self.h.book_appointment("D1", 101, False, today.isoformat(),
                                "08:00", appt_type="General")
        self.h.book_appointment("D1", 101, False, far.isoformat(),
                                "08:00", appt_type="General")
        rows = self.h.calendar_appointments()
        dates = {a["appt_date"] for a in rows}
        self.assertIn(today.isoformat(), dates)
        self.assertNotIn(far.isoformat(), dates)


class TestServerAppointmentCalendar(unittest.TestCase):
    """Step 10 over HTTP: range endpoint, filters, Doctor scoping."""

    @classmethod
    def setUpClass(cls) -> None:
        server.hospital = Hospital()
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), server.HospitalRequestHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        fd, cls.auth_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(cls.auth_db)
        server.auth_store = AuthStore(cls.auth_db)
        server.auth_store.create_user("recep", "recep123", "Receptionist")
        server.auth_store.create_user("doc1", "doc123", "Doctor", "D1")
        cls.cookie = cls._login("admin", "admin123")
        cls.recep_cookie = cls._login("recep", "recep123")
        cls.doc_cookie = cls._login("doc1", "doc123")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        if os.path.exists(cls.auth_db):
            os.remove(cls.auth_db)

    @classmethod
    def _login(cls, username: str, password: str) -> str:
        url = f"http://127.0.0.1:{cls.port}/api/login"
        data = json.dumps({"username": username,
                           "password": password}).encode("utf-8")
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
        _seed(server.hospital)

    def _request(self, method: str, path: str, body: dict | None = None,
                 cookie: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie is None:
            cookie = self.cookie
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_range_returns_everything_inclusive(self):
        status, payload = self._request(
            "GET", "/api/appointments/calendar?from=2026-11-28&to=2026-11-29")
        self.assertEqual(status, 200)
        self.assertEqual(payload["from"], "2026-11-28")
        self.assertEqual(payload["to"], "2026-11-29")
        self.assertEqual(len(payload["appointments"]), 5)
        self.assertTrue(all("status" in a for a in payload["appointments"]))
        self.assertTrue(all("apt_id" in a for a in payload["appointments"]))

    def test_defaults_are_this_month(self):
        status, payload = self._request("GET", "/api/appointments/calendar")
        self.assertEqual(status, 200)
        today = datetime.date.today()
        self.assertEqual(payload["from"],
                         today.replace(day=1).isoformat())
        if today.month == 12:
            last = datetime.date(today.year + 1, 1, 1) \
                - datetime.timedelta(days=1)
        else:
            last = datetime.date(today.year, today.month + 1, 1) \
                - datetime.timedelta(days=1)
        self.assertEqual(payload["to"], last.isoformat())

    def test_doctor_and_department_filters(self):
        status, payload = self._request(
            "GET", "/api/appointments/calendar?from=2026-11-28"
            "&to=2026-11-30&doctor_id=D1")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["appointments"]), 3)
        status, payload = self._request(
            "GET", "/api/appointments/calendar?from=2026-11-28"
            "&to=2026-11-30&department=Orthopedic")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["appointments"]), 2)
        self.assertTrue(all(a["department"] == "Orthopedic"
                            for a in payload["appointments"]))

    def test_bad_dates_are_400(self):
        status, payload = self._request(
            "GET", "/api/appointments/calendar?from=28-11-2026&to=2026-11-30")
        self.assertEqual(status, 400)
        status, payload = self._request(
            "GET", "/api/appointments/calendar?from=2026-11-30&to=2026-11-28")
        self.assertEqual(status, 400)

    def test_doctor_sees_only_own_even_when_asking_for_another(self):
        status, payload = self._request(
            "GET", "/api/appointments/calendar?from=2026-11-28"
            "&to=2026-11-30&doctor_id=D2",
            cookie=self.doc_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["appointments"]), 3)
        self.assertTrue(all(a["doctor_id"] == "D1"
                            for a in payload["appointments"]))

    def test_no_login_is_401(self):
        status, payload = self._request(
            "GET", "/api/appointments/calendar?from=2026-11-28&to=2026-11-30",
            cookie="")
        self.assertEqual(status, 401)


class TestCliCalendar(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)
        self.user = {"username": "admin", "role": "Admin",
                     "doctor_id": None}

    def test_month_grid_counts_and_today_mark(self):
        import hospital as hosp_mod
        inputs = iter(["2026-11", "", "", ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.calendar_menu(self.h, "Admin", self.user)
        text = out.getvalue()
        self.assertIn("November 2026", text)
        self.assertIn("(4)", text)  # 2026-11-28: 4 appointments
        self.assertIn("(1)", text)  # 2026-11-29 and 2026-11-30: 1 each
        self.assertIn("Mo", text)

    def test_open_day_lists_time_ordered(self):
        import hospital as hosp_mod
        inputs = iter(["2026-11", "", "", "28", "", ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.calendar_menu(self.h, "Admin", self.user)
        text = out.getvalue()
        self.assertIn("2026-11-28", text)
        self.assertIn("APT-0001", text)
        self.assertIn("Ahmed Khan", text)
        self.assertIn("10:00", text)
        self.assertIn("Cancelled", text)  # APT-0004 still shows
        # Time order: 10:00 rows come before 11:00 and 14:00.
        self.assertLess(text.index("10:00"), text.index("11:00"))
        self.assertLess(text.index("11:00"), text.index("14:00"))

    def test_open_day_then_details(self):
        import hospital as hosp_mod
        inputs = iter(["2026-11", "", "", "28", "APT-0001", "B", ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.calendar_menu(self.h, "Admin", self.user)
        text = out.getvalue()
        self.assertIn("Father: Muhammad Khan", text)
        self.assertIn("Payment: Bill #1", text)
        self.assertIn("No status changes yet.", text)

    def test_bad_month_and_bad_day(self):
        import hospital as hosp_mod
        inputs = iter(["2026-13"])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.calendar_menu(self.h, "Admin", self.user)
        self.assertIn("YYYY-MM", out.getvalue())
        inputs = iter(["2026-11", "", "", "32", ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.calendar_menu(self.h, "Admin", self.user)
        self.assertIn("Pick a day in this month.", out.getvalue())

    def test_cli_doctor_sees_only_own(self):
        import hospital as hosp_mod
        user = {"username": "doc1", "role": "Doctor", "doctor_id": "D1"}
        inputs = iter(["2026-11", "", "28", "", ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.calendar_menu(self.h, "Doctor", user)
        text = out.getvalue()
        self.assertIn("APT-0001", text)
        self.assertIn("APT-0002", text)
        self.assertNotIn("APT-0004", text)
        self.assertNotIn("APT-0003", text)


if __name__ == "__main__":
    unittest.main()
