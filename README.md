# Hospital Management System

A lightweight, zero-dependency hospital reception and OPD management system written in Python, paired with a browser-based dashboard.

It is built around classic data structures — a **singly linked list** for patient notes, a **stack** for undo, a **queue** for the pharmacy line, a **circular queue** for doctor shifts, and a **priority queue (binary heap)** for appointments — plus **bubble sort** and **binary search**, served over Python's standard-library `ThreadingHTTPServer`.

## Features

- **Patient Registration & Lookup**: Patients stored in a dict + list with input validation and duplicate-ID prevention; sorted view by ID or age (bubble sort); lookup by ID (binary search).
- **Patient Notes**: Visit notes kept in a linked list per patient; each note counts as a visit.
- **Doctor Directory**: 5 starter doctors with specialty and token fee. View doctors or add a new one.
- **Priority Appointments**: Book an appointment as Emergency or Normal — emergency tokens (`E-1`, `E-2`…) are always served before normal ones (`N-1`, `N-2`…).
- **Beds**: 10 beds — assign a patient to a free bed or free an occupied one.
- **Undo**: One-click undo for the last action (add patient, add note, book appointment, bed assign/free), powered by an undo stack.
- **Pharmacy Queue**: FIFO token queue — add a token, serve the next one, or view the line.
- **Shift Rotation**: Doctors rotate on duty through a circular queue.
- **Dashboard**: Live counts — patients, doctors, waiting appointments, free beds.
- **SQLite Persistence**: All data is saved to `hospital.db` (created automatically) on every change — patients, notes, doctors, appointments, beds, pharmacy queue and shifts all survive a restart. The CLI and the web dashboard share the same database file. Delete `hospital.db` to start fresh.
- **Login & Roles**: Every user logs in with a username, password and role — Admin, Doctor or Receptionist — and can only do what their role allows (see [Login & Roles](#login--roles) below). Passwords are stored as salted hashes, never as plain text.
- **Zero External Dependencies**: Runs on Python 3.9+ using only the standard library.

## Login & Roles

Both the CLI (`python hospital.py`) and the web dashboard ask you to log in first. On the very first run the system creates one default Admin account:

| Username | Password |
| -------- | -------- |
| `admin`  | `admin123` |

**Change this password after your first login** — in the CLI choose menu option **14. Change My Password**, or in the dashboard click **Change Password** at the top. An Admin can also reset any user's password from the **Users** tile.

### Roles — what each role can do

| Action | Admin | Doctor | Receptionist |
| ------ | :---: | :----: | :----------: |
| View patients, doctors, dashboard | ✅ | ✅ | ✅ |
| Register patients | ✅ | ❌ | ✅ |
| Add patient notes | ✅ | ✅ | ❌ |
| Book appointments | ✅ | ❌ | ✅ |
| Manage beds & pharmacy queue | ✅ | ❌ | ✅ |
| Rotate shifts | ✅ | ✅ | ✅ |
| Add doctors | ✅ | ❌ | ❌ |
| Undo last action | ✅ | ❌ | ❌ |
| Manage users (add user, reset password) | ✅ | ❌ | ❌ |
| Change own password | ✅ | ✅ | ✅ |

A Doctor account can be **linked to one doctor** (e.g. `D1`) — then the dashboard and the CLI show only that doctor's appointment queue. A Doctor with no linked doctor sees all queues.

### Adding users (Admin only)

- **CLI:** log in as Admin → menu option **13. Users (Admin)** → *Add user* — enter a username, password, role, and (for a Doctor) the linked doctor ID.
- **Dashboard:** log in as Admin → **Users** tile → fill in the *Add User* form.

Users are stored in the same `hospital.db` file, so they survive a restart. Sessions last 12 hours; closing the browser tab does not log you out, but restarting the server does (log in again).

## Quick Start

1. Clone the repository:

```bash
git clone https://github.com/WaleedAfzaal/smart-hospital-management-system.git
cd smart-hospital-management-system
```

2. Start the server:

```bash
python server.py
```

By default, the server binds to `http://127.0.0.1:8000` and opens the dashboard in your default browser.

### CLI Options

```bash
python server.py --host 127.0.0.1 --port 8080 --no-browser
```

- `--host`: Bind address (default: `127.0.0.1`)
- `--port`: Port number (default: `8000`)
- `--no-browser`: Start the server without opening a browser tab

You can also run the console version directly:

```bash
python hospital.py
```

## Project Structure

```text
.
├── hospital.py               # Hospital class — all data structures & operations
├── auth.py                   # Login & roles — password hashing, users table, permissions
├── database.py               # SQLite persistence — save/load whole state to hospital.db
├── hospital.db               # Local database file (auto-created, gitignored, never committed)
├── server.py                 # ThreadingHTTPServer, static file serving, and JSON REST API
├── web/
│   ├── index.html           # Single-page dashboard (all operations on one page)
│   ├── login.html           # Login page (shown when not logged in)
│   ├── styles.css           # Dashboard styling (light + dark mode)
│   └── app.js               # Frontend state, API calls, and DOM updates
├── docs/
│   ├── architecture.md      # Data structures, concurrency model, and state lifecycle
│   └── api.md               # REST API reference and payload examples
├── tests/
│   ├── test_hospital.py     # Unit tests for the Hospital class
│   ├── test_server.py       # End-to-end API tests for the web server
│   ├── test_auth.py         # Login, password hashing, and role permission tests
│   └── test_database.py     # Persistence round-trip tests (save → restart → reload)
├── CONTRIBUTING.md
└── LICENSE
```

`hospital_system.py` is an earlier version of the backend, superseded by `hospital.py` (kept for reference).

## Running Tests

Run the test suite with Python's built-in `unittest` runner:

```bash
python -m unittest discover -s tests -v
```

## Documentation

- [Architecture & Data Structures](docs/architecture.md)
- [REST API Reference](docs/api.md)

## License

Released under the [MIT License](LICENSE).
