# Architecture

## Model (`hospital.py` — Ali's file, unchanged)

All state lives in one `Hospital` object, built only from the data
structures already in the file:

| Structure | Used for |
| --------- | -------- |
| `Node` / `LinkedList` | Patient notes (each patient has a notes list) |
| `Stack` | Undo stack — `("ADD_PATIENT", pid)`, `("ADD_NOTE", pid)`, `("BOOK_APPT", doc_id, token, pid)`, `("BED", idx, old_pid)` |
| `Queue` (linked list) | Pharmacy queue (FIFO) |
| `CircularQueue(20)` | Doctor shift rotation — front item is on duty |
| `PriorityQueue` (binary heap) | One per doctor — appointment tokens; Emergency (priority 1) before Normal (priority 2), FIFO within a priority by enqueue count. Token format `E-n` / `N-n` |
| `bubble_sort`, `binary_search` | View Sorted (by id / age) and Find Patient |
| `patients_map` (dict) + `patients_list` (list) | O(1) lookup by id + ordered records |
| `doctors_map` (dict) | Doctors with id, name, specialty, fee, queue |
| `beds` (list of 10) | Bed slots — `0` = free, otherwise patient id |

Starter doctors (`STARTER_DOCTORS`): D1 Dr. Tariq (Neurosurgeon, 2500),
D2 Dr. Ayesha (Cardiologist, 2000), D3 Dr. Bilal (Orthopedic, 1800),
D4 Dr. Sana (Physician, 1200), D5 Dr. Kamran (Pediatrician, 1500).
They also seed the shift rotation.

## Database — Step 1 (SQLite persistence)

- `database.py` — `Database` class (stdlib `sqlite3` only, no new
  dependencies). One file, `hospital.db`, next to `hospital.py`.
- `Hospital(db_path="hospital.db")` loads saved state on start (or seeds
  the file with the 5 starter doctors on first run) and saves after
  every change — add patient/note/doctor, book appointment, bed
  assign/free, undo, pharmacy add/serve, shift rotate. `Hospital()`
  with no path stays fully in-memory (used by the tests).
- Saved: patients + notes (in order), doctors + token counters,
  appointment heaps (heap order kept, so Emergency-first still works
  and token numbers keep counting up), beds, pharmacy queue (FIFO
  order), shift rotation (duty order). The undo stack is session-only
  and starts empty after a restart.
- The CLI (`hospital.py`) and the server (`server.py`) open the same
  `hospital.db`, so a patient added in one shows up in the other.
- `hospital.db` is local data, not source: it is in `.gitignore` and
  is never committed. Delete it to start fresh.
- Tests: `tests/test_database.py` — save → restart → reload round-trips
  for patients/notes, appointments, beds, pharmacy, shifts, doctors.

## Login & Roles — Step 2 (`auth.py` + `server.py` + `web/`)

- `auth.py` — `AuthStore` (stdlib `sqlite3` only). One table, `users`,
  in the same `hospital.db` file: `username` (PK), `password_hash`,
  `role` (Admin / Doctor / Receptionist), `doctor_id` (nullable —
  links a Doctor user to one doctor's queue). Passwords are hashed
  with PBKDF2-HMAC-SHA256 (200,000 rounds, random 16-byte salt, stored
  as `salt_hex:hash_hex`) — plain-text passwords are never stored.
- On first run, when the `users` table is empty, the store seeds one
  default Admin: `admin` / `admin123`. Change it after login.
- `PERMISSIONS` in `auth.py` is the single source of truth: Admin can
  do everything; Doctor can view, add notes, rotate shifts, and see
  their own appointments; Receptionist can register patients, book
  appointments, manage beds and pharmacy, and rotate shifts. Only
  Admin can add doctors/users, undo, or reset passwords. Every role
  can change its own password.
- **Sessions:** the server keeps login sessions in memory — a random
  32-byte token (`secrets.token_hex`) in an `hms_session` cookie
  (HttpOnly, `SameSite=Lax`, 12-hour expiry). Restarting the server
  ends all sessions (log in again); users themselves live in
  `hospital.db` and survive restarts. Enforcement lives in the UI
  layers (CLI menu + server endpoints), not in the `Hospital` class —
  tests call the class directly without a login.
- **CLI:** `hospital.py` asks for username/password at startup
  (`getpass`, so the password is not echoed) and loops until the
  credentials are right; the menu then shows only the options the
  role may use, and every choice is permission-checked again.
- **Web:** `web/login.html` is the login page (served instead of the
  dashboard when there is no session); `app.js` calls `/api/me` on
  boot, hides tiles the role cannot use, and redirects to the login
  page on any 401. Admin gets a **Users** tile (list users, add user,
  reset password) and every role gets **Change Password** in the
  header. The "Add Doctor" form is hidden for non-Admins.
- Tests: `tests/test_auth.py` (hashing, permissions table, default
  admin, restart persistence, password change/reset) and
  `tests/test_server.py::TestServerAuth` (401 without login, 403 per
  role, doctor queue filtering, logout).

## Billing + Reports — Step 3 (`billing.py` + `hospital.py`)

- `billing.py` — `Billing` class (stdlib `sqlite3` + `csv` only). Two
  tables in the same `hospital.db` file, managed only here (like the
  users table): `bills` (`id`, `patient_id`, `patient_name`,
  `doctor_id`, `doctor_name`, `token`, `date`, `status`) and
  `bill_items` (`bill_id`, `position`, `label`, `amount`). With no
  database path the store is purely in-memory (used by the tests).
- **Billing flow:** a bill is created from a booked appointment
  (`Hospital.create_bill(patient_id, doctor_id, token)`) — the first
  line item is always that appointment's consultation fee. Extra
  charges (medicine, bed charges, lab...) are added with
  `add_charge(bill_id, label, amount)`. A token can be billed only
  once. Amounts are whole rupees (integers); a bill's total is always
  the SUM of its line items, never a stored number. No tax or discount
  logic. Status is `Unpaid` or `Paid` (`mark_paid` / `mark_unpaid`).
- **Dates for reports:** patients remember their registration date
  (`registered`) and appointments their booking date (7th field of the
  heap row) — both stored in `hospital.db`. Databases made before Step
  3 are migrated on connect (`ALTER TABLE ... ADD COLUMN`); old rows
  get NULL ("date unknown") and are never counted for any day.
- **Reports** (`Hospital.daily_report(date)`, today by default) are
  computed from live data: patients registered that day, appointments
  booked that day, money billed / collected that day, per-doctor
  appointments (that day + waiting now), bed occupancy, unpaid bills
  (count + total owed, all time). CSV export (`Billing.to_csv()`) is
  one header row + one row per bill, items as `label: Rs amount`.
- **Permissions** (see `PERMISSIONS` in `auth.py`): Admin and
  Receptionist create and change bills; every role may view bills and
  reports (Doctor read-only). Enforced in the CLI menu and on every
  API route (401 without login, 403 for a disallowed role).
- **CLI:** menu **15. Billing** (create bill, add charge, view/list
  bills, mark paid/unpaid, export `bills.csv`) and **16. Reports**
  (daily report for today or any date you type).
- **Web:** **Billing** tile (create-bill form, bills table, bill
  detail with items + add-charge + pay/unpay, Download CSV link) and
  **Reports** tile (date picker + stat cards + per-doctor table). The
  dashboard also shows Billed Today / Collected Today cards.
- Tests: `tests/test_billing.py` (bill creation, duplicate-token guard,
  charges, totals, paid/unpaid, report numbers, CSV contents,
  restart persistence, old-database migration) and
  `tests/test_server.py::TestServerBilling` (API flow, 401/403 per
  role, reports, CSV download, dashboard money stats).

## Appointment Date & Time — Step 4 (`hospital.py` + `database.py` + `server.py`)

- **Heap row, now 9 fields:** `(priority, seq, token, pid, name, fee,
  booked_date, appt_date, appt_time)`. Field 7 is still the booking
  date (when the token was issued — the daily report counts by it);
  fields 8 and 9 are the SCHEDULED date/time (when the patient should
  come). The heap orders by `(priority, seq)` only, so date/time never
  changes who is served first.
- **Validation** (`validate_appt_datetime` in `hospital.py`): date must
  be `YYYY-MM-DD` (empty = today), time must be `HH:MM` 24-hour
  (empty = no fixed time). Anything else raises `ValueError` with a
  plain message. `PriorityQueue.enqueue` validates; `Hospital.book_appointment`
  catches it, prints the reason and books nothing (returns `None`).
- **Views** (`Hospital.appointments_for_date(date, doctor_id)`, today by
  default; `Hospital.doctor_schedule(doctor_id, date)`): dicts with
  token, doctor, patient, fee, emergency, booking date and scheduled
  date/time, sorted by time (no fixed time last), then Emergency-first,
  then token order. Appointments booked before Step 4 have
  `appt_date=None` and never appear in a dated view — they still show
  in the doctor's queue and still work for billing.
- **Database:** `appointments` gains `appt_date` / `appt_time` columns
  (added to `SCHEMA` and to `_migrate`, same `ALTER TABLE` pattern as
  Step 3). Save/load carry all 9 fields, with length guards for rows
  built before Step 3/4.
- **API:** `POST /api/appointments` takes optional `date` / `time`
  (400 on bad values, token + date/time in the answer);
  `GET /api/appointments?date=...&doctor_id=...` lists that day's
  appointments. A Doctor user linked to one doctor only sees that
  doctor's day, whatever they ask for. Queue items in `/api/doctors`
  and `/api/state` also carry `booked_date`, `appt_date`, `appt_time`.
- **CLI:** booking (menu **6**) asks for date and time; menu
  **17. Schedule** shows one day's appointments (today by default)
  for one doctor or all. **Web:** the booking form has Date (defaults
  to today) and Time fields; the **Schedule** tile has a date picker
  and doctor filter; doctor queues show each token's scheduled
  date/time.
- **Permissions:** booking uses the existing `book_appointment` rule
  (Admin/Receptionist); the schedule view is `view` (every role).
- Tests: `tests/test_appointments.py` (validation, defaults, bad
  date/time books nothing, queue order unchanged, date/doctor
  filters, time-then-emergency sorting, billing from a dated token,
  restart persistence, old 7-field rows still load) and
  `tests/test_server.py::TestServerAppointments` (API booking,
  defaults, 400s, schedule filters, Doctor-only-own-day, billing
  from a dated token).

## Patient Search / Edit / Delete — Step 5 (`hospital.py` + `auth.py` + `server.py` + `web/`)

- **Search** (`Hospital.search_patients(name)`): partial, case-insensitive
  substring match on the patient name, results sorted by ID, returned as
  plain dicts (notes as a list — see `_public_patient` in `hospital.py`).
  An empty query matches nothing, so the caller gets an empty list
  instead of the whole register. No database change was needed: the
  patients table already holds everything, and `Database.save()` rewrites
  it on every change.
- **Edit** (`Hospital.edit_patient(pid, name, age, problem)`): only the
  fields you pass change; the ID never changes. Values are validated
  (non-empty name/problem, age 0–150) and a bad value raises ValueError
  with the patient unchanged. A renamed patient's queued appointment rows
  get the new name too (heap order is untouched — priority/seq never
  change, so the heap stays valid); bills keep the name they were made
  with. Edits are saved at once and survive a restart.
- **Delete** (`Hospital.delete_patient(pid)`): removes the patient record
  + notes + queued appointments (via the existing `PriorityQueue.remove_token`,
  so the heap stays valid) and frees any bed they occupy. Bills are KEPT
  — they are financial records. Returns a summary dict
  (`notes_removed`, `appointments_removed`, `beds_freed`). Stale undo
  entries that mention the patient are harmless: every undo branch
  already guards on the patient existing.
- **Permissions** (`auth.py`): `edit_patient` is Admin + Receptionist,
  `delete_patient` is Admin only, search is the open `view` rule.
- **API:** `GET /api/patients?name=...` (empty list on no match);
  `POST /api/patients/<id>` with any of `{name, age, problem}` (400 on
  bad values, 404 on a missing patient); `DELETE /api/patients/<id>`
  (new `do_DELETE` handler in `server.py` — same login + role checks
  as the other routes; 404 on a missing patient).
- **CLI:** menu **3. Find Patient** now asks *By ID* or *By Name*; menu
  **18. Edit Patient** (blank keeps the current value) and **19. Delete
  Patient** (shows what will go away and only deletes on a typed `YES`).
  **Web:** the Find Patient tile has a *Search by Name* form (results as
  patient cards), and each card has Edit (Admin/Receptionist) and Delete
  (Admin only) buttons — Delete asks with a `confirm()` dialog first.
- Tests: `tests/test_patients.py` (search partial/case-insensitive/no
  match/empty, edit all/one field/bad values/rename updates queue/restart
  persistence, delete record + notes + appointments + bed freed/bills
  kept/missing patient/restart persistence) and
  `tests/test_server.py::TestServerPatients` (API search, edit partial
  update + 400s + 404, role rules for edit and delete, delete removes
  record/notes/appointments/frees bed/keeps bill, delete 404).

## Server (`server.py`)

- Python stdlib only: `ThreadingHTTPServer` + `SimpleHTTPRequestHandler`.
- Serves the static single-page UI from `web/` and the JSON API in
  `docs/api.md`.
- One global `Hospital` instance, guarded by a `threading.Lock` so two
  browser tabs can't race on the same structures.
- `hospital.py` methods mostly print instead of returning, so the server
  usually determines success from state before/after each call.
  `book_appointment` returns its token (or `None` on failure), and the
  Step 3/5 methods (`create_bill`, `search_patients`, `edit_patient`,
  `delete_patient`) return their results, which the endpoints use directly.
- State is saved to `hospital.db` on every change (see Database above),
  so restarting the server keeps all data. This is a university
  project, not for production.

## Frontend (`web/`)

- `index.html` — single page: header (logo + Undo + dark-mode toggle),
  live stat strip (Patients / Doctors / Waiting / Free Beds), all 14
  operations as tiles grouped in sections (Patients / Doctors &
  Appointments / Hospital), and a central workspace panel where the
  clicked operation opens its form. No separate pages.
- `styles.css` — light theme white / blue / dark blue (patient tiles
  teal, doctor tiles blue); dark mode uses dark-green accents and a
  green glowing logo. Buttons are rounded with borders darker than the
  button body; hover/press lightens them.
- `app.js` — vanilla JS, `fetch` against `/api/*`. No build step, no
  dependencies, no images — icons are inline SVG (doctor cap, ♂/♀
  symbols, clock, bed) and the logo is a blue rounded square with a
  white cross.

## Tests (`tests/test_hospital.py`)

`unittest` tests for the `Hospital` class: add / duplicate id, find
(binary search), sort (bubble sort), notes, booking tokens
(Emergency before Normal), beds assign/free, undo (add patient /
note / booking / bed), pharmacy FIFO, shift rotation. Step 5 adds
`tests/test_patients.py` (name search, edit, delete + delete rule)
and `tests/test_server.py::TestServerPatients` (the same over HTTP).
Run with:

```bash
python -m unittest discover -s tests -v
```
