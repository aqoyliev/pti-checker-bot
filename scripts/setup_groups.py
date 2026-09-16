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
    python scripts/setup_groups.py --suggest           # who to pick, for a person
    python scripts/setup_groups.py --pair=-100123:456 --apply    # one reviewed pick

The `=` in that last one is not optional: a group id starts with a minus sign,
and argparse reads a bare `-100123:456` as another option. (A plain `--group
-100123` is fine -- that one *is* a number, and argparse lets negative numbers
through when no option looks like one.)

`--suggest` is for what is left over. The commonest decline by far is one of
the two numbers matching no Telegram account -- the driver is in the chat, they
just cannot be found by phone (that is a privacy setting, and their own to
keep) -- so the pairing has to come from a person. This mode does the reading
for them: it pairs the names the fleet wrote against the member list and prints
only the pairs that are proven, by the same rule /fixnames uses. It writes
nothing, ever, and spends no phone lookup at all.

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
import re
import sys
from dataclasses import dataclass
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
from utils.auto_onboard import MAX_DRIVERS  # noqa: E402
# _clean_name formats a name the way the About text's own names are formatted,
# and _words is the tokenizer the pairing rule is defined in terms of -- so the
# evidence printed beside a suggestion is the words the decision was made on
# rather than a second opinion about them.
from utils.driver_names import (  # noqa: E402
    _clean_name,
    _words,
    match_names_to_drivers,
    parse_driver_names,
)
from utils.unit_parse import guess_unit  # noqa: E402

# Once the lookup account is contact-import limited, every remaining group gets
# the same non-answer, and marching through forty of them would fill the report
# with "declined" for groups that were never really asked. Stop and let the
# operator come back to it.
LOOKUP_FAILURE_LIMIT = 3

# How many shared words a name pairing needs before `--pair` will write it
# without a phone number behind it. One is how two men called Mohamed pair to
# each other; two is a first and a last name agreeing, which is the case the
# operator is confirming from the report.
STRONG_SHARED_WORDS = 2


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


# Words a title puts around the unit number rather than around a name.
_TITLE_LABELS = {"unit", "truck", "sub", "trailer", "lo", "jr", "no", "u"}


def _names_from_title(title: str, unit: str | None) -> list[str]:
    """Driver names as the chat title writes them: "<unit> - NAME / NAME".

    A fallback source for the suggestion report, tried only after the About
    text, which is the fleet's own record. Roughly half of these groups write
    the drivers into the title and nowhere a parser can see them otherwise --
    no label, no phone line to sit above. Nothing read this way decides
    anything: it is a name to show a person, and they confirm the pick.
    """
    text = title.replace(unit, " ") if unit else title
    out = []
    for part in re.split(r"[/;|]", text):
        words = [w.strip("()#.,-–—") for w in re.split(r"[\s,]+", part) if w]
        words = [w for w in words
                 if w and not any(ch.isdigit() for ch in w)
                 and w.lower() not in _TITLE_LABELS]
        # A part has to carry a real word to be a name at all; initials and
        # leftovers like "D" or "-" are not. `_clean_name` then formats it the
        # way the About text's own names are formatted, so a name read here and
        # a name read there are stored in one style.
        if any(len(w) >= 3 for w in words):
            name = _clean_name(" ".join(words))
            if name:
                out.append(name)
    return out[:MAX_DRIVERS]


@dataclass
class Pairing:
    """What one group's names and member list say about each other."""
    title: str
    unit: str | None
    roster: list
    placed: dict[int, str]          # user_id -> the fleet's name for them
    unplaced: list[str]
    where: str                      # which text the names were read from

    def shared(self, user_id: int) -> set[str]:
        """The words that proved this pair — the evidence, not a score."""
        member = next((m for m in self.roster if m.user_id == user_id), None)
        if member is None or user_id not in self.placed:
            return set()
        return _words(self.placed[user_id]) & _words(member.label)


async def _pair_group(gid: int) -> tuple[Pairing | None, str]:
    """(pairing, "") for one group, or (None, why there isn't one).

    The pairing is `match_names_to_drivers`, the same proven-only rule
    /fixnames uses -- a shared word of three letters or more that picks out
    exactly one person, with nobody claimed twice. Two members sharing a
    surname pair to neither, and a name that cannot be placed is left unplaced
    rather than resolved by guess.
    """
    try:
        chat = await bot.get_chat(gid)
    except Exception as e:
        return None, f"unreachable — {type(e).__name__}: {e}"
    title = chat.title or ""
    roster = [m for m in await userbot.list_members(gid) if not m.is_bot]
    if not roster:
        return None, "no member list (is the bot still in the chat?)"
    description = await userbot.get_description(gid)
    unit, _source = guess_unit(title, description)

    names = parse_driver_names(description)
    where = "About text"
    if not names:
        names = _names_from_title(title, unit)
        where = "title"
    if not names:
        return None, "no driver names in the About text or the title"

    people = [{"user_id": m.user_id, "name": m.label} for m in roster]
    placed, unplaced = match_names_to_drivers(names, people)
    return Pairing(title=title, unit=unit, roster=roster, placed=placed,
                   unplaced=unplaced, where=where), ""


async def _suggest_one(group: dict, hidden: set[int]) -> tuple[int, list[str]]:
    """(proven pairs, report lines) for one group. Writes nothing, asks nothing."""
    gid = group["group_id"]
    pairing, problem = await _pair_group(gid)
    if pairing is None:
        return 0, [f"{gid:>15}  {problem}"]

    roster, placed = pairing.roster, pairing.placed
    by_id = {m.user_id: m for m in roster}
    lines = [f"{gid:>15}  unit {pairing.unit or '?'}  ·  {len(roster)} members  ·  "
             f"{pairing.title[:44]}",
             f"      names read from the {pairing.where}"]
    for uid, name in placed.items():
        m = by_id[uid]
        shared = ", ".join(sorted(pairing.shared(uid)))
        # Say so when the person to pick is one the automatic sweep has since
        # filed as a non-driver: they are real, and the picker hides them
        # behind "Show N hidden" until someone asks for them.
        flag = "   [hidden as a non-driver — tap Show hidden]" if uid in hidden else ""
        lines.append(f"      ✓ {name}  →  {m.label} ({uid})   shared: {shared}{flag}")
    for name in pairing.unplaced:
        cands = [m for m in roster if _words(name) & _words(m.label)]
        if not cands:
            lines.append(f"      ? {name}  —  no member's name shares a word with it")
        else:
            who = "; ".join(f"{m.label} ({m.user_id})" for m in cands[:4])
            lines.append(f"      ? {name}  —  {len(cands)} possible: {who}")
    return len(placed), lines


async def suggest(only: list[int], sleep: float, limit: int | None) -> int:
    """Report who to pick for every group still unconfigured. Writes nothing."""
    await db.init_db()
    groups = await db.get_unconfigured_groups()
    if only:
        groups = [g for g in groups if g["group_id"] in set(only)]
    if limit:
        groups = groups[:limit]
    hidden = await db.get_non_driver_ids()
    print(f"{len(groups)} unconfigured group(s) — suggestions only, nothing is "
          f"written and no phone lookup is spent\n")

    proven = 0
    for i, g in enumerate(groups):
        pairs, lines = await _suggest_one(g, hidden)
        proven += pairs
        print("\n".join(lines))
        if sleep and i + 1 < len(groups):
            await asyncio.sleep(sleep)

    print(f"\n{proven} proven pair(s) across {len(groups)} group(s).")
    print("Confirm each in the web panel (the group's driver search takes the "
          "name above), or run /onboard <group_id> in the bot's DM and tap it.")
    return 0


async def _confirm_one(gid: int, uid: int, apply: bool) -> tuple[bool, str]:
    """Register one reviewed pair. (wrote?, what happened).

    A `--pair` is a person's answer to the suggestion report, so this writes
    what they approved and nothing more -- one driver, plus the unit the title
    names, and no non-driver sweep: confirming one pick is not a judgement on
    the rest of the roster (the panel's driver search takes the same view).

    Every fact is re-derived here rather than trusted from the command line,
    because the report the operator read is minutes old and the roster is
    live. The pair has to still be proven, and proven *strongly* -- one shared
    word can be a coincidence between two men called Mohamed, and this path
    has no phone number to corroborate it with.
    """
    pairing, problem = await _pair_group(gid)
    if pairing is None:
        return False, problem
    if uid not in pairing.placed:
        return False, f"{uid} is no longer the proven match for any name here"
    if not pairing.unit:
        # Writing a unit nothing corroborates is the one thing onboarding never
        # does unattended, and a wrong unit misfiles every later inspection.
        return False, "no unit could be read from the title"
    shared = pairing.shared(uid)
    if len(shared) < STRONG_SHARED_WORDS:
        return False, (f"only {len(shared)} shared word "
                       f"({', '.join(sorted(shared)) or 'none'}) — too weak to "
                       f"write without a person looking at it")

    name = pairing.placed[uid]
    existing = await db.get_drivers(gid)
    if any(d["user_id"] == uid for d in existing):
        return False, f"{name} is already registered here"
    if len(existing) >= MAX_DRIVERS:
        return False, f"already has {len(existing)} drivers"
    if not apply:
        return False, f"would register {name} ({uid}) on unit {pairing.unit}"

    await db.upsert_group(gid)
    await db.add_driver(gid, uid, name)
    # Last, because it is what flips setup_complete: a group is not set up
    # until somebody is registered in it.
    await db.set_group_unit(gid, pairing.unit)
    # Being chosen as a driver outranks a stale "not a driver" row, here as
    # everywhere else -- and after a fleet-wide setup that row is common.
    await db.unmark_non_drivers([uid])
    return True, f"registered {name} ({uid}) on unit {pairing.unit}"


async def confirm(pairs: list[str], apply: bool, sleep: float) -> int:
    """Write the `--pair GID:UID` entries an operator has reviewed."""
    await db.init_db()
    parsed: list[tuple[int, int]] = []
    for raw in pairs:
        try:
            gid, uid = (int(x) for x in raw.split(":", 1))
        except ValueError:
            print(f"skipping {raw!r}: expected GROUP_ID:USER_ID")
            continue
        parsed.append((gid, uid))
    print(f"{len(parsed)} reviewed pair(s); "
          f"{'writing' if apply else 'dry run, writing nothing'}\n")

    wrote = 0
    for i, (gid, uid) in enumerate(parsed):
        ok, detail = await _confirm_one(gid, uid, apply)
        wrote += ok
        print(f"{gid:>15}  {'ok ' if ok else '-- '} {detail}")
        if sleep and i + 1 < len(parsed):
            await asyncio.sleep(sleep)
    print(f"\n{wrote} of {len(parsed)} written.")
    return 0


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
    p.add_argument("--suggest", action="store_true",
                   help="report who to pick for the groups that need a person; "
                        "writes nothing and spends no phone lookup")
    p.add_argument("--pair", action="append", default=[], metavar="GID:UID",
                   help="register a pair from that report, as --pair=-100:456 "
                        "(repeatable; the = is required, a group id starts with "
                        "a minus). Needs --apply; re-checks before writing")
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

    if args.suggest and args.apply:
        p.error("--suggest reports; it never writes. Drop --apply.")
    if args.suggest and args.pair:
        p.error("--suggest reports and --pair writes; run them separately.")

    async def _main():
        try:
            if args.suggest:
                return await suggest(args.group, args.sleep, args.limit)
            if args.pair:
                return await confirm(args.pair, args.apply, args.sleep)
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
