"""Login + roles for the Hospital Management System.

Step 2 of the improvement plan: before this, anyone who opened the CLI
or the dashboard could change everything. Now every user logs in with a
username and password, and each role can only do what its job needs.

Only the Python standard library is used — passwords are hashed with
``hashlib.pbkdf2_hmac`` (salted, 200,000 rounds) and NEVER stored in
plain text. Users live in a ``users`` table in the same SQLite file
(``hospital.db``) as the hospital data, so they survive a restart too,
but the table is managed only here — ``Database.save()`` never touches
it.

Roles
-----
* Admin — everything: patients, notes, appointments, beds, pharmacy,
  shifts, undo, add doctors, manage users.
* Doctor — sees patients and their own appointments, can add notes.
  No adding patients, no booking, no beds/pharmacy, no undo, no
  doctor/user management.
* Receptionist — registers patients, books appointments, manages beds
  and pharmacy. No notes, no undo, no doctor/user management.

A Doctor user can be linked to a doctor record (``doctor_id``, e.g.
``D1``) — then they only see that doctor's appointment queue. A
Doctor user with no link sees every queue (and the dashboard says so).

First run: when the users table is empty, one default Admin account is
created — username ``admin``, password ``admin123``. Change it after
the first login (Admin menu / Users page). The credentials are written
in README.md so nobody gets locked out of their own hospital.
"""

import hashlib
import hmac
import os
import sqlite3
import threading

ROLES = ("Admin", "Doctor", "Receptionist")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"

# action -> roles allowed to do it. "view" covers reading anything
# (dashboard, patients, doctors, beds, pharmacy, shifts).
PERMISSIONS = {
    "view": {"Admin", "Doctor", "Receptionist"},
    "add_patient": {"Admin", "Receptionist"},
    "add_note": {"Admin", "Doctor"},
    "book_appointment": {"Admin", "Receptionist"},
    "manage_beds": {"Admin", "Receptionist"},
    "pharmacy": {"Admin", "Receptionist"},
    "rotate_shift": {"Admin", "Doctor", "Receptionist"},
    "undo": {"Admin"},
    "add_doctor": {"Admin"},
    "manage_users": {"Admin"},
    # Step 3 (Billing + Reports): Admin and Receptionist make and manage
    # bills; every role may VIEW bills and reports (Doctor read-only).
    "create_bill": {"Admin", "Receptionist"},
    "manage_bills": {"Admin", "Receptionist"},
    "view_bills": {"Admin", "Doctor", "Receptionist"},
    "view_reports": {"Admin", "Doctor", "Receptionist"},
    # Step 5 (Patient search / edit / delete): searching is "view"
    # (every role); editing is Admin + Receptionist; deleting is
    # Admin only (with a confirmation step in the CLI / dashboard).
    "edit_patient": {"Admin", "Receptionist"},
    "delete_patient": {"Admin"},
}

_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    doctor_id TEXT
);
"""

_PBKDF2_ROUNDS = 200_000


def can(role, action):
    """True if ``role`` is allowed to do ``action`` (see PERMISSIONS)."""
    return role in PERMISSIONS.get(action, set())


def hash_password(password, salt=None):
    """Hash a password with a random salt. Returns ``salt_hex:hash_hex``."""
    if salt is None:
        salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password, stored):
    """Check a plain password against a stored ``salt_hex:hash_hex`` value."""
    try:
        salt_hex, hash_hex = stored.split(":", 1)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex),
        _PBKDF2_ROUNDS,
    )
    return hmac.compare_digest(digest, expected)


class AuthStore:
    """User accounts in the hospital SQLite file.

    One persistent connection (``check_same_thread=False``) guarded by a
    lock, so the threaded web server can safely share one store.
    """

    def __init__(self, db_path):
        self.path = os.path.abspath(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(_USERS_SCHEMA)
            self._conn.commit()
            self._ensure_default_admin()

    def _ensure_default_admin(self):
        """First run only: create admin / admin123 when no users exist."""
        count = self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            self._conn.execute(
                "INSERT INTO users(username, password_hash, role, doctor_id)"
                " VALUES (?, ?, ?, ?)",
                (DEFAULT_ADMIN_USERNAME,
                 hash_password(DEFAULT_ADMIN_PASSWORD), "Admin", None),
            )
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ login
    def authenticate(self, username, password):
        """Return {"username", "role", "doctor_id"} on success, else None."""
        if not username or not password:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT username, password_hash, role, doctor_id FROM users"
                " WHERE username = ? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
        if not row or not (verify_password(password, row[1]) or verify_password(password.strip(), row[1])):
            return None
        return {"username": row[0], "role": row[2], "doctor_id": row[3]}

    # ------------------------------------------------------------ users
    def get_user(self, username):
        """Public user record (no password hash) or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT username, role, doctor_id FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
        if not row:
            return None
        return {"username": row[0], "role": row[1], "doctor_id": row[2]}

    def list_users(self):
        """All users, without password hashes, sorted by username."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT username, role, doctor_id FROM users ORDER BY username"
            ).fetchall()
        return [{"username": r[0], "role": r[1], "doctor_id": r[2]}
                for r in rows]

    def create_user(self, username, password, role, doctor_id=None):
        """Add a user. Raises ValueError on bad input or duplicate name."""
        username = (username or "").strip()
        if not username:
            raise ValueError("Username is required.")
        if not password:
            raise ValueError("Password is required.")
        if role not in ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
        doctor_id = (doctor_id or "").strip().upper() or None
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
            if exists:
                raise ValueError("Username already exists.")
            self._conn.execute(
                "INSERT INTO users(username, password_hash, role, doctor_id)"
                " VALUES (?, ?, ?, ?)",
                (username, hash_password(password), role, doctor_id),
            )
            self._conn.commit()
        return {"username": username, "role": role, "doctor_id": doctor_id}

    def change_password(self, username, current_password, new_password):
        """Change your own password. Returns False if the current one is wrong."""
        if not new_password:
            raise ValueError("New password is required.")
        with self._lock:
            row = self._conn.execute(
                "SELECT username, password_hash FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
            if not row or not (verify_password(current_password, row[1]) or verify_password(current_password.strip(), row[1])):
                return False
            self._conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(new_password.strip()), row[0]),
            )
            self._conn.commit()
        return True

    def reset_password(self, username, new_password):
        """Admin reset: set a user's password without knowing the old one."""
        if not new_password:
            raise ValueError("New password is required.")
        with self._lock:
            row = self._conn.execute(
                "SELECT username FROM users WHERE username = ? COLLATE NOCASE", (username.strip(),)
            ).fetchone()
            if not row:
                raise ValueError("User not found.")
            self._conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(new_password.strip()), row[0]),
            )
            self._conn.commit()
