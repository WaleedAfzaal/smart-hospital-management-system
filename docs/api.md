# REST API Reference

Base URL: `http://127.0.0.1:8000` (default). All responses are JSON with an
`ok` flag. Errors return `{"ok": false, "error": "..."}` with a 4xx status.

## Login first

**Every `/api/*` call needs a login.** Log in with `POST /api/login`
(sets an `hms_session` cookie, valid 12 hours), then send that cookie
with every later call. Without a valid session the API answers
**401 `{"ok": false, "error": "Please log in first."}`**; an action your
role is not allowed returns **403 `{"ok": false, "error": "Not allowed
for your role (X)."}`**. The dashboard serves `web/login.html` instead
of the dashboard when you are not logged in.

Default Admin account (first run): `admin` / `admin123` — change it
after login. Roles and permissions: Admin (everything), Doctor (view
patients + own appointments, add notes), Receptionist (register
patients, book appointments, beds, pharmacy). See the README's
[Login & Roles](../README.md#login--roles) table.

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| POST | `/api/login` | `{username, password}` | Log in. Sets the `hms_session` cookie (HttpOnly, 12h). Returns `{ok, message, user: {username, role, doctor_id}}` |
| POST | `/api/logout` | — | Log out (drops the session, clears the cookie) |
| GET | `/api/me` | — | The logged-in user `{username, role, doctor_id}` |
| GET | `/api/users` | — | Admin only. All users (username, role, linked doctor) — never passwords |
| POST | `/api/users` | `{username, password, role, doctor_id?}` | Admin only. Create a user. `role` is Admin / Doctor / Receptionist; `doctor_id` links a Doctor to one doctor's queue (empty = all queues) |
| POST | `/api/users/<username>/reset` | `{new_password}` | Admin only. Reset a user's password without knowing the old one |
| POST | `/api/change-password` | `{current_password, new_password, new_password_again}` | Any logged-in user. Change your own password |

A Doctor user linked to a doctor (e.g. `D1`) sees only that doctor in
`GET /api/doctors` and in the `doctors` list inside `GET /api/state`;
other roles see all doctors.

The server saves all data to `hospital.db` (SQLite, created automatically
next to `server.py`) on every change, so restarting the server keeps
everything — patients, notes, doctors, appointments, beds, pharmacy
queue and shifts. A fresh install starts with no doctors — staff add
their own with the Add Doctor form (CLI menu or web dashboard).

## State & Dashboard

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/state` | Everything the UI needs: patients (with notes), doctors (with queues), beds, pharmacy queue, shifts, on-duty doctor, last undo action, stats |
| GET | `/api/dashboard` | Step 7: the full dashboard payload — counts (patients, doctors, today's appointments, free beds, total revenue from Paid bills, pending bills), monthly appointment series (this year + rolling last 12 months), department split (appointments per doctor specialization), today's appointments with real statuses, recent-activity feed (status changes, registrations, bills, bookings), notification count (pending appointments + unpaid bills) and the pending items for the bell. Every number is live data from `hospital.db` — nothing is hardcoded. The old stats keys (patients, doctors, waiting, free_beds, billed_today, collected_today, unpaid_total) are still returned for compatibility |

## Patients (ops 1–4)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| POST | `/api/patients` | `{id, name, age, problem, father_name?, phone?, gender?}` | Add patient. Fails if the ID already exists or fields are invalid. Step 8: Father Name, Phone and Gender are optional — a bad phone (letters in it) or gender (not Male / Female / Other) gets a 400 |
| GET | `/api/patients?sort=id\|age` | — | Patients sorted with `bubble_sort` by id or age |
| GET | `/api/patients?name=ahm` | — | Step 5: patients whose name contains the query (partial, case-insensitive), sorted by id. No match returns an empty list, not an error. Step 8: the same search also matches an exact patient ID ("101") and a phone fragment ("0300" finds a phone 0300-...) |
| GET | `/api/patients/<id>` | — | Find one patient via `binary_search` on the id-sorted list; includes notes |
| POST | `/api/patients/<id>` | `{name?, age?, problem?, father_name?, phone?, gender?}` | Step 5: edit a patient — only the given fields change, the ID never changes. Admin + Receptionist only. Bad values get a 400; missing patient gets a 404. Step 8: father_name, phone and gender can be changed the same way ("" clears one) |
| DELETE | `/api/patients/<id>` | — | Step 5: delete a patient — Admin only. Removes the record, notes and queued appointments and frees their bed; their bills are KEPT. Returns a summary of what was removed. Missing patient gets a 404 |
| POST | `/api/patients/<id>/notes` | `{note}` | Add a note (LinkedList) — also bumps the visit count |

## Doctors (op 5)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/api/doctors` | — | All doctors with fee, waiting count and their token queue (Emergency first) |
| POST | `/api/doctors` | `{id, name, spec, fee}` | Add a doctor (also joins the shift rotation) |

## Appointments (op 6)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| POST | `/api/appointments` | `{doctor_id, patient_id \| new_patient, type?, status?, reason?, notes?, date?, time?}` | Book a token. Step 8: `type` is General / Follow-up / Emergency — Emergency → `E-n`, the others → `N-n` (n = that doctor's queue count); when no `type` is given the old `emergency` flag still decides. PriorityQueue (binary heap) serves Emergency first. `date` is `YYYY-MM-DD` (empty = today), `time` is `HH:MM` 24-hour (empty = no fixed time); bad values get a 400 with a plain message. `status` is Pending (default) or Confirmed. `new_patient` (`{name, age, problem, father_name?, phone?, gender?, id?}`) registers a patient inline — the ID is auto-assigned when left out. Returns the full appointment including its unique Appointment ID (`APT-0001`, …) + token + fee + scheduled date/time. **Step 6:** a double booking — same doctor, same date, same time, while the existing appointment is still active — gets a 400 that says which token is in the way. New appointments start Pending |
| GET | `/api/appointments?date=YYYY-MM-DD&doctor_id=D1` | — | Appointments scheduled for that date (today by default), sorted by time then Emergency-first. `doctor_id` is optional; a Doctor user linked to one doctor only ever sees that doctor's appointments |
| GET | `/api/appointments/slots?doctor_id=D1&date=YYYY-MM-DD` | — | Step 8: that doctor's booked times for the day (time, token, status, patient name), sorted by time — the availability the booking form shows. Cancelled / No Show times are listed but do not block their slot. Bad date or unknown doctor gets a 400/404; a Doctor user linked to one doctor only ever sees their own day. Step 11: also returns the doctor's `availability` (working days/hours/breaks/duration, or `null` when not set), the generated `slots` (each `{time, available, token}` — working hours minus breaks in duration steps, booked ones marked with their token), and `unavailable_reason` (why the day has no slots: leave, holiday, non-working day — or `null`) |
| POST | `/api/appointments/<doctor>/<token>/status` | `{status}` | Step 6: change an appointment's status (one of the seven). Admin/Receptionist: any status, any queue. Doctor: only their own queue, only Checked In / In Progress / Completed — otherwise 403. Bad status or missing token gets a 400. Returns the updated appointment + its full history |
| GET | `/api/appointments/<doctor>/<token>/history` | — | Step 6: the recorded status changes for one appointment, oldest first — old status, new status, who changed it, when. A Doctor linked to one doctor may only read their own queue's. Missing doctor/token gets a 404 |
| GET | `/api/appointments/list?search=&date=&doctor_id=&department=&status=&sort=&order=&page=&per_page=` | — | Step 9: the appointments table, filtered/sorted/paged on the server. `search` matches patient name, APT ID or token (partial, case-insensitive); `sort` is date / time / patient_name / doctor_name / status (`order` asc or desc, default asc); `page` starts at 1, `per_page` is 1–100 (default 10). Returns `{appointments, total, pages, page, per_page}` — appointments booked before Step 8 have no APT ID (shown as "—"). A Doctor user linked to one doctor only ever sees that doctor's appointments. Bad `sort`/`page` values get a 400 |
| GET | `/api/appointments/by-id/<apt_id>` | — | Step 9: one appointment with everything — appointment (APT ID, token, date/time, type, reason, notes, fee, status, booking date), patient (incl. father_name / phone / gender), doctor, `payment` (`billed`, `bill_id`, `status` Paid/Unpaid, `total` — or "Not billed yet"), and the status `history`. Rows booked before Step 8 also work with `"D1:N-1"` (doctor + token) instead of an APT ID. Unknown APT ID gets a 404; a Doctor asking for another doctor's appointment gets a 403 |
| POST | `/api/appointments/by-id/<apt_id>/edit` | `{date?, time?, type?, reason?, notes?}` | Step 9: change date/time/type/reason/notes — only the keys sent change. Admin/Receptionist only (a Doctor gets a 403). A date/time change re-runs the Step 6 double-booking guard (a taken slot gets a 400 naming the token in the way); Step 11: it also re-runs the full availability check (working day, hours, break, leave, holiday, overlapping slot — a reschedule into a break or onto a leave day gets a 400 naming the reason, and editing only reason/notes/type skips the checks). A type change that flips the priority changes it in place and re-orders the queue — the token, APT ID, status and history stay put. Empty `date` keeps the current date; empty `time` clears the time (no fixed time). Unknown APT ID gets a 404, bad values a 400 |
| GET | `/api/appointments/export.csv?search=&date=&doctor_id=&department=&status=&sort=&order=` | — | Step 9: the currently filtered table as a CSV download (12 columns: APT ID, Patient, Patient ID, Doctor, Department, Date, Time, Type, Status, Token, Fee, Booked Date) — opens in Excel. Same filters as the list endpoint, no pagination. A Doctor linked to one doctor only exports their own queue |
| GET | `/api/appointments/export.pdf?search=&date=&doctor_id=&department=&status=&sort=&order=` | — | Step 9: the currently filtered table as a PDF download (pure-Python generator, `pdf_export.py` — no external library): landscape A4, title + date + the active filters, paginated with page numbers. Same filters as the list endpoint, no pagination |

Each queue item in `GET /api/doctors` and `/api/state` now also carries `booked_date` (when the token was issued), `appt_date` and `appt_time` (when the patient should come — `null` for appointments booked before Step 4), `status` (Step 6 — `Pending` for appointments booked before Step 6) and `apt_id`, `appt_type`, `reason`, `notes` (Step 8 — `null`/empty for appointments booked before Step 8).

## Availability, Leave & Holidays (op 23–24)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/api/availability?doctor_id=D1` | — | Step 11: that doctor's availability (`working_days`, `start_time`, `end_time`, `duration_minutes`, `breaks` — or `null` when not set) + their leave list. A Doctor user linked to one doctor is forced to their own doctor; other roles see any doctor. Unknown doctor gets a 404 |
| POST | `/api/availability` | `{doctor_id, working_days, start_time, end_time, duration_minutes, breaks}` | Step 11: set a doctor's working pattern. Admin only (403 otherwise). `working_days` is e.g. `["Mon","Tue","Wed","Thu","Fri"]`, `breaks` is e.g. `["13:00-14:00"]` (empty = none), `duration_minutes` is 5–120. Bad values get a 400 with a plain message |
| POST | `/api/availability/clear` | `{doctor_id}` | Step 11: remove a doctor's availability (back to "not set yet"). Admin only |
| POST | `/api/leaves` | `{doctor_id, start_date, end_date?, reason?}` | Step 11: add leave for a doctor (single date, or a range when `end_date` is given). Admin: any doctor. Doctor: only their own doctor (403 otherwise). Bad dates get a 400 |
| POST | `/api/leaves/<id>/delete` | — | Step 11: delete a leave. Admin: any. Doctor: only their own leaves. Unknown id gets a 404 |
| GET | `/api/holidays` | — | Step 11: all hospital holidays (`id`, `date`, `name`), sorted by date. Every role may view |
| POST | `/api/holidays` | `{date, name}` | Step 11: add a hospital holiday. Admin only. A date already listed gets a 400 |
| POST | `/api/holidays/<id>/delete` | — | Step 11: delete a holiday. Admin only. Unknown id gets a 404 |

Step 11 booking rules (server-side, in `Hospital.book_appointment` and the
booking API — never client-only): a booking is refused with a 400 naming
the reason when the date is not one of the doctor's working days, the
time is outside working hours, the time falls inside a break, the doctor
is on leave that day, the day is a hospital holiday, or the slot overlaps
an active appointment (a 30-minute appointment at 09:00 blocks a 09:15
booking — the Step 6 guard is now duration-aware). A refused booking
never leaves a stray patient behind (the check runs before a new patient
is registered). A doctor with no availability set is only checked for
holiday/leave, and still books by free time entry; an untimed booking is
only checked for holiday/leave/working day.

## Beds (op 7)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/api/beds` | — | 10 beds with status Free / Occupied + patient id |
| POST | `/api/beds/assign` | `{bed, patient_id}` | Assign a free bed to a patient |
| POST | `/api/beds/free` | `{bed}` | Free an occupied bed |

## Undo (op 8)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| POST | `/api/undo` | — | Undo the last action from the undo stack (add patient / add note / book appointment / bed change). Returns what was undone |

## Pharmacy Queue (op 9)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/api/pharmacy` | — | Current pharmacy queue (FIFO) |
| POST | `/api/pharmacy` | `{token}` | Add a token / name to the queue |
| POST | `/api/pharmacy/serve` | — | Serve (dequeue) the next token |

## Shifts (op 10)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/api/shifts` | — | Shift rotation list + who is on duty now |
| POST | `/api/shifts/rotate` | — | Rotate the circular queue — next doctor on duty |

## Billing (op 15)

Bills are created from a booked appointment — the first line item is
that appointment's consultation fee; extra charges (medicine, bed,
lab...) are added afterwards. Amounts are whole rupees; a bill's total
is always the sum of its line items. A token can be billed only once.
**Create / change needs Admin or Receptionist; reading is open to
every role (Doctor read-only).**

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/api/bills` | — | All bills, newest last. Optional `?patient_id=<id>` and `?status=Paid\|Unpaid` filters |
| GET | `/api/bills/<id>` | — | One bill with its line items + total + status |
| POST | `/api/bills` | `{patient_id, doctor_id, token}` | Create a bill from a booked appointment. Fails if the token is unknown, belongs to another patient, or was already billed |
| POST | `/api/bills/<id>/charges` | `{label, amount}` | Add an extra charge (amount must be a whole number above 0) |
| POST | `/api/bills/<id>/pay` | — | Mark the bill Paid |
| POST | `/api/bills/<id>/unpay` | — | Mark the bill Unpaid again |
| GET | `/api/bills/export` | — | Download all bills as CSV (`bills.csv`) — `text/csv`, still needs a login |

## Reports (op 16)

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/reports` | Daily report for today: patients registered, appointments booked, billed / collected, per-doctor appointments, bed occupancy, unpaid bills |
| GET | `/api/reports?date=YYYY-MM-DD` | Same report for any date |

The dashboard (`GET /api/dashboard` and the stats inside
`GET /api/state`) also carries `billed_today`, `collected_today` and
`unpaid_total`.

## Notes

- `hospital.py` methods print instead of returning — except
  `create_bill()` and `daily_report()` (Step 3), which return their
  results so the server can use them. The server decides success from
  state before/after each call (e.g. `pid in patients_map`,
  notes count +1) and computes the booking token from the doctor's queue
  count after enqueue. `hospital.py` itself is not modified.
- There is no Exit operation in the web UI (op 12 in the CLI menu is
  intentionally left out).
