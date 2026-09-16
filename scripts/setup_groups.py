"""Configure every unconfigured group the automatic path can decide, in one pass.

Standing up a fleet does not happen in the order onboarding assumes. The bot is
added to sixty groups on one afternoon and the drivers are added to them over
the days that follow, so at join time the phone numbers in each About text
resolved to accounts that were not in the chat yet -- which
`utils/auto_onboard` correctly refuses to configure from ("an account that is
not in this group can never post a PTI"). Every one of those groups fell
through to the admin picker, and the setup nag sends that picker exactly once
per group, so nothing ever asks again. The groups sit unconfigured with the
answer now sitting in their member lists.

This is that second ask, for every group at once. It makes no decision of its
own: it reads each group's roster and About text the way onboarding does and
hands both to the same `plan_auto_config`, so a group configured here is a
group the bot would have configured itself had the drivers been present when it
joined. Nothing is written for a group the decision declines -- those still
need an admin, and the report names them with the command to open each one.

    python scripts/setup_groups.py                     # preview, writes nothing
    python scripts/setup_groups.py --apply
    python scripts/setup_groups.py --group -1001234 --apply

Preview is the default because this writes unattended, to a live fleet, over
every group at once -- the same reason `/titlecheck` and `/fixnames` show their
work before they commit it.

Unlike `scripts/fleet_report.py`, this needs the bot's own credentials and says
so plainly: the roster read is the bot token over MTProto and the phone lookup
is the fleet's lookup account, so it runs where those live (`railway ssh`), not
on a dev box. Contact import is the most rate-limited call a user account has,
hence `--sleep` between groups and the abort below.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Run as a file (`python /app/scripts/setup_groups.py`), Python puts *this*
# directory on the path and not the repo root, so the bot's own packages are
# invisible. Same one-liner as scripts/tg_phone_lookup.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from handlers.admin.onboard import (  # noqa: E402
    LOOKUP_UNAVAILABLE,
    _apply_auto_config,
    _try_auto_config,
)
from loader import bot  # noqa: E402
from utils import db, phone_lookup, userbot  # noqa: E402
from utils.unit_parse import guess_unit  # noqa: E402

# Once the lookup account is contact-import limited, every remaining group gets
# the same non-answer, and marching through forty of them would fill the report
# with "declined" for groups that were never really asked. Stop and let the
# operator come back to it.
LOOKUP_FAILURE_LIMIT = 3


async def _setup_one(group: dict, apply: bool) -> tuple[str, str]:
    """(outcome, detail) for one group. Writes only when `apply`."""
    gid = group["group_id"]
    # The title is read fresh from Telegram rather than from `groups.title`:
    # the cache is only refreshed when someone talks in the group, so the
    # stalest titles belong to the quietest groups, and the unit is parsed out
    # of it. The About text comes from the userbot, as it does in onboarding --
    # the Bot API does not return one for a basic group.
    try:
        chat = await bot.get_chat(gid)
    except Exception as e:
        return "unreachable", f"{type(e).__name__}: {e}"
    title = chat.title or ""
    members = await userbot.list_members(gid)
    roster = [m for m in members if not m.is_bot]
    if not roster:
        return "no-roster", "no member list available (is the bot still in the chat?)"
    description = await userbot.get_description(gid)
    unit, _source = guess_unit(title, description)

    plan, reason = await _try_auto_config(gid, unit, description, roster)
    if plan is None:
        return "declined", reason or "phone lookup is not configured"
    who = ", ".join(f"{name} ({uid})" for uid, name in plan.drivers)
    if not apply:
        return "would-configure", f"unit {plan.unit} — {who}"
    await _apply_auto_config(gid, plan, roster)
    return "configured", f"unit {plan.unit} — {who}"


async def run(apply: bool, only: list[int], sleep: float, limit: int | None) -> int:
    await db.init_db()
    groups = await db.get_unconfigured_groups()
    if only:
        groups = [g for g in groups if g["group_id"] in set(only)]
    if limit:
        groups = groups[:limit]
    print(f"{len(groups)} unconfigured group(s); "
          f"{'writing' if apply else 'preview only, writing nothing'}\n")

    counts: dict[str, int] = {}
    needs_admin: list[int] = []
    lookup_failures = 0
    for i, g in enumerate(groups):
        gid = g["group_id"]
        outcome, detail = await _setup_one(g, apply)
        counts[outcome] = counts.get(outcome, 0) + 1
        print(f"{gid:>15}  {outcome:<15} {detail}")
        if outcome in ("declined", "no-roster", "unreachable"):
            needs_admin.append(gid)
        if outcome == "declined" and detail.startswith(LOOKUP_UNAVAILABLE):
            lookup_failures += 1
            if lookup_failures >= LOOKUP_FAILURE_LIMIT:
                print(f"\nStopping: the phone lookup has been unavailable "
                      f"{lookup_failures} times in a row. The remaining "
                      f"{len(groups) - i - 1} group(s) were not asked.")
                break
        else:
            lookup_failures = 0
        if sleep and i + 1 < len(groups):
            await asyncio.sleep(sleep)

    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if needs_admin:
        print("\nStill need a person — each one re-opens the picker:")
        for gid in needs_admin:
            print(f"  /onboard {gid}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true",
                   help="write the units and drivers (default: preview only)")
    p.add_argument("--group", type=int, action="append", default=[],
                   metavar="ID", help="only this group id (repeatable)")
    p.add_argument("--sleep", type=float, default=4.0, metavar="SECONDS",
                   help="pause between groups; contact import is rate-limited")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="stop after N groups — try a few before the whole fleet")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING)
    # Telethon narrates every connection at INFO, which buries the report.
    logging.getLogger("telethon").setLevel(logging.WARNING)

    async def _main():
        try:
            return await run(args.apply, args.group, args.sleep, args.limit)
        finally:
            # Both MTProto clients get a clean disconnect: the lookup account is
            # the fragile one, and a session dropped mid-flight is exactly what
            # it should not have to recover from.
            await userbot.close()
            await phone_lookup.close()
            session = await bot.get_session()
            await session.close()

    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
