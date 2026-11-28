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

    # ---- Assemble the PDF: catalog, pages, fonts, one page +
    # content object per page, then the cross-reference table.
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
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_W} {_PAGE_H}]"
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
