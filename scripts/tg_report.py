"""Cross-reference the Telegram dialog scan against the groups table and the
fleet's active-unit list, and print the exact remediation list.

Read-only. Prints a plan; changes nothing.

    railway run --service Postgres py -3.11 scripts/tg_report.py \
        --dialogs tg_dialogs.json --units fleet_units.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncpg


def key(u: str | None) -> str:
    """Loose match key: digits only, no leading zeros ('0822' == '822')."""
    if not u:
        return ""
    d = re.sub(r"\D", "", str(u).replace("<", "").replace(">", "").strip())
    return d.lstrip("0") or d


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dialogs", default="tg_dialogs.json")
    ap.add_argument("--units", required=True, help="whitespace-separated unit list")
    args = ap.parse_args()

    tg = json.loads(Path(args.dialogs).read_text(encoding="utf-8"))
    wanted = Path(args.units).read_text(encoding="utf-8").split()

    dsn = os.environ.get("DATABASE_PUBLIC_URL") or os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    db_rows = await conn.fetch(
        "SELECT group_id, unit_number, title, is_active, setup_complete FROM groups")
    pti = dict(await conn.fetch(
        "SELECT p.group_id, count(*) FROM pti_log p GROUP BY 1"))
    await conn.close()

    db_by_gid = {r["group_id"]: r for r in db_rows}

    tg_by_unit: dict[str, list[dict]] = {}
    for r in tg:
        if k := key(r["unit"]):
            tg_by_unit.setdefault(k, []).append(r)

    db_by_unit: dict[str, list] = {}
    for r in db_rows:
        if k := key(r["unit_number"]):
            db_by_unit.setdefault(k, []).append(r)

    buckets: dict[str, list[str]] = {
        "ok": [], "flip_flag": [], "readd_bot": [], "no_group": [],
        "id_changed": [], "needs_setup": [],
    }
    detail: list[str] = []

    for u in wanted:
        k = key(u)
        chats = tg_by_unit.get(k, [])
        if not chats:
            buckets["no_group"].append(u)
            continue

        # Prefer a chat the bot is in; else the largest.
        chats.sort(key=lambda c: (c["bot"] in ("member", "admin"),
                                  c["participants"] or 0), reverse=True)
        c = chats[0]
        gid, status = c["group_id"], c["bot"]
        db = db_by_gid.get(gid)

        if status not in ("member", "admin"):
            buckets["readd_bot"].append(u)
            detail.append(f"  {u:>8}  {gid:>15}  bot={status}  {c['title'][:50]}")
        elif db is None:
            # Bot is in a chat the DB doesn't know under this id.
            others = [r for r in db_by_unit.get(k, []) if r["group_id"] != gid]
            if others:
                buckets["id_changed"].append(u)
                old = others[0]
                detail.append(f"  {u:>8}  db has {old['group_id']} -> telegram says {gid} "
                              f"(ptis on old id: {pti.get(old['group_id'], 0)})")
            else:
                buckets["needs_setup"].append(u)
        elif not db["is_active"]:
            buckets["flip_flag"].append(u)
        elif not db["setup_complete"]:
            buckets["needs_setup"].append(u)
        else:
            buckets["ok"].append(u)

    print(f"fleet units: {len(wanted)}   telegram groups: {len(tg)}   db rows: {len(db_rows)}\n")
    labels = {
        "ok": "OK - bot in group, active, set up",
        "flip_flag": "FIX IN DB - bot is in the group, is_active is wrongly FALSE",
        "readd_bot": "RE-ADD BOT - group exists, bot is not in it",
        "id_changed": "UPDATE ID - group migrated to a new chat id",
        "needs_setup": "RUN SETUP - bot present but setup incomplete / group unknown",
        "no_group": "NO GROUP - no telegram group found for this unit",
    }
    for name, label in labels.items():
        items = buckets[name]
        print(f"=== {label} ({len(items)}) ===")
        if items:
            print("  " + " ".join(items))
        print()

    if detail:
        print("=== detail ===")
        print("\n".join(detail))

    stray = [r for r in tg
             if key(r["unit"]) and key(r["unit"]) not in {key(u) for u in wanted}
             and r["bot"] in ("member", "admin")]
    print(f"\n=== bot is in these groups, but the unit is NOT on the fleet list ({len(stray)}) ===")
    for r in stray:
        print(f"  {str(r['unit']):>8}  {r['group_id']:>15}  ptis={pti.get(r['group_id'], 0):<3} {r['title'][:50]}")


if __name__ == "__main__":
    asyncio.run(main())
