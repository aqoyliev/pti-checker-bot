"""Find units whose group has gone quiet, locate the replacement group, vet it,
and add the bot to it.

Pipeline, per the fleet's rules:
  1. Take groups the user AND the bot are both in.
  2. Quiet for --stale-days (default 3) with no *human* message -> propose
     deactivating that group.
  3. For each such unit, look for another group with the same unit number that
     the bot is NOT in.
  4. Vet that candidate before touching it. BOTH must hold:
       a. @HOS_Monitoring24 is a member;
       b. at least one member's name/username contains "grm" or "gurman".
  5. Only then add the bot.

DRY RUN BY DEFAULT. Writes rehome_plan.json either way; --apply performs
step 5 only. Deactivations are NOT done here (no DB access in this env) --
run scripts/apply_plan.py under the Postgres service for those.

    railway run --service pti-checker-bot py -3.11 scripts/tg_rehome.py \
        --dialogs tg_dialogs.json --units fleet_units.txt
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from telethon import TelegramClient
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    UserAlreadyParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import Channel, Chat

SESSION = Path.home() / ".pti-tg" / "fleet_audit"
BOT_ID = 8698369700
HOS_USERNAME = "HOS_Monitoring24"
NAME_MARKERS = re.compile(r"gurman|grm", re.IGNORECASE)
PLAN = Path("rehome_plan.json")


def key(u) -> str:
    if not u:
        return ""
    d = re.sub(r"\D", "", str(u))
    return d.lstrip("0") or d


def parse_dt(s):
    if not s or str(s).startswith("unknown"):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


async def vet(client, entity, hos_id):
    """Return (passed, detail). Read-only membership inspection."""
    try:
        members = await client.get_participants(entity, limit=200)
    except ChatAdminRequiredError:
        return False, "cannot list members (need admin)"
    except FloodWaitError:
        raise
    except Exception as e:
        return False, f"member list failed: {type(e).__name__}"

    has_hos = any(m.id == hos_id for m in members)
    matches = [
        (m.username or " ".join(filter(None, [m.first_name, m.last_name])) or str(m.id))
        for m in members
        if NAME_MARKERS.search(
            " ".join(filter(None, [m.username, m.first_name, m.last_name])) or ""
        )
    ]
    if not has_hos:
        return False, f"@{HOS_USERNAME} not a member ({len(members)} members)"
    if not matches:
        return False, f"no grm/gurman member among {len(members)}"
    return True, f"@{HOS_USERNAME} present; matched {', '.join(matches[:3])}"


async def add_bot(client, entity, bot) -> str:
    try:
        if isinstance(entity, Channel):
            await client(InviteToChannelRequest(entity, [bot]))
        elif isinstance(entity, Chat):
            await client(AddChatUserRequest(entity.id, bot, fwd_limit=0))
        else:
            return f"skipped:unsupported-{type(entity).__name__}"
        return "added"
    except UserAlreadyParticipantError:
        return "already-there"
    except ChatAdminRequiredError:
        return "failed:you-lack-invite-rights"
    except UserPrivacyRestrictedError:
        return "failed:privacy-restricted"
    except FloodWaitError as e:
        return f"floodwait:{e.seconds}"
    except Exception as e:
        return f"failed:{type(e).__name__}:{e}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dialogs", default="tg_dialogs.json")
    ap.add_argument("--units", required=True)
    ap.add_argument("--db-groups", help="JSON dump of the groups table; its "
                                        "unit_number is authoritative for groups "
                                        "the bot is already in")
    ap.add_argument("--stale-days", type=int, default=3)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=15.0)
    args = ap.parse_args()

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH not set.")

    dialogs = json.loads(Path(args.dialogs).read_text(encoding="utf-8"))
    fleet = {key(u) for u in Path(args.units).read_text(encoding="utf-8").split()}
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.stale_days)

    # Titles are an unreliable source for the unit (only ~80% parse, and some
    # parse to the *wrong* unit). For any group already in the DB, the stored
    # unit_number is authoritative -- use it and fall back to the title only
    # for groups the DB has never seen.
    db_unit: dict[int, str] = {}
    if args.db_groups:
        for r in json.loads(Path(args.db_groups).read_text(encoding="utf-8")):
            if r.get("unit_number"):
                db_unit[r["group_id"]] = r["unit_number"]

    for d in dialogs:
        resolved = db_unit.get(d["group_id"]) or db_unit.get(d.get("migrated_from"))
        d["unit_src"] = "db" if resolved else "title"
        d["unit_final"] = resolved or d.get("unit")

    with_bot = [d for d in dialogs if d["bot"] in ("member", "admin")]
    without_bot = [d for d in dialogs if d["bot"] == "absent"]

    stale = []
    for d in with_bot:
        dt = parse_dt(d.get("last_human"))
        if dt is None or dt < cutoff:
            d["_quiet_days"] = (datetime.now(timezone.utc) - dt).days if dt else None
            stale.append(d)

    print(f"groups you and the bot share : {len(with_bot)}")
    print(f"quiet for >{args.stale_days}d (no human msg): {len(stale)}"
          f"   [{len(stale) / max(len(with_bot), 1) * 100:.0f}% of them]")
    print(f"groups the bot is absent from: {len(without_bot)}\n")

    by_unit_absent: dict[str, list[dict]] = {}
    for d in without_bot:
        if k := key(d["unit_final"]):
            by_unit_absent.setdefault(k, []).append(d)

    # Only units that are on the fleet list AND have a stale group AND have a
    # candidate replacement are worth vetting.
    jobs = []
    seen_candidates: set[int] = set()
    for d in stale:
        k = key(d["unit_final"])
        if k and k in fleet and k in by_unit_absent:
            for cand in by_unit_absent[k]:
                # A unit can have several stale groups (e.g. a basic group and
                # its migration tombstone), which would otherwise queue the same
                # target twice.
                if cand["group_id"] in seen_candidates:
                    continue
                seen_candidates.add(cand["group_id"])
                jobs.append({"unit": d["unit_final"], "stale": d, "candidate": cand})

    print(f"units with a stale group AND a candidate replacement: {len(jobs)}")
    if args.limit:
        jobs = jobs[: args.limit]

    plan = {"deactivate": [], "add_bot": [], "rejected": [], "stale_no_candidate": []}
    for d in stale:
        k = key(d["unit_final"])
        if not (k and k in fleet and k in by_unit_absent):
            plan["stale_no_candidate"].append(
                {"group_id": d["group_id"], "unit": d["unit_final"], "title": d["title"],
                 "last_human": d.get("last_human")})

    if not jobs:
        PLAN.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
        print("\nNo candidates to vet. Plan written; nothing to add.")
        return

    async with TelegramClient(str(SESSION), int(api_id), api_hash) as client:
        hos = await client.get_entity(HOS_USERNAME)
        bot = await client.get_entity(BOT_ID)
        print(f"vetting against @{HOS_USERNAME} (id {hos.id}) and /grm|gurman/\n")

        for i, job in enumerate(jobs, 1):
            cand = job["candidate"]
            ent = await client.get_entity(cand["group_id"])
            while True:
                try:
                    passed, why = await vet(client, ent, hos.id)
                    break
                except FloodWaitError as e:
                    print(f"  flood wait {e.seconds}s"); await asyncio.sleep(e.seconds + 1)

            row = {
                "unit": job["unit"],
                "old_group_id": job["stale"]["group_id"],
                "old_title": job["stale"]["title"],
                "old_last_human": job["stale"].get("last_human"),
                "new_group_id": cand["group_id"],
                "new_title": cand["title"],
                "vet": why,
            }
            print(f"[{i:>3}/{len(jobs)}] {str(job['unit']):>8}  "
                  f"{'PASS' if passed else 'REJECT'}  {why}")
            print(f"          old: {job['stale']['title'][:52]}")
            print(f"          new: {cand['title'][:52]}")

            if not passed:
                plan["rejected"].append(row)
                continue

            if args.apply:
                outcome = await add_bot(client, ent, bot)
                if outcome.startswith("floodwait:"):
                    w = int(outcome.split(":")[1])
                    print(f"          flood wait {w}s"); await asyncio.sleep(w + 1)
                    outcome = await add_bot(client, ent, bot)
                row["outcome"] = outcome
                print(f"          -> {outcome}")
                await asyncio.sleep(args.delay)
            else:
                row["outcome"] = "dry-run"

            plan["add_bot"].append(row)
            plan["deactivate"].append(
                {"group_id": job["stale"]["group_id"], "unit": job["unit"],
                 "title": job["stale"]["title"], "reason": f"quiet >{args.stale_days}d, "
                 f"replaced by {cand['group_id']}"})

    PLAN.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nplan -> {PLAN}")
    print(f"  add_bot           : {len(plan['add_bot'])}")
    print(f"  rejected by vetting: {len(plan['rejected'])}")
    print(f"  deactivate         : {len(plan['deactivate'])}")
    print(f"  stale, no replacement found: {len(plan['stale_no_candidate'])}")
    if not args.apply:
        print("\nDRY RUN -- no bot was added. Re-run with --apply.")
    print("Deactivations still need: railway run --service Postgres py -3.11 "
          "scripts/apply_plan.py --apply")


if __name__ == "__main__":
    asyncio.run(main())
