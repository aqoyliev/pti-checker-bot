"""Load a fleet roster paste (unit + trailer per line) into `active_units`.

The one-off companion to `/units`: the weekly command takes a bare list of unit
numbers, while the office's roster carries the trailer assignment beside each
truck. Used to seed a *new* fleet's list, where there is no weekly paste yet.

    railway run py -3.11 scripts/load_fleet_roster.py roster.txt          # preview
    railway run py -3.11 scripts/load_fleet_roster.py roster.txt --apply  # write

Preview first, like `/units`: nothing is written without `--apply`, and the
preview names every line that could not be read. Adds and updates only -- it
never retires a unit, because that would deactivate the groups filed under it
and that decision belongs to `/units`, where an admin confirms the casualties.

DATABASE_URL comes from the environment, so run it under `railway run` (with
`--service` naming the fleet you mean) and check that first line of output --
it is the only thing standing between "seeded the new fleet" and "wrote another
fleet's trucks into this one".
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.config import DATABASE_URL  # noqa: E402
from utils.db import get_fleet_roster, init_db, set_fleet_roster  # noqa: E402
from utils.fleet_roster import parse_roster  # noqa: E402


def _redacted(url: str) -> str:
    """postgres://user:pw@host:5432/db -> host:5432/db"""
    return url.rsplit("@", 1)[-1] if url else "(unset)"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="roster file: '<unit>  <trailer>  <drivers>' per line")
    ap.add_argument("--apply", action="store_true", help="write to the database")
    args = ap.parse_args()

    text = Path(args.path).read_text(encoding="utf-8")
    rows, problems = parse_roster(text)

    print(f"database: {_redacted(DATABASE_URL)}")
    print(f"parsed {len(rows)} units, {len(problems)} unreadable lines")
    for p in problems:
        print(f"  ! {p['reason']}: {p['line'][:70]}")
    if rows:
        preview = ", ".join(f"{r['unit']}/{r['trailer']}" for r in rows[:5])
        print(f"  e.g. {preview}, ...")

    if not args.apply:
        print("\nnothing written — re-run with --apply")
        return 0
    if not rows:
        print("\nnothing to write")
        return 1

    await init_db()
    before = await get_fleet_roster()
    added, updated = await set_fleet_roster(rows)
    after = await get_fleet_roster()
    print(f"\nwrote {added} new units, updated {updated}; "
          f"active_units now holds {len(after)} (was {len(before)})")
    missing = [u for u, t in after.items() if not t]
    if missing:
        print(f"{len(missing)} units carry no trailer: {', '.join(sorted(missing)[:10])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
