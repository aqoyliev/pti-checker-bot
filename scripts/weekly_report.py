"""A week of a fleet's inspections, as a PDF you can open on a phone.

    railway link -p jrd-pti -e production
    railway run py -3.11 scripts/weekly_report.py --fleet JRD --out jrd-week.pdf

Needs ``pip install reportlab``; everything else is already a dependency.

The database is on Railway's private network, so this opens its own tunnel
(``railway connect --tunnel-only``) and closes it again -- the second terminal
is the step that gets skipped, and skipping it fails with a connection error
that reads like the database is down. Pass ``--port`` if something already
listens there and the script will use it rather than starting a second one.

**Why a PDF and not a chat message.** The panel answers "how is the fleet doing
right now"; this answers "what happened last week", which is the thing that gets
forwarded to somebody who does not have a panel login, and read on a phone. A
document survives both.

The window is a rolling 7 days by default, not the quota week. The quota week
resets midnight Monday in FLEET_TZ, so on a Tuesday "this week" is one day long
and a report of it says nothing -- while "the last 7 days" is the same size
whenever you ask. The exact window is printed on the report either way, because
a compliance number without its period on the page is a number somebody will
misread later. ``--days`` changes it; ``--quota-week`` asks for the bot's own
week instead, which is the one to use when the numbers have to match the panel.

Read-only: it runs SELECTs and writes one file.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import asyncpg

# The compliance rule this report measures against. Kept in step with
# utils/enforcement.REQUIRED_PER_WEEK -- imported rather than copied would drag
# the whole config import (and its required env vars) into a reporting script.
REQUIRED_PER_WEEK = 2


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


@contextlib.contextmanager
def tunnel(args):
    """Hold open a Railway tunnel to the database for the duration.

    Nothing is started when --dsn names a database directly, or when something
    already listens on the port -- re-running against a tunnel you opened
    yourself should use it, not race a second one onto the same port.
    """
    if args.dsn or _port_open(args.host, args.port):
        yield
        return

    proc = subprocess.Popen(
        ["railway", "connect", args.service, "--tunnel-only", "-P", str(args.port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + args.tunnel_timeout
        while not _port_open(args.host, args.port):
            if proc.poll() is not None:
                # railway's own message says which of the usual things it is:
                # no project linked, no SSH key registered, wrong service name.
                raise SystemExit(
                    "Could not open the database tunnel.\n"
                    + (proc.stderr.read() or "").strip())
            if time.monotonic() > deadline:
                raise SystemExit(
                    f"The tunnel did not come up within {args.tunnel_timeout}s. "
                    "Outbound SSH (port 22) may be blocked on this network.")
            time.sleep(0.5)
        yield
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _conn_kwargs(args) -> dict:
    """Credentials from the environment, host and port from the tunnel.

    `railway run` injects the service's own variables, and DATABASE_URL there
    names *.railway.internal -- correct inside Railway, unreachable from a
    laptop. So the credentials are taken from whichever variable carries them
    and the address is replaced with the tunnel's. Nothing is printed.
    """
    if args.dsn:
        return {"dsn": args.dsn}

    url = os.environ.get("DATABASE_URL", "")
    if url:
        p = urlparse(url)
        user, password, database = p.username, p.password, (p.path or "/").lstrip("/")
    else:
        user = os.environ.get("PGUSER", "postgres")
        password = os.environ.get("PGPASSWORD", "")
        database = os.environ.get("PGDATABASE", "railway")

    if not password:
        raise SystemExit(
            "No database password in the environment. Run this under "
            "`railway run` so the service's variables are injected, or pass "
            "--dsn explicitly."
        )
    return {"host": args.host, "port": args.port, "user": user,
            "password": password, "database": database}


async def _window(conn, args) -> tuple[datetime, datetime, str]:
    """(start, end, label) in UTC. The quota week is asked of Postgres so the
    boundary is the same one utils/db.py uses, rather than a second opinion."""
    end = datetime.now(timezone.utc).replace(tzinfo=None)
    if args.quota_week:
        start = await conn.fetchval(
            "SELECT (date_trunc('week', NOW() AT TIME ZONE $1) AT TIME ZONE $1) "
            "AT TIME ZONE 'UTC'", args.tz)
        return start, end, f"quota week (from Monday, {args.tz})"
    return end - timedelta(days=args.days), end, f"rolling {args.days} days"


async def collect(conn, start, end) -> dict:
    units = await conn.fetch(
        """SELECT g.group_id, g.unit_number, g.title,
                  COUNT(p.id)                                AS ptis,
                  COUNT(p.id) FILTER (WHERE p.passed)        AS passed,
                  COUNT(p.id) FILTER (WHERE p.passed = FALSE) AS failed,
                  MAX(p.submitted_at)                        AS last_at
           FROM groups g
           LEFT JOIN pti_log p
             ON p.group_id = g.group_id
            AND p.submitted_at >= $1 AND p.submitted_at < $2
           WHERE COALESCE(g.is_active, TRUE) AND g.setup_complete
           GROUP BY g.group_id, g.unit_number, g.title
           ORDER BY g.unit_number NULLS LAST""",
        start, end)

    drivers = await conn.fetch(
        """SELECT d.group_id, d.user_id, d.name, g.unit_number,
                  COUNT(p.id) AS ptis
           FROM group_drivers d
           JOIN groups g ON g.group_id = d.group_id
           LEFT JOIN pti_log p
             ON p.group_id = d.group_id AND p.user_id = d.user_id
            AND p.submitted_at >= $1 AND p.submitted_at < $2
           WHERE COALESCE(g.is_active, TRUE) AND g.setup_complete
           GROUP BY d.group_id, d.user_id, d.name, g.unit_number
           ORDER BY COUNT(p.id), g.unit_number NULLS LAST""",
        start, end)

    # Severity is only meaningful on a failure, and only for the ones that
    # actually named one -- an older row can carry NULL.
    severities = await conn.fetch(
        """SELECT COALESCE(severity, 'unspecified') AS severity, COUNT(*) AS n
           FROM pti_log
           WHERE submitted_at >= $1 AND submitted_at < $2 AND passed = FALSE
           GROUP BY 1 ORDER BY 2 DESC""",
        start, end)

    return {"units": units, "drivers": drivers, "severities": severities}


def _fmt(dt, tz_label: str) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def build_pdf(path, fleet, start, end, window_label, data) -> None:
    try:
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
            TableStyle,
        )
    except ImportError:
        raise SystemExit("reportlab is not installed — run: pip install reportlab")

    units, drivers = data["units"], data["drivers"]
    total = sum(u["ptis"] for u in units)
    passed = sum(u["passed"] for u in units)
    failed = sum(u["failed"] for u in units)
    silent = [u for u in units if not u["ptis"]]
    short = [d for d in drivers if d["ptis"] < REQUIRED_PER_WEEK]

    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=10)
    doc = SimpleDocTemplate(
        path, pagesize=A4, title=f"{fleet} PTI — weekly report",
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm)
    story = []

    story.append(Paragraph(f"{fleet} PTI — weekly report", styles["Title"]))
    story.append(Paragraph(
        f"{start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M} UTC &nbsp;·&nbsp; "
        f"{window_label} &nbsp;·&nbsp; generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        small))
    story.append(Spacer(1, 8 * mm))

    pass_rate = f"{passed / total * 100:.0f}%" if total else "—"
    summary = [
        ["Inspections", str(total)],
        ["Passed", f"{passed}  ({pass_rate})"],
        ["Failed", str(failed)],
        ["Active configured units", str(len(units))],
        ["Units with no inspection", str(len(silent))],
        ["Registered drivers", str(len(drivers))],
        [f"Drivers below quota ({REQUIRED_PER_WEEK}/week)", str(len(short))],
    ]
    t = Table(summary, colWidths=[70 * mm, 40 * mm])
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.lightgrey),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
    ]))
    story.append(t)

    if data["severities"]:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("Failures by severity", styles["Heading3"]))
        sev = Table([["Severity", "Count"]] + [[r["severity"], str(r["n"])]
                                               for r in data["severities"]],
                    colWidths=[70 * mm, 40 * mm])
        sev.setStyle(_grid(colors))
        story.append(sev)

    # Drivers below quota lead, because that is the page somebody acts on.
    story.append(PageBreak())
    story.append(Paragraph(
        f"Drivers below quota — {len(short)} of {len(drivers)}", styles["Heading2"]))
    if short:
        rows = [["Unit", "Driver", "PTIs"]] + [
            [d["unit_number"] or str(d["group_id"]), d["name"], str(d["ptis"])]
            for d in short]
        tbl = Table(rows, colWidths=[30 * mm, 100 * mm, 20 * mm], repeatRows=1)
        tbl.setStyle(_grid(colors))
        story.append(tbl)
    else:
        story.append(Paragraph("Every registered driver met the quota.", styles["Normal"]))

    story.append(PageBreak())
    story.append(Paragraph(f"All units — {len(units)}", styles["Heading2"]))
    rows = [["Unit", "PTIs", "Pass", "Fail", "Last inspection (UTC)"]] + [
        [u["unit_number"] or str(u["group_id"]), str(u["ptis"]), str(u["passed"]),
         str(u["failed"]), _fmt(u["last_at"], "UTC")]
        for u in units]
    tbl = Table(rows, colWidths=[30 * mm, 20 * mm, 20 * mm, 20 * mm, 50 * mm],
                repeatRows=1)
    style = _grid(colors)
    # A unit that filmed nothing all week is the row to find at a glance.
    for i, u in enumerate(units, start=1):
        if not u["ptis"]:
            style.add("TEXTCOLOR", (0, i), (-1, i), colors.HexColor("#b00020"))
    tbl.setStyle(style)
    story.append(tbl)

    doc.build(story)


def _grid(colors):
    from reportlab.platypus import TableStyle
    return TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
    ])


async def run(args) -> None:
    try:
        conn = await asyncpg.connect(**_conn_kwargs(args))
    except (OSError, asyncpg.PostgresError) as e:
        # The likeliest cause by far, and a raw traceback here reads as "the
        # database is down" rather than "nothing is listening on the tunnel".
        raise SystemExit(
            f"Could not reach the database at {args.dsn or f'{args.host}:{args.port}'} "
            f"— {type(e).__name__}: {e}") from e
    try:
        start, end, label = await _window(conn, args)
        data = await collect(conn, start, end)
    finally:
        await conn.close()

    out = args.out or f"{args.fleet.lower()}-pti-{end:%Y-%m-%d}.pdf"
    build_pdf(out, args.fleet, start, end, label, data)
    print(f"{out}  —  {sum(u['ptis'] for u in data['units'])} inspection(s), "
          f"{len(data['units'])} unit(s), {start:%Y-%m-%d} to {end:%Y-%m-%d}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fleet", default="Fleet", help="name printed on the report")
    ap.add_argument("--out", help="output path (default: <fleet>-pti-<date>.pdf)")
    ap.add_argument("--days", type=int, default=7, help="rolling window (default 7)")
    ap.add_argument("--quota-week", action="store_true",
                    help="use the bot's Monday-start quota week instead of --days")
    ap.add_argument("--tz", default=os.environ.get("FLEET_TZ", "America/New_York"),
                    help="timezone the quota week starts in")
    ap.add_argument("--host", default="127.0.0.1", help="tunnel host")
    ap.add_argument("--port", type=int, default=15432, help="tunnel port")
    ap.add_argument("--service", default="Postgres", help="Railway database service")
    ap.add_argument("--tunnel-timeout", type=int, default=30,
                    help="seconds to wait for the tunnel (default 30)")
    ap.add_argument("--dsn", help="full connection string, instead of the tunnel")
    args = ap.parse_args()
    with tunnel(args):
        asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
