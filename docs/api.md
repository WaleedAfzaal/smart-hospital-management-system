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
queue and shifts. The 5 starter doctors are `STARTER_DOCTORS` in
`hospital.py`: D1 Dr. Tariq (Neurosurgeon, 2500), D2 Dr. Ayesha
(Cardiologist, 2000), D3 Dr. Bilal (Orthopedic, 1800), D4 Dr. Sana
(Physician, 1200), D5 Dr. Kamran (Pediatrician, 1500).

## State & Dashboard

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/state` | Everything the UI needs: patients (with notes), doctors (with queues), beds, pharmacy queue, shifts, on-duty doctor, last undo action, stats |
| GET | `/api/dashboard` | Stats only: patients, doctors, waiting (total tokens), free_beds / total_beds |

## Patients (ops 1–4)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| POST | `/api/patients` | `{id, name, age, problem}` | Add patient. Fails if the ID already exists or fields are invalid |
| GET | `/api/patients?sort=id\|age` | — | Patients sorted with `bubble_sort` by id or age |
| GET | `/api/patients?name=ahm` | — | Step 5: patients whose name contains the query (partial, case-insensitive), sorted by id. No match returns an empty list, not an error |
| GET | `/api/patients/<id>` | — | Find one patient via `binary_search` on the id-sorted list; includes notes |
| POST | `/api/patients/<id>` | `{name?, age?, problem?}` | Step 5: edit a patient — only the given fields change, the ID never changes. Admin + Receptionist only. Bad values get a 400; missing patient gets a 404 |
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
| POST | `/api/appointments` | `{doctor_id, patient_id, emergency, date?, time?}` | Book a token. Emergency → `E-n`, Normal → `N-n` (n = that doctor's queue count). PriorityQueue (binary heap) serves Emergency first. `date` is `YYYY-MM-DD` (empty = today), `time` is `HH:MM` 24-hour (empty = no fixed time); bad values get a 400 with a plain message. Returns the token + fee + scheduled date/time |
| GET | `/api/appointments?date=YYYY-MM-DD&doctor_id=D1` | — | Appointments scheduled for that date (today by default), sorted by time then Emergency-first. `doctor_id` is optional; a Doctor user linked to one doctor only ever sees that doctor's appointments |

Each queue item in `GET /api/doctors` and `/api/state` now also carries `booked_date` (when the token was issued), `appt_date` and `appt_time` (when the patient should come — `null` for appointments booked before Step 4).

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
