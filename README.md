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
- **Zero External Dependencies**: Runs on Python 3.9+ using only the standard library.

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
├── server.py                 # ThreadingHTTPServer, static file serving, and JSON REST API
├── web/
│   ├── index.html           # Single-page dashboard (all operations on one page)
│   ├── styles.css           # Dashboard styling (light + dark mode)
│   └── app.js               # Frontend state, API calls, and DOM updates
├── docs/
│   ├── architecture.md      # Data structures, concurrency model, and state lifecycle
│   └── api.md               # REST API reference and payload examples
├── tests/
│   └── test_hospital.py     # Unit tests for the Hospital class
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
