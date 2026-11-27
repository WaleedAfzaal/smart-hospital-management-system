"""Persistence tests for Step 1 (SQLite database).

Each test builds a Hospital on a throwaway database file, makes changes,
then opens a SECOND Hospital on the same file — like restarting the app —
and checks everything came back. Run from the staging root:
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hospital import Hospital, PriorityQueue  # noqa: E402


def _notes_of(patient):
    out = []
    node = patient["notes"].head
    while node:
        out.append(node.val)
        node = node.next
    return out


def _queue_items(queue):
    out = []
    node = queue.front
    while node:
        out.append(node.val)
        node = node.next
    return out


def _shifts_of(hospital):
    q = hospital.shift_q
    return [q.items[(q.front + i) % q.size] for i in range(q.total)]


class PersistenceTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # let the Hospital create it fresh

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _reopen(self):
        """Simulate an app restart: new Hospital on the same db file."""
        return Hospital(db_path=self.db_path)


class TestPatientPersistence(PersistenceTestCase):
    def test_patients_and_notes_survive_restart(self):
        h = Hospital(db_path=self.db_path)
        h.add_patient(101, "Ahmed", 30, "Fever")
        h.add_patient(102, "Sara", 25, "Cold")
        h.add_note(101, "Follow-up in 2 weeks")
        h.add_note(101, "Prescribed paracetamol")

        h2 = self._reopen()
        self.assertEqual(len(h2.patients_list), 2)
        self.assertEqual(h2.patients_map[101]["name"], "Ahmed")
        self.assertEqual(h2.patients_map[101]["visits"], 2)
        self.assertEqual(_notes_of(h2.patients_map[101]),
                         ["Follow-up in 2 weeks", "Prescribed paracetamol"])
        self.assertEqual(h2.patients_map[102]["age"], 25)

    def test_duplicate_id_still_rejected_after_restart(self):
        h = Hospital(db_path=self.db_path)
        h.add_patient(101, "Ahmed", 30, "Fever")
        h2 = self._reopen()
        h2.add_patient(101, "Ali", 25, "Cold")
        self.assertEqual(len(h2.patients_list), 1)
        self.assertEqual(h2.patients_map[101]["name"], "Ahmed")

    def test_undo_of_add_patient_is_saved(self):
        h = Hospital(db_path=self.db_path)
        h.add_patient(101, "Ahmed", 30, "Fever")
        h.undo()
        h2 = self._reopen()
        self.assertNotIn(101, h2.patients_map)


class TestAppointmentPersistence(PersistenceTestCase):
    def test_appointments_and_token_numbering_survive_restart(self):
        h = Hospital(db_path=self.db_path)
        h.add_patient(101, "Ahmed", 30, "Fever")
        h.add_patient(102, "Sara", 25, "Cold")
        h.book_appointment("D1", 101, False)  # N-1
        h.book_appointment("D1", 102, True)   # E-2

        h2 = self._reopen()
        doc = h2.doctors_map["D1"]
        tokens = sorted(row[2] for row in doc["queue"].heap)
        self.assertEqual(tokens, ["E-2", "N-1"])
        # Emergency still comes first after reload
        ordered = sorted(doc["queue"].heap, key=lambda r: (r[0], r[1]))
        self.assertEqual(ordered[0][2], "E-2")
        # Token numbers keep counting up — no duplicate N-1/E-2
        h2.book_appointment("D1", 101, False)
        tokens = sorted(row[2] for row in doc["queue"].heap)
        self.assertIn("N-3", tokens)

    def test_added_doctor_survives_restart(self):
        h = Hospital(db_path=self.db_path)
        h.doctors_map["D6"] = {"id": "D6", "name": "Dr. Hina", "spec": "ENT",
                               "fee": 1500, "queue": PriorityQueue()}
        h.shift_q.enqueue("D6 - Dr. Hina")
        h.save()
        h2 = self._reopen()
        self.assertIn("D6", h2.doctors_map)
        self.assertEqual(h2.doctors_map["D6"]["spec"], "ENT")
        self.assertIn("D6 - Dr. Hina", _shifts_of(h2))


class TestBedsPharmacyShiftsPersistence(PersistenceTestCase):
    def test_beds_survive_restart(self):
        h = Hospital(db_path=self.db_path)
        h.add_patient(101, "Ahmed", 30, "Fever")
        h.beds[0] = 101
        h.beds[4] = 101
        h.save()
        h2 = self._reopen()
        self.assertEqual(h2.beds[0], 101)
        self.assertEqual(h2.beds[4], 101)
        self.assertEqual(sum(1 for b in h2.beds if b == 0), 8)

    def test_pharmacy_fifo_order_survives_restart(self):
        h = Hospital(db_path=self.db_path)
        h.pharmacy_q.enqueue("T-1")
        h.pharmacy_q.enqueue("T-2")
        h.pharmacy_q.enqueue("T-3")
        h.save()
        h2 = self._reopen()
        self.assertEqual(_queue_items(h2.pharmacy_q), ["T-1", "T-2", "T-3"])
        self.assertEqual(h2.pharmacy_q.dequeue(), "T-1")

    def test_shift_rotation_survives_restart(self):
        h = Hospital(db_path=self.db_path)
        h.shift_q.rotate()  # D2 now on duty
        h.shift_q.rotate()  # D3 now on duty
        h.save()
        h2 = self._reopen()
        shifts = _shifts_of(h2)
        self.assertEqual(len(shifts), 5)
        self.assertIn("D3", shifts[0])  # still D3 on duty after restart
        self.assertIn("D4", shifts[1])

    def test_fresh_database_seeds_starter_doctors(self):
        h = Hospital(db_path=self.db_path)
        self.assertEqual(len(h.doctors_map), 5)
        self.assertEqual(h.doctors_map["D1"]["name"], "Dr. Tariq")
        h2 = self._reopen()
        self.assertEqual(len(h2.doctors_map), 5)
        self.assertEqual(_shifts_of(h2)[0], "D1 - Dr. Tariq")

    def test_in_memory_hospital_writes_nothing(self):
        h = Hospital()  # no db_path: pure in-memory, as before
        self.assertIsNone(h.db)
        h.add_patient(101, "Ahmed", 30, "Fever")
        self.assertFalse(os.path.exists(self.db_path))


if __name__ == "__main__":
    unittest.main()
