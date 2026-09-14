"""The weekly inspection report, rendered to PDF bytes.

Formatting only -- the caller does the query (``db.get_weekly_report``) and
decides what to do with the bytes. Two callers want different things: the web
panel sends them straight to Telegram as a document, and
``scripts/weekly_report.py`` writes a file, so nothing here touches disk.

**Why the panel sends a document instead of serving a download.** The panel runs
inside Telegram's in-app browser, where a link that starts a download has
nowhere to put the file -- which is the bug this exists to fix. A document sent
into the admin's own chat saves and forwards on every phone, so the panel asks
the bot to send it rather than trying to hand the WebView a file.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

_SILENT = "#b00020"
_HEADER_BG = "#f0f0f0"


def _require_reportlab():
    try:
        import reportlab  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "reportlab is not installed — pip install -r requirements.txt") from e


def _grid():
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle
    return TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_HEADER_BG)),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ])


def render(fleet: str, start: datetime, end: datetime, window_label: str,
           data: dict, required_per_week: int) -> bytes:
    """The report as PDF bytes. `data` is what db.get_weekly_report returned.

    The quota comes in as an argument rather than being imported: importing it
    from utils.enforcement would pull in loader, and a formatter that builds a
    Bot to find out what number to print is a dependency nobody expects.
    """
    _require_reportlab()
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
    )

    units, drivers = data["units"], data["drivers"]
    total = sum(u["ptis"] for u in units)
    passed = sum(u["passed"] for u in units)
    failed = sum(u["failed"] for u in units)
    silent = [u for u in units if not u["ptis"]]
    short = [d for d in drivers if d["ptis"] < required_per_week]

    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=10)
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title=f"{fleet} — weekly PTI report",
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm)
    story = []

    story.append(Paragraph(f"{fleet} — weekly PTI report", styles["Title"]))
    story.append(Paragraph(
        f"{start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M} UTC &nbsp;·&nbsp; "
        f"{window_label} &nbsp;·&nbsp; "
        f"generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", small))
    story.append(Spacer(1, 8 * mm))

    rate = f"{passed / total * 100:.0f}%" if total else "—"
    summary = Table([
        ["Inspections", str(total)],
        ["Passed", f"{passed}  ({rate})"],
        ["Failed", str(failed)],
        ["Active configured units", str(len(units))],
        ["Units with no inspection", str(len(silent))],
        ["Registered drivers", str(len(drivers))],
        [f"Drivers below quota ({required_per_week}/week)", str(len(short))],
    ], colWidths=[70 * mm, 45 * mm])
    summary.setStyle(_grid())
    story.append(summary)

    if data["severities"]:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("Failures by severity", styles["Heading3"]))
        sev = Table([["Severity", "Count"]]
                    + [[r["severity"], str(r["n"])] for r in data["severities"]],
                    colWidths=[70 * mm, 45 * mm])
        sev.setStyle(_grid())
        story.append(sev)

    # Drivers below quota lead, because that is the page somebody acts on.
    story.append(PageBreak())
    story.append(Paragraph(
        f"Drivers below quota — {len(short)} of {len(drivers)}", styles["Heading2"]))
    if short:
        rows = [["Unit", "Driver", "PTIs"]] + [
            [d["unit_number"] or str(d["group_id"]), d["name"], str(d["ptis"])]
            for d in short]
        t = Table(rows, colWidths=[30 * mm, 100 * mm, 20 * mm], repeatRows=1)
        t.setStyle(_grid())
        story.append(t)
    else:
        story.append(Paragraph("Every registered driver met the quota.",
                               styles["Normal"]))

    story.append(PageBreak())
    story.append(Paragraph(f"All units — {len(units)}", styles["Heading2"]))
    rows = [["Unit", "PTIs", "Pass", "Fail", "Last inspection (UTC)"]] + [
        [u["unit_number"] or str(u["group_id"]), str(u["ptis"]), str(u["passed"]),
         str(u["failed"]),
         u["last_at"].strftime("%Y-%m-%d %H:%M") if u["last_at"] else "—"]
        for u in units]
    t = Table(rows, colWidths=[30 * mm, 20 * mm, 20 * mm, 20 * mm, 50 * mm],
              repeatRows=1)
    style = _grid()
    # A unit that filmed nothing all week is the row to find at a glance.
    for i, u in enumerate(units, start=1):
        if not u["ptis"]:
            style.add("TEXTCOLOR", (0, i), (-1, i), colors.HexColor(_SILENT))
    t.setStyle(style)
    story.append(t)

    doc.build(story)
    return buf.getvalue()
