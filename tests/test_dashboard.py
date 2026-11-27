"""Step 7 (New Dashboard): the dashboard data is real, never sample.

Hospital.dashboard_data() aggregates counts, money, the monthly
series, the department split, today's list and the activity feed from
the patients, doctors, appointments, bills and status history
actually stored — an empty hospital reports honest zeros, and every
number in a seeded hospital can be counted by hand. The /api/dashboard
endpoint serves the same payload (plus the old stats keys, kept for
compatibility) behind the login.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hospital import Hospital  # noqa: E402
from status_history import StatusHistory  # noqa: E402
from test_server import TestServerAPI  # noqa: E402


def _seeded_hospital():
    """2–3 doctors in different specializations, 3 patients,
    appointments across two months, one bill paid and one unpaid,
    one bed taken, one status changed. 'Today' is fixed so the
    monthly counts are deterministic."""
    h = Hospital()
    h.add_doctor("D1", "Dr. Tariq", "Cardiology", 2000)
    h.add_doctor("D2", "Dr. Ayesha", "General Medicine", 1500)
    h.add_doctor("D3", "Dr. Bilal", "Cardiology", 1800)
    h.add_patient(101, "Ayesha Khan", 30, "Chest pain")
    h.add_patient(102, "Muhammad Ali", 45, "Fever")
    h.add_patient(103, "Fatima Noor", 28, "Checkup")
    # Two appointments today (2026-11-27), one in October, one tomorrow.
    h.book_appointment("D1", 101, False, "2026-11-27", "09:00")
    h.book_appointment("D1", 102, False, "2026-11-27", "10:00")
    h.book_appointment("D2", 103, False, "2026-10-15", "11:00")
    h.book_appointment("D3", 101, False, "2026-11-28", "09:30")
    # One status change, recorded with user + time.
    h.update_status("D1", "N-1", "Confirmed", changed_by="admin")
    # One bill paid (Rs 2000), one unpaid (Rs 1500).
    paid = h.create_bill(101, "D1", "N-1")
    h.billing.mark_paid(paid["id"])
    h.create_bill(103, "D2", "N-1")
    # One bed occupied.
    h.beds[0] = 101
    h.save()
    return h


class TestStatusHistoryRecent(unittest.TestCase):
    def test_recent_returns_newest_first(self):
        hist = StatusHistory()
        hist.record("D1", "N-1", "Pending", "Confirmed", "admin")
        hist.record("D1", "N-1", "Confirmed", "Checked In", "admin")
        hist.record("D2", "N-1", "Pending", "Cancelled", "reception1")
        recent = hist.recent(2)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["new_status"], "Cancelled")
        self.assertEqual(recent[1]["new_status"], "Checked In")

    def test_recent_empty_when_nothing_recorded(self):
        self.assertEqual(StatusHistory().recent(), [])


class TestDashboardData(unittest.TestCase):
    def test_empty_hospital_reports_honest_zeros(self):
        d = Hospital().dashboard_data("2026-11-27")
        self.assertEqual(d["total_patients"], 0)
        self.assertEqual(d["total_doctors"], 0)
        self.assertEqual(d["todays_appointments"], 0)
        self.assertEqual(d["available_beds"], 10)
        self.assertEqual(d["total_revenue"], 0)
        self.assertEqual(d["pending_bills"], 0)
        self.assertEqual(d["pending_bills_total"], 0)
        self.assertEqual(d["pending_appointments"], 0)
        self.assertEqual(d["notification_count"], 0)
        self.assertEqual(len(d["monthly_year"]), 12)
        self.assertTrue(all(m["count"] == 0 for m in d["monthly_year"]))
        self.assertEqual(d["departments"], [])
        self.assertEqual(d["todays_list"], [])
        self.assertEqual(d["activities"], [])

    def test_counts_and_money(self):
        d = _seeded_hospital().dashboard_data("2026-11-27")
        self.assertEqual(d["total_patients"], 3)
        self.assertEqual(d["total_doctors"], 3)
        self.assertEqual(d["todays_appointments"], 2)
        self.assertEqual(d["available_beds"], 9)
        self.assertEqual(d["total_beds"], 10)
        # Only the Paid bill counts as revenue.
        self.assertEqual(d["total_revenue"], 2000)
        self.assertEqual(d["pending_bills"], 1)
        self.assertEqual(d["pending_bills_total"], 1500)

    def test_monthly_series_counts_by_scheduled_month(self):
        d = _seeded_hospital().dashboard_data("2026-11-27")
        by_label = {m["label"]: m["count"] for m in d["monthly_year"]}
        self.assertEqual(by_label["Nov"], 3)  # two today + one tomorrow
        self.assertEqual(by_label["Oct"], 1)
        self.assertEqual(by_label["Jan"], 0)
        self.assertEqual(sum(by_label.values()), 4)
        roll = {m["label"]: m["count"] for m in d["monthly_last12"]}
        self.assertEqual(len(roll), 12)
        self.assertEqual(roll["Nov '26"], 3)
        self.assertEqual(roll["Oct '26"], 1)

    def test_department_split_is_appointments_per_specialization(self):
        d = _seeded_hospital().dashboard_data("2026-11-27")
        by_name = {x["name"]: x for x in d["departments"]}
        self.assertEqual(by_name["Cardiology"]["count"], 3)
        self.assertEqual(by_name["General Medicine"]["count"], 1)
        self.assertEqual(by_name["Cardiology"]["percent"], 75)
        self.assertEqual(by_name["General Medicine"]["percent"], 25)
        # Biggest department first.
        self.assertEqual(d["departments"][0]["name"], "Cardiology")

    def test_todays_list_has_real_statuses_and_departments(self):
        d = _seeded_hospital().dashboard_data("2026-11-27")
        rows = d["todays_list"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["appt_time"], "09:00")
        self.assertEqual(rows[0]["status"], "Confirmed")
        self.assertEqual(rows[0]["department"], "Cardiology")
        self.assertEqual(rows[1]["patient_name"], "Muhammad Ali")
        self.assertEqual(rows[1]["status"], "Pending")

    def test_activities_include_status_change_and_registration(self):
        d = _seeded_hospital().dashboard_data("2026-11-27")
        texts = [a["text"] for a in d["activities"]]
        self.assertTrue(any("Confirmed" in t and "N-1" in t for t in texts),
                        f"status change missing from {texts}")
        self.assertTrue(any("Patient registered: Fatima Noor" in t
                            for t in texts))
        self.assertTrue(any("Bill #1" in t and "Paid" in t for t in texts))
        self.assertLessEqual(len(d["activities"]), 8)

    def test_notification_count_and_pending_items(self):
        d = _seeded_hospital().dashboard_data("2026-11-27")
        # 3 appointments still Pending + 1 unpaid bill.
        self.assertEqual(d["pending_appointments"], 3)
        self.assertEqual(d["notification_count"], 4)
        self.assertEqual(len(d["pending_items"]["appointments"]), 3)
        self.assertEqual(len(d["pending_items"]["bills"]), 1)
        self.assertEqual(d["pending_items"]["bills"][0]["total"], 1500)


class TestServerDashboard(TestServerAPI):
    """GET /api/dashboard serves the Step 7 payload behind login."""

    def test_dashboard_payload_matches_seeded_data(self):
        self._request("POST", "/api/patients",
                      {"id": 101, "name": "Ali Khan", "age": 32,
                       "problem": "Fever"})
        status, _ = self._request(
            "POST", "/api/appointments",
            {"doctor_id": "D1", "patient_id": 101, "emergency": False})
        self.assertEqual(status, 200)
        self._request("POST", "/api/bills",
                      {"patient_id": 101, "doctor_id": "D1",
                       "token": "N-1"})
        status, payload = self._request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        stats = payload["stats"]
        # Old keys kept for compatibility.
        self.assertEqual(stats["patients"], 1)
        self.assertEqual(stats["doctors"], 5)
        self.assertEqual(stats["unpaid_total"], 2500)
        # Step 7 keys, all from the live data just created.
        self.assertEqual(stats["total_patients"], 1)
        self.assertEqual(stats["total_doctors"], 5)
        self.assertEqual(stats["todays_appointments"], 1)
        self.assertEqual(stats["total_revenue"], 0)
        self.assertEqual(stats["pending_bills"], 1)
        self.assertEqual(stats["notification_count"], 2)  # 1 pending + 1 bill
        self.assertEqual(len(stats["monthly_year"]), 12)
        self.assertEqual(len(stats["monthly_last12"]), 12)
        self.assertEqual(stats["departments"][0]["name"], "Neurosurgeon")
        self.assertEqual(stats["departments"][0]["count"], 1)
        self.assertEqual(len(stats["todays_list"]), 1)
        self.assertEqual(stats["todays_list"][0]["token"], "N-1")
        self.assertEqual(stats["todays_list"][0]["status"], "Pending")
        self.assertTrue(any("Patient registered: Ali Khan" in a["text"]
                            for a in stats["activities"]))

    def test_dashboard_needs_login(self):
        status, _ = self._request("GET", "/api/dashboard", cookie="")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
