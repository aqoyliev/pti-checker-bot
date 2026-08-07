"""Propose driver registrations for freshly-added groups by matching the driver
names in the group title against the actual Telegram members.

Titles read like "1310 - LAGUERRE, DUNON / JEAN BAPTISTE, BILLY (COMPANY)" --
LASTNAME, FIRSTNAME pairs separated by / -- while the members list also holds
Gurman staff, the HOS account and bots. This scores each member against each
title name and proposes the best match.

Read-only against Telegram. Writes driver_matches.json for review; performing
the DB writes is scripts/apply_drivers.py, separately.

    railway run --service pti-checker-bot py -3.11 scripts/tg_match_drivers.py \
        --plan rehome_plan.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from telethon import TelegramClient

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SESSION = Path.home() / ".pti-tg" / "fleet_audit"
BOT_ID = 8698369700
STAFF = re.compile(r"gurman|grm|monitoring|dispatch|safety", re.IGNORECASE)
NOISE = re.compile(r"\b(company|owner|lo|md|ml|lease|sub|unit|truck)\b", re.IGNORECASE)


def title_driver_names(title: str) -> list[str]:
    """Pull the 'LASTNAME, FIRSTNAME' chunks out of a group title."""
    t = re.sub(r"^\s*(unit|truck)\s*#?\s*[:#]?\s*\d+\s*", "", title or "", flags=re.I)
    t = re.sub(r"\(.*?\)", " ", t)          # drop (COMPANY), (MD), ...
    t = re.sub(r"^\s*\d{3,6}\s*[-/]*\s*", "", t)  # leading bare unit number
    parts = [p.strip(" -/,") for p in re.split(r"[/]{1,3}", t)]
    return [NOISE.sub("", p).strip() for p in parts if len(NOISE.sub("", p).strip()) > 3]


def tokens(s: str) -> set[str]:
    return {w for w in re.split(r"[^A-Za-z]+", (s or "").upper()) if len(w) >= 4}


def score(name_in_title: str, member_name: str) -> int:
    return len(tokens(name_in_title) & tokens(member_name))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="rehome_plan.json")
    ap.add_argument("--out", default="driver_matches.json")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    jobs = plan["add_bot"]

    out = []
    async with TelegramClient(str(SESSION), int(os.environ["TELEGRAM_API_ID"]),
                              os.environ["TELEGRAM_API_HASH"]) as client:
        for job in jobs:
            gid, unit = job["new_group_id"], job["unit"]
            ent = await client.get_entity(gid)
            title = getattr(ent, "title", job.get("new_title", ""))
            wanted = title_driver_names(title)

            members = await client.get_participants(ent, limit=200)
            pool = [
                m for m in members
                if not m.bot and m.id != BOT_ID
                and not STAFF.search(" ".join(filter(None, [m.username, m.first_name,
                                                            m.last_name])) or "")
            ]

            picks, used = [], set()
            for want in wanted:
                best, best_s = None, 0
                for m in pool:
                    if m.id in used:
                        continue
                    full = f"{m.first_name or ''} {m.last_name or ''}".strip()
                    s = score(want, full)
                    if s > best_s:
                        best, best_s = m, s
                if best and best_s >= 1:
                    used.add(best.id)
                    picks.append({
                        "user_id": best.id,
                        "tg_name": f"{best.first_name or ''} {best.last_name or ''}".strip(),
                        "username": best.username,
                        "title_name": want,
                        "score": best_s,
                    })
                else:
                    picks.append({"user_id": None, "title_name": want, "score": 0,
                                  "tg_name": None, "username": None})

            rec = {"group_id": gid, "unit": unit, "title": title,
                   "members": len(members), "candidates": len(pool), "picks": picks}
            out.append(rec)

            print(f"\n=== unit {unit}  {gid}  ({len(members)} members, "
                  f"{len(pool)} non-staff) ===")
            print(f"    {title}")
            for p in picks:
                if p["user_id"]:
                    print(f"    OK   {p['title_name']:<34} -> {p['tg_name']} "
                          f"(@{p['username']}) id={p['user_id']} score={p['score']}")
                else:
                    print(f"    MISS {p['title_name']:<34} -> no member matched")
            await asyncio.sleep(0.3)

    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False),
                              encoding="utf-8")
    tot = sum(len(r["picks"]) for r in out)
    hit = sum(1 for r in out for p in r["picks"] if p["user_id"])
    print(f"\nmatched {hit}/{tot} driver slots across {len(out)} groups -> {args.out}")
    print("Review it, then apply with scripts/apply_drivers.py")


if __name__ == "__main__":
    asyncio.run(main())
