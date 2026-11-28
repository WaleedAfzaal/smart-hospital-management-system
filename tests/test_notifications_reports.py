"""Step 12 (Notifications + Reports & Analytics) tests.

Notifications live in hospital.db (the Notifications class) and are
created by the Hospital itself — booking, cancellation, reschedule,
status changes, tomorrow reminders, schedule changes. Reports are
computed live from the appointments + bills for a daily / weekly /
monthly period. The API tests below run against the real HTTP server
(the TestServerAPI harness in test_server.py).
"""

import datetime
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hospital import Hospital  # noqa: E402
from pdf_export import report_pdf  # noqa: E402
from tests.test_server import TestServerAPI  # noqa: E402


def _seed_report_hospital():
    """A known dataset, all inside the week that holds today:

    * today: D1 09:00 Completed, D1 10:00 Cancelled, D2 11:00 Pending
    * another day of the same week: D2 09:00 No Show, D1 12:00 Confirmed
    * a third day of the same week: D1 09:00 Pending

    (The two extra days are picked from the Monday–Sunday week that
    holds today, never today itself, so the weekly total is 6 on any
    weekday. The monthly total is computed from the dates — the week
    can straddle a month boundary.)

    Plus two bills dated today (bills are always dated the day they
    are created): the Completed appointment's bill (Rs 2500) is Paid,
    the Confirmed appointment's bill (Rs 2500) stays Unpaid.
    """
    h = Hospital()
    h.add_doctor("D1", "Dr. Tariq", "Cardiology", 2500)
    h.add_doctor("D2", "Dr. Ayesha", "General Medicine", 2000)
    for pid, name in ((101, "Ali Khan"), (102, "Fatima Noor"),
                      (103, "Bilal Ahmed"), (104, "Sana Iqbal")):
        h.add_patient(pid, name, 30, "Fever")
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    other_days = [monday + datetime.timedelta(days=i)
                  for i in range(7) if monday + datetime.timedelta(days=i)
                  != today][:2]
    d0 = today.isoformat()
    d1 = other_days[0].isoformat()
    d2 = other_days[1].isoformat()
    t1 = h.book_appointment("D1", 101, appt_date=d0, appt_time="09:00")
    t2 = h.book_appointment("D1", 102, appt_date=d0, appt_time="10:00")
    t3 = h.book_appointment("D2", 103, appt_date=d0, appt_time="11:00")
    t4 = h.book_appointment("D2", 104, appt_date=d1, appt_time="09:00")
    t5 = h.book_appointment("D1", 101, appt_date=d1, appt_time="12:00")
    t6 = h.book_appointment("D1", 102, appt_date=d2, appt_time="09:00")
    h.update_status("D1", t1, "Completed")
    h.update_status("D1", t2, "Cancelled")
    h.update_status("D2", t4, "No Show")
    h.update_status("D1", t5, "Confirmed")
    paid = h.create_bill(101, "D1", t1)
    h.billing.mark_paid(paid["id"])
    h.create_bill(101, "D1", t5)
    return h, {"today": today, "d0": d0, "d1": d1, "d2": d2,
               "tokens": (t1, t2, t3, t4, t5, t6)}


class TestNotifications(unittest.TestCase):
    def setUp(self):
        self.h = Hospital()
        self.h.add_doctor("D1", "Dr. Tariq", "Cardiology", 2500)
        self.h.add_doctor("D2", "Dr. Ayesha", "General Medicine", 2000)
        self.h.add_patient(101, "Ali Khan", 32, "Fever")
        self.h.add_patient(102, "Fatima Noor", 28, "Cough")

    def test_booking_creates_exactly_one_notification(self):
        token = self.h.book_appointment("D1", 101)
        rows = self.h.list_notifications()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "booking")
        self.assertIn("APT-", rows[0]["message"])
        self.assertIn(token, rows[0]["message"])
        self.assertFalse(rows[0]["is_read"])
        self.assertEqual(rows[0]["doctor_id"], "D1")
        self.assertEqual(self.h.unread_notifications_count(), 1)

    def test_status_change_and_cancel_notifications(self):
        token = self.h.book_appointment("D1", 101)
        self.h.update_status("D1", token, "Confirmed")
        self.h.update_status("D1", token, "Completed")
        self.h.update_status("D1", token, "Cancelled")
        rows = self.h.list_notifications()
        self.assertEqual(len(rows), 4)  # booking + 3 changes
        self.assertEqual([r["type"] for r in rows],
                         ["booking", "status", "status", "cancellation"])
        self.assertEqual(rows[-1]["title"], "Appointment cancelled")
        # A no-op (same status again) notifies nobody.
        self.h.update_status("D1", token, "Cancelled")
        self.assertEqual(len(self.h.list_notifications()), 4)

    def test_reschedule_notification_only_when_date_or_time_changes(self):
        token = self.h.book_appointment(
            "D1", 101, appt_date="2026-12-02", appt_time="09:00")
        apt_id = self.h.calendar_appointments(
            "2026-12-02", "2026-12-02")[0]["apt_id"]
        self.h.edit_appointment(apt_id, reason="Follow-up visit")
        self.assertEqual(len(self.h.list_notifications()), 1)  # booking only
        self.h.edit_appointment(apt_id, date="2026-12-03")
        rows = self.h.list_notifications()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["type"], "reschedule")
        self.assertIn("2026-12-02", rows[1]["message"])
        self.assertIn("2026-12-03", rows[1]["message"])
        self.h.edit_appointment(apt_id, time="10:30")
        self.assertEqual(len(self.h.list_notifications()), 3)
        self.assertEqual(self.h.list_notifications()[-1]["type"],
                         "reschedule")

    def test_tomorrow_reminder_generated_once_only(self):
        tomorrow = (datetime.date.today()
                    + datetime.timedelta(days=1)).isoformat()
        today = datetime.date.today().isoformat()
        self.h.book_appointment("D1", 101, appt_date=tomorrow,
                                appt_time="09:00")
        self.h.book_appointment("D2", 102, appt_date=tomorrow,
                                appt_time="10:00")
        self.h.book_appointment("D1", 102, appt_date=today,
                                appt_time="11:00")
        self.assertEqual(self.h.refresh_reminders(), 2)
        self.assertEqual(self.h.refresh_reminders(), 0)  # never duplicated
        rows = self.h.list_notifications(ntype="reminder")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("tomorrow" in r["message"] for r in rows))
        # A cancelled tomorrow appointment gets no reminder.
        token = self.h.book_appointment("D1", 101, appt_date=tomorrow,
                                        appt_time="12:00")
        self.h.update_status("D1", token, "Cancelled")
        self.assertEqual(self.h.refresh_reminders(), 0)

    def test_schedule_change_notifications(self):
        self.h.set_doctor_availability(
            "D1", ["Mon", "Tue", "Wed", "Thu", "Fri"], "09:00", "17:00", 30)
        self.h.add_doctor_leave("D1", "2026-12-05", "2026-12-06",
                                reason="Annual leave")
        self.h.add_holiday("2026-12-25", "Quaid-e-Azam Day")
        rows = self.h.list_notifications(ntype="schedule")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["doctor_id"], "D1")
        self.assertIn("Dr. Tariq", rows[0]["message"])
        self.assertIn("Annual leave", rows[1]["message"])
        self.assertIsNone(rows[2]["doctor_id"])  # holiday: everyone

    def test_read_and_mark_all_read(self):
        self.h.book_appointment("D1", 101)
        self.h.book_appointment("D2", 102)
        self.assertEqual(self.h.unread_notifications_count(), 2)
        first = self.h.list_notifications()[0]
        self.assertTrue(self.h.mark_notification_read(first["id"]))
        self.assertEqual(self.h.unread_notifications_count(), 1)
        self.assertFalse(self.h.mark_notification_read(9999))
        self.assertEqual(self.h.mark_all_notifications_read(), 1)
        self.assertEqual(self.h.unread_notifications_count(), 0)
        self.assertEqual(self.h.mark_all_notifications_read(), 0)

    def test_doctor_scoping_own_queue_only(self):
        self.h.book_appointment("D1", 101)
        self.h.book_appointment("D2", 102)
        self.h.add_holiday("2026-12-25", "Quaid-e-Azam Day")
        d1_rows = self.h.list_notifications(role="Doctor",
                                            user_doctor_id="D1")
        self.assertEqual(len(d1_rows), 2)  # own booking + hospital-wide
        self.assertTrue(all(r["doctor_id"] in ("D1", None)
                            for r in d1_rows))
        self.assertEqual(self.h.unread_notifications_count(
            role="Doctor", user_doctor_id="D1"), 2)
        d2_rows = self.h.list_notifications(role="Doctor",
                                            user_doctor_id="D2")
        self.assertEqual(len(d2_rows), 2)
        # A Doctor may not mark another doctor's notification read.
        other = [r for r in self.h.list_notifications()
                 if r["doctor_id"] == "D2"][0]
        self.assertFalse(self.h.mark_notification_read(
            other["id"], role="Doctor", user_doctor_id="D1"))
        self.assertEqual(self.h.mark_all_notifications_read(
            role="Doctor", user_doctor_id="D1"), 2)

    def test_notifications_survive_restart_and_no_duplicates(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        try:
            h = Hospital(db_path=path)
            h.add_doctor("D1", "Dr. Tariq", "Cardiology", 2500)
            h.add_patient(101, "Ali Khan", 32, "Fever")
            h.book_appointment("D1", 101)
            self.assertEqual(len(h.list_notifications()), 1)
            # A fresh load of the same file: still exactly one.
            h2 = Hospital(db_path=path)
            self.assertEqual(len(h2.list_notifications()), 1)
            self.assertEqual(h2.list_notifications()[0]["type"], "booking")
            h2.refresh_reminders()
            self.assertEqual(len(h2.list_notifications()), 1)
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestReports(unittest.TestCase):
    def test_daily_report_figures(self):
        h, info = _seed_report_hospital()
        r = h.appointment_report(period="daily", date=info["d0"])
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["by_status"]["Completed"], 1)
        self.assertEqual(r["by_status"]["Cancelled"], 1)
        self.assertEqual(r["by_status"]["Pending"], 1)
        self.assertEqual(r["by_status"]["No Show"], 0)
        self.assertEqual(r["completed"], 1)
        self.assertEqual(r["cancelled"], 1)
        self.assertEqual(r["no_show"], 0)
        self.assertEqual(r["completion_rate"], 33.3)
        self.assertEqual(r["cancellation_rate"], 33.3)
        self.assertEqual(r["no_show_rate"], 0)
        self.assertEqual(r["revenue"], 2500)  # paid bill only
        self.assertEqual(r["unpaid_total"], 2500)  # unpaid bill only
        self.assertEqual(len(r["per_doctor"]), 2)
        d1 = next(d for d in r["per_doctor"] if d["doctor_id"] == "D1")
        self.assertEqual((d1["appointments"], d1["completed"],
                          d1["cancelled"]), (2, 1, 1))
        self.assertEqual(len(r["per_department"]), 2)
        self.assertEqual(len(r["cancelled_list"]), 1)
        self.assertEqual(len(r["no_show_list"]), 0)
        self.assertEqual(len(r["per_day"]), 1)
        self.assertEqual(r["per_day"][0]["count"], 3)

    def test_weekly_and_monthly_totals(self):
        h, info = _seed_report_hospital()
        monday = info["today"] - datetime.timedelta(
            days=info["today"].weekday())
        r = h.appointment_report(period="weekly", date=monday.isoformat())
        self.assertEqual(r["total"], 6)
        self.assertEqual(r["completed"], 1)
        self.assertEqual(r["cancelled"], 1)
        self.assertEqual(r["no_show"], 1)
        self.assertEqual(r["completion_rate"], 16.7)
        self.assertEqual(r["cancellation_rate"], 16.7)
        self.assertEqual(r["no_show_rate"], 16.7)
        self.assertEqual(r["revenue"], 2500)
        self.assertEqual(r["unpaid_total"], 2500)
        self.assertEqual(len(r["per_day"]), 7)
        self.assertEqual(sum(d["count"] for d in r["per_day"]), 6)
        first = info["today"].replace(day=1)
        r2 = h.appointment_report(period="monthly", date=first.isoformat())
        # The seed week can straddle a month boundary, so count only
        # the appointments whose date falls in this month (3 on d0,
        # 2 on d1, 1 on d2).
        expected = sum(n for d, n in
                       ((info["d0"], 3), (info["d1"], 2), (info["d2"], 1))
                       if d[:7] == first.isoformat()[:7])
        self.assertEqual(r2["total"], expected)
        self.assertEqual(len(r2["per_day"]),
                         _days_in_month(info["today"].year,
                                        info["today"].month))
        self.assertEqual(sum(d["count"] for d in r2["per_day"]), expected)

    def test_report_filters_by_doctor_and_department(self):
        h, info = _seed_report_hospital()
        r = h.appointment_report(period="weekly", date=info["d0"],
                                 doctor_id="D1")
        self.assertEqual(r["total"], 4)
        self.assertEqual(len(r["per_doctor"]), 1)
        self.assertEqual(r["per_doctor"][0]["doctor_id"], "D1")
        self.assertEqual(r["revenue"], 2500)
        r2 = h.appointment_report(period="weekly", date=info["d0"],
                                  department="General Medicine")
        self.assertEqual(r2["total"], 2)
        self.assertEqual(r2["revenue"], 0)  # D2 has no paid bill
        self.assertEqual(r2["unpaid_total"], 0)

    def test_doctor_report_scoping_own_queue_only(self):
        h, info = _seed_report_hospital()
        r = h.appointment_report(period="weekly", date=info["d0"],
                                 doctor_id="D2")
        self.assertEqual(r["total"], 2)
        self.assertEqual(r["per_doctor"][0]["doctor_id"], "D2")
        self.assertEqual(r["revenue"], 0)

    def test_empty_report_is_zero_not_crash(self):
        h = Hospital()
        h.add_doctor("D1", "Dr. Tariq", "Cardiology", 2500)
        r = h.appointment_report(period="daily", date="2026-11-27")
        self.assertEqual(r["total"], 0)
        self.assertEqual(r["completion_rate"], 0)
        self.assertEqual(r["revenue"], 0)
        with self.assertRaises(ValueError):
            h.appointment_report(period="yearly", date="2026-11-27")

    def test_report_csv_matches_figures(self):
        h, info = _seed_report_hospital()
        r = h.appointment_report(period="daily", date=info["d0"])
        text = h.report_to_csv(r)
        self.assertIn("Total appointments,3", text)
        self.assertIn("Completed,1", text)
        self.assertIn("Cancelled,1", text)
        self.assertIn('"Revenue (Paid bills, Rs)",2500', text)
        self.assertIn("Unpaid in period (Rs),2500", text)
        self.assertIn("Dr. Tariq (D1),Cardiology,2,1,1,0", text)
        self.assertIn("Dr. Ayesha (D2),General Medicine,1,0,0,0", text)

    def test_report_pdf_is_valid(self):
        h, info = _seed_report_hospital()
        r = h.appointment_report(period="weekly", date=info["d0"])
        data = report_pdf(r)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"Appointments Report", data)
        self.assertIn(b"Dr. Tariq", data)


def _days_in_month(year, month):
    if month == 12:
        nxt = datetime.date(year + 1, 1, 1)
    else:
        nxt = datetime.date(year, month + 1, 1)
    return (nxt - datetime.date(year, month, 1)).days


class TestServerNotificationsReports(TestServerAPI):
    """Step 12 on the HTTP API: auth, scoping, exports."""

    def _raw(self, method, path, body=None, cookie=None, raw=False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if cookie is None:
            cookie = self.cookie
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return (resp.status, resp.headers.get("Content-Type", ""),
                        payload if raw else payload.decode("utf-8"))
        except urllib.error.HTTPError as err:
            payload = err.read()
            return (err.code, err.headers.get("Content-Type", ""),
                    payload if raw else payload.decode("utf-8"))

    def _book(self, doctor_id, pid, appt_date=None, appt_time=None):
        self._request("POST", "/api/patients",
                      {"id": pid, "name": f"Patient {pid}", "age": 30,
                       "problem": "Fever"})
        body = {"doctor_id": doctor_id, "patient_id": pid,
                "emergency": False}
        if appt_date:
            body["date"] = appt_date
        if appt_time:
            body["time"] = appt_time
        status, payload = self._request("POST", "/api/appointments", body)
        self.assertEqual(status, 200, payload)
        return payload["appointment"]

    def _doctor_cookie(self, username, doctor_id):
        status, payload = self._request(
            "POST", "/api/users",
            {"username": username, "password": "pw123",
             "role": "Doctor", "doctor_id": doctor_id})
        self.assertEqual(status, 200, payload)
        cookie = self._login(username, "pw123")
        self.assertTrue(cookie)
        return cookie

    def test_notification_endpoints_need_login(self):
        for method, path in [
                ("GET", "/api/notifications"),
                ("POST", "/api/notifications/1/read"),
                ("POST", "/api/notifications/read-all"),
                ("GET", "/api/reports/analytics"),
                ("GET", "/api/reports/analytics.csv"),
                ("GET", "/api/reports/analytics.pdf")]:
            status, payload = self._request(method, path, {}, cookie="")
            self.assertEqual(status, 401, path)

    def test_booking_creates_notification_and_read_flow(self):
        self._book("D1", 101)
        status, payload = self._request("GET", "/api/notifications")
        self.assertEqual(status, 200)
        self.assertEqual(payload["unread_count"], 1)
        self.assertEqual(len(payload["notifications"]), 1)
        self.assertEqual(payload["notifications"][0]["type"], "booking")
        nid = payload["notifications"][0]["id"]
        status, payload = self._request(
            "POST", f"/api/notifications/{nid}/read", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["unread_count"], 0)
        status, payload = self._request(
            "POST", "/api/notifications/9999/read", {})
        self.assertEqual(status, 404)
        self._book("D2", 102)
        status, payload = self._request(
            "POST", "/api/notifications/read-all", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["marked"], 1)
        self.assertEqual(payload["unread_count"], 0)

    def test_notification_type_filter_and_bad_type(self):
        self._book("D1", 101)
        status, payload = self._request(
            "GET", "/api/notifications?type=booking")
        self.assertEqual(len(payload["notifications"]), 1)
        status, payload = self._request(
            "GET", "/api/notifications?type=reminder")
        self.assertEqual(len(payload["notifications"]), 0)
        status, payload = self._request(
            "GET", "/api/notifications?type=nope")
        self.assertEqual(status, 400)

    def test_doctor_sees_only_own_notifications(self):
        self._book("D1", 101)
        self._book("D2", 102)
        cookie = self._doctor_cookie("doc2a", "D2")
        status, payload = self._request("GET", "/api/notifications",
                                        cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["notifications"]), 1)
        self.assertEqual(payload["notifications"][0]["doctor_id"], "D2")
        self.assertEqual(payload["unread_count"], 1)

    def test_analytics_endpoint_figures_and_exports(self):
        today = datetime.date.today().isoformat()
        appt = self._book("D1", 101, appt_date=today, appt_time="09:00")
        self._request("POST",
                      f"/api/appointments/D1/{appt['token']}/status",
                      {"status": "Completed"})
        self._request("POST", "/api/bills",
                      {"patient_id": 101, "doctor_id": "D1",
                       "token": appt["token"]})
        status, payload = self._request(
            "POST", "/api/bills/1/pay", {})
        self.assertEqual(status, 200, payload)
        status, payload = self._request(
            "GET", f"/api/reports/analytics?period=daily&date={today}")
        self.assertEqual(status, 200)
        r = payload["report"]
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["completed"], 1)
        self.assertEqual(r["completion_rate"], 100)
        self.assertEqual(r["revenue"], 2500)
        status, ctype, text = self._raw(
            "GET", f"/api/reports/analytics.csv?period=daily&date={today}")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", ctype)
        self.assertIn("Total appointments,1", text)
        self.assertIn('"Revenue (Paid bills, Rs)",2500', text)
        status, ctype, data = self._raw(
            "GET", f"/api/reports/analytics.pdf?period=daily&date={today}",
            raw=True)
        self.assertEqual(status, 200)
        self.assertIn("application/pdf", ctype)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))

    def test_doctor_report_scoping(self):
        today = datetime.date.today().isoformat()
        self._book("D1", 101, appt_date=today, appt_time="09:00")
        self._book("D2", 102, appt_date=today, appt_time="10:00")
        cookie = self._doctor_cookie("doc2b", "D2")
        status, payload = self._request(
            "GET", f"/api/reports/analytics?period=daily&date={today}",
            cookie=cookie)
        self.assertEqual(status, 200)
        r = payload["report"]
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["per_doctor"][0]["doctor_id"], "D2")
        # Even asking for D1 explicitly, a Doctor only sees their own.
        status, payload = self._request(
            "GET", f"/api/reports/analytics?period=daily&date={today}"
                   "&doctor_id=D1", cookie=cookie)
        self.assertEqual(payload["report"]["total"], 1)
        self.assertEqual(payload["report"]["per_doctor"][0]["doctor_id"],
                         "D2")

    def test_analytics_bad_period_rejected(self):
        status, payload = self._request(
            "GET", "/api/reports/analytics?period=yearly")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
