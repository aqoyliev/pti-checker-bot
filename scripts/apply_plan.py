"""Apply the DB half of rehome_plan.json: deactivate superseded groups.

Only ever sets is_active = FALSE, and only for group_ids the plan lists as
having a vetted replacement the bot was added to. DRY RUN BY DEFAULT.

    railway run --service Postgres py -3.11 scripts/apply_plan.py [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncpg


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="rehome_plan.json")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    rows = plan.get("deactivate", [])
    if not rows:
        print("nothing to deactivate")
        return

    dsn = os.environ.get("DATABASE_PUBLIC_URL") or os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)

    ids = [r["group_id"] for r in rows]
    current = {r["group_id"]: r for r in await conn.fetch(
        "SELECT group_id, unit_number, is_active, title FROM groups"
        " WHERE group_id = ANY($1::bigint[])", ids)}

    print(f"{len(rows)} groups in the deactivate list:\n")
    todo = []
    for r in rows:
        db = current.get(r["group_id"])
        if db is None:
            print(f"  {str(r['unit']):>8}  {r['group_id']:>15}  not in DB -- skip")
            continue
        if not db["is_active"]:
            print(f"  {str(r['unit']):>8}  {r['group_id']:>15}  already inactive -- skip")
            continue
        todo.append(r["group_id"])
        print(f"  {str(r['unit']):>8}  {r['group_id']:>15}  DEACTIVATE  {r['reason']}")

    print(f"\n{len(todo)} rows would change.")
    if not args.apply:
        print("DRY RUN -- nothing changed. Re-run with --apply.")
        await conn.close()
        return

    res = await conn.execute(
        "UPDATE groups SET is_active = FALSE WHERE group_id = ANY($1::bigint[])", todo)
    print(f"updated: {res}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
