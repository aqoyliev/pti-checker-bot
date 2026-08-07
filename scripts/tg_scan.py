"""READ-ONLY audit of fleet groups via a logged-in user session.

Lists every group your account is in, pulls the unit number out of the title,
and checks whether the PTI bot is a member. Writes tg_dialogs.json; the
cross-reference against the database happens in tg_report.py.

This script only reads. It never sends a message, joins, leaves, or changes
anything. Run scripts/tg_login.py once first.

    py -3.11 scripts/tg_scan.py [--out tg_dialogs.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

# Group titles are full of emoji; the Windows console is cp1252.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from telethon import TelegramClient, utils
from telethon.errors import ChatAdminRequiredError, FloodWaitError, UserNotParticipantError

SESSION = Path.home() / ".pti-tg" / "fleet_audit"
BOT_ID = 8698369700  # @<pti bot>, from getMe

# Titles look like "UNIT# 1216 / MARTINEZ, JOSE / ...", "0822 // FRANCOIS...",
# "1136 LORISTON, ALEX". Take the first standalone number-ish token.
_UNIT_RE = re.compile(r"(?:unit\s*#?\s*)?(\d{3,6})", re.IGNORECASE)


def unit_from_title(title: str) -> str | None:
    if not title:
        return None
    m = _UNIT_RE.search(title)
    return m.group(1) if m else None


def key_digits(u) -> str:
    """Loose match key: digits only, leading zeros dropped ('0822' == '822')."""
    if not u:
        return ""
    d = re.sub(r"\D", "", str(u))
    return d.lstrip("0") or d


async def bot_status(client: TelegramClient, entity) -> str:
    """'member' / 'admin' / 'absent' / 'unknown:<reason>' -- read-only."""
    try:
        perms = await client.get_permissions(entity, BOT_ID)
    except UserNotParticipantError:
        return "absent"
    except ChatAdminRequiredError:
        return "unknown:need-admin-to-list"
    except FloodWaitError:
        raise  # handled by the caller's retry loop
    except Exception as e:  # ValueError for unresolvable peers, etc.
        return f"unknown:{type(e).__name__}"
    # Reaching here at all means the bot IS a participant -- absence is signalled
    # by UserNotParticipantError above. Don't probe optional attributes that
    # differ between basic groups and channels.
    return "admin" if getattr(perms, "is_admin", False) else "member"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tg_dialogs.json")
    ap.add_argument("--units", help="fleet unit list; restricts the expensive "
                                    "per-group checks to relevant groups")
    ap.add_argument("--known-ids", help="JSON list of group_ids already in the DB; "
                                        "always checked regardless of title")
    args = ap.parse_args()

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH not set (use `railway run`).")
    if not SESSION.with_suffix(".session").exists():
        raise SystemExit(f"No session at {SESSION}.session -- run scripts/tg_login.py first.")

    out = []
    async with TelegramClient(str(SESSION), int(api_id), api_hash) as client:
        me = await client.get_me()
        print(f"scanning as {me.first_name} (id {me.id})\n")

        dialogs = [d async for d in client.iter_dialogs() if d.is_group]
        print(f"{len(dialogs)} groups visible")

        # This account is in far more groups than the fleet (other carriers,
        # alert channels, etc). Doing 2 extra API calls on all of them is slow
        # and risks flood limits, so narrow to groups that could plausibly be
        # ours: title parses to a fleet unit, or the DB already knows the id.
        fleet = set()
        if args.units:
            fleet = {key_digits(u) for u in Path(args.units).read_text().split()}
        known = set()
        if args.known_ids:
            known = set(json.loads(Path(args.known_ids).read_text()))

        if fleet or known:
            kept = [d for d in dialogs
                    if utils.get_peer_id(d.entity) in known
                    or key_digits(unit_from_title(d.title)) in fleet]
            print(f"{len(kept)} are fleet-relevant (unit on the list, or known to the DB);"
                  f" skipping {len(dialogs) - len(kept)} others")
            # Keep the rest in the output, but unchecked, so nothing is lost.
            skipped = [
                {"group_id": utils.get_peer_id(d.entity), "title": d.title,
                 "unit": unit_from_title(d.title), "bot": "not-checked",
                 "megagroup": bool(getattr(d.entity, "megagroup", False)),
                 "participants": getattr(d.entity, "participants_count", None),
                 "last_any": d.date.isoformat() if d.date else None,
                 "last_human": None}
                for d in dialogs if d not in kept
            ]
            out.extend(skipped)
            dialogs = kept

        print(f"checking bot membership + activity on {len(dialogs)}...\n")

        for i, d in enumerate(dialogs, 1):
            ent = d.entity
            gid = utils.get_peer_id(ent)  # bot-API style id (-100... for supergroups)

            # A basic group upgraded to a supergroup leaves behind a tombstone
            # Chat with deactivated=True and migrated_to pointing at the new
            # channel. The member list is ChatParticipantsForbidden there, so
            # follow the pointer and report on the real group instead.
            migrated_from = None
            if getattr(ent, "migrated_to", None) is not None:
                try:
                    new_ent = await client.get_entity(ent.migrated_to)
                    migrated_from, ent = gid, new_ent
                    gid = utils.get_peer_id(new_ent)
                except Exception as e:
                    print(f"    ! {gid} migrated but unresolvable: {type(e).__name__}")
            while True:
                try:
                    status = await bot_status(client, ent)
                    break
                except FloodWaitError as e:
                    print(f"  flood wait {e.seconds}s...")
                    await asyncio.sleep(e.seconds + 1)

            # Last activity. dialog.date is the last message of any kind --
            # including the bot's own PTI results and reminders, which would
            # make a dead group look alive. So also find the last message from
            # a real person.
            last_any = d.date.isoformat() if d.date else None
            last_human = None
            try:
                async for m in client.iter_messages(ent, limit=40):
                    sender = m.sender_id
                    if sender == BOT_ID or m.action is not None:
                        continue  # bot chatter / join-leave service messages
                    last_human = m.date.isoformat()
                    break
            except Exception as e:
                last_human = f"unknown:{type(e).__name__}"

            rec = {
                "group_id": gid,
                "title": d.title,
                "unit": unit_from_title(d.title),
                "bot": status,
                "megagroup": bool(getattr(ent, "megagroup", False)),
                "participants": getattr(ent, "participants_count", None),
                "last_any": last_any,
                "last_human": last_human,
                "migrated_from": migrated_from,
            }
            out.append(rec)
            print(f"[{i:>3}/{len(dialogs)}] {str(rec['unit']):>8}  {gid:>15}  "
                  f"bot={status:<10} last_human={str(last_human)[:10]:<12} {d.title[:36]}")
            if i % 25 == 0:  # checkpoint -- this pass is slow, don't lose it
                Path(args.out).write_text(
                    json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
            await asyncio.sleep(0.25)

    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False),
                              encoding="utf-8")
    print(f"\nwrote {len(out)} groups to {args.out}")
    absent = [r for r in out if r["bot"] == "absent"]
    print(f"bot absent from {len(absent)} of them")


if __name__ == "__main__":
    asyncio.run(main())
