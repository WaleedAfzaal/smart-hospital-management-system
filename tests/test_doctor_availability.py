"""Step 11 tests: doctor availability & scheduling.

Covers the Availability store (working pattern validation, leave,
holidays, persistence), slot generation (hours minus breaks, duration
steps, booked marking), booking enforcement (holiday / leave /
non-working day / outside hours / break / duration-aware overlap),
reschedule re-checks, roles, the HTTP endpoints and the CLI menus.

Dates used: 2026-11-28 is a Saturday, 2026-11-29 a Sunday,
2026-11-30 a Monday, 2026-12-01 a Tuesday, 2026-12-02 a Wednesday.
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

MON = "2026-11-30"   # Monday
TUE = "2026-12-01"   # Tuesday
WED = "2026-12-02"   # Wednesday
SAT = "2026-11-28"   # Saturday
SUN = "2026-11-29"   # Sunday
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def _seed(h):
    """D1 works Mon–Fri 09:00–17:00, break 13:00–14:00, 30-minute
    consultations. D2 has no availability set (any time allowed).
    3 patients."""
    h.add_doctor("D1", "Dr. Tariq", "Cardiologist", 2500)
    h.add_doctor("D2", "Dr. Ayesha", "Orthopedic", 1800)
    h.set_doctor_availability(
        "D1", WEEKDAYS, "09:00", "17:00", 30, ["13:00-14:00"],
        role="Admin")
    h.add_patient(101, "Ahmed Khan", 30, "Fever",
                  father_name="Muhammad Khan", phone="0300-1111111",
                  gender="Male")
    h.add_patient(102, "Sara Ahmed", 25, "Cold",
                  phone="0300-2222222", gender="Female")
    h.add_patient(103, "Bilal Raza", 40, "Back pain")


class TestAvailabilityStore(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)

    def test_set_and_get_round_trip(self):
        avail = self.h.get_doctor_availability("D1")
        self.assertEqual(avail["working_days"], WEEKDAYS)
        self.assertEqual(avail["start_time"], "09:00")
        self.assertEqual(avail["end_time"], "17:00")
        self.assertEqual(avail["duration_minutes"], 30)
        self.assertEqual(avail["breaks"], [["13:00", "14:00"]])
        self.assertIsNone(self.h.get_doctor_availability("D2"))

    def test_clear_returns_to_not_set(self):
        self.assertTrue(self.h.clear_doctor_availability("D1", role="Admin"))
        self.assertIsNone(self.h.get_doctor_availability("D1"))
        self.assertFalse(self.h.clear_doctor_availability("D1", role="Admin"))

    def test_bad_patterns_are_refused(self):
        with self.assertRaises(ValueError):
            self.h.set_doctor_availability(
                "D1", ["Funday"], "09:00", "17:00", 30, role="Admin")
        with self.assertRaises(ValueError):
            self.h.set_doctor_availability(
                "D1", [], "09:00", "17:00", 30, role="Admin")
        with self.assertRaises(ValueError):
            self.h.set_doctor_availability(
                "D1", WEEKDAYS, "17:00", "09:00", 30, role="Admin")
        with self.assertRaises(ValueError):
            self.h.set_doctor_availability(
                "D1", WEEKDAYS, "09:00", "17:00", 0, role="Admin")
        with self.assertRaises(ValueError):
            self.h.set_doctor_availability(
                "D1", WEEKDAYS, "09:00", "17:00", "30", role="Admin")
        with self.assertRaises(ValueError):
            self.h.set_doctor_availability(
                "D1", WEEKDAYS, "09:00", "17:00", 30,
                ["08:00-09:00"], role="Admin")
        with self.assertRaises(ValueError):
            self.h.set_doctor_availability(
                "D1", WEEKDAYS, "09:00", "17:00", 30,
                ["12:00-13:00", "12:30-13:30"], role="Admin")
        with self.assertRaises(ValueError):
            self.h.set_doctor_availability(
                "D1", WEEKDAYS, "09:00", "17:00", 30,
                ["14:00-13:00"], role="Admin")
        with self.assertRaises(ValueError):
            self.h.set_doctor_availability(
                "D9", WEEKDAYS, "09:00", "17:00", 30, role="Admin")

    def test_leave_add_list_delete(self):
        row = self.h.add_doctor_leave(
            "D1", TUE, TUE, "Annual leave", role="Admin")
        self.assertEqual(row["doctor_id"], "D1")
        self.assertTrue(self.h.availability.is_on_leave("D1", TUE))
        self.assertFalse(self.h.availability.is_on_leave("D1", WED))
        self.assertFalse(self.h.availability.is_on_leave("D2", TUE))
        leaves = self.h.list_doctor_leaves("D1")
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0]["reason"], "Annual leave")
        self.assertTrue(self.h.delete_doctor_leave(
            row["id"], role="Admin"))
        self.assertFalse(self.h.availability.is_on_leave("D1", TUE))
        with self.assertRaises(ValueError):
            self.h.delete_doctor_leave(row["id"], role="Admin")
        with self.assertRaises(ValueError):
            self.h.add_doctor_leave("D1", WED, TUE, role="Admin")

    def test_holidays(self):
        row = self.h.add_holiday(WED, "Test Holiday", role="Admin")
        self.assertEqual(self.h.availability.holiday_on(WED),
                         "Test Holiday")
        self.assertIsNone(self.h.availability.holiday_on(TUE))
        self.assertEqual(len(self.h.list_holidays()), 1)
        with self.assertRaises(ValueError):
            self.h.add_holiday(WED, "Again", role="Admin")
        with self.assertRaises(ValueError):
            self.h.add_holiday(WED, "", role="Admin")
        self.assertTrue(self.h.delete_holiday(row["id"], role="Admin"))
        self.assertIsNone(self.h.availability.holiday_on(WED))
        with self.assertRaises(ValueError):
            self.h.delete_holiday(row["id"], role="Admin")

    def test_persistence_across_restart(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        try:
            h1 = Hospital(db_path=path)
            h1.add_doctor("D1", "Dr. Tariq", "Cardiologist", 2500)
            h1.set_doctor_availability(
                "D1", WEEKDAYS, "09:00", "17:00", 30, ["13:00-14:00"],
                role="Admin")
            h1.add_doctor_leave("D1", TUE, TUE, "Annual leave",
                                role="Admin")
            h1.add_holiday(WED, "Test Holiday", role="Admin")
            h2 = Hospital(db_path=path)
            avail = h2.get_doctor_availability("D1")
            self.assertEqual(avail["working_days"], WEEKDAYS)
            self.assertEqual(avail["breaks"], [["13:00", "14:00"]])
            self.assertEqual(len(h2.list_doctor_leaves("D1")), 1)
            self.assertEqual(len(h2.list_holidays()), 1)
            self.assertIsNotNone(h2.check_availability("D1", MON, "13:30"))
            self.assertIsNone(h2.check_availability("D1", MON, "10:00"))
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestSlotGeneration(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)

    def test_slots_are_hours_minus_breaks(self):
        slots = self.h.availability_slots("D1", MON)
        times = [s["time"] for s in slots["slots"]]
        self.assertEqual(len(times), 14)
        self.assertEqual(times[0], "09:00")
        self.assertEqual(times[-1], "16:30")
        self.assertIn("12:30", times)  # ends exactly at the break start
        self.assertIn("14:00", times)  # starts exactly at the break end
        self.assertNotIn("13:00", times)
        self.assertNotIn("13:30", times)
        self.assertTrue(all(s["available"] for s in slots["slots"]))
        self.assertIsNone(slots["unavailable_reason"])
        self.assertIsNotNone(slots["availability"])

    def test_slots_mark_booked_ones(self):
        self.h.book_appointment("D1", 101, False, MON, "09:00")
        slots = self.h.availability_slots("D1", MON)
        by_time = {s["time"]: s for s in slots["slots"]}
        self.assertFalse(by_time["09:00"]["available"])
        self.assertIsNotNone(by_time["09:00"]["token"])
        self.assertTrue(by_time["09:30"]["available"])

    def test_duration_steps(self):
        self.h.add_doctor("D3", "Dr. Sana", "General Medicine", 1200)
        self.h.set_doctor_availability(
            "D3", WEEKDAYS, "09:00", "10:00", 20, role="Admin")
        times = [s["time"] for s in
                 self.h.availability_slots("D3", MON)["slots"]]
        self.assertEqual(times, ["09:00", "09:20", "09:40"])

    def test_no_slots_on_weekend(self):
        for day in (SAT, SUN):
            slots = self.h.availability_slots("D1", day)
            self.assertEqual(slots["slots"], [])
            self.assertIn("does not work", slots["unavailable_reason"])

    def test_no_slots_on_leave_or_holiday(self):
        self.h.add_doctor_leave("D1", TUE, TUE, role="Admin")
        slots = self.h.availability_slots("D1", TUE)
        self.assertEqual(slots["slots"], [])
        self.assertIn("on leave", slots["unavailable_reason"])
        self.h.add_holiday(WED, "Test Holiday", role="Admin")
        slots = self.h.availability_slots("D1", WED)
        self.assertEqual(slots["slots"], [])
        self.assertIn("closed", slots["unavailable_reason"])

    def test_no_availability_means_no_slots_listed(self):
        slots = self.h.availability_slots("D2", MON)
        self.assertIsNone(slots["availability"])
        self.assertEqual(slots["slots"], [])
        self.assertIsNone(slots["unavailable_reason"])

    def test_unknown_doctor_or_bad_date(self):
        with self.assertRaises(ValueError):
            self.h.availability_slots("D9", MON)
        with self.assertRaises(ValueError):
            self.h.availability_slots("D1", "30-11-2026")


class TestBookingEnforcement(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)

    def test_working_day_bookings(self):
        self.assertIsNotNone(
            self.h.book_appointment("D1", 101, False, MON, "09:00"))
        self.assertIsNotNone(
            self.h.book_appointment("D1", 102, False, MON, "16:30"))
        self.assertIsNotNone(
            self.h.book_appointment("D1", 103, False, MON, "12:30"))
        self.assertIsNotNone(
            self.h.book_appointment("D1", 101, False, MON, "14:00"))

    def test_break_and_outside_hours_refused(self):
        for bad_time in ("13:00", "13:30", "08:30", "17:00", "17:30"):
            self.assertIsNone(
                self.h.book_appointment("D1", 101, False, MON, bad_time),
                bad_time)

    def test_weekend_refused(self):
        self.assertIsNone(
            self.h.book_appointment("D1", 101, False, SAT, "10:00"))
        self.assertIsNone(
            self.h.book_appointment("D1", 101, False, SUN, "10:00"))

    def test_leave_and_holiday_refused(self):
        self.h.add_doctor_leave("D1", TUE, TUE, role="Admin")
        self.assertIsNone(
            self.h.book_appointment("D1", 101, False, TUE, "10:00"))
        self.assertIsNotNone(
            self.h.book_appointment("D1", 101, False, WED, "10:00"))
        self.h.add_holiday(WED, "Test Holiday", role="Admin")
        self.assertIsNone(
            self.h.book_appointment("D1", 102, False, WED, "10:00"))
        self.assertIsNone(
            self.h.book_appointment("D2", 102, False, WED, "10:00"))

    def test_overlapping_slot_refused(self):
        self.assertIsNotNone(
            self.h.book_appointment("D1", 101, False, MON, "09:00"))
        # 09:15 overlaps the 30-minute 09:00 appointment.
        self.assertIsNone(
            self.h.book_appointment("D1", 102, False, MON, "09:15"))
        # 09:30 starts exactly when it ends — fine.
        self.assertIsNotNone(
            self.h.book_appointment("D1", 102, False, MON, "09:30"))
        # The exact same minute is still refused.
        self.assertIsNone(
            self.h.book_appointment("D1", 103, False, MON, "09:00"))

    def test_cancelled_frees_the_overlap(self):
        token = self.h.book_appointment("D1", 101, False, MON, "09:00")
        self.h.update_status("D1", token, "Cancelled",
                             changed_by="admin", role="Admin")
        self.assertIsNotNone(
            self.h.book_appointment("D1", 102, False, MON, "09:15"))

    def test_completed_still_blocks_the_overlap(self):
        token = self.h.book_appointment("D1", 101, False, MON, "09:00")
        self.h.update_status("D1", token, "Completed",
                             changed_by="admin", role="Admin")
        self.assertIsNone(
            self.h.book_appointment("D1", 102, False, MON, "09:15"))

    def test_no_availability_doctor_books_freely(self):
        self.assertIsNotNone(
            self.h.book_appointment("D2", 101, False, MON, "10:00"))
        # No duration set, so only the exact minute is guarded.
        self.assertIsNotNone(
            self.h.book_appointment("D2", 102, False, MON, "10:15"))
        self.assertIsNone(
            self.h.book_appointment("D2", 103, False, MON, "10:00"))
        # Even on a Saturday — no working days to enforce.
        self.assertIsNotNone(
            self.h.book_appointment("D2", 101, False, SAT, "10:00"))

    def test_untimed_bookings(self):
        # Untimed on a working day is fine.
        self.assertIsNotNone(
            self.h.book_appointment("D1", 101, False, MON))
        # Untimed on a non-working day is still refused.
        self.assertIsNone(
            self.h.book_appointment("D1", 101, False, SAT))
        # Untimed on a holiday is refused for every doctor.
        self.h.add_holiday(WED, "Test Holiday", role="Admin")
        self.assertIsNone(
            self.h.book_appointment("D2", 101, False, WED))

    def test_find_conflicting_with_duration(self):
        self.h.book_appointment("D1", 101, False, MON, "09:00")
        self.assertIsNotNone(self.h.find_conflicting_appointment(
            "D1", MON, "09:15", duration=30))
        self.assertIsNone(self.h.find_conflicting_appointment(
            "D1", MON, "09:30", duration=30))
        # Without a duration it is still the exact-minute check.
        self.assertIsNone(self.h.find_conflicting_appointment(
            "D1", MON, "09:15"))
        self.assertIsNotNone(self.h.find_conflicting_appointment(
            "D1", MON, "09:00"))


class TestRescheduleAvailability(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)
        self.h.book_appointment("D1", 101, False, MON, "09:00",
                                appt_type="General")
        self.h.book_appointment("D1", 102, False, MON, "10:00",
                                appt_type="General")

    def test_reschedule_into_break_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.h.edit_appointment("APT-0001", date=MON, time="13:30")
        self.assertIn("break", str(ctx.exception))

    def test_reschedule_into_overlap_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.h.edit_appointment("APT-0001", date=MON, time="10:15")
        self.assertIn("already has an appointment", str(ctx.exception))

    def test_reschedule_to_free_slot_ok(self):
        updated = self.h.edit_appointment("APT-0001", date=MON,
                                          time="10:30")
        self.assertEqual(updated["appt_time"], "10:30")
        self.assertEqual(updated["apt_id"], "APT-0001")

    def test_reschedule_to_non_working_day_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.h.edit_appointment("APT-0001", date=SAT, time="10:00")
        self.assertIn("does not work", str(ctx.exception))

    def test_reschedule_onto_own_slot_ok(self):
        updated = self.h.edit_appointment("APT-0001", date=MON,
                                          time="09:00")
        self.assertEqual(updated["appt_time"], "09:00")

    def test_reason_only_edit_skips_availability(self):
        # APT-0001 sits at 09:00 on a Monday; editing only the reason
        # is not a reschedule, so no availability check runs.
        updated = self.h.edit_appointment("APT-0001",
                                          reason="Annual checkup")
        self.assertEqual(updated["reason"], "Annual checkup")
        self.assertEqual(updated["appt_time"], "09:00")


class TestAvailabilityRoles(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)

    def test_only_admin_sets_availability(self):
        with self.assertRaises(PermissionError):
            self.h.set_doctor_availability(
                "D1", WEEKDAYS, "09:00", "17:00", 30, role="Doctor")
        with self.assertRaises(PermissionError):
            self.h.set_doctor_availability(
                "D1", WEEKDAYS, "09:00", "17:00", 30,
                role="Receptionist")
        with self.assertRaises(PermissionError):
            self.h.clear_doctor_availability("D1", role="Receptionist")

    def test_doctor_leave_only_own(self):
        row = self.h.add_doctor_leave(
            "D1", TUE, TUE, role="Doctor", user_doctor_id="D1")
        self.assertEqual(row["doctor_id"], "D1")
        with self.assertRaises(PermissionError):
            self.h.add_doctor_leave(
                "D1", TUE, TUE, role="Doctor", user_doctor_id="D2")
        with self.assertRaises(PermissionError):
            self.h.add_doctor_leave(
                "D1", TUE, TUE, role="Receptionist")
        with self.assertRaises(PermissionError):
            self.h.delete_doctor_leave(
                row["id"], role="Doctor", user_doctor_id="D2")
        self.assertTrue(self.h.delete_doctor_leave(
            row["id"], role="Doctor", user_doctor_id="D1"))

    def test_only_admin_manages_holidays(self):
        with self.assertRaises(PermissionError):
            self.h.add_holiday(WED, "Test Holiday", role="Doctor")
        with self.assertRaises(PermissionError):
            self.h.add_holiday(WED, "Test Holiday",
                               role="Receptionist")
        row = self.h.add_holiday(WED, "Test Holiday", role="Admin")
        with self.assertRaises(PermissionError):
            self.h.delete_holiday(row["id"], role="Doctor")


class TestServerAvailability(unittest.TestCase):
    """Step 11 over HTTP: availability/leave/holiday endpoints, the
    extended slots endpoint, booking refusals and role rules."""

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
        server.auth_store.create_user("doc2", "doc123", "Doctor", "D2")
        cls.cookie = cls._login("admin", "admin123")
        cls.recep_cookie = cls._login("recep", "recep123")
        cls.doc1_cookie = cls._login("doc1", "doc123")
        cls.doc2_cookie = cls._login("doc2", "doc123")

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
                cookie = resp.headers.get("Set-Cookie", "")
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

    def test_get_availability(self):
        status, payload = self._request(
            "GET", "/api/availability?doctor_id=D1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["availability"]["working_days"], WEEKDAYS)
        self.assertEqual(payload["availability"]["duration_minutes"], 30)
        self.assertEqual(payload["leaves"], [])
        status, payload = self._request(
            "GET", "/api/availability?doctor_id=D2")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["availability"])
        status, payload = self._request("GET", "/api/availability")
        self.assertEqual(status, 400)
        status, payload = self._request(
            "GET", "/api/availability?doctor_id=D9")
        self.assertEqual(status, 400)
        status, payload = self._request(
            "GET", "/api/availability?doctor_id=D1", cookie="")
        self.assertEqual(status, 401)

    def test_doctor_forced_to_own_availability(self):
        status, payload = self._request(
            "GET", "/api/availability?doctor_id=D2",
            cookie=self.doc1_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload["doctor_id"], "D1")

    def test_set_availability_roles(self):
        status, payload = self._request(
            "POST", "/api/availability",
            {"doctor_id": "D2", "working_days": WEEKDAYS,
             "start_time": "10:00", "end_time": "16:00",
             "duration_minutes": 20, "breaks": []})
        self.assertEqual(status, 200)
        self.assertEqual(payload["availability"]["duration_minutes"], 20)
        status, payload = self._request(
            "POST", "/api/availability",
            {"doctor_id": "D2", "working_days": WEEKDAYS,
             "start_time": "10:00", "end_time": "16:00",
             "duration_minutes": 20, "breaks": []},
            cookie=self.recep_cookie)
        self.assertEqual(status, 403)
        status, payload = self._request(
            "POST", "/api/availability",
            {"doctor_id": "D2", "working_days": ["Funday"],
             "start_time": "10:00", "end_time": "16:00",
             "duration_minutes": 20, "breaks": []})
        self.assertEqual(status, 400)

    def test_clear_availability(self):
        status, payload = self._request(
            "POST", "/api/availability/clear", {"doctor_id": "D1"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["cleared"])
        status, payload = self._request(
            "GET", "/api/availability?doctor_id=D1")
        self.assertIsNone(payload["availability"])
        status, payload = self._request(
            "POST", "/api/availability/clear", {"doctor_id": "D1"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["cleared"])

    def test_slots_endpoint_extended(self):
        status, payload = self._request(
            "GET", f"/api/appointments/slots?doctor_id=D1&date={MON}")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["slots"]), 14)
        self.assertEqual(payload["slots"][0]["time"], "09:00")
        self.assertTrue(payload["slots"][0]["available"])
        self.assertIsNone(payload["unavailable_reason"])
        self.assertEqual(payload["availability"]["duration_minutes"], 30)
        status, payload = self._request(
            "GET", f"/api/appointments/slots?doctor_id=D2&date={MON}")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["availability"])
        self.assertEqual(payload["slots"], [])
        self.assertIsNone(payload["unavailable_reason"])
        status, payload = self._request(
            "GET", f"/api/appointments/slots?doctor_id=D1&date={SAT}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["slots"], [])
        self.assertIn("does not work", payload["unavailable_reason"])

    def test_booking_refusals_over_http(self):
        self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101,
             "date": MON, "time": "09:00"})
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 102,
             "date": MON, "time": "09:15"})
        self.assertEqual(status, 400)
        self.assertIn("already has an appointment", payload["error"])
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 102,
             "date": MON, "time": "13:30"})
        self.assertEqual(status, 400)
        self.assertIn("break", payload["error"])
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 102,
             "date": SAT, "time": "10:00"})
        self.assertEqual(status, 400)
        self.assertIn("does not work", payload["error"])

    def test_refused_booking_leaves_no_stray_patient(self):
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1",
             "new_patient": {"name": "New Person", "age": 28,
                             "problem": "Cough"},
             "date": MON, "time": "13:30"})
        self.assertEqual(status, 400)
        self.assertIn("break", payload["error"])
        status, payload = self._request("GET", "/api/patients/104")
        self.assertEqual(status, 404)

    def test_leave_endpoints_and_roles(self):
        status, payload = self._request(
            "POST", "/api/leaves",
            {"doctor_id": "D1", "start_date": TUE, "end_date": TUE,
             "reason": "Annual leave"}, cookie=self.doc1_cookie)
        self.assertEqual(status, 200)
        leave_id = payload["leave"]["id"]
        status, payload = self._request(
            "POST", "/api/leaves",
            {"doctor_id": "D2", "start_date": TUE, "end_date": TUE},
            cookie=self.doc1_cookie)
        self.assertEqual(status, 403)
        status, payload = self._request(
            "POST", "/api/leaves",
            {"doctor_id": "D1", "start_date": TUE, "end_date": TUE},
            cookie=self.recep_cookie)
        self.assertEqual(status, 403)
        status, payload = self._request(
            "GET", "/api/availability?doctor_id=D1")
        self.assertEqual(len(payload["leaves"]), 1)
        # Another doctor cannot delete it; the owner can.
        status, payload = self._request(
            "POST", f"/api/leaves/{leave_id}/delete",
            {}, cookie=self.doc2_cookie)
        self.assertEqual(status, 403)
        status, payload = self._request(
            "POST", f"/api/leaves/{leave_id}/delete",
            {}, cookie=self.doc1_cookie)
        self.assertEqual(status, 200)

    def test_holiday_endpoints_and_roles(self):
        status, payload = self._request(
            "POST", "/api/holidays",
            {"date": WED, "name": "Test Holiday"})
        self.assertEqual(status, 200)
        holiday_id = payload["holiday"]["id"]
        status, payload = self._request(
            "POST", "/api/holidays",
            {"date": WED, "name": "Again"})
        self.assertEqual(status, 400)
        status, payload = self._request(
            "POST", "/api/holidays",
            {"date": WED, "name": "Nope"}, cookie=self.recep_cookie)
        self.assertEqual(status, 403)
        status, payload = self._request("GET", "/api/holidays")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["holidays"]), 1)
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101,
             "date": WED, "time": "10:00"})
        self.assertEqual(status, 400)
        self.assertIn("closed", payload["error"])
        status, payload = self._request(
            "POST", f"/api/holidays/{holiday_id}/delete", {})
        self.assertEqual(status, 200)
        status, payload = self._request(
            "POST", f"/api/holidays/{holiday_id}/delete", {})
        self.assertEqual(status, 400)


class TestCliAvailability(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)
        self.admin = {"username": "admin", "role": "Admin",
                      "doctor_id": None}
        self.doc1 = {"username": "doc1", "role": "Doctor",
                     "doctor_id": "D1"}

    def test_availability_menu_set_and_show(self):
        import hospital as hosp_mod
        # A hospital with doctors but NO availability set yet.
        h = Hospital()
        h.add_doctor("D1", "Dr. Tariq", "Cardiologist", 2500)
        h.add_doctor("D2", "Dr. Ayesha", "Orthopedic", 1800)
        inputs = iter(["D1", MON, "S", "Mon,Tue,Wed,Thu,Fri", "09:00",
                       "17:00", "30", "13:00-14:00"])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.availability_menu(h, "Admin", self.admin)
        text = out.getvalue()
        self.assertIn("No availability set", text)  # before the save
        self.assertIn("Availability saved for Dr. Tariq", text)
        # A second look shows the real slots for the day.
        inputs = iter(["D1", MON, ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.availability_menu(h, "Admin", self.admin)
        text = out.getvalue()
        self.assertIn("09:00 — free", text)
        self.assertIn("16:30 — free", text)
        self.assertNotIn("13:00 — free", text)
        self.assertIn("Breaks: 13:00–14:00", text)

    def test_availability_menu_doctor_read_only(self):
        import hospital as hosp_mod
        inputs = iter([MON, ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.availability_menu(self.h, "Doctor", self.doc1)
        text = out.getvalue()
        self.assertIn("Doctor: D1 (your availability)", text)
        self.assertIn("Only Admin can change availability.", text)

    def test_leave_and_holiday_menus(self):
        import hospital as hosp_mod
        inputs = iter(["1", "D1", "A", TUE, TUE, "Annual leave"])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.leave_holidays_menu(self.h, "Admin", self.admin)
        self.assertIn("Leave #1 added", out.getvalue())
        inputs = iter(["2", "A", "2026-12-25", "Christmas"])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.leave_holidays_menu(self.h, "Admin", self.admin)
        self.assertIn("Holiday #1 added", out.getvalue())
        self.assertEqual(len(self.h.list_holidays()), 1)

    def test_doctor_adds_own_leave_via_cli(self):
        import hospital as hosp_mod
        inputs = iter(["1", "A", "2026-12-05", "2026-12-05", ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.leave_holidays_menu(self.h, "Doctor", self.doc1)
        self.assertIn("Leave #1 added", out.getvalue())


if __name__ == "__main__":
    unittest.main()
