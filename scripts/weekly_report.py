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

# The query and the rendering are the bot's own (utils/), not a second
# implementation: this report gets compared against the panel, and two copies of
# a compliance query drift. Importing them needs the service's env, which is
# what `railway run` is for.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


async def run(args) -> None:
    from utils import weekly_pdf
    from utils.db import get_weekly_report
    from utils.enforcement import REQUIRED_PER_WEEK

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
        data = await get_weekly_report(start, end, conn=conn)
    finally:
        await conn.close()

    out = args.out or f"{args.fleet.lower()}-pti-{end:%Y-%m-%d}.pdf"
    with open(out, "wb") as fh:
        fh.write(weekly_pdf.render(args.fleet, start, end, label, data,
                                   REQUIRED_PER_WEEK))
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
