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

## Server (`server.py`)

- Python stdlib only: `ThreadingHTTPServer` + `SimpleHTTPRequestHandler`.
- Serves the static single-page UI from `web/` and the JSON API in
  `docs/api.md`.
- One global `Hospital` instance, guarded by a `threading.Lock` so two
  browser tabs can't race on the same structures.
- `hospital.py` methods print instead of returning, so the server
  determines success from state before/after each call and computes the
  booking token from the doctor's queue count after enqueue.
- Everything is in-memory: restarting the server resets all data.
  This is a university project, not for production.

## Frontend (`web/`)

- `index.html` — single page: header (logo + Undo + dark-mode toggle),
  live stat strip (Patients / Doctors / Waiting / Free Beds), all 11
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
note / booking / bed), pharmacy FIFO, shift rotation. Run with:

```bash
python -m unittest discover -s tests -v
```
