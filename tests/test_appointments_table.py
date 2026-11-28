"""Step 9 tests: appointments table + details page.

Covers Hospital.list_appointments (search, filters, sort,
pagination), get_appointment_by_id / appointment_detail (full
fields, payment status, history), edit_appointment (field changes,
reschedule + double-booking guard, type flip (token kept, queue
re-ordered), roles),
CSV + PDF exports, and the HTTP endpoints (list, detail, edit,
cancel, exports, role rules) — plus the CLI table menu.
"""

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
from pdf_export import appointments_pdf  # noqa: E402


def _add_doctors(h):
    h.add_doctor("D1", "Dr. Tariq", "Cardiologist", 2500)
    h.add_doctor("D2", "Dr. Ayesha", "Orthopedic", 1800)
    h.add_doctor("D3", "Dr. Sana", "General Medicine", 1200)


def _seed(h):
    """3 doctors, 3 patients, 5 APT appointments + 1 old-style row
    (no APT ID) + bills for the payment-status checks. Returns the
    tokens booked, in APT order."""
    _add_doctors(h)
    h.add_patient(101, "Ahmed Khan", 30, "Fever",
                  father_name="Muhammad Khan", phone="0300-1111111",
                  gender="Male")
    h.add_patient(102, "Sara Ahmed", 25, "Cold",
                  phone="0300-2222222", gender="Female")
    h.add_patient(103, "Bilal Raza", 40, "Back pain")
    tokens = []
    # APT-0001: D1, 2026-11-28 10:00, General, Pending.
    tokens.append(h.book_appointment(
        "D1", 101, False, "2026-11-28", "10:00",
        appt_type="General", reason="Checkup"))
    # APT-0002: D1, 2026-11-28 11:00, Follow-up, Confirmed.
    tokens.append(h.book_appointment(
        "D1", 102, False, "2026-11-28", "11:00",
        appt_type="Follow-up", status="Confirmed"))
    # APT-0003: D2, 2026-11-29 09:30, Emergency, Completed (E-1).
    tokens.append(h.book_appointment(
        "D2", 103, False, "2026-11-29", "09:30",
        appt_type="Emergency", reason="Fracture"))
    h.update_status("D2", "E-1", "Completed", changed_by="admin")
    # APT-0004: D3, 2026-11-28 10:00, General, Cancelled (N-1).
    tokens.append(h.book_appointment(
        "D3", 101, False, "2026-11-28", "10:00", appt_type="General"))
    h.update_status("D3", "N-1", "Cancelled", changed_by="admin")
    # APT-0005: D1, 2026-11-30, no fixed time, General, Pending.
    tokens.append(h.book_appointment(
        "D1", 103, False, "2026-11-30", None, appt_type="General"))
    # Old-style row: booked before Step 8, so no APT ID (D2, N-2).
    h.doctors_map["D2"]["queue"].enqueue(
        False, 101, "Ahmed Khan", 1800, "2026-11-28", "14:00")
    # Bills: APT-0001 paid, APT-0003 unpaid.
    bill1 = h.create_bill(101, "D1", tokens[0])
    h.billing.mark_paid(bill1["id"])
    h.create_bill(103, "D2", tokens[2])
    return tokens


class TestListAppointments(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)

    def _ids(self, result):
        return [a["apt_id"] for a in result["appointments"]]

    def test_no_filters_lists_everything(self):
        result = self.h.list_appointments()
        self.assertEqual(result["total"], 6)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["pages"], 1)
        self.assertEqual(len(result["appointments"]), 6)

    def test_filter_by_date(self):
        result = self.h.list_appointments(date="2026-11-28")
        self.assertEqual(result["total"], 4)
        self.assertEqual(
            set(self._ids(result)),
            {"APT-0001", "APT-0002", "APT-0004", None})

    def test_filter_by_doctor(self):
        result = self.h.list_appointments(doctor_id="D1")
        self.assertEqual(result["total"], 3)
        self.assertEqual(set(self._ids(result)),
                         {"APT-0001", "APT-0002", "APT-0005"})

    def test_filter_by_department(self):
        result = self.h.list_appointments(department="Orthopedic")
        self.assertEqual(result["total"], 2)
        self.assertEqual(set(self._ids(result)), {"APT-0003", None})
        self.assertEqual(
            self.h.list_appointments(department="Cardiologist")["total"], 3)

    def test_filter_by_status(self):
        self.assertEqual(
            self.h.list_appointments(status="Pending")["total"], 3)
        self.assertEqual(
            self.h.list_appointments(status="Cancelled")["total"], 1)
        self.assertEqual(
            self.h.list_appointments(status="Completed")["total"], 1)

    def test_filters_combine(self):
        result = self.h.list_appointments(date="2026-11-28",
                                          status="Pending")
        self.assertEqual(result["total"], 2)
        self.assertEqual(set(self._ids(result)), {"APT-0001", None})

    def test_search_by_name(self):
        # "ahmed" is in Ahmed Khan AND Sara Ahmed.
        result = self.h.list_appointments(search="ahmed")
        self.assertEqual(result["total"], 4)
        result = self.h.list_appointments(search="bilal")
        self.assertEqual(result["total"], 2)
        self.assertEqual(set(self._ids(result)), {"APT-0003", "APT-0005"})

    def test_search_by_apt_id_and_token(self):
        self.assertEqual(
            self.h.list_appointments(search="APT-0003")["total"], 1)
        self.assertEqual(
            self.h.list_appointments(search="apt-0002")["total"], 1)
        self.assertEqual(
            self.h.list_appointments(search="E-1")["total"], 1)
        self.assertEqual(
            self.h.list_appointments(search="zzz")["total"], 0)

    def test_sort_date(self):
        result = self.h.list_appointments(sort="date", order="asc")
        self.assertEqual(result["appointments"][0]["apt_id"], "APT-0001")
        self.assertEqual(result["appointments"][-1]["apt_id"], "APT-0005")
        desc = self.h.list_appointments(sort="date", order="desc")
        self.assertEqual(desc["appointments"][0]["apt_id"], "APT-0005")

    def test_sort_time(self):
        result = self.h.list_appointments(sort="time", order="asc")
        times = [a["appt_time"] for a in result["appointments"]]
        self.assertEqual(times, ["09:30", "10:00", "10:00", "11:00",
                                 "14:00", None])

    def test_sort_patient_name(self):
        result = self.h.list_appointments(sort="patient_name", order="asc")
        names = [a["patient_name"] for a in result["appointments"]]
        self.assertEqual(names, ["Ahmed Khan", "Ahmed Khan", "Ahmed Khan",
                                 "Bilal Raza", "Bilal Raza", "Sara Ahmed"])

    def test_sort_doctor_name(self):
        result = self.h.list_appointments(sort="doctor_name", order="asc")
        doctors = [a["doctor_name"] for a in result["appointments"]]
        self.assertEqual(doctors, ["Dr. Ayesha", "Dr. Ayesha", "Dr. Sana",
                                   "Dr. Tariq", "Dr. Tariq", "Dr. Tariq"])

    def test_sort_status(self):
        result = self.h.list_appointments(sort="status", order="asc")
        statuses = [a["status"] for a in result["appointments"]]
        self.assertEqual(statuses, ["Cancelled", "Completed", "Confirmed",
                                    "Pending", "Pending", "Pending"])

    def test_pagination(self):
        page1 = self.h.list_appointments(per_page=2, page=1)
        page2 = self.h.list_appointments(per_page=2, page=2)
        page3 = self.h.list_appointments(per_page=2, page=3)
        self.assertEqual((page1["total"], page1["pages"]), (6, 3))
        self.assertEqual(len(page1["appointments"]), 2)
        self.assertEqual(len(page2["appointments"]), 2)
        self.assertEqual(len(page3["appointments"]), 2)
        ids = (self._ids(page1) + self._ids(page2) + self._ids(page3))
        self.assertEqual(sorted(i for i in ids if i),
                         ["APT-0001", "APT-0002", "APT-0003",
                          "APT-0004", "APT-0005"])
        # A page past the end clamps to the last page.
        self.assertEqual(
            self.h.list_appointments(per_page=2, page=99)["page"], 3)

    def test_bad_params_raise(self):
        with self.assertRaises(ValueError):
            self.h.list_appointments(sort="bogus")
        with self.assertRaises(ValueError):
            self.h.list_appointments(order="sideways")
        with self.assertRaises(ValueError):
            self.h.list_appointments(date="28-11-2026")
        with self.assertRaises(ValueError):
            self.h.list_appointments(doctor_id="D9")
        with self.assertRaises(ValueError):
            self.h.list_appointments(status="Bogus")
        with self.assertRaises(ValueError):
            self.h.list_appointments(page="abc")


class TestAppointmentDetail(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.tokens = _seed(self.h)

    def test_detail_by_apt_id(self):
        detail = self.h.appointment_detail("APT-0001")
        self.assertIsNotNone(detail)
        a = detail["appointment"]
        self.assertEqual(a["token"], "N-1")
        self.assertEqual(a["patient_name"], "Ahmed Khan")
        self.assertEqual(a["doctor_name"], "Dr. Tariq")
        self.assertEqual(a["department"], "Cardiologist")
        self.assertEqual(a["appt_date"], "2026-11-28")
        self.assertEqual(a["appt_time"], "10:00")
        self.assertEqual(a["appt_type"], "General")
        self.assertEqual(a["reason"], "Checkup")
        self.assertEqual(a["status"], "Pending")
        self.assertEqual(a["fee"], 2500)
        self.assertEqual(a["booked_date"], "2026-11-28")
        pat = detail["patient"]
        self.assertEqual(pat["father_name"], "Muhammad Khan")
        self.assertEqual(pat["phone"], "0300-1111111")
        self.assertEqual(pat["gender"], "Male")
        self.assertEqual(detail["doctor"]["spec"], "Cardiologist")
        self.assertEqual(detail["history"], [])

    def test_detail_payment_status(self):
        paid = self.h.appointment_detail("APT-0001")
        self.assertTrue(paid["payment"]["billed"])
        self.assertEqual(paid["payment"]["status"], "Paid")
        self.assertEqual(paid["payment"]["total"], 2500)
        unpaid = self.h.appointment_detail("APT-0003")
        self.assertTrue(unpaid["payment"]["billed"])
        self.assertEqual(unpaid["payment"]["status"], "Unpaid")
        self.assertEqual(unpaid["payment"]["total"], 1800)
        not_billed = self.h.appointment_detail("APT-0002")
        self.assertFalse(not_billed["payment"]["billed"])
        self.assertIsNone(not_billed["payment"]["bill_id"])

    def test_detail_history(self):
        detail = self.h.appointment_detail("APT-0003")
        self.assertEqual(len(detail["history"]), 1)
        entry = detail["history"][0]
        self.assertEqual(entry["old_status"], "Pending")
        self.assertEqual(entry["new_status"], "Completed")
        self.assertEqual(entry["changed_by"], "admin")

    def test_detail_unknown_is_none(self):
        self.assertIsNone(self.h.appointment_detail("APT-9999"))
        self.assertIsNone(self.h.appointment_detail(""))
        self.assertIsNone(self.h.appointment_detail(None))

    def test_detail_old_row_by_doctor_token(self):
        # Rows booked before Step 8 have no APT ID — "D2:N-2" opens them.
        detail = self.h.appointment_detail("D2:N-2")
        self.assertIsNotNone(detail)
        self.assertIsNone(detail["appointment"]["apt_id"])
        self.assertEqual(detail["appointment"]["patient_name"], "Ahmed Khan")
        self.assertEqual(detail["appointment"]["appt_time"], "14:00")
        doc, appt = self.h.get_appointment_by_id("D2:N-2")
        self.assertEqual(doc["id"], "D2")
        self.assertIsNone(self.h.get_appointment_by_id("D9:N-1")[0])


class TestEditAppointment(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.tokens = _seed(self.h)

    def test_edit_reason_and_notes(self):
        updated = self.h.edit_appointment(
            "APT-0001", reason="Follow-up check", notes="Bring reports")
        self.assertEqual(updated["reason"], "Follow-up check")
        self.assertEqual(updated["notes"], "Bring reports")
        self.assertEqual(updated["apt_id"], "APT-0001")
        self.assertEqual(updated["token"], "N-1")
        self.assertEqual(updated["status"], "Pending")
        self.assertEqual(updated["appt_date"], "2026-11-28")

    def test_reschedule(self):
        updated = self.h.edit_appointment(
            "APT-0001", date="2026-11-29", time="12:00")
        self.assertEqual(updated["appt_date"], "2026-11-29")
        self.assertEqual(updated["appt_time"], "12:00")
        self.assertEqual(updated["apt_id"], "APT-0001")

    def test_reschedule_onto_taken_slot_refused(self):
        # APT-0002 (D1) is at 2026-11-28 11:00 and still active.
        with self.assertRaises(ValueError) as ctx:
            self.h.edit_appointment("APT-0001", date="2026-11-28",
                                    time="11:00")
        self.assertIn("already has an appointment", str(ctx.exception))
        # The appointment itself never blocks its own slot.
        self.h.edit_appointment("APT-0001", date="2026-11-28",
                                time="10:00")
        # A Cancelled appointment's slot is free again (APT-0004, D3).
        self.h.edit_appointment("APT-0001", date="2026-11-28",
                                time="10:00")  # own slot, still fine

    def test_reschedule_onto_cancelled_slot_allowed(self):
        # Move APT-0005 (D1) onto D3's cancelled 10:00 slot — but that
        # is a different doctor, so instead use D1's own cancelled
        # slot: cancel APT-0001, then move APT-0002 onto 10:00.
        self.h.update_status("D1", "N-1", "Cancelled", changed_by="admin")
        updated = self.h.edit_appointment(
            "APT-0002", date="2026-11-28", time="10:00")
        self.assertEqual(updated["appt_time"], "10:00")

    def test_type_change_same_priority_keeps_token(self):
        updated = self.h.edit_appointment("APT-0001", appt_type="Follow-up")
        self.assertEqual(updated["appt_type"], "Follow-up")
        self.assertEqual(updated["token"], "N-1")
        self.assertEqual(updated["apt_id"], "APT-0001")

    def test_type_flip_keeps_token_and_reorders(self):
        # Give APT-0001 a status change first, so there is history
        # that must survive the flip.
        self.h.update_status("D1", "N-1", "Confirmed",
                             changed_by="admin")
        updated = self.h.edit_appointment("APT-0001", appt_type="Emergency")
        self.assertEqual(updated["appt_type"], "Emergency")
        self.assertEqual(updated["apt_id"], "APT-0001")
        # The status is preserved through the flip (Confirmed, not
        # reset to Pending).
        self.assertEqual(updated["status"], "Confirmed")
        self.assertEqual(updated["reason"], "Checkup")
        self.assertTrue(updated["emergency"])
        # The TOKEN STAYS N-1 — only the priority changes, so the
        # bill and the status history stay linked. The queue
        # re-orders: the flipped row is now first.
        self.assertEqual(updated["token"], "N-1")
        queue = self.h.doctors_map["D1"]["queue"]
        self.assertEqual(queue.heap[0][2], "N-1")
        self.assertEqual(queue.heap[0][0], 1)
        # The paid bill and the status history are still reachable
        # from the appointment after the flip.
        detail = self.h.appointment_detail("APT-0001")
        self.assertEqual(detail["payment"]["status"], "Paid")
        self.assertEqual(len(detail["history"]), 1)
        # And back again: Emergency -> General keeps the token too.
        back = self.h.edit_appointment("APT-0001", appt_type="General")
        self.assertEqual(back["token"], "N-1")
        self.assertEqual(back["apt_id"], "APT-0001")
        self.assertFalse(back["emergency"])

    def test_edit_clears_time_and_keeps_date(self):
        updated = self.h.edit_appointment("APT-0001", time="")
        self.assertIsNone(updated["appt_time"])
        self.assertEqual(updated["appt_date"], "2026-11-28")

    def test_edit_bad_values(self):
        with self.assertRaises(ValueError):
            self.h.edit_appointment("APT-0001", appt_type="Urgent")
        with self.assertRaises(ValueError):
            self.h.edit_appointment("APT-0001", date="32-13-2026")
        with self.assertRaises(ValueError):
            self.h.edit_appointment("APT-0001", time="25:00")
        with self.assertRaises(ValueError):
            self.h.edit_appointment("APT-9999", reason="x")

    def test_edit_role_rules(self):
        with self.assertRaises(PermissionError):
            self.h.edit_appointment("APT-0001", reason="x", role="Doctor")
        # Receptionist may edit; Admin may edit.
        self.h.edit_appointment("APT-0001", reason="ok",
                                role="Receptionist")
        self.h.edit_appointment("APT-0001", reason="ok2", role="Admin")

    def test_edit_old_row(self):
        updated = self.h.edit_appointment("D2:N-2", reason="Old checkup")
        self.assertIsNone(updated["apt_id"])
        self.assertEqual(updated["reason"], "Old checkup")
        self.assertEqual(updated["token"], "N-2")

    def test_edit_survives_restart(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        try:
            h = Hospital(db_path=path)
            _seed(h)
            h.edit_appointment("APT-0001", date="2026-12-01",
                               time="09:00", reason="Moved")
            h2 = Hospital(db_path=path)
            detail = h2.appointment_detail("APT-0001")
            self.assertEqual(detail["appointment"]["appt_date"], "2026-12-01")
            self.assertEqual(detail["appointment"]["reason"], "Moved")
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestCsvPdfExport(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)

    def test_csv_all_rows(self):
        rows = self.h.filter_appointments()
        text = self.h.appointments_to_csv(rows)
        lines = list(io.StringIO(text))
        self.assertEqual(len(lines), 7)  # header + 6 appointments
        self.assertTrue(lines[0].startswith("Appointment ID,Token,"))
        self.assertIn("APT-0001", text)
        self.assertIn("Ahmed Khan", text)
        self.assertIn("Cardiologist", text)

    def test_csv_respects_filters(self):
        rows = self.h.filter_appointments(status="Pending")
        text = self.h.appointments_to_csv(rows)
        self.assertIn("APT-0001", text)
        self.assertNotIn("APT-0002", text)
        self.assertNotIn("APT-0003", text)

    def test_pdf_is_valid(self):
        rows = self.h.filter_appointments()
        data = appointments_pdf(rows, "Appointments — City Care Hospital",
                                "All appointments  •  6 appointment(s)")
        self.assertTrue(data.startswith(b"%PDF-1.4\n"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"APT-0001", data)
        self.assertIn(b"Ahmed Khan", data)
        self.assertIn(b"City Care Hospital", data)
        # Cross-reference offsets really point at their objects.
        # One page = 6 objects (catalog, pages, 2 fonts, page,
        # content) so the xref lists 7 entries (object 0 + 6).
        xref_at = data.index(b"xref\n")
        trailer = data[xref_at:]
        lines = trailer.split(b"\n")
        self.assertEqual(lines[1], b"0 7")
        for i in range(1, 7):
            offset = int(lines[2 + i].split(b" ")[0])
            self.assertTrue(data[offset:].startswith(
                f"{i} 0 obj".encode("latin-1")))

    def test_pdf_empty_and_multipage(self):
        empty = appointments_pdf([], "Appointments", "None match")
        self.assertIn(b"No appointments match", empty)
        many = []
        for i in range(40):
            many.append({"apt_id": f"APT-{i:04d}",
                         "patient_name": f"Patient {i}",
                         "doctor_name": "Dr. Tariq",
                         "department": "Cardiologist",
                         "appt_date": "2026-11-28", "appt_time": "10:00",
                         "appt_type": "General", "status": "Pending",
                         "fee": 2500})
        data = appointments_pdf(many, "Appointments", "40 rows")
        self.assertIn(b"/Count 2", data)
        self.assertIn(b"APT-0039", data)


class TestServerAppointmentsTable(unittest.TestCase):
    """Step 9 over HTTP: list, detail, edit, cancel, exports, roles."""

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
        self.tokens = _seed(server.hospital)

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

    def _request_raw(self, method: str, path: str,
                     cookie: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        headers = {}
        if cookie is None:
            cookie = self.cookie
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return (resp.status,
                        resp.headers.get("Content-Type", ""),
                        resp.headers.get("Content-Disposition", ""),
                        resp.read())
        except urllib.error.HTTPError as err:
            return err.code, err.headers.get("Content-Type", ""), "", err.read()

    def test_list_filters_search_sort_page(self):
        status, payload = self._request(
            "GET", "/api/appointments/list?status=Pending")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 3)
        status, payload = self._request(
            "GET", "/api/appointments/list?search=APT-0003")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["appointments"][0]["token"], "E-1")
        status, payload = self._request(
            "GET", "/api/appointments/list?date=2026-11-28&status=Pending")
        self.assertEqual(payload["total"], 2)
        status, payload = self._request(
            "GET", "/api/appointments/list?sort=patient_name&order=desc")
        self.assertEqual(payload["appointments"][0]["patient_name"],
                         "Sara Ahmed")
        status, payload = self._request(
            "GET", "/api/appointments/list?per_page=2&page=2")
        self.assertEqual((payload["total"], payload["pages"]), (6, 3))
        self.assertEqual(len(payload["appointments"]), 2)

    def test_list_bad_params(self):
        status, payload = self._request(
            "GET", "/api/appointments/list?sort=bogus")
        self.assertEqual(status, 400)
        status, payload = self._request(
            "GET", "/api/appointments/list?status=Bogus")
        self.assertEqual(status, 400)

    def test_list_doctor_sees_only_own(self):
        status, payload = self._request(
            "GET", "/api/appointments/list", cookie=self.doc_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 3)
        self.assertTrue(all(a["doctor_id"] == "D1"
                            for a in payload["appointments"]))

    def test_detail_full_payload(self):
        status, payload = self._request(
            "GET", "/api/appointments/by-id/APT-0001")
        self.assertEqual(status, 200)
        self.assertEqual(payload["appointment"]["patient_name"], "Ahmed Khan")
        self.assertEqual(payload["patient"]["father_name"], "Muhammad Khan")
        self.assertEqual(payload["patient"]["phone"], "0300-1111111")
        self.assertEqual(payload["doctor"]["spec"], "Cardiologist")
        self.assertTrue(payload["payment"]["billed"])
        self.assertEqual(payload["payment"]["status"], "Paid")
        self.assertEqual(payload["history"], [])
        # APT-0003 has one recorded status change + an unpaid bill.
        status, payload = self._request(
            "GET", "/api/appointments/by-id/APT-0003")
        self.assertEqual(len(payload["history"]), 1)
        self.assertEqual(payload["payment"]["status"], "Unpaid")

    def test_detail_unknown_and_doctor_forbidden(self):
        status, payload = self._request(
            "GET", "/api/appointments/by-id/APT-9999")
        self.assertEqual(status, 404)
        status, payload = self._request(
            "GET", "/api/appointments/by-id/APT-0003",
            cookie=self.doc_cookie)
        self.assertEqual(status, 403)
        status, payload = self._request(
            "GET", "/api/appointments/by-id/APT-0001",
            cookie=self.doc_cookie)
        self.assertEqual(status, 200)

    def test_edit_success_and_double_booking(self):
        status, payload = self._request(
            "POST", "/api/appointments/by-id/APT-0001/edit",
            {"reason": "Annual checkup", "time": "12:00"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["appointment"]["reason"], "Annual checkup")
        self.assertEqual(payload["appointment"]["appt_time"], "12:00")
        self.assertEqual(payload["appointment"]["apt_id"], "APT-0001")
        self.assertEqual(payload["appointment"]["status"], "Pending")
        # Reschedule onto APT-0002's slot (D1, 11:00) is refused.
        status, payload = self._request(
            "POST", "/api/appointments/by-id/APT-0001/edit",
            {"date": "2026-11-28", "time": "11:00"})
        self.assertEqual(status, 400)
        self.assertIn("already has an appointment", payload["error"])

    def test_edit_roles_and_unknown(self):
        status, payload = self._request(
            "POST", "/api/appointments/by-id/APT-0001/edit",
            {"reason": "x"}, cookie=self.doc_cookie)
        self.assertEqual(status, 403)
        status, payload = self._request(
            "POST", "/api/appointments/by-id/APT-0001/edit",
            {"reason": "receptionist edit"}, cookie=self.recep_cookie)
        self.assertEqual(status, 200)
        status, payload = self._request(
            "POST", "/api/appointments/by-id/APT-9999/edit",
            {"reason": "x"})
        self.assertEqual(status, 404)

    def test_cancel_then_detail_shows_cancelled(self):
        status, payload = self._request(
            "POST", "/api/appointments/D1/N-2/status",
            {"status": "Cancelled"})
        self.assertEqual(status, 200)
        status, payload = self._request(
            "GET", "/api/appointments/by-id/APT-0002")
        self.assertEqual(payload["appointment"]["status"], "Cancelled")
        self.assertEqual(len(payload["history"]), 1)
        status, payload = self._request(
            "GET", "/api/appointments/list?status=Cancelled")
        self.assertEqual(payload["total"], 2)

    def test_csv_export(self):
        status, ctype, disp, body = self._request_raw(
            "GET", "/api/appointments/export.csv?status=Pending")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/csv"))
        self.assertIn("appointments.csv", disp)
        text = body.decode("utf-8")
        lines = text.strip().split("\n")
        self.assertEqual(len(lines), 4)  # header + 3 pending
        self.assertIn("APT-0001", text)
        self.assertNotIn("APT-0002", text)

    def test_pdf_export(self):
        status, ctype, disp, body = self._request_raw(
            "GET", "/api/appointments/export.pdf")
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("application/pdf"))
        self.assertIn("appointments.pdf", disp)
        self.assertTrue(body.startswith(b"%PDF-"))
        self.assertIn(b"APT-0001", body)
        self.assertIn(b"APT-0005", body)
        # The filter is respected in the PDF too.
        status, _ctype, _disp, body = self._request_raw(
            "GET", "/api/appointments/export.pdf?status=Completed")
        self.assertIn(b"APT-0003", body)
        self.assertNotIn(b"APT-0001", body)

    def test_unauthenticated_list_rejected(self):
        status, _ctype, _disp, body = self._request_raw(
            "GET", "/api/appointments/list", cookie="")
        self.assertEqual(status, 401)


class TestCliAppointmentsTable(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _seed(self.h)
        self.user = {"username": "admin", "role": "Admin",
                     "doctor_id": None}

    def test_table_list_then_detail_then_back(self):
        import hospital as hosp_mod
        inputs = iter(["", "", "", "", "", "",
                       "APT-0001", "B", "APT-0003", "B", ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.appointments_table_menu(
                    self.h, "Admin", self.user)
        text = out.getvalue()
        self.assertIn("APT-0001", text)
        self.assertIn("Ahmed Khan", text)
        self.assertIn("Father: Muhammad Khan", text)
        self.assertIn("Payment: Bill #1", text)
        self.assertIn("No status changes yet.", text)
        # APT-0003 has a recorded status change — its history prints.
        self.assertIn("History:", text)
        self.assertIn("Pending -> Completed", text)

    def test_cli_cancel(self):
        import hospital as hosp_mod
        inputs = iter(["", "", "", "", "", "",
                       "APT-0002", "C", "YES", ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.appointments_table_menu(
                    self.h, "Admin", self.user)
        self.assertIn("Cancelled", out.getvalue())
        detail = self.h.appointment_detail("APT-0002")
        self.assertEqual(detail["appointment"]["status"], "Cancelled")

    def test_cli_doctor_sees_only_own(self):
        import hospital as hosp_mod
        user = {"username": "doc1", "role": "Doctor", "doctor_id": "D1"}
        inputs = iter(["", "", "", "", "", "", ""])
        with patch("builtins.input", lambda _prompt="": next(inputs)):
            out = io.StringIO()
            with redirect_stdout(out):
                hosp_mod.appointments_table_menu(
                    self.h, "Doctor", user)
        text = out.getvalue()
        self.assertIn("APT-0001", text)
        self.assertNotIn("APT-0003", text)
        self.assertNotIn("APT-0004", text)


if __name__ == "__main__":
    unittest.main()
