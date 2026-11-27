"""Step 5 tests: patient search by name, edit, and delete (core logic).

Run from the staging root:
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hospital import Hospital  # noqa: E402


class TestSearchPatients(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.h.add_patient(101, "Ahmed Khan", 30, "Fever")
        self.h.add_patient(102, "Ayesha Malik", 25, "Cold")
        self.h.add_patient(103, "Ali Ahmed", 40, "Cough")

    def test_partial_match(self):
        found = self.h.search_patients("ahm")
        self.assertEqual([p["id"] for p in found], [101, 103])

    def test_case_insensitive(self):
        found = self.h.search_patients("AHMED")
        self.assertEqual([p["id"] for p in found], [101, 103])
        found = self.h.search_patients("AYESHA")
        self.assertEqual([p["id"] for p in found], [102])

    def test_full_name_match(self):
        found = self.h.search_patients("Ayesha Malik")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], 102)

    def test_no_match_returns_empty(self):
        self.assertEqual(self.h.search_patients("Zubair"), [])
        self.assertEqual(self.h.search_patients(""), [])
        self.assertEqual(self.h.search_patients("   "), [])
        self.assertEqual(self.h.search_patients(None), [])

    def test_results_sorted_by_id(self):
        self.h.add_patient(99, "Ahmed Raza", 22, "Flu")
        found = self.h.search_patients("ahmed")
        self.assertEqual([p["id"] for p in found], [99, 101, 103])


class TestEditPatient(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.h.add_patient(101, "Ahmed Khan", 30, "Fever")
        self.h.add_note(101, "First visit")

    def test_edit_all_fields(self):
        updated = self.h.edit_patient(101, name="Ahmed Ali", age=31,
                                      problem="Flu")
        self.assertEqual(updated["name"], "Ahmed Ali")
        self.assertEqual(updated["age"], 31)
        self.assertEqual(updated["problem"], "Flu")
        self.assertEqual(updated["id"], 101)  # ID never changes
        self.assertEqual(updated["visits"], 1)
        self.assertEqual(updated["notes"], ["First visit"])

    def test_edit_one_field_keeps_the_rest(self):
        updated = self.h.edit_patient(101, age=45)
        self.assertEqual(updated["name"], "Ahmed Khan")
        self.assertEqual(updated["age"], 45)
        self.assertEqual(updated["problem"], "Fever")

    def test_edit_missing_patient_raises(self):
        with self.assertRaises(ValueError):
            self.h.edit_patient(999, name="Nobody")

    def test_edit_bad_values_raise(self):
        with self.assertRaises(ValueError):
            self.h.edit_patient(101, name="   ")
        with self.assertRaises(ValueError):
            self.h.edit_patient(101, problem="")
        with self.assertRaises(ValueError):
            self.h.edit_patient(101, age=-1)
        with self.assertRaises(ValueError):
            self.h.edit_patient(101, age=200)
        with self.assertRaises(ValueError):
            self.h.edit_patient(101, age="thirty")
        # Nothing changed after the failed edits.
        pat = self.h.patients_map[101]
        self.assertEqual((pat["name"], pat["age"], pat["problem"]),
                         ("Ahmed Khan", 30, "Fever"))

    def test_rename_updates_queued_appointments(self):
        self.h.book_appointment("D1", 101, False)
        self.h.edit_patient(101, name="Ahmed Ali")
        row = self.h.doctors_map["D1"]["queue"].heap[0]
        self.assertEqual(row[4], "Ahmed Ali")

    def test_edit_survives_restart(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        try:
            h = Hospital(db_path=path)
            h.add_patient(201, "Sara", 25, "Cold")
            h.edit_patient(201, age=26, problem="Fever")
            h2 = Hospital(db_path=path)
            pat = h2.patients_map[201]
            self.assertEqual((pat["age"], pat["problem"]), (26, "Fever"))
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestDeletePatient(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.h.add_patient(101, "Ahmed Khan", 30, "Fever")
        self.h.add_patient(102, "Sara", 25, "Cold")
        self.h.add_note(101, "First visit")
        self.h.add_note(101, "Follow-up")

    def test_delete_removes_patient_and_notes(self):
        removed = self.h.delete_patient(101)
        self.assertEqual(removed["name"], "Ahmed Khan")
        self.assertEqual(removed["notes_removed"], 2)
        self.assertNotIn(101, self.h.patients_map)
        self.assertNotIn(101, [p["id"] for p in self.h.patients_list])
        # The other patient is untouched.
        self.assertIn(102, self.h.patients_map)

    def test_delete_removes_queued_appointments(self):
        self.h.book_appointment("D1", 101, False)
        self.h.book_appointment("D2", 101, True)
        self.h.book_appointment("D1", 102, False)
        removed = self.h.delete_patient(101)
        self.assertEqual(len(removed["appointments_removed"]), 2)
        self.assertEqual(len(self.h.doctors_map["D1"]["queue"].heap), 1)
        self.assertEqual(len(self.h.doctors_map["D2"]["queue"].heap), 0)
        # The queue still serves the remaining patient.
        self.assertEqual(
            self.h.doctors_map["D1"]["queue"].heap[0][3], 102)

    def test_delete_frees_bed(self):
        self.h.beds[2] = 101
        self.h.beds[4] = 102
        removed = self.h.delete_patient(101)
        self.assertEqual(removed["beds_freed"], [3])
        self.assertEqual(self.h.beds[2], 0)
        self.assertEqual(self.h.beds[4], 102)

    def test_delete_keeps_bills(self):
        self.h.book_appointment("D1", 101, False)
        bill = self.h.create_bill(101, "D1", "N-1")
        self.h.delete_patient(101)
        kept = self.h.billing.get_bill(bill["id"])
        self.assertIsNotNone(kept)
        self.assertEqual(kept["patient_name"], "Ahmed Khan")
        self.assertEqual(kept["patient_id"], 101)

    def test_delete_missing_patient_raises(self):
        with self.assertRaises(ValueError):
            self.h.delete_patient(999)

    def test_delete_survives_restart(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        try:
            h = Hospital(db_path=path)
            h.add_patient(201, "Sara", 25, "Cold")
            h.add_patient(202, "Ali", 40, "Cough")
            h.book_appointment("D1", 201, False)
            bill = h.create_bill(201, "D1", "N-1")
            h.delete_patient(201)
            h2 = Hospital(db_path=path)
            self.assertNotIn(201, h2.patients_map)
            self.assertIn(202, h2.patients_map)
            self.assertEqual(len(h2.doctors_map["D1"]["queue"].heap), 0)
            self.assertIsNotNone(h2.billing.get_bill(bill["id"]))
        finally:
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
