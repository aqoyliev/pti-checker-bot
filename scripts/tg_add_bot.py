"""Add the PTI bot to fleet groups it is missing from, using a logged-in user
session. Requires scripts/tg_scan.py to have been run first.

DRY RUN BY DEFAULT -- prints what it would do and exits. Pass --apply to act.

    py -3.11 scripts/tg_add_bot.py --dialogs tg_dialogs.json --units fleet_units.txt
    py -3.11 scripts/tg_add_bot.py ... --apply --limit 5

Safety rules baked in:
  * only touches groups where the scan says the bot is absent AND the unit is
    on the supplied fleet list -- never a group outside that intersection;
  * --limit caps how many are touched in one run (start small);
  * --delay seconds between invites, and it honours Telegram's flood waits,
    because bulk-inviting from a user account is exactly what gets rate limited;
  * appends every outcome to add_bot_log.jsonl so a re-run skips what worked.

Adding the bot posts two visible setup messages into each group, so drivers
will see it happen. That is not reversible from here.
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
LOG = Path("add_bot_log.jsonl")


def key(u) -> str:
    if not u:
        return ""
    d = re.sub(r"\D", "", str(u))
    return d.lstrip("0") or d


async def invite(client: TelegramClient, entity, bot) -> str:
    """Add `bot` to `entity`. Returns an outcome string; never raises FloodWait."""
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


def already_done() -> set[int]:
    if not LOG.exists():
        return set()
    done = set()
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["outcome"] in ("added", "already-there"):
            done.add(r["group_id"])
    return done


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dialogs", default="tg_dialogs.json")
    ap.add_argument("--units", required=True)
    ap.add_argument("--apply", action="store_true", help="actually add (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="max groups this run (0 = no cap)")
    ap.add_argument("--delay", type=float, default=15.0, help="seconds between invites")
    args = ap.parse_args()

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH not set.")

    dialogs = json.loads(Path(args.dialogs).read_text(encoding="utf-8"))
    wanted = {key(u) for u in Path(args.units).read_text(encoding="utf-8").split()}
    done = already_done()

    targets = [
        d for d in dialogs
        if d["bot"] == "absent" and key(d["unit"]) in wanted and d["group_id"] not in done
    ]
    targets.sort(key=lambda d: key(d["unit"]))
    if args.limit:
        targets = targets[: args.limit]

    print(f"{len(dialogs)} groups scanned; {len(targets)} need the bot"
          f"{f' (capped at {args.limit})' if args.limit else ''}\n")
    for d in targets:
        print(f"  {str(d['unit']):>8}  {d['group_id']:>15}  {d['title'][:52]}")

    if not args.apply:
        print(f"\nDRY RUN -- nothing changed. Re-run with --apply to add the bot"
              f" ({args.delay:.0f}s apart, ~{len(targets) * args.delay / 60:.0f} min).")
        return
    if not targets:
        return

    async with TelegramClient(str(SESSION), int(api_id), api_hash) as client:
        bot = await client.get_entity(BOT_ID)
        print(f"\nadding @{bot.username} to {len(targets)} groups...\n")

        for i, d in enumerate(targets, 1):
            entity = await client.get_entity(d["group_id"])
            outcome = await invite(client, entity, bot)

            if outcome.startswith("floodwait:"):
                wait = int(outcome.split(":")[1])
                print(f"  flood wait {wait}s -- pausing")
                await asyncio.sleep(wait + 1)
                outcome = await invite(client, entity, bot)

            with LOG.open("a") as f:
                f.write(json.dumps({
                    "group_id": d["group_id"], "unit": d["unit"],
                    "title": d["title"], "outcome": outcome,
                }) + "\n")
            print(f"[{i:>3}/{len(targets)}] {str(d['unit']):>8}  {outcome}")

            if i < len(targets):
                await asyncio.sleep(args.delay)

    print(f"\ndone -- outcomes in {LOG}")
    print("Groups that were previously configured are now active again.")
    print("Brand-new groups still need /setunit and /adddriver run in each.")


if __name__ == "__main__":
    asyncio.run(main())
