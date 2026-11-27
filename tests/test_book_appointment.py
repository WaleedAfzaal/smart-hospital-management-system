"""Tests for Step 8 (Book Appointment form).

Every booking gets a unique Appointment ID (APT-0001, ...), a Type
(General / Follow-up / Emergency — Emergency books an E- token),
a Reason for Visit and Notes; patients carry Father Name, Phone and
Gender; one search finds patients by ID, name or phone; and the
booking form shows a doctor's booked times for the day. Run from the
staging root:
    python -m unittest discover -s tests -v
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


def _today():
    return datetime.date.today().isoformat()


def _add_doctors(h):
    """Two doctors in DIFFERENT departments — the booking form's
    department -> doctor filter needs that."""
    h.add_doctor("D1", "Dr. Tariq", "Neurosurgeon", 2500)
    h.add_doctor("D2", "Dr. Ayesha", "Cardiologist", 2000)
    h.add_doctor("D3", "Dr. Bilal", "Cardiologist", 1800)


class TestAptIds(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _add_doctors(self.h)
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_patient(102, "Sara", 25, "Cold")

    def test_apt_ids_are_sequential_and_unique(self):
        t1 = self.h.book_appointment("D1", 101, False, _today(), "10:00")
        t2 = self.h.book_appointment("D1", 102, False, _today(), "11:00")
        t3 = self.h.book_appointment("D2", 101, False, _today(), "09:00")
        # One hospital-wide counter — the doctor does not matter.
        self.assertEqual(
            self.h.get_appointment("D1", t1)["apt_id"], "APT-0001")
        self.assertEqual(
            self.h.get_appointment("D1", t2)["apt_id"], "APT-0002")
        self.assertEqual(
            self.h.get_appointment("D2", t3)["apt_id"], "APT-0003")
        self.assertEqual(self.h.apt_counter, 4)

    def test_failed_bookings_do_not_consume_an_id(self):
        # A bad date books nothing and takes no APT number...
        self.assertIsNone(self.h.book_appointment(
            "D1", 101, False, "2026-13-01", "10:00"))
        # ...and neither does a refused double booking.
        self.h.book_appointment("D1", 101, False, _today(), "10:00")
        self.assertIsNone(self.h.book_appointment(
            "D1", 102, False, _today(), "10:00"))
        token = self.h.book_appointment("D1", 102, False, _today(), "11:00")
        self.assertEqual(
            self.h.get_appointment("D1", token)["apt_id"], "APT-0002")

    def test_apt_ids_survive_restart(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        try:
            h = Hospital(db_path=path)
            _add_doctors(h)
            h.add_patient(101, "Ahmed", 30, "Fever")
            h.book_appointment("D1", 101, False, _today(), "10:00")
            h.book_appointment("D2", 101, False, _today(), "11:00")
            h2 = Hospital(db_path=path)
            token = h2.book_appointment("D1", 101, False, _today(), "12:00")
            self.assertEqual(
                h2.get_appointment("D1", token)["apt_id"], "APT-0003")
            # And the earlier IDs are still on their appointments.
            self.assertEqual(
                h2.get_appointment("D1", "N-1")["apt_id"], "APT-0001")
            self.assertEqual(
                h2.get_appointment("D2", "N-1")["apt_id"], "APT-0002")
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestPatientSearchByIdNamePhone(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.h.add_patient(101, "Ahmed Khan", 30, "Fever",
                           phone="0300-1234567")
        self.h.add_patient(102, "Ayesha Malik", 25, "Cold",
                           phone="0321-7654321")
        self.h.add_patient(103, "Ali Ahmed", 40, "Cough")

    def test_search_by_id(self):
        found = self.h.search_patients("101")
        self.assertEqual([p["id"] for p in found], [101])
        self.assertEqual(self.h.search_patients("999"), [])

    def test_search_by_name_still_works(self):
        found = self.h.search_patients("ahm")
        self.assertEqual([p["id"] for p in found], [101, 103])

    def test_search_by_phone_fragment(self):
        self.assertEqual([p["id"] for p in self.h.search_patients("0300")],
                         [101])
        self.assertEqual([p["id"] for p in self.h.search_patients("1234567")],
                         [101])
        # Dashes/spaces in the query do not matter — only digits count.
        self.assertEqual([p["id"] for p in self.h.search_patients("0300 123")],
                         [101])
        self.assertEqual([p["id"] for p in self.h.search_patients("0321")],
                         [102])
        self.assertEqual(self.h.search_patients("9999"), [])

    def test_empty_query_matches_nothing(self):
        self.assertEqual(self.h.search_patients(""), [])
        self.assertEqual(self.h.search_patients("   "), [])


class TestNewPatientFields(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()

    def test_add_patient_with_new_fields(self):
        self.h.add_patient(101, "Ahmed Khan", 30, "Fever",
                           father_name="Muhammad Khan",
                           phone="0300-1234567", gender="Male")
        pat = self.h.patients_map[101]
        self.assertEqual(pat["father_name"], "Muhammad Khan")
        self.assertEqual(pat["phone"], "0300-1234567")
        self.assertEqual(pat["gender"], "Male")
        # The public copy carries them too.
        found = self.h.search_patients("Ahmed")[0]
        self.assertEqual(found["father_name"], "Muhammad Khan")
        self.assertEqual(found["phone"], "0300-1234567")
        self.assertEqual(found["gender"], "Male")

    def test_new_fields_default_to_empty(self):
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        pat = self.h.patients_map[101]
        self.assertEqual(
            (pat["father_name"], pat["phone"], pat["gender"]), ("", "", ""))

    def test_bad_phone_and_gender_rejected(self):
        with self.assertRaises(ValueError):
            self.h.add_patient(101, "Ahmed", 30, "Fever",
                               phone="call me")
        with self.assertRaises(ValueError):
            self.h.add_patient(101, "Ahmed", 30, "Fever",
                               phone="0300-ABC-123")
        with self.assertRaises(ValueError):
            self.h.add_patient(101, "Ahmed", 30, "Fever",
                               gender="Unknown")
        # Nothing was added by the failed attempts.
        self.assertEqual(self.h.patients_list, [])
        # "male" (any case) is fine, and stored title-cased.
        self.h.add_patient(101, "Ahmed", 30, "Fever", gender="male",
                           phone="+92 300 1234567")
        self.assertEqual(self.h.patients_map[101]["gender"], "Male")
        self.assertEqual(self.h.patients_map[101]["phone"],
                         "+92 300 1234567")

    def test_edit_new_fields(self):
        self.h.add_patient(101, "Ahmed Khan", 30, "Fever")
        updated = self.h.edit_patient(101, father_name="Muhammad Khan",
                                      phone="0301-1112222",
                                      gender="Male")
        self.assertEqual(updated["father_name"], "Muhammad Khan")
        self.assertEqual(updated["phone"], "0301-1112222")
        self.assertEqual(updated["gender"], "Male")
        # "" clears one, None leaves it alone.
        updated = self.h.edit_patient(101, phone="")
        self.assertEqual(updated["phone"], "")
        self.assertEqual(updated["father_name"], "Muhammad Khan")
        with self.assertRaises(ValueError):
            self.h.edit_patient(101, phone="not a phone")
        self.assertEqual(self.h.patients_map[101]["phone"], "")

    def test_new_fields_survive_restart(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        try:
            h = Hospital(db_path=path)
            h.add_patient(101, "Ahmed Khan", 30, "Fever",
                          father_name="Muhammad Khan",
                          phone="0300-1234567", gender="Male")
            h2 = Hospital(db_path=path)
            pat = h2.patients_map[101]
            self.assertEqual((pat["father_name"], pat["phone"],
                              pat["gender"]),
                             ("Muhammad Khan", "0300-1234567", "Male"))
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_next_patient_id(self):
        self.assertEqual(self.h.next_patient_id(), 101)
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_patient(105, "Sara", 25, "Cold")
        self.assertEqual(self.h.next_patient_id(), 106)


class TestDepartments(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _add_doctors(self.h)

    def test_departments_are_sorted_unique_specializations(self):
        self.assertEqual(self.h.departments(),
                         ["Cardiologist", "Neurosurgeon"])

    def test_doctors_in_department(self):
        docs = self.h.doctors_in_department("Cardiologist")
        self.assertEqual([d["id"] for d in docs], ["D2", "D3"])
        self.assertEqual(self.h.doctors_in_department("ENT"), [])
        self.assertEqual(self.h.departments(), ["Cardiologist", "Neurosurgeon"])


class TestBookingTypeReasonNotesStatus(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _add_doctors(self.h)
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_patient(102, "Sara", 25, "Cold")

    def test_type_decides_the_token(self):
        t1 = self.h.book_appointment("D1", 101, False, _today(), "10:00",
                                    appt_type="Emergency")
        self.assertEqual(t1, "E-1")
        t2 = self.h.book_appointment("D1", 102, False, _today(), "11:00",
                                    appt_type="Follow-up")
        self.assertEqual(t2, "N-2")
        t3 = self.h.book_appointment("D1", 101, False, _today(), "12:00",
                                    appt_type="General")
        self.assertEqual(t3, "N-3")
        # The type is stored on the appointment, with the emergency flag.
        self.assertEqual(
            self.h.get_appointment("D1", "E-1")["appt_type"], "Emergency")
        self.assertTrue(
            self.h.get_appointment("D1", "E-1")["emergency"])
        self.assertEqual(
            self.h.get_appointment("D1", "N-2")["appt_type"], "Follow-up")
        self.assertFalse(
            self.h.get_appointment("D1", "N-2")["emergency"])

    def test_no_type_uses_the_old_emergency_flag(self):
        self.assertEqual(
            self.h.book_appointment("D1", 101, True, _today(), "10:00"),
            "E-1")
        self.assertEqual(
            self.h.book_appointment("D1", 102, False, _today(), "11:00"),
            "N-2")

    def test_reason_notes_and_status_at_booking(self):
        token = self.h.book_appointment(
            "D1", 101, False, _today(), "10:00", appt_type="Follow-up",
            reason="Follow-up checkup", notes="Bring old reports",
            status="Confirmed")
        appt = self.h.get_appointment("D1", token)
        self.assertEqual(appt["reason"], "Follow-up checkup")
        self.assertEqual(appt["notes"], "Bring old reports")
        self.assertEqual(appt["status"], "Confirmed")
        self.assertEqual(appt["appt_type"], "Follow-up")
        # Defaults: no reason/notes, status Pending.
        token2 = self.h.book_appointment("D1", 102, False, _today(), "11:00")
        appt2 = self.h.get_appointment("D1", token2)
        self.assertEqual((appt2["reason"], appt2["notes"]), ("", ""))
        self.assertEqual(appt2["status"], "Pending")
        self.assertEqual(appt2["appt_type"], "General")

    def test_bad_type_or_status_books_nothing(self):
        self.assertIsNone(self.h.book_appointment(
            "D1", 101, False, _today(), "10:00", appt_type="Urgent"))
        self.assertIsNone(self.h.book_appointment(
            "D1", 101, False, _today(), "10:00", status="Completed"))
        self.assertIsNone(self.h.book_appointment(
            "D1", 101, False, _today(), "10:00", status="Checked In"))
        self.assertEqual(len(self.h.doctors_map["D1"]["queue"].heap), 0)
        # The next real booking still gets APT-0001.
        token = self.h.book_appointment("D1", 101, False, _today(), "10:00")
        self.assertEqual(
            self.h.get_appointment("D1", token)["apt_id"], "APT-0001")


class TestBookedTimes(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _add_doctors(self.h)
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_patient(102, "Sara", 25, "Cold")

    def test_booked_times_listed_sorted(self):
        self.h.book_appointment("D1", 101, False, _today(), "11:00")
        self.h.book_appointment("D1", 102, False, _today(), "09:00")
        self.h.book_appointment("D1", 101, False, _today())  # no time
        self.h.book_appointment("D2", 101, False, _today(), "09:00")
        booked = self.h.doctor_booked_times("D1", _today())
        self.assertEqual([b["time"] for b in booked], ["09:00", "11:00"])
        self.assertEqual(booked[0]["token"], "N-2")
        self.assertEqual(booked[0]["status"], "Pending")
        self.assertEqual(booked[0]["patient_name"], "Sara")
        # The other doctor's day is separate, and another day is empty.
        self.assertEqual(len(self.h.doctor_booked_times("D2", _today())), 1)
        self.assertEqual(self.h.doctor_booked_times("D1", "2020-01-01"), [])

    def test_cancelled_times_still_listed_but_free(self):
        self.h.book_appointment("D1", 101, False, _today(), "10:00")
        self.h.update_status("D1", "N-1", "Cancelled", changed_by="admin")
        booked = self.h.doctor_booked_times("D1", _today())
        self.assertEqual(len(booked), 1)
        self.assertEqual(booked[0]["status"], "Cancelled")
        # ...and the slot can be booked again.
        token = self.h.book_appointment("D1", 102, False, _today(), "10:00")
        self.assertEqual(token, "N-2")

    def test_booked_times_bad_doctor_or_date(self):
        with self.assertRaises(ValueError):
            self.h.doctor_booked_times("D9", _today())
        with self.assertRaises(ValueError):
            self.h.doctor_booked_times("D1", "not-a-date")


class TestOldRowsAndStatusKeepsNewFields(unittest.TestCase):
    def test_old_rows_load_without_apt_fields(self):
        """A database from before Step 8 (10-field heap rows, no
        apt_id / appt_type / reason / notes columns) still loads —
        the old appointments just have no APT ID and read as General."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        try:
            h = Hospital(db_path=path)
            h.add_patient(101, "Ahmed", 30, "Fever")
            h.add_doctor("D1", "Dr. Tariq", "Neurosurgeon", 2500)
            doc = h.doctors_map["D1"]
            doc["queue"].heap.append(
                (2, 1, "N-1", 101, "Ahmed", 2500, _today(),
                 _today(), "10:00", "Pending"))
            doc["queue"].count = 1
            h.save()
            h2 = Hospital(db_path=path)
            appt = h2.get_appointment("D1", "N-1")
            self.assertIsNone(appt["apt_id"])
            self.assertEqual(appt["appt_type"], "General")
            self.assertEqual((appt["reason"], appt["notes"]), ("", ""))
            # A new booking on the same doctor gets APT-0001.
            token = h2.book_appointment("D1", 101, False, _today(), "11:00")
            self.assertEqual(
                h2.get_appointment("D1", token)["apt_id"], "APT-0001")
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_update_status_keeps_apt_id_type_reason_notes(self):
        """Step 6's update_status used to rebuild the row from its
        first 9 fields — the Step 8 fields must survive a status
        change."""
        h = Hospital()
        _add_doctors(h)
        h.add_patient(101, "Ahmed", 30, "Fever")
        token = h.book_appointment(
            "D1", 101, False, _today(), "10:00", appt_type="Emergency",
            reason="Chest pain", notes="Urgent", status="Confirmed")
        updated = h.update_status("D1", token, "Checked In",
                                  changed_by="admin")
        self.assertEqual(updated["status"], "Checked In")
        self.assertEqual(updated["apt_id"], "APT-0001")
        self.assertEqual(updated["appt_type"], "Emergency")
        self.assertEqual(updated["reason"], "Chest pain")
        self.assertEqual(updated["notes"], "Urgent")


class TestServerBookAppointment(unittest.TestCase):
    """Step 8 over HTTP: inline new-patient booking, types, slots,
    validation and role rules."""

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
        cls.cookie = cls._login("admin", "admin123")

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
        _add_doctors(server.hospital)

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

    def _add_patient(self, pid=101, **kw):
        body = {"id": pid, "name": "Ahmed Khan", "age": 30,
                "problem": "Fever"}
        body.update(kw)
        status, _ = self._request("POST", "/api/patients", body)
        self.assertEqual(status, 200)

    def test_book_with_new_patient_inline(self) -> None:
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1",
             "new_patient": {"name": "Ahmed Khan", "age": 30,
                             "problem": "Fever",
                             "father_name": "Muhammad Khan",
                             "phone": "0300-1234567", "gender": "Male"},
             "date": _today(), "time": "10:30"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["apt_id"], "APT-0001")
        self.assertEqual(payload["token"], "N-1")
        self.assertEqual(payload["appointment"]["apt_id"], "APT-0001")
        self.assertEqual(payload["appointment"]["status"], "Pending")
        self.assertEqual(payload["department"], "Neurosurgeon")
        # The patient was registered inline — ID auto-assigned (101).
        status, payload = self._request("GET", "/api/patients/101")
        self.assertEqual(status, 200)
        self.assertEqual(payload["patient"]["father_name"], "Muhammad Khan")
        self.assertEqual(payload["patient"]["phone"], "0300-1234567")
        self.assertEqual(payload["patient"]["gender"], "Male")
        # A second inline booking gets the next patient ID and APT ID.
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1",
             "new_patient": {"name": "Sara", "age": 25,
                             "problem": "Cold"},
             "date": _today(), "time": "11:00"})
        self.assertEqual(payload["apt_id"], "APT-0002")
        status, payload = self._request("GET", "/api/patients/102")
        self.assertEqual(payload["patient"]["name"], "Sara")

    def test_book_with_type_emergency_and_details(self) -> None:
        self._add_patient()
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101, "type": "Emergency",
             "reason": "Chest pain", "notes": "Bring old reports",
             "status": "Confirmed", "date": _today(), "time": "10:30"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["token"], "E-1")
        self.assertTrue(payload["emergency"])
        self.assertEqual(payload["apt_id"], "APT-0001")
        self.assertEqual(payload["status"], "Confirmed")
        appt = payload["appointment"]
        self.assertEqual(appt["appt_type"], "Emergency")
        self.assertEqual(appt["reason"], "Chest pain")
        self.assertEqual(appt["notes"], "Bring old reports")
        # The queue shows the new fields too.
        status, payload = self._request("GET", "/api/doctors")
        queue = payload["doctors"][0]["queue"]
        self.assertEqual(queue[0]["apt_id"], "APT-0001")
        self.assertEqual(queue[0]["appt_type"], "Emergency")

    def test_legacy_emergency_flag_still_works(self) -> None:
        self._add_patient()
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101, "emergency": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["token"], "E-1")
        self.assertEqual(payload["appointment"]["appt_type"], "Emergency")

    def test_slots_endpoint(self) -> None:
        self._add_patient()
        self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101,
             "date": _today(), "time": "10:30"})
        self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101,
             "date": _today(), "time": "09:00"})
        status, payload = self._request(
            "GET", f"/api/appointments/slots?doctor_id=D1&date={_today()}")
        self.assertEqual(status, 200)
        self.assertEqual([b["time"] for b in payload["booked"]],
                         ["09:00", "10:30"])
        self.assertEqual(payload["booked"][0]["status"], "Pending")
        # Another day is empty; the other doctor's day is empty.
        status, payload = self._request(
            "GET", "/api/appointments/slots?doctor_id=D1&date=2020-01-01")
        self.assertEqual(payload["booked"], [])
        status, payload = self._request(
            "GET", f"/api/appointments/slots?doctor_id=D2&date={_today()}")
        self.assertEqual(payload["booked"], [])
        # Cancelled times stay listed (but do not block the slot).
        self._request(
            "POST", "/api/appointments/D1/N-1/status",
            {"status": "Cancelled"})
        status, payload = self._request(
            "GET", f"/api/appointments/slots?doctor_id=D1&date={_today()}")
        self.assertEqual(payload["booked"][1]["status"], "Cancelled")
        # Bad doctor / bad date / missing doctor_id.
        status, payload = self._request(
            "GET", f"/api/appointments/slots?doctor_id=D9&date={_today()}")
        self.assertEqual(status, 404)
        status, payload = self._request(
            "GET", "/api/appointments/slots?doctor_id=D1&date=not-a-date")
        self.assertEqual(status, 400)
        status, payload = self._request(
            "GET", f"/api/appointments/slots?date={_today()}")
        self.assertEqual(status, 400)

    def test_booking_validation_errors(self) -> None:
        self._add_patient()
        bad_bodies = [
            # Bad type / bad status.
            {"doctor_id": "D1", "patient_id": 101, "type": "Urgent"},
            {"doctor_id": "D1", "patient_id": 101, "status": "Completed"},
            # Unknown doctor / unknown patient.
            {"doctor_id": "D9", "patient_id": 101},
            {"doctor_id": "D1", "patient_id": 999},
            # Neither patient_id nor new_patient.
            {"doctor_id": "D1"},
            # Bad date / bad time.
            {"doctor_id": "D1", "patient_id": 101, "date": "2026-13-01"},
            {"doctor_id": "D1", "patient_id": 101, "time": "25:00"},
            # Bad inline patient: no name, bad age, bad phone, bad gender.
            {"doctor_id": "D1", "new_patient": {"age": 30,
                                                "problem": "Fever"}},
            {"doctor_id": "D1", "new_patient": {"name": "X", "age": 200,
                                                "problem": "Fever"}},
            {"doctor_id": "D1", "new_patient": {"name": "X", "age": 30,
                                                "problem": "Fever",
                                                "phone": "call me"}},
            {"doctor_id": "D1", "new_patient": {"name": "X", "age": 30,
                                                "problem": "Fever",
                                                "gender": "Unknown"}},
            # Inline patient with a duplicate ID.
            {"doctor_id": "D1", "new_patient": {"id": 101, "name": "X",
                                                "age": 30,
                                                "problem": "Fever"}},
        ]
        for body in bad_bodies:
            status, payload = self._request(
                "POST", "/api/appointments", body)
            self.assertEqual(status, 400, body)
            self.assertFalse(payload["ok"])
        # A failed inline booking leaves NO stray patient behind.
        status, payload = self._request("GET", "/api/patients/102")
        self.assertEqual(status, 404)
        # And a double booking is still refused.
        self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101,
             "date": _today(), "time": "10:30"})
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101,
             "date": _today(), "time": "10:30"})
        self.assertEqual(status, 400)
        self.assertIn("already has an appointment", payload["error"])

    def test_doctor_cannot_book(self) -> None:
        self._add_patient()
        server.auth_store.create_user("doc1", "pw123", "Doctor", "D1")
        cookie = self._login("doc1", "pw123")
        self.assertTrue(cookie)
        status, payload = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101}, cookie=cookie)
        self.assertEqual(status, 403)
        status, payload = self._request(
            "GET", f"/api/appointments/slots?doctor_id=D1&date={_today()}",
            cookie=cookie)
        self.assertEqual(status, 200)

    def test_search_by_phone_and_id_over_http(self) -> None:
        self._add_patient(101, name="Ahmed Khan", phone="0300-1234567")
        self._add_patient(102, name="Ayesha Malik", phone="0321-7654321")
        status, payload = self._request("GET", "/api/patients?name=0300")
        self.assertEqual([p["id"] for p in payload["patients"]], [101])
        status, payload = self._request("GET", "/api/patients?name=102")
        self.assertEqual([p["id"] for p in payload["patients"]], [102])

    def test_patient_new_fields_over_http(self) -> None:
        status, payload = self._request(
            "POST", "/api/patients",
            {"id": 101, "name": "Ahmed Khan", "age": 30,
             "problem": "Fever", "father_name": "Muhammad Khan",
             "phone": "0300-1234567", "gender": "Male"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["patient"]["father_name"], "Muhammad Khan")
        self.assertEqual(payload["patient"]["phone"], "0300-1234567")
        self.assertEqual(payload["patient"]["gender"], "Male")
        # Bad phone / bad gender are 400s.
        for body in ({"id": 102, "name": "X", "age": 30, "problem": "Fever",
                      "phone": "call me"},
                     {"id": 102, "name": "X", "age": 30, "problem": "Fever",
                      "gender": "Unknown"}):
            status, payload = self._request("POST", "/api/patients", body)
            self.assertEqual(status, 400, body)

    def test_edit_patient_new_fields_over_http(self) -> None:
        self._add_patient()
        status, payload = self._request(
            "POST", "/api/patients/101",
            {"phone": "0301-1112222", "gender": "Female",
             "father_name": "Muhammad Khan"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["patient"]["phone"], "0301-1112222")
        self.assertEqual(payload["patient"]["gender"], "Female")
        self.assertEqual(payload["patient"]["father_name"], "Muhammad Khan")
        self.assertEqual(payload["patient"]["name"], "Ahmed Khan")
        status, payload = self._request(
            "POST", "/api/patients/101", {"phone": "not a phone"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
