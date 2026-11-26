# REST API Reference

Base URL: `http://127.0.0.1:8000` (default). All responses are JSON with an
`ok` flag. Errors return `{"ok": false, "error": "..."}` with a 4xx status.

The server keeps all data in memory (from `hospital.py` — `Hospital`,
`Node`/`LinkedList`, `Stack`, `Queue`, `CircularQueue`, `PriorityQueue`
(binary heap), `bubble_sort`, `binary_search`). Restarting the server
resets everything. The 5 starter doctors are `STARTER_DOCTORS` in
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
| GET | `/api/patients/<id>` | — | Find one patient via `binary_search` on the id-sorted list; includes notes |
| POST | `/api/patients/<id>/notes` | `{note}` | Add a note (LinkedList) — also bumps the visit count |

## Doctors (op 5)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| GET | `/api/doctors` | — | All doctors with fee, waiting count and their token queue (Emergency first) |
| POST | `/api/doctors` | `{id, name, spec, fee}` | Add a doctor (also joins the shift rotation) |

## Appointments (op 6)

| Method | Path | Body | Description |
| ------ | ---- | ---- | ----------- |
| POST | `/api/appointments` | `{doctor_id, patient_id, emergency}` | Book a token. Emergency → `E-n`, Normal → `N-n` (n = that doctor's queue count). PriorityQueue (binary heap) serves Emergency first. Returns the token + fee |

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

## Notes

- `hospital.py` methods print instead of returning. The server decides
  success from state before/after each call (e.g. `pid in patients_map`,
  notes count +1) and computes the booking token from the doctor's queue
  count after enqueue. `hospital.py` itself is not modified.
- There is no Exit operation in the web UI (op 12 in the CLI menu is
  intentionally left out).
