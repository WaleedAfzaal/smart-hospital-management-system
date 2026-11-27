"""Basic tests for the Hospital class in hospital.py (Ali's file).

Run from the staging root:
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hospital import Hospital, binary_search, bubble_sort  # noqa: E402


def _add_doctors(h):
    """The doctors the booking/shift tests need, added explicitly —
    a fresh Hospital now starts with zero doctors."""
    h.add_doctor("D1", "Dr. Tariq", "Neurosurgeon", 2500)
    h.add_doctor("D2", "Dr. Ayesha", "Cardiologist", 2000)


class TestPatients(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()

    def test_add_and_find(self):
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.assertIn(101, self.h.patients_map)
        found = binary_search(bubble_sort(self.h.patients_list, "id"), 101)
        self.assertIsNotNone(found)
        self.assertEqual(found["name"], "Ahmed")

    def test_duplicate_id_rejected(self):
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_patient(101, "Ali", 25, "Cold")
        self.assertEqual(len(self.h.patients_list), 1)
        self.assertEqual(self.h.patients_map[101]["name"], "Ahmed")

    def test_find_missing_returns_none(self):
        self.assertIsNone(binary_search(bubble_sort(self.h.patients_list, "id"), 999))

    def test_sorted_by_age(self):
        self.h.add_patient(3, "C", 50, "X")
        self.h.add_patient(1, "A", 20, "X")
        self.h.add_patient(2, "B", 35, "X")
        ages = [p["age"] for p in bubble_sort(self.h.patients_list, "age")]
        self.assertEqual(ages, [20, 35, 50])

    def test_add_note_bumps_visits(self):
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_note(101, "Follow-up in 2 weeks")
        pat = self.h.patients_map[101]
        self.assertEqual(pat["visits"], 1)
        notes = []
        node = pat["notes"].head
        while node:
            notes.append(node.val)
            node = node.next
        self.assertEqual(notes, ["Follow-up in 2 weeks"])

    def test_add_note_missing_patient(self):
        self.h.add_note(999, "note")
        self.assertNotIn(999, self.h.patients_map)


class TestDoctorsAndBooking(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _add_doctors(self.h)
        self.h.add_patient(101, "Ahmed", 30, "Fever")
        self.h.add_patient(102, "Sara", 25, "Cold")

    def test_fresh_hospital_has_no_doctors(self):
        self.assertEqual(len(Hospital().doctors_map), 0)

    def test_add_doctor_registers_doctor(self):
        h = Hospital()
        doc = h.add_doctor("D1", "Dr. Tariq", "Neurosurgeon", 2500)
        self.assertEqual(doc["name"], "Dr. Tariq")
        self.assertEqual(h.doctors_map["D1"]["spec"], "Neurosurgeon")
        self.assertEqual(h.doctors_map["D1"]["fee"], 2500)
        self.assertEqual(len(h.doctors_map), 1)
        # The doctor joins the shift rotation too.
        self.assertIn("D1 - Dr. Tariq",
                      [h.shift_q.items[(h.shift_q.front + i) % h.shift_q.size]
                       for i in range(h.shift_q.total)])
        # Duplicate ID and bad values are rejected, nothing changes.
        with self.assertRaises(ValueError):
            h.add_doctor("D1", "Dr. Other", "ENT", 100)
        with self.assertRaises(ValueError):
            h.add_doctor("D2", "", "ENT", 100)
        with self.assertRaises(ValueError):
            h.add_doctor("D2", "Dr. X", "ENT", -50)
        self.assertEqual(len(h.doctors_map), 1)

    def test_booking_tokens(self):
        doc = self.h.doctors_map["D1"]
        t1 = doc["queue"].enqueue(False, 101, "Ahmed", doc["fee"])
        t2 = doc["queue"].enqueue(True, 102, "Sara", doc["fee"])
        self.assertEqual(t1, "N-1")
        self.assertEqual(t2, "E-2")

    def test_emergency_served_first(self):
        doc = self.h.doctors_map["D1"]
        doc["queue"].enqueue(False, 101, "Ahmed", doc["fee"])
        doc["queue"].enqueue(True, 102, "Sara", doc["fee"])
        heap = sorted(doc["queue"].heap, key=lambda r: (r[0], r[1]))
        self.assertEqual(heap[0][2], "E-2")  # emergency first
        self.assertEqual(heap[1][2], "N-1")

    def test_book_appointment_undo(self):
        self.h.book_appointment("D1", 101, False)
        self.assertEqual(len(self.h.doctors_map["D1"]["queue"].heap), 1)
        self.h.undo()
        self.assertEqual(len(self.h.doctors_map["D1"]["queue"].heap), 0)
        self.assertEqual(self.h.patients_map[101]["visits"], 0)


class TestBedsUndoPharmacyShifts(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        _add_doctors(self.h)
        self.h.add_patient(101, "Ahmed", 30, "Fever")

    def test_beds_assign_free_and_undo(self):
        self.h.beds[2] = 101
        self.h.undo_stack.push(("BED", 2, 0))
        self.assertEqual(self.h.beds[2], 101)
        self.h.undo()  # restores bed 3 to free
        self.assertEqual(self.h.beds[2], 0)

    def test_undo_add_patient(self):
        self.h.add_patient(102, "Sara", 25, "Cold")
        self.h.undo()
        self.assertNotIn(102, self.h.patients_map)

    def test_undo_add_note(self):
        self.h.add_note(101, "note one")
        self.h.undo()
        self.assertEqual(self.h.patients_map[101]["visits"], 0)
        self.assertIsNone(self.h.patients_map[101]["notes"].head)

    def test_pharmacy_fifo(self):
        self.h.pharmacy_q.enqueue("T-1")
        self.h.pharmacy_q.enqueue("T-2")
        self.assertEqual(self.h.pharmacy_q.dequeue(), "T-1")
        self.assertEqual(self.h.pharmacy_q.dequeue(), "T-2")
        self.assertIsNone(self.h.pharmacy_q.dequeue())

    def test_shift_rotate(self):
        first = self.h.shift_q.items[self.h.shift_q.front]
        self.assertIn("D1", first)
        now = self.h.shift_q.rotate()
        self.assertIn("D2", now)

    def test_dashboard_counts(self):
        free = sum(1 for b in self.h.beds if b == 0)
        self.assertEqual(free, 10)
        self.assertEqual(len(self.h.patients_map), 1)


if __name__ == "__main__":
    unittest.main()
