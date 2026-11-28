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

Doctors start empty — staff add their own doctors, and each added
doctor also joins the shift rotation.

## Database — Step 1 (SQLite persistence)

- `database.py` — `Database` class (stdlib `sqlite3` only, no new
  dependencies). One file, `hospital.db`, next to `hospital.py`.
- `Hospital(db_path="hospital.db")` loads saved state on start (or
  saves the fresh empty state to the file on first run) and saves after
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
  boot, hides sidebar entries the role cannot use, and redirects to
  the login page on any 401. Admin gets **Settings → Manage Users**
  (list users, add user, reset password) and every role gets
  **Change Password** in the profile menu. The "Add Doctor" form is
  hidden for non-Admins.
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
- **Web:** **Billing & Payments** in the sidebar (create-bill form, bills table, bill
  detail with items + add-charge + pay/unpay, Download CSV link) and
  **Reports & Analytics** in the sidebar (date picker + stat cards + per-doctor table). The
  dashboard shows Total Revenue (Paid bills) and Pending Bills cards.
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
  to today) and Time fields; the **Schedule** view has a date picker
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
  **Web:** the Find Patient view has a *Search by Name* form (results as
  patient cards), and each card has Edit (Admin/Receptionist) and Delete
  (Admin only) buttons — Delete asks with a `confirm()` dialog first.
- Tests: `tests/test_patients.py` (search partial/case-insensitive/no
  match/empty, edit all/one field/bad values/rename updates queue/restart
  persistence, delete record + notes + appointments + bed freed/bills
  kept/missing patient/restart persistence) and
  `tests/test_server.py::TestServerPatients` (API search, edit partial
  update + 400s + 404, role rules for edit and delete, delete removes
  record/notes/appointments/frees bed/keeps bill, delete 404).

## Appointment Statuses + History — Step 6 (`hospital.py` + `status_history.py` + `database.py` + `auth.py` + `server.py` + `web/`)

- **Status** (`hospital.py`): the seven statuses are
  `APPOINTMENT_STATUSES` = Pending, Confirmed, Checked In, In
  Progress, Completed, Cancelled, No Show; `DOCTOR_STATUSES` is the
  three a Doctor may set. The status rides as the **10th field** of
  the heap row `(priority, seq, token, pid, name, fee, booked_date,
  appt_date, appt_time, status)`; `_row_status(row)` returns
  `"Pending"` for 9-field rows (booked before Step 6) or an empty
  stored value, and `_public_appointment(doc, row)` is the one
  dict shape the CLI, the API and the schedule views all use, so the
  status is the same everywhere. `Hospital.update_status(doc_id,
  token, new_status, changed_by, role, user_doctor_id)` validates the
  status, the doctor and the token (ValueError), enforces the role
  rule (PermissionError — Doctor: own queue only, and only the three
  consultation statuses; Admin/Receptionist: any status on any
  queue), then changes only the 10th field (priority/seq untouched,
  so the heap stays valid), records the change and saves at once.
  Setting the status it already has is a no-op — no history row.
- **History** (`status_history.py`): a new `StatusHistory` class,
  following the `billing.py` pattern — its own locked sqlite3
  connection to the same `hospital.db`, table
  `appointment_status_history(id, doctor_id, token, old_status,
  new_status, changed_by, changed_at)` with an index on
  `(doctor_id, token, changed_at)`, `record()` + `history()` (oldest
  first), in-memory list fallback when no db path is given. The key
  is doctor_id + token (stable until a global appointment ID arrives
  in Step 8 of the appointment module plan). History is append-only
  audit — never edited or deleted, and kept even when the patient or
  the appointment is deleted or undone. `Database.save()` never
  touches it.
- **Database** (`database.py`): `appointments` gains a `status`
  column (schema + `_migrate` ALTER TABLE for older files); old rows
  save/load as Pending (`status_ if status_ else "Pending"` on
  load). A database built before Step 6 is verified to load this way
  in the tests (hand-built old table without the column + a NULL
  status in a new table).
- **No double booking** (`Hospital.find_conflicting_appointment`):
  the same doctor, same `appt_date`, same `appt_time`, while the
  existing appointment is ACTIVE (anything except Cancelled / No
  Show — Completed still blocks its slot). Untimed appointments
  never collide. `book_appointment` checks first and refuses with a
  plain message naming the token in the way; the API returns the
  same as a 400.
- **Permissions** (`auth.py`): `update_appointment_status` is
  Admin + Doctor + Receptionist in PERMISSIONS (every role may open
  the action); the fine rule lives in `Hospital.update_status` and
  the server returns 403 on PermissionError.
- **API:** `POST /api/appointments/<doctor>/<token>/status`
  (`{status}` — 400 on a bad status/missing token, 403 on the role
  rule; returns the updated appointment + its full history) and
  `GET /api/appointments/<doctor>/<token>/history` (404 on a missing
  doctor/token; a linked Doctor may only read their own queue's).
  The booking route checks the guard before booking (400). Queue
  items in `_serialize_doctor` and rows in
  `appointments_for_date` now include `status`.
- **CLI:** menu **20. Appointment Status**
  (`appointment_status_menu`) — shows the current status and the
  full history, then lets the user pick 1–7; the same role rules
  apply. Doctor queues and the Schedule view print the status too.
- **Web:** queue tokens carry a coloured status badge (7 CSS
  classes in `styles.css`); each doctor card has an Appointment
  dropdown + New status dropdown (Doctors only get their three) +
  Update Status and History buttons; the Schedule table has a Status
  column; the booking result card says "Status: Pending".
- Tests: `tests/test_appointment_status.py` (default Pending,
  transitions + history with user + timestamp, full journey, no-op,
  bad status/token, heap order unchanged, schedule view, old-table
  and NULL-status migration, double-booking rejection vs allowed
  cases — different doctor/time/date, Cancelled/No Show freeing the
  slot, Completed still blocking, untimed never colliding — role
  enforcement incl. unlinked Doctor, history persistence across
  restart and across patient delete) and
  `tests/test_appointment_status.py::TestStatusServer` (status +
  history endpoints, Doctor 403 on Cancel, Doctor check-in allowed,
  Receptionist any status, double-booking 400, state + schedule
  include status). One older test changed: `test_appointments.py`
  `test_same_time_emergency_first` books its second 09:00 on D2
  instead of D1, because the guard now refuses it on D1 (the test's
  point — same time, Emergency first — is unchanged).

## New Dashboard — Step 7 (`hospital.py` + `status_history.py` + `server.py` + `web/`)

- **Layout (from Waleed's design image):** dark navy left sidebar
  (City Care Hospital, menu with submenus, promo box, Version 1.0.0),
  white top bar (search, notification bell with badge, Admin profile
  menu), 6 stat cards, Monthly Appointments Overview line chart,
  Today's Appointments table, Department Statistics donut, Recent
  Activities timeline, Quick Actions, "Your Health Our Priority"
  promo card. Colours: Primary Blue #1769AA, background #F1F5F9,
  text Dark Navy #172033. Responsive (sidebar collapses / becomes an
  overlay on small screens). Dark mode kept.
- **Real data only:** `Hospital.dashboard_data()` aggregates everything
  from live data — counts, money (Total Revenue = Paid bills only),
  the monthly series (this year + rolling last 12 months, counted by
  scheduled month, falling back to booking month), the department
  split (appointments per doctor specialization), today's list with
  real Step 6 statuses, the activity feed (latest status changes with
  timestamps, registrations, bills, bookings) and the notification
  count (pending appointments + unpaid bills). An empty hospital shows
  honest zeros. No number from the design image is hardcoded.
- **Search:** the top bar searches patients (name/ID), doctors
  (name/ID/specialty) and appointments (token/patient name) from the
  live state; a click opens the patient card, the doctors view, or
  the schedule for that appointment's date.
- **Bell:** badge = pending appointments + unpaid bills; the panel
  lists them (click opens the schedule day or the billing view).
- **Sidebar:** every existing module stays reachable (patients,
  doctors + shifts, appointments, beds, pharmacy, billing, reports,
  users under Settings). Departments is a real view (specializations
  with doctor/appointment counts — departments come from doctor
  specializations, there is no separate Departments module yet).
  OPD/IPD, Laboratory, Staff Management and Medical Records are
  marked "Soon" and open an honest note instead of a dead page.
  Settings is real (hospital info, theme, change password, users for
  Admin). Charts are inline SVG drawn in vanilla JS — no CDN, the
  page works offline.
- **API:** `GET /api/dashboard` returns the full payload (see
  `docs/api.md`); the old stats keys are still returned.
- Tests: `tests/test_dashboard.py` (`dashboard_data` counts/money/
  monthly series/department split/today's list/activities/notifications
  on a seeded hospital, empty-hospital zeros, `StatusHistory.recent`,
  `/api/dashboard` payload + 401 without login).

## Book Appointment Form — Step 8 (`hospital.py` + `database.py` + `server.py` + `web/`)

- **Appointment ID** (`Hospital.next_apt_id`): `APT-0001`, `APT-0002`,
  … — one hospital-wide counter, handed out only when a booking is
  really about to happen (after the double-booking check passes).
  The counter lives in the `meta` table (`apt_counter`) and is saved
  on every save, so an ID is never reused after a restart; on load it
  is also pushed above the highest APT number already stored, so a
  missing meta row can never cause a duplicate. The ID rides as the
  **11th field** of the heap row, with the type (12th), reason (13th)
  and notes (14th).
- **Type** (`APPOINTMENT_TYPES` = General, Follow-up, Emergency):
  the type decides the queue priority — Emergency books an `E-` token,
  the others an `N-` token. When no type is given, the old `is_emg`
  flag decides (so older callers and the old API shape keep working).
  `Hospital.book_appointment` takes the new fields as keyword
  arguments (`appt_type`, `reason`, `notes`, `status`) and still
  returns the token (or `None` on failure, printing the reason);
  the full appointment — including the APT ID — is read back with
  `Hospital.get_appointment(doc_id, token)`. The status at booking
  may only be Pending or Confirmed (`BOOKING_STATUSES`).
- **Patient fields** (`father_name`, `phone`, `gender`): stored on
  the patient record, validated by `validate_phone` (digits, spaces,
  dashes, brackets, leading `+` only) and `validate_gender` (Male /
  Female / Other, any case). `Hospital.search_patients(query)` now
  matches all three ways — an exact patient ID, a name substring
  (partial, case-insensitive, as before) or a phone fragment
  (digits only, so "0300" finds "0300-1234567"). `next_patient_id`
  is one above the highest ID in use (101 when empty) — used when a
  patient is registered inside the booking flow.
- **Departments / availability** (`Hospital.departments`,
  `doctors_in_department`, `doctor_booked_times`): departments are
  the distinct doctor specializations (no separate Departments module
  yet). `doctor_booked_times(doctor_id, date)` lists that doctor's
  timed appointments for the day (time, token, status, patient name),
  sorted by time — Cancelled / No Show entries are listed but do not
  block their slot. There is no working-hours system yet (that is
  Step 11), so the form shows booked times and refuses a taken slot
  via the Step 6 guard instead of inventing fixed slots.
- **History key unchanged:** the status history is still keyed by
  doctor + token (see Step 6) — the APT ID rides on the appointment
  record and the API reply, but nothing in the history was re-keyed.
  One Step 6 bug was fixed along the way: `update_status` used to
  rebuild the heap row from its first 9 fields, which would have
  dropped the Step 8 fields — it now keeps everything after the
  status field.
- **Database** (`database.py`): `patients` gains `father_name` /
  `phone` / `gender`, `appointments` gains `apt_id` / `appt_type` /
  `reason` / `notes` (schema + `_migrate` ALTER TABLE for older
  files); old rows load with the new fields empty (APT ID `None`,
  type General). A database built before Step 8 is verified to load
  this way in the tests.
- **API:** `POST /api/appointments` takes `{doctor_id, patient_id |
  new_patient, type?, status?, reason?, notes?, date?, time?}` —
  `new_patient` (`{name, age, problem, father_name?, phone?,
  gender?, id?}`) registers a patient inline (ID auto-assigned when
  left out); everything is validated BEFORE the patient is
  registered, so a failed booking never leaves a stray patient
  behind. The reply carries the full `appointment` (with `apt_id`)
  plus the confirmation fields. `GET /api/appointments/slots?
  doctor_id=...&date=...` returns the booked times. `POST
  /api/patients` and the edit endpoint accept the three new patient
  fields; `GET /api/patients?name=...` matches ID/name/phone. The
  old `emergency` flag still works when no `type` is given.
- **CLI:** menu **6. Book Appt** (`book_appointment_menu`) runs the
  same three steps — search or register a patient, pick department
  then doctor, see the booked times for the day, then
  date/time/type/reason/notes/status — shows a confirmation summary
  and offers to generate the bill (Step 3 flow, only when asked).
  Menu **1. Add Patient** asks the three optional fields too, and
  menu **18. Edit Patient** can change them (blank keeps the value).
- **Web:** the **Book Appointment** form (Appointments in the
  sidebar, or the dashboard Quick Action) is the same three steps —
  live patient search (ID / name / phone) with a selectable result
  list, an inline new-patient form, department → doctor dropdowns
  with fee + specialization shown, live booked-times for the chosen
  doctor/day, type/status dropdowns, reason/notes, and a
  confirmation summary with the Appointment ID and a **Generate
  Bill** button. The Add Patient form and the patient-card edit form
  have the three new fields too, and patient cards show them.
- **Permissions:** booking is still Admin/Receptionist only
  (Doctor cannot book — existing rule, enforced by the server with a
  403); the slots endpoint is `view` for every role, with a linked
  Doctor only ever seeing their own day.
- Tests: `tests/test_book_appointment.py` (APT ID sequential +
  unique + persistent across restart, old rows load without APT
  fields, patient search by ID/name/phone, new patient fields +
  validation, department → doctor filtering, type → token mapping,
  reason/notes/status at booking, double booking still refused,
  booked-times incl. Cancelled freeing the slot, update_status keeps
  the Step 8 fields, plus the API: inline new-patient booking, type
  / reason / notes / status, slots endpoint, validation 400s,
  Doctor booking 403, search by phone/ID, new fields on add/edit).

## Appointments Table + Details Page — Step 9 (`hospital.py` + `pdf_export.py` + `server.py` + `web/`)

- **List** (`Hospital.list_appointments` / `filter_appointments`):
  the whole table is built from all doctors' heaps, flattened to
  plain appointment dicts (patient + doctor names resolved in the
  same pass) and filtered in memory — the hospital is small, and
  this keeps the search (patient name / APT ID / token) and the
  date/doctor/department/status filters in one place. Sorting is by
  date, time, patient name, doctor name or status, ascending or
  descending; undated/untimed rows sort last in ascending order.
  `list_appointments` clamps `per_page` to 1–100 (default 10) and
  `page` to the last page, and returns `{appointments, total,
  pages, page, per_page}`. The pre-Step-8 rows with no APT ID ride
  along with `apt_id` `None` (the table shows "—" for those).
- **Detail** (`Hospital.get_appointment_by_id` /
  `appointment_detail`): finds an appointment by its APT ID — or by
  `"D1:N-1"` (doctor + token) for rows booked before Step 8, which
  have no APT ID — and returns the appointment plus the patient
  (with father/phone/gender), the doctor, the payment status from
  billing (`find_bill_by_token` → `get_bill`, so the total is the
  computed sum of the line items) and the status history. Returns
  `None` for an unknown ID.
- **Edit** (`Hospital.edit_appointment`, `auth.py` permission
  `"edit_appointment"` = Admin + Receptionist): only the fields sent
  change — empty `date` keeps the current date, empty `time` clears
  the time (no fixed time). A date/time change re-runs the Step 6
  double-booking guard **excluding the appointment itself** (a
  reschedule onto the appointment's own slot is allowed; a taken
  slot gets the same 400 naming the token in the way). A type change
  that flips the queue priority changes field 0 in place and
  re-orders the queue with `heapq.heapify` — the token stays the
  same, so the bill and the status history (both keyed by doctor +
  token) stay linked to the appointment, and the APT ID, the status
  and the history are never touched. A type change inside the same
  priority just rebuilds the row in place. Old rows shorter than 14
  fields are padded out properly here. A Doctor gets a
  `PermissionError`; an unknown APT ID or a bad value gets a
  `ValueError`.
- **Export:** `Hospital.appointments_to_csv(rows)` — 12 columns, one
  row per appointment. `pdf_export.py` is a small pure-Python PDF
  generator (stdlib only — no reportlab, no new dependency): it
  builds a landscape-A4 PDF by hand (catalog, pages, Helvetica +
  Helvetica-Bold, content streams, a cross-reference table with
  real byte offsets, `%%EOF`) with a title, the date and the active
  filters, the table (9 columns, long text truncated) and page
  numbers, paginated. Latin-1 sanitisation + PDF-string escaping
  keep any patient/doctor name safe inside the file.
- **API:** `GET /api/appointments/list` (server-side filters/sort/
  page), `GET /api/appointments/by-id/<apt_id>` (full detail),
  `POST /api/appointments/by-id/<apt_id>/edit` (unknown APT ID is
  a 404, not a 400), `GET /api/appointments/export.csv` and
  `/export.pdf` (the current filters, no pagination, `Content-
  Disposition: attachment`). The exports return a binary download
  via the server's `_send_bytes`. A Doctor user linked to one doctor
  is forced to their own doctor on every one of these endpoints
  (list, detail, edit → 403, exports).
- **CLI:** menu **21. All Appointments** (`appointments_table_menu`
  + `_appointment_detail_menu`): the same filters + sort, paged 10
  at a time ([N]ext / [P]revious / APT ID to open / Enter back),
  then the full details (patient, doctor, fee, payment, history)
  with [E]dit / Reschedule, [C]ancel (typed `YES` to confirm) and
  [B]ack. A Doctor sees only their own queue and is read-only.
- **Web:** the **All Appointments** view (sidebar Appointments, or
  the dashboard's "View All") is a filter bar (search/date/doctor/
  department/status + Apply/Clear) over a table with clickable
  sortable headers (▲/▼), pagination (Prev/Next + counts) and
  Export CSV / Export PDF links that carry the current filters;
  each row has View / Edit / Cancel (Edit/Cancel hidden for
  Doctor). The details view shows the details grid, the payment
  block (with a Generate Bill button when unbilled), the
  status-history timeline, a status-change dropdown (Step 6 rules),
  one edit/reschedule form, Cancel with confirmation and **Print**
  (`window.print()` + `@media print` rules that hide the sidebar,
  top bar and buttons). After edit/reschedule/cancel the table
  refreshes to show the change. No new dependency — the PDF is
  generated server-side in pure Python.
- Tests: `tests/test_appointments_table.py` (list filters each
  alone + combined, search matching, sorting each sortable column
  in both orders, pagination boundaries + total count, detail by
  APT ID incl. payment status + history + the `"D1:N-1"` fallback,
  edit field changes / reschedule / double-booking refusal /
  type-flip keeping the token + re-ordering the queue / roles /
  restart persistence, cancel via the status endpoint, CSV + PDF
  exports incl. filters respected and the PDF's xref offsets, the
  HTTP endpoints incl. 404/400/403 rules, and the CLI table menu
  incl. Doctor seeing only their own queue).

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

- `index.html` — single page: dark-navy sidebar (City Care Hospital,
  menu with submenus, promo box, Version 1.0.0), white top bar (search,
  notification bell, profile menu), the Step 7 dashboard view, and a
  central workspace panel where the clicked module opens its form.
  No separate pages.
- `styles.css` — Step 7 dashboard theme (Primary Blue #1769AA, light
  gray #F1F5F9 background, dark navy #172033 text; pastel stat cards);
  dark mode uses dark-green accents and a green glowing logo. Buttons
  are rounded with borders darker than the button body; hover/press
  lightens them.
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
Step 6 adds `tests/test_appointment_status.py` (default Pending,
transitions + history, old-row migration, double-booking guard,
role enforcement, history persistence, plus
`TestStatusServer` over HTTP). Step 7 adds `tests/test_dashboard.py`.
Step 8 adds `tests/test_book_appointment.py` (Appointment IDs,
patient search by ID/name/phone, new patient fields, type → token,
booked-times/slots endpoint, inline new-patient booking over HTTP).
Step 9 adds `tests/test_appointments_table.py` (list filters/search/
sort/pagination, detail incl. payment + history, edit incl. reschedule
guard + type flip, CSV + PDF exports, HTTP role rules, CLI table menu).
Run with:

```bash
python -m unittest discover -s tests -v
```
