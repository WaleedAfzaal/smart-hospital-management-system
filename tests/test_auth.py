"""Login + roles tests for Step 2 (auth.py).

Covers the AuthStore (users in a throwaway SQLite file), password
hashing, and the role permission table. The HTTP-level rules
(401/403 on the API) are tested in test_server.py::TestServerAuth.
Run from the staging root:
    python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import (DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME,  # noqa: E402
                  AuthStore, can, hash_password, verify_password)


class TestPasswordHashing(unittest.TestCase):
    def test_correct_password_verifies_wrong_one_does_not(self):
        stored = hash_password("secret123")
        self.assertTrue(verify_password("secret123", stored))
        self.assertFalse(verify_password("Secret123", stored))
        self.assertFalse(verify_password("", stored))
        self.assertFalse(verify_password("secret123", "not-a-hash"))
        self.assertFalse(verify_password("secret123", ""))

    def test_password_is_never_stored_in_plain_text(self):
        stored = hash_password("secret123")
        self.assertNotIn("secret123", stored)
        # Same password twice -> different hashes (random salt).
        self.assertNotEqual(hash_password("secret123"),
                            hash_password("secret123"))


class TestPermissions(unittest.TestCase):
    def test_admin_can_do_everything(self):
        for action in ["view", "add_patient", "add_note", "book_appointment",
                       "manage_beds", "pharmacy", "rotate_shift", "undo",
                       "add_doctor", "manage_users"]:
            self.assertTrue(can("Admin", action), action)

    def test_doctor_can_only_view_add_notes_and_rotate(self):
        self.assertTrue(can("Doctor", "view"))
        self.assertTrue(can("Doctor", "add_note"))
        self.assertTrue(can("Doctor", "rotate_shift"))
        for action in ["add_patient", "book_appointment", "manage_beds",
                       "pharmacy", "undo", "add_doctor", "manage_users"]:
            self.assertFalse(can("Doctor", action), action)

    def test_receptionist_front_desk_only(self):
        for action in ["view", "add_patient", "book_appointment",
                       "manage_beds", "pharmacy", "rotate_shift"]:
            self.assertTrue(can("Receptionist", action), action)
        for action in ["add_note", "undo", "add_doctor", "manage_users"]:
            self.assertFalse(can("Receptionist", action), action)

    def test_unknown_role_or_action_is_denied(self):
        self.assertFalse(can("Nurse", "view"))
        self.assertFalse(can("Admin", "delete_everything"))


class AuthStoreTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # let the store create it fresh
        self.store = AuthStore(self.db_path)

    def tearDown(self):
        self.store.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _reopen(self):
        self.store.close()
        self.store = AuthStore(self.db_path)


class TestDefaultAdmin(AuthStoreTestCase):
    def test_first_run_seeds_admin_admin123(self):
        user = self.store.authenticate(DEFAULT_ADMIN_USERNAME,
                                       DEFAULT_ADMIN_PASSWORD)
        self.assertIsNotNone(user)
        self.assertEqual(user["role"], "Admin")
        self.assertIsNone(user["doctor_id"])

    def test_no_second_admin_is_seeded_once_users_exist(self):
        self.store.create_user("recep1", "pw123", "Receptionist")
        self._reopen()
        users = self.store.list_users()
        self.assertEqual(len(users), 2)
        self.assertEqual(
            [u["username"] for u in users], ["admin", "recep1"])


class TestAuthenticate(AuthStoreTestCase):
    def test_wrong_password_rejected(self):
        self.assertIsNone(self.store.authenticate("admin", "wrong"))
        self.assertIsNone(self.store.authenticate("admin", ""))

    def test_unknown_user_rejected(self):
        self.assertIsNone(self.store.authenticate("nobody", "admin123"))
        self.assertIsNone(self.store.authenticate("", "admin123"))

    def test_created_user_can_log_in_with_its_role(self):
        self.store.create_user("drtariq", "docpass", "Doctor", "D1")
        user = self.store.authenticate("drtariq", "docpass")
        self.assertEqual(user, {"username": "drtariq", "role": "Doctor",
                                "doctor_id": "D1"})

    def test_users_survive_a_restart(self):
        self.store.create_user("recep1", "pw123", "Receptionist")
        self._reopen()
        self.assertIsNotNone(self.store.authenticate("recep1", "pw123"))
        self.assertIsNotNone(
            self.store.authenticate("admin", "admin123"))


class TestUserManagement(AuthStoreTestCase):
    def test_duplicate_username_rejected(self):
        self.store.create_user("recep1", "pw123", "Receptionist")
        with self.assertRaises(ValueError):
            self.store.create_user("recep1", "other", "Doctor")

    def test_bad_role_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_user("x", "pw123", "Nurse")
        with self.assertRaises(ValueError):
            self.store.create_user("x", "pw123", "")

    def test_empty_username_or_password_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_user("", "pw123", "Admin")
        with self.assertRaises(ValueError):
            self.store.create_user("x", "", "Admin")

    def test_list_users_never_exposes_password_hashes(self):
        self.store.create_user("recep1", "pw123", "Receptionist")
        for user in self.store.list_users():
            self.assertNotIn("password_hash", user)
            self.assertNotIn("pw123", str(user))

    def test_change_password_needs_the_current_one(self):
        self.store.create_user("recep1", "old", "Receptionist")
        self.assertFalse(
            self.store.change_password("recep1", "wrong", "new"))
        self.assertTrue(
            self.store.change_password("recep1", "old", "new"))
        self.assertIsNotNone(self.store.authenticate("recep1", "new"))
        self.assertIsNone(self.store.authenticate("recep1", "old"))

    def test_admin_reset_password_without_the_old_one(self):
        self.store.create_user("recep1", "old", "Receptionist")
        self.store.reset_password("recep1", "brand-new")
        self.assertIsNotNone(
            self.store.authenticate("recep1", "brand-new"))
        with self.assertRaises(ValueError):
            self.store.reset_password("ghost", "brand-new")


if __name__ == "__main__":
    unittest.main()
