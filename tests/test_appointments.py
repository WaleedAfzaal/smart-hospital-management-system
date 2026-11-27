"""Tests for Step 4 (Appointment Date & Time).

Every appointment now carries the date and time it is SCHEDULED for —
different from the booking date (when the token was issued). The
Emergency-first queue order is unchanged; date/time is for scheduling
and display only. Run from the staging root:
    python -m unittest discover -s tests -v
"""

import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hospital import Hospital, validate_appt_datetime  # noqa: E402


def _today():
    return datetime.date.today().isoformat()


def _tomorrow():
    return (datetime.date.today() + datetime.timedelta(days=1)).isoformat()


def _add_doctors(h):
    """The doctors the booking tests need, added explicitly — a fresh
    Hospital now starts with zero doctors."""
    h.add_doctor("D1", "Dr. Tariq", "Neurosurgeon", 2500)
    h.add_doctor("D2", "Dr. Ayesha", "Cardiologist", 2000)


class TestValidateApptDatetime(unittest.TestCase):
    def test_empty_date_means_today_and_empty_time_means_none(self):
        date, time = validate_appt_datetime(None, None)
        self.assertEqual(date, _today())
        self.assertIsNone(time)
        date, time = validate_appt_datetime("", "")
        self.assertEqual(date, _today())
        self.assertIsNone(time)

    def test_valid_date_and_time_pass_through(self):
        self.assertEqual(
            validate_appt_datetime("2026-11-28", "10:30"),
            ("2026-11-28", "10:30"))
        self.assertEqual(
            validate_appt_datetime("2026-11-28", "00:00"),
            ("2026-11-28", "00:00"))
        self.assertEqual(
            validate_appt_datetime("2026-11-28", "23:59"),
            ("2026-11-28", "23:59"))

    def test_bad_dates_rejected(self):
        for bad in ("2026-13-01", "2026-02-30", "28-11-2026",
                    "2026/11/28", "tomorrow", "2026-11-32"):
            with self.assertRaises(ValueError, msg=bad):
                validate_appt_datetime(bad, None)

    def test_bad_times_rejected(self):
        for bad in ("25:00", "10:60", "9:30", "10:3", "10:30:00",
                    "noon", "24:00"):
            with self.assertRaises(ValueError, msg=bad):
                validate_appt_datetime("2026-11-28", bad)


class TestBookingWithDateTime(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _add_doctors(self.h)
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_patient(102, "Sara", 25, "Cold")

    def test_defaults_are_today_and_no_time(self):
        self.h.book_appointment("D1", 101, False)
        row = self.h.doctors_map["D1"]["queue"].heap[0]
        self.assertEqual(row[7], _today())
        self.assertIsNone(row[8])
        self.assertEqual(row[6], _today())  # booking date still today

    def test_explicit_date_and_time_stored(self):
        token = self.h.book_appointment("D1", 101, False,
                                        "2026-11-28", "10:30")
        self.assertEqual(token, "N-1")
        row = self.h.doctors_map["D1"]["queue"].heap[0]
        self.assertEqual(row[7], "2026-11-28")
        self.assertEqual(row[8], "10:30")
        self.assertEqual(row[6], _today())  # booking date is still today

    def test_bad_date_books_nothing(self):
        token = self.h.book_appointment("D1", 101, False,
                                        "2026-13-01", "10:30")
        self.assertIsNone(token)
        self.assertEqual(len(self.h.doctors_map["D1"]["queue"].heap), 0)
        self.assertEqual(self.h.patients_map[101]["visits"], 0)

    def test_bad_time_books_nothing(self):
        token = self.h.book_appointment("D1", 101, False,
                                        "2026-11-28", "25:00")
        self.assertIsNone(token)
        self.assertEqual(len(self.h.doctors_map["D1"]["queue"].heap), 0)

    def test_queue_order_still_emergency_first(self):
        # A later time does NOT jump the queue — Emergency still first.
        self.h.book_appointment("D1", 101, False, "2026-11-28", "09:00")
        self.h.book_appointment("D1", 102, True, "2026-11-28", "11:00")
        ordered = sorted(self.h.doctors_map["D1"]["queue"].heap,
                         key=lambda r: (r[0], r[1]))
        self.assertEqual(ordered[0][2], "E-2")
        self.assertEqual(ordered[1][2], "N-1")


class TestScheduleViews(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _add_doctors(self.h)
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_patient(102, "Sara", 25, "Cold")
        self.h.book_appointment("D1", 101, False, _today(), "11:00")
        self.h.book_appointment("D1", 102, True, _today(), "09:00")
        self.h.book_appointment("D2", 101, False, _tomorrow(), "10:00")

    def test_appointments_for_date_filters_by_day(self):
        today = self.h.appointments_for_date(_today())
        self.assertEqual([a["token"] for a in today], ["E-2", "N-1"])
        tomorrow = self.h.appointments_for_date(_tomorrow())
        self.assertEqual([a["token"] for a in tomorrow], ["N-1"])
        self.assertEqual(tomorrow[0]["doctor_id"], "D2")
        self.assertEqual(
            self.h.appointments_for_date("2020-01-01"), [])

    def test_appointments_for_date_defaults_to_today(self):
        self.assertEqual(len(self.h.appointments_for_date()), 2)

    def test_appointments_sorted_by_time_then_emergency(self):
        # 09:00 (Emergency) before 11:00 (Normal) — time first.
        rows = self.h.appointments_for_date(_today())
        self.assertEqual(rows[0]["appt_time"], "09:00")
        self.assertEqual(rows[1]["appt_time"], "11:00")
        self.assertTrue(rows[0]["emergency"])

    def test_same_time_emergency_first(self):
        self.h.book_appointment("D1", 101, False, _today(), "09:00")
        rows = self.h.appointments_for_date(_today())
        times = [a["appt_time"] for a in rows]
        self.assertEqual(times, ["09:00", "09:00", "11:00"])
        self.assertTrue(rows[0]["emergency"])  # E-2 before N-3 at 09:00

    def test_doctor_schedule_filters_by_doctor(self):
        d1 = self.h.doctor_schedule("D1", _today())
        self.assertEqual(len(d1), 2)
        self.assertTrue(all(a["doctor_id"] == "D1" for a in d1))
        d2 = self.h.doctor_schedule("D2", _today())
        self.assertEqual(d2, [])
        d2_tomorrow = self.h.doctor_schedule("D2", _tomorrow())
        self.assertEqual(len(d2_tomorrow), 1)

    def test_doctor_schedule_unknown_doctor(self):
        with self.assertRaises(ValueError):
            self.h.doctor_schedule("D9", _today())

    def test_appointments_for_date_bad_date(self):
        with self.assertRaises(ValueError):
            self.h.appointments_for_date("not-a-date")

    def test_billing_still_works_from_dated_token(self):
        bill = self.h.create_bill(101, "D1", "N-1")
        self.assertEqual(bill["total"], 2500)
        self.assertEqual(bill["status"], "Unpaid")


class TestDateTimePersistence(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # let the Hospital create it fresh

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_date_and_time_survive_restart(self):
        h = Hospital(db_path=self.db_path)
        h.add_doctor("D1", "Dr. Tariq", "Neurosurgeon", 2500)
        h.add_patient(101, "Ahmed", 30, "Fever")
        h.add_patient(102, "Sara", 25, "Cold")
        h.book_appointment("D1", 101, False, "2026-11-28", "10:30")
        h.book_appointment("D1", 102, True, "2026-11-29", "09:00")

        h2 = Hospital(db_path=self.db_path)
        rows = h2.appointments_for_date("2026-11-28")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token"], "N-1")
        self.assertEqual(rows[0]["appt_time"], "10:30")
        rows = h2.appointments_for_date("2026-11-29")
        self.assertEqual(rows[0]["token"], "E-2")
        self.assertEqual(rows[0]["appt_time"], "09:00")
        # Token numbering keeps counting up after restart, too.
        h2.book_appointment("D1", 101, False, "2026-11-28", "12:00")
        tokens = sorted(r[2] for r in h2.doctors_map["D1"]["queue"].heap)
        self.assertIn("N-3", tokens)

    def test_old_rows_without_date_time_still_load(self):
        """A database from before Step 4 (7-field heap rows, no
        appt_date/appt_time columns) still loads and keeps working —
        the old appointments just never appear in a dated view."""
        h = Hospital(db_path=self.db_path)
        h.add_patient(101, "Ahmed", 30, "Fever")
        h.add_doctor("D1", "Dr. Tariq", "Neurosurgeon", 2500)
        doc = h.doctors_map["D1"]
        # Simulate a pre-Step-4 heap row: 7 fields, no scheduled date.
        doc["queue"].heap.append(
            (2, 1, "N-1", 101, "Ahmed", 2500, _today()))
        doc["queue"].count = 1
        h.save()

        h2 = Hospital(db_path=self.db_path)
        rows = h2.doctors_map["D1"]["queue"].heap
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], "N-1")
        self.assertIsNone(rows[0][7])  # appt_date
        self.assertIsNone(rows[0][8])  # appt_time
        # It does not appear in today's schedule (no scheduled date)...
        self.assertEqual(h2.appointments_for_date(_today()), [])
        # ...but the queue and billing still work with it.
        self.assertEqual(len(h2.doctors_map["D1"]["queue"].heap), 1)
        bill = h2.create_bill(101, "D1", "N-1")
        self.assertEqual(bill["total"], 2500)
        # ...and new bookings with a date still work on the same doctor.
        token = h2.book_appointment("D1", 101, False, _today(), "10:00")
        self.assertEqual(token, "N-2")
        self.assertEqual(len(h2.appointments_for_date(_today())), 1)


if __name__ == "__main__":
    unittest.main()
