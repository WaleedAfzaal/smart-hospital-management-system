# Hospital Management System

A lightweight, zero-dependency hospital reception and OPD management system written in Python, paired with a browser-based dashboard.

It is built around classic data structures — a **singly linked list** for patient notes, a **stack** for undo, a **queue** for the pharmacy line, a **circular queue** for doctor shifts, and a **priority queue (binary heap)** for appointments — plus **bubble sort** and **binary search**, served over Python's standard-library `ThreadingHTTPServer`.

## Features

- **Patient Registration & Lookup**: Patients stored in a dict + list with input validation and duplicate-ID prevention; sorted view by ID or age (bubble sort); lookup by ID (binary search) or by name — type part of a name ("ahm" finds "Ahmed Khan") and matching is case-insensitive. A patient's name, age or problem can be edited (the ID never changes); deleting a patient (Admin only, with confirmation) removes their record, notes and queued appointments and frees their bed, while their bills are kept as financial records.
- **Patient Notes**: Visit notes kept in a linked list per patient; each note counts as a visit.
- **Doctor Directory**: Starts empty — add your own doctors with specialty and token fee. View doctors or add a new one.
- **Priority Appointments**: Book an appointment as Emergency or Normal — emergency tokens (`E-1`, `E-2`…) are always served before normal ones (`N-1`, `N-2`…). Every booking gets a unique Appointment ID (`APT-0001`, …) and a Type (General / Follow-up / Emergency), a Reason for Visit and Notes.
- **Appointment Date & Time**: Every appointment is scheduled for a date (`YYYY-MM-DD`, empty = today) and a time (`HH:MM`, 24-hour, empty = no fixed time). The **Schedule** view lists a day's appointments — for one doctor or all — sorted by time, then Emergency-first. The queue order itself never changes: date/time is for scheduling and display only.
- **Appointment Statuses**: Every appointment has a status — Pending, Confirmed, Checked In, In Progress, Completed, Cancelled or No Show. New appointments start Pending; every change is recorded with who made it and when, and the history survives a restart. Booking refuses a double booking: the same doctor cannot have two active appointments at the same date and time. Admin and Receptionist can set any status; a Doctor can only move their own queue's appointments to Checked In, In Progress or Completed.
- **Appointments Table & Details**: **All Appointments** lists every appointment with search (name / APT ID / token), filters (date, doctor, department, status), sortable columns and pagination (10 per page, server-side). Click **View** for the full details — patient, doctor, fee, payment status, booking date and the status-history timeline — with Edit, Reschedule (double-booking check re-run), Cancel and Print. Export the filtered table as **CSV** (Excel) or **PDF** (pure-Python generator, no new dependency).
- **Beds**: 10 beds — assign a patient to a free bed or free an occupied one.
- **Undo**: One-click undo for the last action (add patient, add note, book appointment, bed assign/free), powered by an undo stack.
- **Pharmacy Queue**: FIFO token queue — add a token, serve the next one, or view the line.
- **Shift Rotation**: Doctors rotate on duty through a circular queue.
- **Dashboard**: City Care Hospital dashboard — 6 live stat cards (patients, doctors, today's appointments, free beds, total revenue, pending bills), monthly appointments chart, today's appointments with statuses, department donut, recent activities, working search, notification bell, quick actions. Every number is real data from `hospital.db`.
- **Notifications**: In-app notifications, stored in `hospital.db` — a booking, a cancellation, a reschedule, a status change, a doctor's schedule change, and a reminder for tomorrow's appointments (each reminder created once, never duplicated). The dashboard bell shows the real unread count; clicking a notification opens its appointment's details page. The **Notifications** view filters by type (Booking / Cancellation / Reschedule / Status / Reminder / Schedule change) and All / Unread. A Doctor linked to one doctor only sees their own queue's notifications plus hospital-wide ones.
- **Reports & Analytics**: **Reports & Analytics** — Daily / Weekly / Monthly with a date picker and doctor + department filters: total appointments, by status (all 7), completion / cancellation / no-show rates, per-doctor and per-department breakdowns, revenue from Paid bills in the period + unpaid total, appointments-per-day bar chart and a department donut, plus cancelled and no-show lists. Export the current report as **CSV** (Excel) or **PDF**. A Doctor linked to one doctor only sees their own queue's figures.
- **SQLite Persistence**: All data is saved to `hospital.db` (created automatically) on every change — patients, notes, doctors, appointments, beds, pharmacy queue and shifts all survive a restart. The CLI and the web dashboard share the same database file. Delete `hospital.db` to start fresh.
- **Login & Roles**: Every user logs in with a username, password and role — Admin, Doctor or Receptionist — and can only do what their role allows (see [Login & Roles](#login--roles) below). Passwords are stored as salted hashes, never as plain text.
- **Billing**: Create a bill from a booked appointment — the consultation fee is the first line item — then add extra charges (medicine, bed charges, lab...). Mark bills Paid / Unpaid. A token is billed only once, and totals are always the sum of the line items.
- **Reports**: Daily report — patients registered today, appointments booked today, money billed and collected today, appointments per doctor, bed occupancy, unpaid bills. Export all bills as CSV.
- **Zero External Dependencies**: Runs on Python 3.9+ using only the standard library.

## Login & Roles

Both the CLI (`python hospital.py`) and the web dashboard ask you to log in first. On the very first run the system creates one default Admin account:

| Username | Password |
| -------- | -------- |
| `admin`  | `admin123` |

**Change this password after your first login** — in the CLI choose menu option **14. Change My Password**, or in the dashboard click your profile (top right) → **Change Password**. An Admin can also reset any user's password from **Settings → Manage Users**.

### Roles — what each role can do

| Action | Admin | Doctor | Receptionist |
| ------ | :---: | :----: | :----------: |
| View patients, doctors, dashboard | ✅ | ✅ | ✅ |
| Register patients | ✅ | ❌ | ✅ |
| Search patients by name | ✅ | ✅ | ✅ |
| Edit patient (name, age, problem) | ✅ | ❌ | ✅ |
| Delete patient (with confirmation) | ✅ | ❌ | ❌ |
| Add patient notes | ✅ | ✅ | ❌ |
| Book appointments | ✅ | ❌ | ✅ |
| Manage beds & pharmacy queue | ✅ | ❌ | ✅ |
| Rotate shifts | ✅ | ✅ | ✅ |
| Add doctors | ✅ | ❌ | ❌ |
| Undo last action | ✅ | ❌ | ❌ |
| Manage users (add user, reset password) | ✅ | ❌ | ❌ |
| Create bills & add charges | ✅ | ❌ | ✅ |
| Mark bills Paid / Unpaid | ✅ | ❌ | ✅ |
| View bills, reports & CSV export | ✅ | ✅ | ✅ |
| View notifications (own queue for a linked Doctor) | ✅ | ✅ | ✅ |
| Analytics reports (own queue for a linked Doctor) | ✅ | ✅ | ✅ |
| Change own password | ✅ | ✅ | ✅ |

A Doctor account can be **linked to one doctor** (e.g. `D1`) — then the dashboard and the CLI show only that doctor's appointment queue. A Doctor with no linked doctor sees all queues.

### Adding users (Admin only)

- **CLI:** log in as Admin → menu option **13. Users (Admin)** → *Add user* — enter a username, password, role, and (for a Doctor) the linked doctor ID.
- **Dashboard:** log in as Admin → **Settings → Manage Users** → fill in the *Add User* form.

Users are stored in the same `hospital.db` file, so they survive a restart. Sessions last 12 hours; closing the browser tab does not log you out, but restarting the server does (log in again).

## Billing & Reports

**How a bill is made:** book an appointment first (the token is
issued with the doctor's fee), then create the bill from that token —
the consultation fee becomes the first line item. Add extra charges
afterwards: medicine, bed charges, lab tests, anything (label +
amount in whole rupees). Mark the bill **Paid** when the patient pays.
A token can be billed only once, and the total is always the sum of
the line items — no tax, no discount, nothing hidden.

- **CLI:** log in → menu option **15. Billing** (create bill, add
  charge, view/list bills, mark paid/unpaid, export `bills.csv`) and
  **16. Reports** (1 = daily report for today or any date you type;
  2 = appointment analytics — Daily / Weekly / Monthly with doctor
  and department filters, plus CSV/PDF export to a file), and menu
  option **25. Notifications** (list unread, mark one read, mark all
  read).
- **Dashboard:** **Billing & Payments** in the sidebar (create-bill form, bills table, bill
  detail with items + add-charge + pay/unpay, Download CSV button)
  and **Reports & Analytics** in the sidebar (Daily / Weekly / Monthly
  selector, date picker, doctor + department filters, totals by
  status, completion / cancellation / no-show rates, per-doctor and
  per-department tables, appointments-per-day bar chart, department
  donut, cancelled and no-show lists, Export CSV / PDF buttons), and
  **Notifications** in the sidebar (filter by type and All / Unread,
  click one to open its appointment). The
  dashboard itself also shows **Billed Today** and **Collected Today**.

The daily report counts patients registered that day, appointments
booked that day, money billed and collected that day, appointments per
doctor, bed occupancy, and unpaid bills (count + total owed, all
time). Only Admin and Receptionist can create or change bills —
Doctors can view bills and reports (read-only).

## Appointment Date & Time

When you book an appointment you pick **when** the patient should
come: a date (`YYYY-MM-DD` — leave it empty for today) and a time
(`HH:MM` in 24-hour format, e.g. `10:30` — leave it empty for no fixed
time). A wrong date or time is rejected with a plain message and
nothing is booked. This scheduled date/time is different from the
booking date (the day the token was issued) — the daily report still
counts appointments by booking date, and the Emergency-first queue
order never changes.

- **CLI:** booking (menu option **6. Book Appt**) asks for date and
  time after the Emergency/Normal choice; menu option **17. Schedule**
  shows one day's appointments (today by default, or any date you
  type) for one doctor or all of them, sorted by time.
- **Dashboard:** the **Book Appointment** form has Date and Time
  fields (date defaults to today); the **Schedule** view (Appointments in the sidebar) has a date
  picker and a doctor filter and lists that day's appointments. Doctor
  queues also show each token's scheduled date/time.

Appointments booked before this feature have no scheduled date — they
still work everywhere (queue, billing, reports) but never appear in a
dated schedule view.

## Book Appointment Form

The booking form walks through three steps: **Patient, Doctor,
Appointment**.

1. **Patient** — search an existing patient by **ID, name or phone**
   ("0300" finds a patient whose phone is 0300-1234567) and pick them
   from the results; or register a **new patient without leaving the
   form** (name, age, problem, plus optional Father Name, Phone and
   Gender — the patient ID is assigned automatically).
2. **Doctor** — pick a **Department** (a doctor's specialization),
   then a **Doctor** in that department; the form shows the
   specialization and consultation fee, and the doctor's **already
   booked times for the chosen day**, so a taken slot is visible
   before you pick a time. (There are no fixed working hours yet —
   that comes in a later step — so any free time can be picked.)
3. **Appointment** — date, time, **Type** (General / Follow-up /
   Emergency — Emergency books an E- token, the others an N- token),
   **Reason for Visit**, **Additional Notes** and **Status** (Pending
   or Confirmed). A **Confirm & Book** button saves it and shows a
   confirmation summary — Appointment ID, token, patient, doctor,
   department, date/time, type, fee and status — with a **Generate
   Bill** button (the bill itself is only made when you ask for it).

Every booking gets a unique **Appointment ID** (`APT-0001`,
`APT-0002`, …) that keeps counting up and never repeats, even after a
restart. Patients also carry **Father Name, Phone and Gender** now
(added to the Add Patient and Edit Patient forms too; patients
registered before this feature simply show those fields as empty).

- **CLI:** menu option **6. Book Appt** runs the same flow — search
  or register a patient, pick department then doctor, see the booked
  times for the day, then date/time/type/reason/notes/status; it shows
  a confirmation summary and offers to generate the bill.
- **Dashboard:** the **Book Appointment** form (Appointments in the
  sidebar, or the Quick Action on the dashboard) is the same three
  steps, with live patient search and live booked-times.

## Appointments Table & Details Page

**All Appointments** (Appointments in the sidebar, or **View All** on
the dashboard's Today's Appointments table) lists every appointment
in one table: Appointment ID, Patient Name, Doctor Name, Department,
Date, Time, Type, Status and Actions (View / Edit / Cancel).

- **Search** matches patient name, Appointment ID or token ("bilal",
  "APT-0002", "E-1").
- **Filters** — date, doctor, department (specialization) and status;
  **sortable columns** (date, time, patient name, doctor name,
  status — click a column header); **pagination**, 10 per page, with
  page controls and the total count. Filtering, sorting and
  pagination happen on the server, so the browser only ever loads one
  page.
- **Export** downloads the *currently filtered* table as a **CSV**
  (opens in Excel) or a **PDF** (title, date and the active filters
  noted at the top, paginated, with page numbers).
- **Details page** — click **View** on any row: patient details
  (including Father Name, Phone, Gender), doctor details, department,
  date/time, type, reason, notes, fee, **payment status** (Paid /
  Unpaid / Not billed yet), booking date and status, plus the full
  **status-history timeline** (who changed what, when). From there
  you can **Edit** (date/time/type/reason/notes), **Reschedule**
  (a date/time change re-runs the double-booking check and refuses a
  taken slot, exactly like booking), **Cancel** (with confirmation)
  and **Print** (a print-friendly layout of the details page).
  After any change the table refreshes to show it.
- A type change that flips the priority (General ↔ Emergency)
  changes the priority in place and re-orders the queue — the token,
  the Appointment ID, the status and the status history all stay put,
  so the bill and the history stay linked to the appointment.
- **Roles:** Admin and Receptionist can view, edit, reschedule and
  cancel; a Doctor sees only their own queue and cannot edit
  appointment details (they still update the consultation statuses —
  Checked In / In Progress / Completed — from the status control).

- **CLI:** menu option **21. All Appointments** lists appointments
  with the same filters (search, date, doctor, department, status,
  sort, paged 10 at a time); pick one by Appointment ID for the full
  details, then edit/reschedule or cancel (Admin/Receptionist only;
  a Doctor sees only their own queue).

## Patient Search, Edit & Delete

**Search by ID, name or phone:** type all or part of a name — "ahm"
finds "Ahmed Khan" and "Ali Ahmed", and "AHMED" finds them too
(matching ignores case). A patient ID ("101") finds that patient, and
a phone fragment ("0300") finds patients whose phone contains those
digits. Nothing matching gives a plain "no patient found" message, not
an error. Every role can search.

**Edit:** change a patient's name, age and/or problem — the ID never
changes, and fields you leave alone stay as they are. Father Name,
Phone and Gender can be changed the same way (leave one blank in the
edit form to clear it). A renamed
patient's queued appointments show the new name too; bills keep the
name they were made with. Admin and Receptionist can edit; Doctors are
read-only here.

**Delete (Admin only):** deleting a patient removes their record, their
notes and their queued appointments (their tokens leave every doctor's
queue) and frees any bed they occupy. Their **bills are kept** — they
are financial records and keep the patient name they were made with.
Deletion always asks first: the CLI shows exactly what will go away
and only deletes when you type `YES`; the dashboard asks with a
confirmation dialog.

- **CLI:** menu option **3. Find Patient** now asks *By ID* or *By
  Name*; menu option **18. Edit Patient** (leave a field blank to keep
  it) and **19. Delete Patient** (Admin only, type `YES` to confirm).
- **Dashboard:** the **Find Patient** view (Patient Management in the sidebar) has a *Search by Name* form
  (results as a list of patient cards), and every patient card has
  **Edit** (Admin/Receptionist) and **Delete** (Admin only) buttons.

## Appointment Statuses

Every appointment has a status — **Pending, Confirmed, Checked In, In
Progress, Completed, Cancelled** or **No Show**. A new appointment
starts **Pending**; appointments booked before this feature was added
also show as Pending. Every status change is recorded — the old status,
the new status, who changed it and when — and the history is kept even
if the patient or the appointment is later deleted. The queue order
never changes when a status changes: Emergency is still served first.

**Who can change what:** Admin and Receptionist can set any status on
any doctor's queue. A Doctor can only change appointments in their own
queue, and only to **Checked In**, **In Progress** or **Completed** —
they cannot cancel or confirm an appointment.

**No double booking:** booking refuses a new appointment when the same
doctor already has an active appointment at the same date and time, and
says plainly which token is in the way. A Cancelled or No Show
appointment no longer blocks its slot, so a new booking can take it.
Appointments with no fixed time never collide with anything.

- **CLI:** menu option **20. Appointment Status** — type the doctor ID
  and token, see the current status and the full history, then pick the
  new status (1–7). Doctor queues and the Schedule view (menu 17) also
  show each appointment's status.
- **Dashboard:** every doctor card shows each token's status as a
  coloured badge, with a dropdown to change it and a **History** button
  that shows every recorded change. The Schedule table has a Status
  column too. Doctors only get the three statuses they are allowed to
  set in the dropdown.

## Doctor Availability & Scheduling

Each doctor can have their own working pattern: working days
(Mon–Sun), daily working hours (e.g. 09:00–17:00), breaks (e.g. lunch
13:00–14:00) and a consultation duration in minutes (15/20/30). From
that pattern the system generates the bookable slots for any date —
working hours minus breaks, in duration steps. The booking form shows
only the free slots for the chosen doctor and day (booked ones greyed
out); a doctor with no availability set yet shows a plain
"availability not set yet" note and still books by free time entry,
exactly as before.

**Booking is refused, with a plain reason, when:** the date is not
one of the doctor's working days, the time is outside working hours,
the time falls inside a break, the doctor is on leave that day, the
day is a hospital holiday, or the slot overlaps an active appointment
(a 30-minute appointment at 09:00 blocks a 09:15 booking too).
Rescheduling re-runs all of these checks. Leave (per doctor, single
date or range + optional reason) and hospital-wide holidays (date +
name) are managed from the dashboard and the CLI; on a leave or
holiday day the doctor has no slots and bookings are refused.

**Who can do what:** Admin manages availability, leave and holidays.
A Doctor views their own availability and adds/removes their OWN
leave only. A Receptionist views availability and slots only.

- **CLI:** menu **23. Availability** (pick a doctor, see their
  pattern and slots for a date; Admin sets or clears it) and menu
  **24. Leave & Holidays** (doctor leave + hospital holidays).
- **Dashboard:** Doctor Management → per-doctor **Availability**
  (set working days/hours/breaks/duration, view slots for a date,
  add/remove leave) and Appointments → **Holidays** (list/add/delete).
  The booking form's slot chips come live from the same slots API.

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
├── billing.py                # Billing & reports data — bills table, totals, CSV export
├── database.py               # SQLite persistence — save/load whole state to hospital.db
├── pdf_export.py             # Pure-Python PDF generator (stdlib only) for the appointments table
├── status_history.py         # Append-only appointment status history table
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
│   ├── test_billing.py      # Billing, reports, CSV export, and persistence tests
│   ├── test_appointments.py # Appointment date/time, schedule views, migration tests
│   ├── test_appointment_status.py # Appointment statuses, history, double-booking guard
│   ├── test_dashboard.py    # Step 7 dashboard data + API tests
│   ├── test_book_appointment.py # Step 8 booking form, APT IDs, slots endpoint
│   ├── test_appointments_table.py # Step 9 table, details, edit, CSV/PDF export
│   ├── test_patients.py     # Patient search by name, edit, delete, delete rule
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
