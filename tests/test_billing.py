"""Billing + reports tests for Step 3.

Unit tests use an in-memory Hospital (no database file); persistence
tests use a throwaway .db file and reopen it, like restarting the app.
Run from the staging root:
    python -m unittest discover -s tests -v
"""

import csv
import datetime
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hospital import Hospital  # noqa: E402

TODAY = datetime.date.today().isoformat()


def _setup_hospital(h):
    """One patient + one booked appointment (fee 2500, token N-1)."""
    h.add_patient(101, "Ahmed", 30, "Fever")
    h.book_appointment("D1", 101, False)
    return h


class TestCreateBill(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()

    def test_bill_from_appointment_has_consultation_fee(self):
        _setup_hospital(self.h)
        bill = self.h.create_bill(101, "D1", "N-1")
        self.assertEqual(bill["id"], 1)
        self.assertEqual(bill["patient_id"], 101)
        self.assertEqual(bill["patient_name"], "Ahmed")
        self.assertEqual(bill["doctor_name"], "Dr. Tariq")
        self.assertEqual(bill["token"], "N-1")
        self.assertEqual(bill["date"], TODAY)
        self.assertEqual(bill["status"], "Unpaid")
        self.assertEqual(len(bill["items"]), 1)
        self.assertEqual(bill["items"][0]["amount"], 2500)
        self.assertEqual(bill["total"], 2500)

    def test_unknown_token_patient_or_doctor_rejected(self):
        _setup_hospital(self.h)
        with self.assertRaises(ValueError):
            self.h.create_bill(101, "D1", "N-9")       # no such token
        with self.assertRaises(ValueError):
            self.h.create_bill(999, "D1", "N-1")       # no such patient
        with self.assertRaises(ValueError):
            self.h.create_bill(101, "D9", "N-1")       # no such doctor

    def test_token_of_another_patient_rejected(self):
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_patient(102, "Sara", 25, "Cold")
        self.h.book_appointment("D1", 101, False)       # N-1 is Ahmed's
        with self.assertRaises(ValueError):
            self.h.create_bill(102, "D1", "N-1")

    def test_same_token_cannot_be_billed_twice(self):
        _setup_hospital(self.h)
        self.h.create_bill(101, "D1", "N-1")
        with self.assertRaises(ValueError):
            self.h.create_bill(101, "D1", "N-1")
        self.assertEqual(len(self.h.billing.list_bills()), 1)

    def test_emergency_token_bills_with_same_fee(self):
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.book_appointment("D1", 101, True)        # E-1
        bill = self.h.create_bill(101, "D1", "E-1")
        self.assertEqual(bill["total"], 2500)


class TestChargesAndStatus(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _setup_hospital(self.h)
        self.bill = self.h.create_bill(101, "D1", "N-1")

    def test_add_charge_updates_total(self):
        bill = self.h.billing.add_charge(
            self.bill["id"], "Medicine — Paracetamol", 350)
        self.assertEqual(bill["total"], 2850)
        bill = self.h.billing.add_charge(
            self.bill["id"], "Bed charges (Bed 3, 2 days)", 2000)
        self.assertEqual(bill["total"], 4850)
        self.assertEqual(len(bill["items"]), 3)

    def test_bad_charges_rejected(self):
        with self.assertRaises(ValueError):
            self.h.billing.add_charge(self.bill["id"], "Medicine", 0)
        with self.assertRaises(ValueError):
            self.h.billing.add_charge(self.bill["id"], "Medicine", -50)
        with self.assertRaises(ValueError):
            self.h.billing.add_charge(self.bill["id"], "   ", 100)
        with self.assertRaises(ValueError):
            self.h.billing.add_charge(self.bill["id"], "Medicine", 12.5)
        with self.assertRaises(ValueError):
            self.h.billing.add_charge(999, "Medicine", 100)
        # Total unchanged after all the rejections
        self.assertEqual(
            self.h.billing.get_bill(self.bill["id"])["total"], 2500)

    def test_mark_paid_and_unpaid(self):
        bill = self.h.billing.mark_paid(self.bill["id"])
        self.assertEqual(bill["status"], "Paid")
        bill = self.h.billing.mark_unpaid(self.bill["id"])
        self.assertEqual(bill["status"], "Unpaid")
        with self.assertRaises(ValueError):
            self.h.billing.mark_paid(999)

    def test_list_bills_filters(self):
        self.h.add_patient(102, "Sara", 25, "Cold")
        self.h.book_appointment("D2", 102, False)
        bill2 = self.h.create_bill(102, "D2", "N-1")
        self.h.billing.mark_paid(bill2["id"])
        self.assertEqual(len(self.h.billing.list_bills()), 2)
        self.assertEqual(
            [b["id"] for b in self.h.billing.list_bills(patient_id=102)],
            [bill2["id"]])
        self.assertEqual(
            [b["id"] for b in self.h.billing.list_bills(status="Paid")],
            [bill2["id"]])
        self.assertEqual(
            [b["id"] for b in self.h.billing.list_bills(status="Unpaid")],
            [self.bill["id"]])

    def test_get_unknown_bill_returns_none(self):
        self.assertIsNone(self.h.billing.get_bill(999))


class TestReports(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()

    def test_daily_report_matches_the_data(self):
        _setup_hospital(self.h)
        self.h.add_patient(102, "Sara", 25, "Cold")
        self.h.book_appointment("D2", 102, False)
        self.h.book_appointment("D1", 102, True)        # E-2 for D1
        bill1 = self.h.create_bill(101, "D1", "N-1")   # 2500
        self.h.billing.add_charge(bill1["id"], "Medicine", 500)
        bill2 = self.h.create_bill(102, "D2", "N-1")   # 2000
        self.h.billing.mark_paid(bill2["id"])
        self.h.beds[0] = 101                            # 1 bed occupied

        rep = self.h.daily_report()
        self.assertEqual(rep["date"], TODAY)
        self.assertEqual(rep["patients_today"], 2)
        self.assertEqual(rep["appointments_today"], 3)
        self.assertEqual(rep["billed_today"], 5000)     # 3000 + 2000
        self.assertEqual(rep["collected_today"], 2000)  # only bill2 paid
        self.assertEqual(rep["bills_today"], 2)
        self.assertEqual(rep["paid_today"], 1)
        self.assertEqual(rep["unpaid_today"], 1)
        self.assertEqual(rep["unpaid_total"], 3000)
        self.assertEqual(rep["unpaid_count"], 1)
        self.assertEqual(rep["beds_occupied"], 1)
        self.assertEqual(rep["beds_total"], 10)
        self.assertEqual(rep["beds_free"], 9)
        per_doctor = {d["doctor_id"]: d for d in rep["per_doctor"]}
        self.assertEqual(per_doctor["D1"]["appointments_today"], 2)
        self.assertEqual(per_doctor["D1"]["waiting"], 2)
        self.assertEqual(per_doctor["D2"]["appointments_today"], 1)
        self.assertEqual(per_doctor["D3"]["appointments_today"], 0)

    def test_empty_day_report_is_all_zeros(self):
        rep = self.h.daily_report()
        self.assertEqual(rep["patients_today"], 0)
        self.assertEqual(rep["appointments_today"], 0)
        self.assertEqual(rep["billed_today"], 0)
        self.assertEqual(rep["collected_today"], 0)
        self.assertEqual(rep["unpaid_total"], 0)
        self.assertEqual(rep["beds_occupied"], 0)
        self.assertEqual(len(rep["per_doctor"]), 5)

    def test_report_for_another_date_is_empty(self):
        _setup_hospital(self.h)
        self.h.create_bill(101, "D1", "N-1")
        rep = self.h.daily_report("2020-01-01")
        self.assertEqual(rep["patients_today"], 0)
        self.assertEqual(rep["appointments_today"], 0)
        self.assertEqual(rep["billed_today"], 0)

    def test_bills_dated_another_day_do_not_count_today(self):
        _setup_hospital(self.h)
        bill = self.h.create_bill(101, "D1", "N-1")
        # Move the bill back a day (as if it was made yesterday).
        self.h.billing.bills[bill["id"]]["date"] = "2020-01-01"
        rep = self.h.daily_report()
        self.assertEqual(rep["billed_today"], 0)
        self.assertEqual(rep["unpaid_total"], 2500)  # still owed overall


class TestBillingCsv(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _setup_hospital(self.h)
        self.bill = self.h.create_bill(101, "D1", "N-1")
        self.h.billing.add_charge(self.bill["id"], "Medicine", 350)

    def test_csv_contains_the_bill(self):
        rows = list(csv.DictReader(io.StringIO(self.h.billing.to_csv())))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["Bill ID"], "1")
        self.assertEqual(row["Date"], TODAY)
        self.assertEqual(row["Patient Name"], "Ahmed")
        self.assertEqual(row["Doctor"], "Dr. Tariq")
        self.assertEqual(row["Token"], "N-1")
        self.assertEqual(row["Total (Rs)"], "2850")
        self.assertEqual(row["Status"], "Unpaid")
        self.assertIn("Consultation", row["Items"])
        self.assertIn("Medicine", row["Items"])

    def test_csv_empty_has_header_only(self):
        h = Hospital()
        rows = list(csv.DictReader(io.StringIO(h.billing.to_csv())))
        self.assertEqual(rows, [])


class TestBillingPersistence(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # let the Hospital create it fresh

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_bills_survive_restart_with_right_totals(self):
        h = Hospital(db_path=self.db_path)
        _setup_hospital(h)
        bill = h.create_bill(101, "D1", "N-1")
        h.billing.add_charge(bill["id"], "Medicine — Paracetamol", 350)
        h.billing.add_charge(bill["id"], "Bed charges", 2000)
        h.billing.mark_paid(bill["id"])

        h2 = Hospital(db_path=self.db_path)
        bills = h2.billing.list_bills()
        self.assertEqual(len(bills), 1)
        reloaded = bills[0]
        self.assertEqual(reloaded["total"], 4850)
        self.assertEqual(reloaded["status"], "Paid")
        self.assertEqual(len(reloaded["items"]), 3)
        self.assertEqual(reloaded["items"][1]["label"],
                         "Medicine — Paracetamol")

    def test_bill_ids_keep_counting_after_restart(self):
        h = Hospital(db_path=self.db_path)
        _setup_hospital(h)
        h.create_bill(101, "D1", "N-1")
        h.billing.close()
        h2 = Hospital(db_path=self.db_path)
        h2.add_patient(102, "Sara", 25, "Cold")
        h2.book_appointment("D2", 102, False)
        bill2 = h2.create_bill(102, "D2", "N-1")
        self.assertEqual(bill2["id"], 2)

    def test_daily_report_works_after_restart(self):
        h = Hospital(db_path=self.db_path)
        _setup_hospital(h)
        h.create_bill(101, "D1", "N-1")
        h2 = Hospital(db_path=self.db_path)
        rep = h2.daily_report()
        self.assertEqual(rep["patients_today"], 1)
        self.assertEqual(rep["appointments_today"], 1)
        self.assertEqual(rep["billed_today"], 2500)

    def test_old_database_without_date_columns_still_loads(self):
        """A hospital.db made before Step 3 has no registered/date
        columns — opening it must migrate, not crash, and the old data
        must come back (dates just unknown)."""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE patients(id INTEGER PRIMARY KEY, name TEXT,"
            " age INTEGER, problem TEXT, visits INTEGER, position INTEGER)")
        conn.execute(
            "CREATE TABLE doctors(id TEXT PRIMARY KEY, name TEXT,"
            " spec TEXT, fee INTEGER, queue_count INTEGER)")
        conn.execute(
            "CREATE TABLE appointments(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " doctor_id TEXT, heap_pos INTEGER, priority INTEGER,"
            " seq INTEGER, token TEXT, patient_id INTEGER,"
            " patient_name TEXT, fee INTEGER)")
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE patient_notes(patient_id INTEGER,"
                     " position INTEGER, note TEXT,"
                     " PRIMARY KEY (patient_id, position))")
        conn.execute("CREATE TABLE beds(bed_no INTEGER PRIMARY KEY,"
                     " patient_id INTEGER)")
        conn.execute("CREATE TABLE pharmacy(position INTEGER PRIMARY KEY,"
                     " value TEXT)")
        conn.execute("CREATE TABLE shifts(position INTEGER PRIMARY KEY,"
                     " value TEXT)")
        conn.execute(
            "INSERT INTO meta VALUES ('initialized', '1')")
        conn.execute(
            "INSERT INTO patients VALUES (101, 'Old Patient', 40, 'Cough',"
            " 0, 0)")
        conn.execute(
            "INSERT INTO doctors VALUES ('D1', 'Dr. Tariq', 'Neurosurgeon',"
            " 2500, 1)")
        conn.execute(
            "INSERT INTO appointments(doctor_id, heap_pos, priority, seq,"
            " token, patient_id, patient_name, fee)"
            " VALUES ('D1', 0, 2, 1, 'N-1', 101, 'Old Patient', 2500)")
        conn.commit()
        conn.close()

        h = Hospital(db_path=self.db_path)
        self.assertIn(101, h.patients_map)
        self.assertEqual(h.patients_map[101]["name"], "Old Patient")
        self.assertIsNone(h.patients_map[101]["registered"])
        self.assertEqual(len(h.doctors_map["D1"]["queue"].heap), 1)
        # Old appointment has no date — not counted for today.
        self.assertEqual(h.daily_report()["appointments_today"], 0)
        # ...but it can still be billed, and saving works afterwards.
        bill = h.create_bill(101, "D1", "N-1")
        self.assertEqual(bill["total"], 2500)
        h2 = Hospital(db_path=self.db_path)
        self.assertEqual(len(h2.billing.list_bills()), 1)


if __name__ == "__main__":
    unittest.main()
