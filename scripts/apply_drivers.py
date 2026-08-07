"""Write the reviewed driver matches into the DB: set each group's unit_number
and register the matched drivers.

Rules:
  * unit_number is set from the plan -- it is corroborated by two independent
    sources (the old group's stored unit, and the new group's title), so it is
    safe to write. Never taken from the title alone.
  * setup_complete is set TRUE only when at least one driver was registered;
    a group with no matched driver stays incomplete so the setup nag keeps
    prompting a human.
  * at most 2 drivers per group, matching the /adddriver handler's limit.
  * --min-score 2 restricts writes to high-confidence matches only.

DRY RUN BY DEFAULT.

    railway run --service Postgres py -3.11 scripts/apply_drivers.py \
        --matches driver_matches.json [--min-score 1] [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matches", default="driver_matches.json")
    ap.add_argument("--min-score", type=int, default=1)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    data = json.loads(Path(args.matches).read_text(encoding="utf-8"))
    dsn = os.environ.get("DATABASE_PUBLIC_URL") or os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)

    units = drivers = skipped = 0
    for rec in data:
        gid, unit = rec["group_id"], rec["unit"]
        picks = [p for p in rec["picks"]
                 if p["user_id"] and p["score"] >= args.min_score][:2]

        cur = await conn.fetchrow(
            "SELECT unit_number, setup_complete FROM groups WHERE group_id = $1", gid)
        if cur is None:
            print(f"  unit {unit:>8}  {gid:>16}  NOT IN DB -- skip")
            continue

        print(f"\nunit {unit:>8}  {gid:>16}  (db unit now {cur['unit_number']!r})")
        if cur["unit_number"] and cur["unit_number"] != unit:
            print(f"    !! db already has unit {cur['unit_number']!r}, plan says "
                  f"{unit!r} -- SKIPPING, needs a human")
            skipped += 1
            continue

        print(f"    set unit_number -> {unit!r}")
        units += 1
        for p in picks:
            print(f"    add driver {p['title_name']:<30} -> {p['tg_name']} "
                  f"(id={p['user_id']}, score={p['score']})")
            drivers += 1
        for p in rec["picks"]:
            if not p["user_id"] or p["score"] < args.min_score:
                print(f"    unmatched: {p['title_name']} -- needs manual /adddriver")

        if not args.apply:
            continue

        await conn.execute(
            "UPDATE groups SET unit_number = $1 WHERE group_id = $2", unit, gid)
        for p in picks:
            await conn.execute(
                "INSERT INTO group_drivers (group_id, user_id, name)"
                " VALUES ($1, $2, $3) ON CONFLICT (group_id, user_id) DO NOTHING",
                gid, p["user_id"], p["title_name"])
        if picks:
            await conn.execute(
                "UPDATE groups SET setup_complete = TRUE WHERE group_id = $1", gid)

    print(f"\ngroups given a unit: {units}   drivers registered: {drivers}"
          f"   skipped (unit conflict): {skipped}")
    if not args.apply:
        print("DRY RUN -- nothing changed. Re-run with --apply.")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
