"""Minimal PDF export for the Hospital Management System.

Step 9 (Appointments table): the appointments list can be downloaded
as a PDF. Python standard library ONLY — no reportlab, no external
dependency. This writes a plain, correct PDF by hand: a landscape A4
page, the built-in Helvetica fonts (no embedding needed — every PDF
reader ships them), a title, a subtitle line (filters + totals), a
table with one row per appointment, and page numbers in the footer.

Text is sanitised to Latin-1 (anything else becomes "?") and the
three characters that are special inside a PDF literal string —
backslash, "(" and ")" — are escaped, so a patient's name or a reason
can never break the file. Columns are truncated to fit their width;
the full text lives in the CSV export instead.

Step 12 (Reports & Analytics): ``report_pdf`` builds the analytics
report the same way — portrait A4, summary + by-status / per-doctor /
per-department tables, a per-day bar chart, and the cancelled /
no-show lists — sharing the same PDF assembly (``_assemble_pdf``).
"""

# Landscape A4 in points (842 x 595).
_PAGE_W, _PAGE_H = 842, 595
_MARGIN = 36
# Columns: (key, heading, x, max characters). The last column ends at
# the right margin (842 - 36 = 806).
_COLUMNS = (
    ("apt_id", "Appt ID", 36, 11),
    ("patient_name", "Patient", 112, 30),
    ("doctor_name", "Doctor", 262, 26),
    ("department", "Department", 392, 22),
    ("appt_date", "Date", 502, 10),
    ("appt_time", "Time", 568, 5),
    ("appt_type", "Type", 612, 12),
    ("status", "Status", 674, 13),
    ("fee", "Fee (Rs)", 750, 8),
)
_TITLE_SIZE, _HEAD_SIZE, _ROW_SIZE = 16, 9, 9
_ROW_STEP = 14
_TOP = 520          # y of the first table row
_BOTTOM = 50        # footer sits below this; rows stop above it


def _pdf_text(value):
    """Make a value safe for a PDF literal string: Latin-1 only,
    with \\, ( and ) escaped."""
    text = str(value if value is not None else "")
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _fit(value, max_chars):
    """Truncate a value to ``max_chars`` characters (with ... when cut)."""
    text = str(value if value is not None else "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..." if max_chars > 3 else text[:max_chars]


def _line(font, size, x, y, text):
    """One line of text as a PDF content-stream fragment."""
    return (f"BT /{font} {size} Tf 1 0 0 1 {x} {y} Tm "
            f"({_pdf_text(text)}) Tj ET\n")


def _row_text(row):
    """One appointment as (x, text) pairs for the table columns."""
    out = []
    for key, _heading, x, max_chars in _COLUMNS:
        value = row.get(key)
        if key == "fee":
            value = str(value) if value is not None else ""
        elif key == "apt_time":
            value = value or "—"
        elif key == "appt_date":
            value = value or "—"
        elif key == "apt_id":
            value = value or "—"
        out.append((x, _fit(value, max_chars)))
    return out


def appointments_pdf(rows, title, subtitle):
    """The appointments table as PDF bytes.

    ``rows`` are the appointment dicts from the list endpoint (keys:
    apt_id, patient_name, doctor_name, department, appt_date,
    appt_time, appt_type, status, fee). ``title`` is the heading,
    ``subtitle`` says which filters were applied and how many rows
    there are. Returns ``bytes`` — a complete, valid PDF document.
    """
    pages = []  # each page is a list of content-stream fragments
    rows = list(rows)
    total = len(rows)
    page_no = 0

    def _new_page():
        nonlocal page_no
        page_no += 1
        lines = []
        lines.append(_line("F1", _TITLE_SIZE, _MARGIN, _PAGE_H - 48, title))
        lines.append(_line("F1", _HEAD_SIZE, _MARGIN, _PAGE_H - 66, subtitle))
        # Table header (bold) + a rule underneath it.
        for _key, heading, x, _max in _COLUMNS:
            lines.append(_line("F2", _HEAD_SIZE, x, _PAGE_H - 88, heading))
        lines.append(f"0.5 w {_MARGIN} {_PAGE_H - 94} m "
                     f"{_PAGE_W - _MARGIN} {_PAGE_H - 94} l S\n")
        lines.append(_line("F1", 8, _MARGIN, 30,
                           f"City Care Hospital — Page {page_no}"))
        lines.append(_line("F1", 8, _PAGE_W - _MARGIN - 90, 30,
                           f"{total} appointment(s)"))
        pages.append(lines)
        return lines

    current = _new_page()
    y = _TOP
    for row in rows:
        if y < _BOTTOM + _ROW_STEP:
            current = _new_page()
            y = _TOP
        for x, text in _row_text(row):
            current.append(_line("F1", _ROW_SIZE, x, y, text))
        y -= _ROW_STEP
    if not rows:
        current.append(_line("F1", _ROW_SIZE, _MARGIN, _TOP,
                             "No appointments match these filters."))

    return _assemble_pdf(pages, _PAGE_W, _PAGE_H)


def _assemble_pdf(pages, page_w, page_h):
    """Pages (lists of content-stream fragments) -> a complete PDF.

    Catalog, Pages, the two built-in Helvetica fonts, one page +
    content object per page, then the cross-reference table. Shared
    by appointments_pdf and report_pdf.
    """
    objects = []
    # 1: Catalog, 2: Pages, 3: Helvetica, 4: Helvetica-Bold.
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_ids = [5 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("latin-1"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    for i, lines in enumerate(pages):
        content = "".join(lines).encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}]"
            f" /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >>"
            f" /Contents {page_ids[i] + 1} 0 R >>".encode("latin-1"))
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode("latin-1")
            + content + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + obj + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode("latin-1")
    return bytes(out)


# Report PDF (Step 12): portrait A4 in points (595 x 842).
_R_PAGE_W, _R_PAGE_H = 595, 842
_R_MARGIN = 40
_R_TOP = 750          # y of the first content line
_R_BOTTOM = 50        # footer sits below this


def report_pdf(report):
    """The Step 12 analytics report as PDF bytes (portrait A4).

    Summary lines, by-status table, per-doctor and per-department
    tables, a per-day bar chart, and the cancelled / no-show lists.
    Same hand-built approach as appointments_pdf (stdlib only).
    """
    title = f"Appointments Report — {report['period'].title()}"
    subtitle = (f"{report['from']} to {report['to']}"
                + (f"  •  Doctor {report['doctor_id']}"
                   if report.get("doctor_id") else "")
                + (f"  •  {report['department']}"
                   if report.get("department") else ""))
    pages = []
    page_no = 0

    def _new_page():
        nonlocal page_no
        page_no += 1
        lines = []
        lines.append(_line("F1", _TITLE_SIZE, _R_MARGIN, _R_PAGE_H - 48, title))
        lines.append(_line("F1", _HEAD_SIZE, _R_MARGIN, _R_PAGE_H - 66, subtitle))
        lines.append(f"0.5 w {_R_MARGIN} {_R_PAGE_H - 74} m "
                     f"{_R_PAGE_W - _R_MARGIN} {_R_PAGE_H - 74} l S\n")
        lines.append(_line("F1", 8, _R_MARGIN, 30,
                           f"City Care Hospital — Page {page_no}"))
        lines.append(_line("F1", 8, _R_PAGE_W - _R_MARGIN - 110, 30,
                           f"{report['total']} appointment(s)"))
        pages.append(lines)
        return lines

    current = [_new_page()]
    y = [_R_TOP]  # mutable box so the helpers below can move it

    def _space(needed):
        if y[0] - needed < _R_BOTTOM:
            current[0] = _new_page()
            y[0] = _R_TOP

    def _heading(text):
        _space(30)
        current[0].append(_line("F2", 12, _R_MARGIN, y[0], text))
        y[0] -= 20

    def _text(text, size=10):
        _space(16)
        current[0].append(_line("F1", size, _R_MARGIN, y[0], text))
        y[0] -= 15

    def _table(columns, rows):
        """columns: [(heading, x, max_chars)]; rows: [[values...]]."""
        _space(30)
        for heading, x, _m in columns:
            current[0].append(_line("F2", 9, x, y[0], heading))
        y[0] -= 6
        current[0].append(f"0.5 w {_R_MARGIN} {y[0]} m "
                           f"{_R_PAGE_W - _R_MARGIN} {y[0]} l S\n")
        y[0] -= 12
        for row in rows:
            _space(16)
            for (_h, x, max_chars), value in zip(columns, row):
                current[0].append(
                    _line("F1", 9, x, y[0], _fit(value, max_chars)))
            y[0] -= 14

    def _bars(per_day):
        """Appointments per day as horizontal bars (label + count)."""
        _space(30)
        max_count = max([d["count"] for d in per_day] + [1])
        bar_x = _R_MARGIN + 90
        bar_max = 300
        for d in per_day:
            _space(18)
            current[0].append(_line("F1", 9, _R_MARGIN, y[0], d["date"]))
            width = bar_max * d["count"] / max_count
            if width > 0:
                current[0].append(
                    f"0.09 0.41 0.67 rg {bar_x} {y[0] - 3} {width:.1f} 9 re f\n"
                    "0 0 0 rg\n")
            current[0].append(
                _line("F1", 9, bar_x + bar_max + 8, y[0],
                      str(d["count"])))
            y[0] -= 16

    _heading("Summary")
    _text(f"Total appointments: {report['total']}")
    _text(f"Completed: {report['completed']} "
          f"({report['completion_rate']}%)")
    _text(f"Cancelled: {report['cancelled']} "
          f"({report['cancellation_rate']}%)")
    _text(f"No Show: {report['no_show']} ({report['no_show_rate']}%)")
    _text(f"Revenue (Paid bills): Rs {report['revenue']}")
    _text(f"Unpaid in period: Rs {report['unpaid_total']}")
    _heading("By status")
    _table([("Status", _R_MARGIN, 20), ("Count", 300, 8)],
           [[s, str(report["by_status"][s])] for s in
            ("Pending", "Confirmed", "Checked In", "In Progress",
             "Completed", "Cancelled", "No Show")])
    _heading("Per doctor")
    _table([("Doctor", _R_MARGIN, 28), ("Department", 210, 18),
            ("Appts", 330, 6), ("Done", 385, 6), ("Canc.", 435, 6),
            ("No Show", 485, 7)],
           [[f"{d['name']} ({d['doctor_id']})", d["department"],
             str(d["appointments"]), str(d["completed"]),
             str(d["cancelled"]), str(d["no_show"])]
            for d in report["per_doctor"]])
    _heading("Per department")
    _table([("Department", _R_MARGIN, 28), ("Appts", 300, 6),
            ("Done", 360, 6), ("Canc.", 420, 6), ("No Show", 480, 7)],
           [[d["name"], str(d["appointments"]), str(d["completed"]),
             str(d["cancelled"]), str(d["no_show"])]
            for d in report["per_department"]])
    _heading("Appointments per day")
    _bars(report["per_day"])
    _heading("Cancelled appointments")
    if report["cancelled_list"]:
        _table([("Appt ID", _R_MARGIN, 11), ("Patient", 120, 22),
                ("Doctor", 280, 20), ("Date", 420, 10),
                ("Time", 490, 5)],
               [[a.get("apt_id") or "", a["patient_name"],
                 a["doctor_name"], a["appt_date"] or "",
                 a["appt_time"] or ""]
                for a in report["cancelled_list"]])
    else:
        _text("None in this period.")
    _heading("No-show appointments")
    if report["no_show_list"]:
        _table([("Appt ID", _R_MARGIN, 11), ("Patient", 120, 22),
                ("Doctor", 280, 20), ("Date", 420, 10),
                ("Time", 490, 5)],
               [[a.get("apt_id") or "", a["patient_name"],
                 a["doctor_name"], a["appt_date"] or "",
                 a["appt_time"] or ""]
                for a in report["no_show_list"]])
    else:
        _text("None in this period.")
    return _assemble_pdf(pages, _R_PAGE_W, _R_PAGE_H)
