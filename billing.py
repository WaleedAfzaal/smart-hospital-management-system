"""Billing + reports data for the Hospital Management System.

Step 3 of the improvement plan: the hospital can now charge for what it
does and see, at a glance, how the day went.

A **bill** belongs to one patient and is created from a booked
appointment — its first line item is always the consultation fee from
that appointment (the doctor's token fee, already stored on the
appointment row). Extra charges can be added afterwards: medicine,
bed charges, lab tests, anything — each is just a label + an amount.

Money is kept simple and honest: amounts are whole rupees (integers),
a bill's total is always the SUM of its line items (never a stored
number that could drift), and there is no tax or discount logic.
A bill is either ``Unpaid`` or ``Paid``.

Bills live in a ``bills`` / ``bill_items`` table in the same SQLite file
(``hospital.db``) as everything else, so they survive a restart — but
like the users table, they are managed only here; ``Database.save()``
never touches them. With no database path the Billing store is purely
in-memory (used by the unit tests).

Reports are computed from live data, not stored: the daily report counts
patients registered today, appointments booked today, money billed and
collected today, per-doctor appointments, bed occupancy and unpaid
bills. See ``Hospital.daily_report()`` in hospital.py.
"""

import csv
import datetime
import io
import os
import sqlite3
import threading

_BILLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS bills(
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL,
    patient_name TEXT NOT NULL,
    doctor_id TEXT NOT NULL,
    doctor_name TEXT NOT NULL,
    token TEXT NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Unpaid'
);
CREATE TABLE IF NOT EXISTS bill_items(
    bill_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    label TEXT NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (bill_id, position)
);
"""

CSV_HEADER = ["Bill ID", "Date", "Patient ID", "Patient Name", "Doctor",
              "Token", "Items", "Total (Rs)", "Status"]


def bill_total(bill):
    """A bill's total — always the sum of its line items."""
    return sum(item["amount"] for item in bill["items"])


def _today():
    return datetime.date.today().isoformat()


class Billing:
    """Bills in memory, saved to SQLite when a database path is given.

    One persistent connection (``check_same_thread=False``) guarded by a
    lock, like AuthStore — the threaded web server shares one store.
    """

    def __init__(self, db_path=None):
        self.path = os.path.abspath(db_path) if db_path else None
        self._lock = threading.Lock()
        self._conn = None
        self.bills = {}       # bill id -> bill dict
        self.next_id = 1
        if self.path is not None:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            with self._lock:
                self._conn.executescript(_BILLS_SCHEMA)
                self._conn.commit()
                self._load()

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------ load
    def _load(self):
        """Rebuild the in-memory bills from the database file."""
        self.bills, self.next_id = {}, 1
        for (bid, pid, pname, did, dname, token, date, status) in \
                self._conn.execute(
                    "SELECT id, patient_id, patient_name, doctor_id,"
                    " doctor_name, token, date, status FROM bills ORDER BY id"):
            items = [
                {"label": label, "amount": amount}
                for label, amount in self._conn.execute(
                    "SELECT label, amount FROM bill_items WHERE bill_id = ?"
                    " ORDER BY position", (bid,))]
            self.bills[bid] = {
                "id": bid, "patient_id": pid, "patient_name": pname,
                "doctor_id": did, "doctor_name": dname, "token": token,
                "date": date, "items": items, "status": status,
            }
            self.next_id = max(self.next_id, bid + 1)

    # ------------------------------------------------------------ save
    def _insert_bill(self, bill):
        """Write a new bill (+ its items) to the database, if any."""
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT INTO bills(id, patient_id, patient_name, doctor_id,"
            " doctor_name, token, date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (bill["id"], bill["patient_id"], bill["patient_name"],
             bill["doctor_id"], bill["doctor_name"], bill["token"],
             bill["date"], bill["status"]),
        )
        for pos, item in enumerate(bill["items"]):
            self._conn.execute(
                "INSERT INTO bill_items(bill_id, position, label, amount)"
                " VALUES (?, ?, ?, ?)",
                (bill["id"], pos, item["label"], item["amount"]),
            )
        self._conn.commit()

    def _insert_item(self, bill_id, position, label, amount):
        if self._conn is None:
            return
        self._conn.execute(
            "INSERT INTO bill_items(bill_id, position, label, amount)"
            " VALUES (?, ?, ?, ?)",
            (bill_id, position, label, amount),
        )
        self._conn.commit()

    def _update_status(self, bill_id, status):
        if self._conn is None:
            return
        self._conn.execute(
            "UPDATE bills SET status = ? WHERE id = ?",
            (status, bill_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------ bills
    def create_bill(self, patient_id, patient_name, doctor_id, doctor_name,
                    token, fee, date=None):
        """Create a bill from a booked appointment.

        The first line item is the consultation fee for that appointment.
        Raises ValueError on bad input or if a bill already exists for
        the same doctor + token (a token is billed only once).
        """
        if not patient_name or not str(patient_name).strip():
            raise ValueError("Patient name is required.")
        if not doctor_name or not str(doctor_name).strip():
            raise ValueError("Doctor name is required.")
        if not token or not str(token).strip():
            raise ValueError("Token is required.")
        if not isinstance(fee, int) or isinstance(fee, bool) or fee < 0:
            raise ValueError("Fee must be a whole number of rupees (0 or more).")
        if self.find_bill_by_token(doctor_id, token) is not None:
            raise ValueError(f"A bill already exists for token {token}.")
        bill = {
            "id": self.next_id,
            "patient_id": patient_id,
            "patient_name": patient_name,
            "doctor_id": doctor_id,
            "doctor_name": doctor_name,
            "token": token,
            "date": date or _today(),
            "items": [{"label": f"Consultation — {doctor_name} ({token})",
                       "amount": fee}],
            "status": "Unpaid",
        }
        with self._lock:
            self.bills[bill["id"]] = bill
            self.next_id += 1
            self._insert_bill(bill)
        return self.get_bill(bill["id"])

    def find_bill_by_token(self, doctor_id, token):
        """The bill for one doctor + token, or None (a token is billed once)."""
        for bill in self.bills.values():
            if bill["doctor_id"] == doctor_id and bill["token"] == token:
                return bill
        return None

    def add_charge(self, bill_id, label, amount):
        """Add one extra charge (medicine, bed, lab...) to a bill.

        Raises ValueError if the bill does not exist or the charge is
        invalid. Returns the updated bill.
        """
        if not label or not str(label).strip():
            raise ValueError("Charge label is required.")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise ValueError("Charge amount must be a whole number of rupees above 0.")
        with self._lock:
            bill = self.bills.get(bill_id)
            if bill is None:
                raise ValueError("Bill not found.")
            position = len(bill["items"])
            bill["items"].append({"label": label.strip(), "amount": amount})
            self._insert_item(bill_id, position, label.strip(), amount)
        return self.get_bill(bill_id)

    def get_bill(self, bill_id):
        """One bill (a copy — callers cannot change the stored bill)."""
        bill = self.bills.get(bill_id)
        if bill is None:
            return None
        return {
            "id": bill["id"], "patient_id": bill["patient_id"],
            "patient_name": bill["patient_name"],
            "doctor_id": bill["doctor_id"],
            "doctor_name": bill["doctor_name"], "token": bill["token"],
            "date": bill["date"],
            "items": [dict(item) for item in bill["items"]],
            "total": bill_total(bill), "status": bill["status"],
        }

    def list_bills(self, patient_id=None, status=None):
        """All bills in id order, optionally filtered by patient / status."""
        out = []
        for bill in sorted(self.bills.values(), key=lambda b: b["id"]):
            if patient_id is not None and bill["patient_id"] != patient_id:
                continue
            if status is not None and bill["status"] != status:
                continue
            out.append(self.get_bill(bill["id"]))
        return out

    def mark_paid(self, bill_id):
        """Mark a bill Paid. Raises ValueError if the bill does not exist."""
        return self._set_status(bill_id, "Paid")

    def mark_unpaid(self, bill_id):
        """Mark a bill Unpaid again. Raises ValueError if it does not exist."""
        return self._set_status(bill_id, "Unpaid")

    def _set_status(self, bill_id, status):
        with self._lock:
            bill = self.bills.get(bill_id)
            if bill is None:
                raise ValueError("Bill not found.")
            bill["status"] = status
            self._update_status(bill_id, status)
        return self.get_bill(bill_id)

    # ------------------------------------------------------------ reports
    def summary_for_date(self, date):
        """Money for one date: billed, collected (paid), counts."""
        billed = collected = paid_count = unpaid_count = 0
        for bill in self.bills.values():
            if bill["date"] != date:
                continue
            total = bill_total(bill)
            billed += total
            if bill["status"] == "Paid":
                collected += total
                paid_count += 1
            else:
                unpaid_count += 1
        return {"date": date, "billed": billed, "collected": collected,
                "bills": paid_count + unpaid_count,
                "paid_count": paid_count, "unpaid_count": unpaid_count}

    def unpaid_total(self):
        """Total rupees still owed across all unpaid bills."""
        return sum(bill_total(b) for b in self.bills.values()
                   if b["status"] == "Unpaid")

    # ------------------------------------------------------------ export
    def to_csv(self, bills=None):
        """All (or the given) bills as a CSV string — stdlib csv only."""
        if bills is None:
            bills = self.list_bills()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(CSV_HEADER)
        for bill in bills:
            items = "; ".join(
                f"{item['label']}: Rs {item['amount']}"
                for item in bill["items"])
            writer.writerow([bill["id"], bill["date"], bill["patient_id"],
                             bill["patient_name"], bill["doctor_name"],
                             bill["token"], items, bill_total(bill),
                             bill["status"]])
        return out.getvalue()
