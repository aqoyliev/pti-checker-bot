"""The fleet's active unit numbers, supplied by an admin once a week.

Onboarding guesses a unit from a group's title or description, but a title
outlives the truck: groups get renamed late, or not at all, so a guess can name
a unit that left the fleet months ago. Checking the guess against the current
list turns that into an honest "not found" instead of a confidently wrong unit
silently attached to a group.

The list is replaced wholesale rather than merged -- the weekly list *is* the
fleet, so a truck that drops off it has to disappear here too.

  /units                    show the current list's size and age
  /units 1332 1303 728453   replace the list (space, comma or newline separated)

An empty table disables the check rather than rejecting every unit, so the
feature is inert until the first list arrives.

A truck leaving the list also retires its group: any active group whose
``unit_number`` is missing from the new list is deactivated. That is a big,
fleet-wide write driven by one pasted message, and a truncated or mistyped paste
once flipped ``is_active`` FALSE across the fleet -- so the sweep is *previewed*
and only runs once the admin confirms. Nothing is written until then, not even
the list itself.

Between two lists, the titles are swept on their own three times a week
(Mon/Wed/Fri, ``run_title_sweep``). A title naming a different unit re-files the
group if that unit is on the stored list and retires it if it isn't; a title
that has stopped naming a unit retires it; a title still naming the stored unit
is silent, even if that unit has left the list -- that last one belongs to the
confirmed sweep below. It runs unattended, so it reads the titles fresh from
Telegram and skips every group it cannot read: "couldn't fetch" must never be
mistaken for "the truck is gone".

Two deliberate asymmetries:

- **Deactivate only.** A unit reappearing on a later list does not reactivate
  its group; that stays a manual panel decision, so the weekly paste can never
  undo a deactivation someone made for an unrelated reason.
- **A group with no unit is left alone.** The list is about trucks, and a group
  still waiting for onboarding has no unit to match -- reading that as "not in
  the list" would retire every group before it was ever configured.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from html import escape
from time import monotonic

from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.config import ADMINS
from loader import bot, dp
from utils.db import (
    active_units_updated_at,
    apply_title_sweep,
    apply_units_sweep,
    get_active_units,
    get_all_groups,
    get_group_message_counts,
    get_setting,
    group_activity_since,
    normalize_unit,
    prune_group_message_days,
    set_group_title,
    set_setting,
)
from utils.group_activity import GROUP_QUIET_DAYS, has_full_window, quiet_groups
from utils.unit_parse import looks_retired, parse_unit

_ADMIN_IDS = [int(a) for a in ADMINS if str(a).strip().isdigit()]

# A pending /units confirmation, per admin: {"units": [...], "casualties": [...]}.
# In memory on purpose — a prompt that doesn't survive a restart is the safe
# failure mode for a write this wide, and the admin just re-pastes the list.
_pending: dict[int, dict] = {}
# How long a preview stays answerable before it's treated as stale.
_PENDING_TTL = 600  # seconds
# Cap the previewed groups so a fleet-wide sweep can't exceed Telegram's message limit.
_PREVIEW_LIMIT = 30

# Kept apart from the list's own updated_at: asking must not look like the list
# was refreshed, or /units would report an age that is really the age of a nag.
_LAST_ASKED_KEY = "active_units_last_asked_at"

# How stale the list may get before the bot asks for a fresh one.
REFRESH_AFTER = timedelta(days=7)
# How often the loop wakes up to check that age.
CHECK_INTERVAL = timedelta(hours=6)

_ASK_TEXT = (
    "📋 <b>Weekly check — active units</b>\n\n"
    "Send me this week's active unit numbers so I can tell a live truck from a "
    "retired one when a new group shows up.\n\n"
    "Reply with:\n<code>/units 1332 1303 728453 ...</code>"
)


def parse_units(raw: str) -> list[str]:
    """Unit numbers out of a pasted blob: space, comma, newline or tab separated.

    Keeps leading zeros ("001", "0822" are real units), so these stay strings.
    Anything that isn't a run of digits is dropped rather than guessed at.
    """
    return [tok for tok in re.split(r"[^0-9]+", raw or "") if tok]


def groups_to_deactivate(groups: list[dict], units: list[str]) -> list[dict]:
    """Active groups whose unit is missing from the new weekly list.

    Pure, so the sweep's blast radius can be asserted in tests without a DB.
    Units are compared through ``normalize_unit`` because group units were typed
    or imported ("<1304 >") while the list is parsed to bare digit runs.

    Skipped on purpose: groups already inactive (nothing to do) and groups with
    no unit at all -- an un-onboarded group isn't a retired truck.
    """
    wanted = {normalize_unit(u) for u in units}
    out: list[dict] = []
    for g in groups:
        if not g.get("is_active", True):
            continue
        unit = normalize_unit(g.get("unit_number"))
        if not unit:
            continue
        if unit not in wanted:
            out.append(g)
    return out


def title_unit_changes(groups: list[dict], units: list[str]) -> list[dict]:
    """Active groups whose title now names a different unit than the one on file.

    Returns ``[{"group": g, "old": str, "new": str}]``, oldest unit first.

    The fleet renames a group when its truck changes, so the title is the
    earliest signal a swap happened -- and a group still filed under the old
    number would be retired by the sweep below for a unit that simply moved.
    Refreshing first is what keeps a live truck from being deactivated.

    A parsed title is only a *suggestion* (a bare digit-run regex measured 79.5%
    across the fleet, and six titles produced a different valid unit rather than
    nothing), so three things must all hold before one is offered, and even then
    an admin confirms it:

    * the group is already configured -- an un-onboarded group belongs to
      onboarding, which asks a human about the very same guess;
    * the title does not read as retired ("INACTIVE", "moved") -- those are
      about to be deactivated anyway, and their titles are the least reliable;
    * the parsed number is on the incoming weekly list. This is the real
      corroboration: it means the title names a truck the fleet says is running,
      not a phone number, a year or a typo.

    Collisions are dropped rather than guessed at: if two groups' titles claim
    the same unit, or the claimed unit is already another active group's, none
    of them move. Two groups filed under one unit is the failure this avoids.
    """
    wanted = {normalize_unit(u) for u in units}
    active = [g for g in groups if g.get("is_active", True)]
    taken = {normalize_unit(g.get("unit_number")) for g in active}

    candidates: list[dict] = []
    for g in active:
        stored = normalize_unit(g.get("unit_number"))
        if not stored:
            continue
        title = g.get("title")
        if not title or looks_retired(title):
            continue
        parsed = normalize_unit(parse_unit(title) or "")
        if not parsed or parsed == stored or parsed not in wanted:
            continue
        candidates.append({"group": g, "old": stored, "new": parsed})

    claims: dict[str, int] = {}
    for c in candidates:
        claims[c["new"]] = claims.get(c["new"], 0) + 1

    out = [c for c in candidates
           if claims[c["new"]] == 1 and c["new"] not in taken]
    out.sort(key=lambda c: c["old"])
    return out


def title_deactivations(groups: list[dict], units: list[str]) -> list[dict]:
    """Active, configured groups whose title says the truck is gone.

    Returns ``[{"group": g, "reason": str}]`` -- the reason is carried because
    the admin's report has to say *which* of the two rules retired a group.

    Pure. Run on post-rename rows, so anything re-filed above already agrees
    with its title and falls out at the ``parsed == stored`` check. Two rules:

    * **The title no longer names a unit** -- the fleet either marks the group
      dead ("INACTIVE - ANTON, ROGEL", "this group has been moved") or drops the
      number off it. ~20% of fleet titles carry no parseable number, so this is a
      much wider net than the weekly list's.
    * **The title names a unit that isn't on the active list** -- the group is
      filed under one truck, its title claims another, and the fleet says that
      other truck isn't running. Note the cost of a mis-parse lands here: six
      known titles ("1275 - NEGRON..." on a group really filed as 2003) parse to
      a *different* number, and if that number is off the list the live group is
      retired. Reversed from the panel, one group at a time.

    Skipped on purpose:

    * **No stored unit.** An un-onboarded group has no truck to retire, and its
      title not parsing is precisely the case onboarding exists to ask a human
      about -- deactivating it would end that conversation before it started.
    * **No title.** The sweep refreshes titles from Telegram first and drops the
      ones it could not fetch, so a missing title here means *no evidence*, not
      an unnamed truck. Acting on a blank would retire a group for being
      unreachable, which is how the fleet was mass-deactivated once before.
    * **An empty units list.** Same rule as everywhere else: no list means the
      check is disabled, not that every unit fails it. Without this the very
      first sweep on a fresh database would retire the entire fleet.
    * **A title still naming the stored unit.** Even if that unit has left the
      list -- title and database agree, so there is nothing here to detect, and
      retiring a truck for falling off the list belongs to /units, where a human
      confirms it.
    """
    wanted = {normalize_unit(u) for u in units}
    out: list[dict] = []
    for g in groups:
        if not g.get("is_active", True):
            continue
        stored = normalize_unit(g.get("unit_number"))
        if not stored:
            continue
        title = g.get("title")
        if not title:
            continue
        parsed = normalize_unit(parse_unit(title) or "")
        if looks_retired(title) or not parsed:
            out.append({"group": g, "reason": "title no longer names a unit"})
            continue
        if not wanted or parsed == stored:
            continue
        if parsed not in wanted:
            out.append({"group": g,
                        "reason": f"title names {parsed}, not on the active list"})
    return out


def apply_renames(groups: list[dict], renames: list[dict]) -> list[dict]:
    """Group rows as they will be *after* the renames land.

    The deactivation sweep has to run on post-rename units, or a truck whose
    number changed this week gets retired under the number it no longer has.
    """
    moved = {r["group"]["group_id"]: r["new"] for r in renames}
    return [{**g, "unit_number": moved.get(g["group_id"], g.get("unit_number"))}
            for g in groups]


def _group_line(g: dict) -> str:
    unit = escape(normalize_unit(g.get("unit_number")) or "—")
    title = escape(g.get("title") or str(g["group_id"]))
    return f"• <b>{unit}</b> — {title}"


def _rename_line(r: dict) -> str:
    title = escape(r["group"].get("title") or str(r["group"]["group_id"]))
    return f"• <b>{escape(r['old'])} → {escape(r['new'])}</b> — {title}"


def _confirm_text(units: list[str], casualties: list[dict],
                  renames: list[dict] | None = None) -> str:
    renames = renames or []
    lines = [f"📋 <b>{len(units)} active units</b> ready to store.", ""]

    if renames:
        shown = renames[:_PREVIEW_LIMIT]
        lines += [
            f"🔄 <b>{len(renames)} group(s) renamed their unit</b> — the title now "
            f"names a truck on this list, so they'd be re-filed first:",
            "",
            *[_rename_line(r) for r in shown],
        ]
        if len(renames) > len(shown):
            lines.append(f"…and {len(renames) - len(shown)} more.")
        lines.append("")

    if casualties:
        shown = casualties[:_PREVIEW_LIMIT]
        lines += [
            f"⚠️ <b>{len(casualties)} group(s) would be deactivated</b> — their unit "
            f"is not on this list:",
            "",
            *[_group_line(g) for g in shown],
        ]
        if len(casualties) > len(shown):
            lines.append(f"…and {len(casualties) - len(shown)} more.")
        lines.append("")

    lines.append("Nothing has been saved yet. Confirm to store the list"
                 + (", re-file the renamed groups" if renames else "")
                 + (" and deactivate these groups." if casualties else "."))
    return "\n".join(lines)


def _confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Confirm", callback_data="un:ok"),
        InlineKeyboardButton("✖️ Cancel", callback_data="un:no"),
    )
    return kb


def _describe(count: int, updated: datetime | None) -> str:
    if not count:
        return ("No active-units list yet — every unit guess is accepted as-is.\n"
                "Send <code>/units 1332 1303 ...</code> to set one.")
    if updated is None:
        return f"{count} active units stored (age unknown)."
    days = (datetime.utcnow() - updated).days
    when = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"
    return f"{count} active units stored, last updated {when}."


@dp.message_handler(commands=["units"], chat_type=types.ChatType.PRIVATE)
async def cmd_units(message: types.Message):
    if message.from_user.id not in _ADMIN_IDS:
        return

    raw = message.get_args() or ""
    units = parse_units(raw)

    if not units:
        current = await get_active_units()
        await message.answer(_describe(len(current), await active_units_updated_at()),
                             parse_mode="HTML")
        return

    groups = await get_all_groups()
    # Order matters: re-file first, then decide what to retire. A truck whose
    # number changed this week is still running, and judging it on the number it
    # no longer has would deactivate a live group.
    renames = title_unit_changes(groups, units)
    casualties = groups_to_deactivate(apply_renames(groups, renames), units)

    if casualties or renames:
        # Nothing is written yet — not the list, not the renames, not the
        # deactivations. They all land together in _apply(), or none do.
        _pending[message.from_user.id] = {
            "units": units,
            "casualties": casualties,
            "renames": renames,
            "at": monotonic(),
        }
        await message.answer(_confirm_text(units, casualties, renames),
                             parse_mode="HTML", reply_markup=_confirm_kb())
        return

    await message.answer(await _apply(units, [], []), parse_mode="HTML")


async def _apply(units: list[str], casualties: list[dict],
                 renames: list[dict] | None = None) -> str:
    """Re-file renamed groups, store the list, retire what fell off it.

    All three writes happen in one transaction (``apply_units_sweep``), so the
    stored state can never disagree with what the admin is told happened.
    """
    renames = renames or []
    group_ids = [g["group_id"] for g in casualties]
    pairs = [(r["group"]["group_id"], r["new"]) for r in renames]
    added, removed, deactivated, refiled = await apply_units_sweep(
        units, group_ids, pairs)

    lines = [f"✅ <b>{len(units)} active units stored.</b>"]
    if added:
        lines.append(f"+{len(added)} new: {', '.join(sorted(added)[:20])}"
                     + (" …" if len(added) > 20 else ""))
    if removed:
        lines.append(f"−{len(removed)} removed: {', '.join(sorted(removed)[:20])}"
                     + (" …" if len(removed) > 20 else ""))
    if not added and not removed:
        lines.append("No change from the previous list.")

    if pairs:
        lines.append(f"\n🔄 <b>{refiled} group(s) re-filed</b> from their title:")
        lines += [f"  {escape(r['old'])} → {escape(r['new'])}" for r in renames[:20]]
        logging.info("units sweep re-filed %s groups from titles: %s", refiled, pairs)

    if group_ids:
        lines.append(f"\n💤 <b>{deactivated} group(s) deactivated</b> — unit no "
                     f"longer on the list.")
        logging.info("units sweep deactivated %s groups: %s", deactivated, group_ids)
    return "\n".join(lines)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("un:"))
async def units_confirm_callback(query: types.CallbackQuery):
    uid = query.from_user.id
    if uid not in _ADMIN_IDS:
        await query.answer("Not allowed.", show_alert=True)
        return

    state = _pending.pop(uid, None)
    if state is None or monotonic() - state["at"] > _PENDING_TTL:
        # Re-pasting the list is cheap; acting on a stale preview is not — the
        # fleet may have changed since it was drawn.
        await query.message.edit_text("That confirmation expired — send /units again.")
        await query.answer()
        return

    if query.data == "un:no":
        await query.message.edit_text("✖️ Cancelled — nothing was saved.")
        await query.answer()
        return

    # A failure here must be visible. Letting it escape to the global error
    # handler leaves the preview on screen still reading "nothing has been saved
    # yet", with no way to tell whether the sweep ran.
    try:
        text = await _apply(state["units"], state["casualties"],
                            state.get("renames", []))
    except Exception:
        logging.exception("units sweep failed for admin %s", uid)
        await query.message.edit_text(
            "⚠️ <b>Something went wrong — nothing was saved.</b>\n"
            "The list and the group changes are applied together, so neither "
            "took effect. Send /units again to retry.",
            parse_mode="HTML",
        )
        await query.answer()
        return

    await query.message.edit_text(text, parse_mode="HTML")
    await query.answer()


async def _quiet_report() -> str:
    """The quiet-groups list, as HTML. Safe to call even with no traffic data."""
    # Counting only runs forward, so for the first few days after this ships
    # every group has zero messages and would be reported as dead. That list is
    # printed directly above "paste this week's active units", i.e. next to the
    # decision that retires trucks — so until a full window has been observed,
    # say so instead of raising a fleet-wide false alarm.
    since = await group_activity_since()
    if not has_full_window(since, datetime.utcnow().date()):
        collected = "no traffic recorded yet" if since is None else f"collecting since {since}"
        return (f"🕓 <b>Quiet-group report not ready</b> — it needs "
                f"{GROUP_QUIET_DAYS} days of message history ({collected}).")

    groups = await get_all_groups()
    counts = await get_group_message_counts(GROUP_QUIET_DAYS)
    quiet = quiet_groups(groups, counts)
    if not quiet:
        return (f"🟢 <b>No quiet groups</b> — every active group has been used in "
                f"the last {GROUP_QUIET_DAYS} days.")

    lines = [f"🌙 <b>{len(quiet)} quiet group(s)</b> — little or no human traffic "
             f"in the last {GROUP_QUIET_DAYS} days:", ""]
    lines += [_group_line(g) for g in quiet[:_PREVIEW_LIMIT]]
    if len(quiet) > _PREVIEW_LIMIT:
        lines.append(f"…and {len(quiet) - _PREVIEW_LIMIT} more.")
    return "\n".join(lines)


@dp.message_handler(commands=["quiet"], chat_type=types.ChatType.PRIVATE)
async def cmd_quiet(message: types.Message):
    if message.from_user.id not in _ADMIN_IDS:
        return
    await message.answer(await _quiet_report(), parse_mode="HTML")


# ---------- the automatic title sweep ----------
#
# The same two decisions the weekly /units paste makes, run on a timer against
# the group titles alone: a title naming a different listed unit re-files the
# group, a title that has stopped naming a unit retires it, and a title that
# still says what it said is silent. Unlike /units this writes without asking,
# so it leans on the guards in the two pure functions above -- above all the
# rule that a re-file needs the new number to be on the stored active list.

# Which days it runs, UTC. Three times a week, spread evenly.
TITLE_SWEEP_WEEKDAYS = frozenset({0, 2, 4})  # Mon, Wed, Fri
_TITLE_SWEEP_KEY = "title_sweep_last_run_on"
# Pause between get_chat calls; ~150 groups, so this costs half a minute and
# keeps the sweep well clear of Telegram's rate limits.
_TITLE_FETCH_DELAY = 0.2


def title_sweep_due(now: datetime, last_run: date | None) -> bool:
    """True on a sweep day that hasn't been swept yet.

    Keyed on the date rather than an elapsed interval so the schedule can't
    drift: a restart, a slow tick or a run that took an hour still leaves the
    next sweep on the next sweep day, and never twice in one day.
    """
    if now.weekday() not in TITLE_SWEEP_WEEKDAYS:
        return False
    return last_run != now.date()


async def _last_swept_on() -> date | None:
    raw = await get_setting(_TITLE_SWEEP_KEY)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def _refresh_titles(groups: list[dict]) -> tuple[list[dict], int]:
    """Current titles from Telegram, plus the number of groups skipped.

    The cached ``groups.title`` is written opportunistically from the message
    middleware, so the groups whose titles are stalest are the quiet ones -- and
    quiet is exactly the state a retired truck is in. Sweeping the cache would
    therefore judge a rename by a title from whenever the group last spoke.

    A group that can't be fetched is **dropped, not defaulted**: a chat the bot
    was removed from, or a Bot API server that is briefly unhappy, must not read
    as "title has no unit any more" and retire a live truck.
    """
    fresh: list[dict] = []
    skipped = 0
    for g in groups:
        try:
            chat = await bot.get_chat(g["group_id"])
        except Exception as exc:
            logging.warning("title sweep could not read chat %s: %s", g["group_id"], exc)
            skipped += 1
            continue
        title = getattr(chat, "title", None)
        if not title:
            skipped += 1
            continue
        if title != g.get("title"):
            await set_group_title(g["group_id"], title)
        fresh.append({**g, "title": title})
        await asyncio.sleep(_TITLE_FETCH_DELAY)
    return fresh, skipped


def _casualty_line(c: dict) -> str:
    return f"{_group_line(c['group'])} — <i>{escape(c['reason'])}</i>"


def _sweep_report(renames: list[dict], casualties: list[dict],
                  refiled: int, deactivated: int, skipped: int) -> str:
    lines = ["🔎 <b>Title check</b> — group titles changed since the last sweep."]

    if renames:
        lines += ["", f"🔄 <b>{refiled} group(s) re-filed</b> — the title now names "
                      f"another unit from the active list:", ""]
        lines += [_rename_line(r) for r in renames[:_PREVIEW_LIMIT]]
        if len(renames) > _PREVIEW_LIMIT:
            lines.append(f"…and {len(renames) - _PREVIEW_LIMIT} more.")

    if casualties:
        lines += ["", f"💤 <b>{deactivated} group(s) deactivated</b> — the title "
                      f"says the truck is gone:", ""]
        lines += [_casualty_line(c) for c in casualties[:_PREVIEW_LIMIT]]
        if len(casualties) > _PREVIEW_LIMIT:
            lines.append(f"…and {len(casualties) - _PREVIEW_LIMIT} more.")
        lines += ["", "Reactivate any of these from the web panel if the truck is "
                      "still running."]

    if skipped:
        lines += ["", f"<i>{skipped} group(s) couldn't be read and were left "
                      f"untouched.</i>"]
    return "\n".join(lines)


async def run_title_sweep() -> str | None:
    """Re-file and retire active groups from their current titles.

    Returns the report that was sent, or ``None`` when nothing changed -- a
    sweep that finds every title saying what it said before is silent, which is
    most of them.
    """
    groups = [g for g in await get_all_groups() if g.get("is_active", True)]
    fresh, skipped = await _refresh_titles(groups)

    units = list(await get_active_units())
    # Same order as the weekly sweep: re-file first, then judge what is left. A
    # truck that moved to a listed unit is not a truck that lost its number --
    # judging it before the re-file would retire it for naming the new one.
    renames = title_unit_changes(fresh, units)
    casualties = title_deactivations(apply_renames(fresh, renames), units)

    if not renames and not casualties:
        logging.info("title sweep: no changes (%s groups read, %s skipped)",
                     len(fresh), skipped)
        return None

    pairs = [(r["group"]["group_id"], r["new"]) for r in renames]
    dead = [c["group"]["group_id"] for c in casualties]
    refiled, deactivated = await apply_title_sweep(pairs, dead)
    logging.info("title sweep re-filed %s (%s) and deactivated %s (%s)",
                 refiled, pairs, deactivated,
                 [(c["group"]["group_id"], c["reason"]) for c in casualties])

    report = _sweep_report(renames, casualties, refiled, deactivated, skipped)
    for admin_id in _ADMIN_IDS:
        try:
            await bot.send_message(admin_id, report, parse_mode="HTML")
        except Exception:
            logging.exception("could not send the title sweep report to %s", admin_id)
    return report


async def run_title_sweep_if_due(now: datetime | None = None) -> bool:
    """Run the sweep when the day calls for it. Returns True if it ran.

    The day is marked *before* the sweep runs, on purpose: a crash halfway
    through has already written some of its changes, and a retry on the next
    tick would re-read every title and re-decide against a fleet it has half
    modified. Missing a sweep costs two days; repeating one costs writes.
    """
    now = now or datetime.utcnow()
    if not title_sweep_due(now, await _last_swept_on()):
        return False
    await set_setting(_TITLE_SWEEP_KEY, now.date().isoformat())
    await run_title_sweep()
    return True


async def _last_asked_at() -> datetime | None:
    raw = await get_setting(_LAST_ASKED_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def ask_for_units_if_stale(now: datetime | None = None) -> bool:
    """DM the admins for a fresh list when the current one is a week old.

    Returns True if the ask went out. Two clocks, deliberately: the list's own
    age decides *whether* a refresh is due, and the last-asked time keeps an
    unanswered ask from repeating every cycle for a week.
    """
    now = now or datetime.utcnow()

    updated = await active_units_updated_at()
    if updated is not None and now - updated < REFRESH_AFTER:
        return False

    asked = await _last_asked_at()
    if asked is not None and now - asked < REFRESH_AFTER:
        return False

    # The quiet list rides along with the ask: which trucks went silent is
    # exactly the question being answered when deciding what goes on the new
    # list. A failure to build it must not cost the ask itself.
    try:
        body = f"{_ASK_TEXT}\n\n{await _quiet_report()}"
    except Exception:
        logging.exception("could not build the quiet-groups report")
        body = _ASK_TEXT

    sent = False
    for admin_id in _ADMIN_IDS:
        try:
            await bot.send_message(admin_id, body, parse_mode="HTML")
            sent = True
        except Exception:
            logging.exception("could not ask admin %s for the units list", admin_id)

    if sent:
        await set_setting(_LAST_ASKED_KEY, now.isoformat())
    return sent


async def units_refresh_loop():
    """Background loop: nudge the admins when the units list goes stale, sweep
    the group titles three times a week, and keep the message-count buckets from
    growing without bound.

    One loop rather than three tasks — the tick is six hours and every step is
    guarded, so a step that throws costs only itself.
    """
    while True:
        try:
            await ask_for_units_if_stale()
        except Exception:
            logging.exception("units refresh loop failed")
        try:
            await run_title_sweep_if_due()
        except Exception:
            logging.exception("title sweep failed")
        try:
            await prune_group_message_days()
        except Exception:
            logging.exception("pruning group message buckets failed")
        await asyncio.sleep(CHECK_INTERVAL.total_seconds())
