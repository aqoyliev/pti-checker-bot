"""Keeping each group filed under the unit its own title names.

Onboarding guesses a unit from a group's title or description, but a title
outlives the truck: groups get renamed late, or not at all. So the titles are
swept once a day (``run_title_sweep``). A title naming a different unit re-files
the group under it; a title that has stopped naming a unit retires it; a title
still naming the stored unit is silent. The sweep runs unattended, so it reads
the titles fresh from Telegram and skips every group it cannot read: "couldn't
fetch" must never be mistaken for "the truck is gone".

  /titlecheck   preview the groups whose title says the truck is gone
  /retitle      preview the groups whose title now names a different unit
  /quiet        the groups that have gone quiet (display only)

Both previews are human-confirmed and per-group toggle, so a title change
doesn't have to wait for the next sweep day.

A weekly active-units list used to live here too: an admin pasted the fleet's
live unit numbers, onboarding refused any unit missing from that list, and every
group filed under a unit that fell off it was retired. **Removed on 2026-08-31 --
the list was never trustworthy enough to carry that weight.** It had already lost
the unattended half of the job on 2026-08-17 for the reasons measured in
``title_deactivations`` below, and what remained still let one truncated paste
retire running trucks. A unit is now decided from the group's own title and the
driver's own video, never from a list of what the fleet is supposed to have.

Two deliberate asymmetries outlived it:

- **Deactivate only.** Nothing here reactivates a group; that stays a manual
  panel decision, so a sweep can never undo a deactivation someone made for an
  unrelated reason.
- **A group with no unit is left alone.** A group still waiting for onboarding
  has no unit to match, and reading that as "gone" would retire every group
  before it was ever configured.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from html import escape
from time import monotonic

from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.config import ADMINS
from loader import bot, dp
from utils.db import (
    apply_title_sweep,
    deactivate_group_ids,
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
from utils.unit_parse import looks_retired, parse_unit, title_names_unit

_ADMIN_IDS = [int(a) for a in ADMINS if str(a).strip().isdigit()]

# A pending /titlecheck confirmation, per admin:
# {"candidates": [...], "selected": {group_id, ...}, "at": ...}. "selected" is
# mutated in place by toggle taps; only tc:ok/tc:no ever pop the entry.
# In memory on purpose — a prompt that doesn't survive a restart is the safe
# failure mode for a write this wide, and the admin just re-runs the command.
_pending_titlecheck: dict[int, dict] = {}
# Same shape, for /retitle. A separate dict so /titlecheck and /retitle can be
# run back to back by the same admin without one's confirmation clobbering
# the other's.
_pending_retitle: dict[int, dict] = {}
# How long a preview stays answerable before it's treated as stale.
_PENDING_TTL = 600  # seconds
# Cap the previewed groups so a fleet-wide sweep can't exceed Telegram's message limit.
_PREVIEW_LIMIT = 30

# How often the loop wakes up.
CHECK_INTERVAL = timedelta(hours=6)

def title_unit_changes(groups: list[dict]) -> list[dict]:
    """Active groups whose title now names a different unit than the one on file.

    Returns ``[{"group": g, "old": str, "new": str}]``, oldest unit first.

    The fleet renames a group when its truck changes, so the title is the
    earliest signal a swap happened -- and a group still filed under the old
    number would be retired by the sweep below for a unit that simply moved.
    Refreshing first is what keeps a live truck from being deactivated.

    **The title is taken at its word.** A rename used to additionally require
    the parsed number to be on the stored active-units list. That requirement
    was dropped on 2026-08-26 at the fleet's instruction -- the rule is that a
    title naming a new unit *is* the truck changing. The list was pasted in by
    hand and went stale between pastes (JRD's was twelve days old at the time),
    so a truck that had just arrived was never on it and its rename was skipped
    in silence, which is the failure the fleet actually hits. The cost, stated
    plainly: a misparsed or mistyped title now re-files a group unattended. The
    sweep reports every rename it made and the panel reverses one.

    Three things still hold, because they are integrity rather than
    corroboration:

    * the group is already configured -- an un-onboarded group belongs to
      onboarding, which asks a human about the very same guess;
    * the title does not read as retired ("INACTIVE", "moved") -- those are
      about to be deactivated anyway, and their titles are the least reliable;
    * no collision. If two groups' titles claim the same unit, or the claimed
      unit is already another active group's, none of them move. Two groups
      filed under one unit is not a worse guess, it is a broken denominator --
      the hourly compliance pass reads both.
    """
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
        if not parsed or parsed == stored:
            continue
        candidates.append({"group": g, "old": stored, "new": parsed})

    claims: dict[str, int] = {}
    for c in candidates:
        claims[c["new"]] = claims.get(c["new"], 0) + 1

    out = [c for c in candidates
           if claims[c["new"]] == 1 and c["new"] not in taken]
    out.sort(key=lambda c: c["old"])
    return out


def title_deactivations(groups: list[dict]) -> list[dict]:
    """Active, configured groups whose title says the truck is gone.

    Returns ``[{"group": g, "reason": str}]`` -- the reason is carried because
    the admin's report has to say why a group was retired.

    Pure, and it reads the **title alone**. One rule: the title no longer names
    a unit, because the fleet either marks the group dead ("INACTIVE - ANTON,
    ROGEL", "this group has been moved") or drops the number off it. ~20% of
    fleet titles carry no parseable number, so this is already a wide net.

    There used to be a second rule -- retire a group whose title names a unit
    that isn't on the stored active list -- and it was **removed on 2026-08-17
    because the list is not trustworthy**. It stacked two unreliable inputs with
    nothing to catch the result: a title parse measured at 79.5% (six known
    titles, e.g. "1275 - NEGRON..." on a group really filed as 2003, parse to a
    *different* valid number) against a list that omits live trucks. Either one
    alone retires a running group, unattended, three times a week. The list
    itself is gone as of 2026-08-31 (see this module's docstring), but the
    reasoning outlived it: absence from any roster is not evidence a truck is
    gone. Retiring a group wants a human, which is what ``/titlecheck`` is for.

    So a title naming some *other* unit is simply not this function's business:
    ``title_unit_changes`` re-files the group under the number the title now
    carries. Retiring and re-filing are different answers to a changed title,
    and only the second one is reversible from the panel without losing a
    group's history.

    Skipped on purpose:

    * **A title that still prints the stored unit.** ``parse_unit`` is a guess
      about a *format*; "is my own number still on this title?" is not, so it
      is asked first and vetoes the retirement (``title_names_unit``). A
      hyphenated prefix the regex could not read -- "T-120 QUINTERO, JOHN /
      ..." -- retired a running truck on 2026-08-26. A ``looks_retired`` marker
      still wins over it, since the fleet leaves the number on those titles.
    * **No stored unit.** An un-onboarded group has no truck to retire, and its
      title not parsing is precisely the case onboarding exists to ask a human
      about -- deactivating it would end that conversation before it started.
    * **No title.** The sweep refreshes titles from Telegram first and drops the
      ones it could not fetch, so a missing title here means *no evidence*, not
      an unnamed truck. Acting on a blank would retire a group for being
      unreachable, which is how the fleet was mass-deactivated once before.
    """
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
        if not looks_retired(title) and title_names_unit(title, stored):
            # The number on file is still printed on the title, whatever
            # parse_unit made of the format. A retired marker still wins: the
            # fleet writes "INACTIVE - 1225 MAGAN" with the number left intact.
            continue
        parsed = normalize_unit(parse_unit(title) or "")
        if looks_retired(title) or not parsed:
            out.append({"group": g, "reason": "title no longer names a unit"})
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


async def _quiet_report() -> str:
    """The quiet-groups list, as HTML. Safe to call even with no traffic data."""
    # Counting only runs forward, so for the first few days after this ships
    # every group has zero messages and would be reported as dead -- so until a
    # full window has been observed, say so instead of raising a fleet-wide
    # false alarm.
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
# Two decisions on a timer, against the group titles alone: a title naming a
# different unit re-files the group, a title that has stopped naming a unit
# retires it, and a title that still says what it said is silent. It writes
# without asking anyone, so it leans entirely on the guards in the two pure
# functions above -- collisions, unreadable titles, unconfigured groups.

_TITLE_SWEEP_KEY = "title_sweep_last_run_on"
# Pause between get_chat calls; ~150 groups, so this costs half a minute and
# keeps the sweep well clear of Telegram's rate limits.
_TITLE_FETCH_DELAY = 0.2


def title_sweep_due(now: datetime, last_run: date | None) -> bool:
    """True on a day that has not been swept yet — every day, UTC.

    Keyed on the date rather than an elapsed interval so the schedule can't
    drift: a restart, a slow tick or a run that took an hour still leaves the
    next sweep on the next day, and never twice in one day. The six-hourly tick
    therefore reaches a due sweep within six hours and finds the other three
    ticks already done.
    """
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

    # Same order as the weekly sweep: re-file first, then judge what is left. A
    # truck whose title now names another unit is not a truck that lost its
    # number -- judging it before the re-file would retire it for naming the
    # new one.
    renames = title_unit_changes(fresh)
    casualties = title_deactivations(apply_renames(fresh, renames))

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


def _titlecheck_button_label(c: dict, selected: bool) -> str:
    unit = normalize_unit(c["group"].get("unit_number")) or "—"
    title = c["group"].get("title") or str(c["group"]["group_id"])
    mark = "✅" if selected else "⬜"
    label = f"{mark} {unit} — {title}"
    return label if len(label) <= 40 else label[:39] + "…"


def _titlecheck_kb(candidates: list[dict], selected: set[int]) -> InlineKeyboardMarkup:
    """One toggle row per group, defaulting to selected -- a plain tap on
    Deactivate still behaves like "deactivate everything shown", but a known
    false positive (the title parser missed a unit it does carry) can be
    unchecked first instead of forcing an all-or-nothing choice."""
    kb = InlineKeyboardMarkup()
    for c in candidates:
        gid = c["group"]["group_id"]
        kb.row(InlineKeyboardButton(
            _titlecheck_button_label(c, gid in selected),
            callback_data=f"tc:t:{gid}",
        ))
    kb.row(
        InlineKeyboardButton(f"💤 Deactivate selected ({len(selected)})", callback_data="tc:ok"),
        InlineKeyboardButton("✖️ Cancel", callback_data="tc:no"),
    )
    return kb


@dp.message_handler(commands=["titlecheck"], chat_type=types.ChatType.PRIVATE)
async def cmd_titlecheck(message: types.Message):
    """On-demand version of the title sweep's one surviving rule.

    Titles are read fresh from Telegram (same as the sweep), and a group that
    can't be fetched is left out rather than flagged -- "couldn't read it" is
    not evidence its title lost the unit. Nothing is deactivated here without
    a tap: this only ever reads ``title_deactivations``, the same pure,
    list-free check the automatic sweep runs, and hands the result to a human,
    who can deselect individual groups before confirming -- the parser is
    conservative but not perfect, and a title it can't read a unit out of
    (e.g. one buried after other words) may still name a live truck.
    """
    if message.from_user.id not in _ADMIN_IDS:
        return

    # _refresh_titles reads every active group from Telegram one at a time
    # (throttled, ~150 groups) -- that's tens of seconds with nothing on
    # screen, so say so up front instead of leaving the admin wondering if the
    # command did anything.
    status = await message.answer("🔎 Checking group titles — this can take a "
                                  "minute for the full fleet…")
    groups = [g for g in await get_all_groups() if g.get("is_active", True)]
    fresh, skipped = await _refresh_titles(groups)
    candidates = title_deactivations(fresh)

    if not candidates:
        text = "🟢 Every active group's title still carries a unit number."
        if skipped:
            text += f"\n\n<i>{skipped} group(s) couldn't be read and were skipped.</i>"
        await status.edit_text(text, parse_mode="HTML")
        return

    # Only the shown subset is ever toggleable/actionable in one round -- acting
    # on a group the admin never saw a line for would defeat the point of this
    # being a reviewed, not automatic, deactivation.
    shown = candidates[:_PREVIEW_LIMIT]
    selected = {c["group"]["group_id"] for c in shown}
    _pending_titlecheck[message.from_user.id] = {
        "candidates": shown,
        "selected": selected,
        "at": monotonic(),
    }
    lines = [f"🔎 <b>{len(candidates)} active group(s)</b> whose title carries "
             f"no unit number:", ""]
    lines += [_casualty_line(c) for c in shown]
    if len(candidates) > len(shown):
        lines.append(f"…and {len(candidates) - len(shown)} more -- re-run "
                     f"/titlecheck after these are handled to reach them.")
    if skipped:
        lines.append(f"\n<i>{skipped} group(s) couldn't be read and were left out.</i>")
    lines.append("\nTap a group to uncheck it if its title actually does name a "
                 "unit. Nothing has been deactivated yet.")
    await status.edit_text("\n".join(lines), parse_mode="HTML",
                           reply_markup=_titlecheck_kb(shown, selected))


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("tc:"))
async def titlecheck_callback(query: types.CallbackQuery):
    uid = query.from_user.id
    if uid not in _ADMIN_IDS:
        await query.answer("Not allowed.", show_alert=True)
        return

    state = _pending_titlecheck.get(uid)
    if state is None or monotonic() - state["at"] > _PENDING_TTL:
        # The fleet may have changed since this list was drawn -- re-run rather
        # than act on a stale one.
        _pending_titlecheck.pop(uid, None)
        await query.message.edit_text("That list expired — send /titlecheck again.")
        await query.answer()
        return

    action = query.data[len("tc:"):]

    if action == "no":
        _pending_titlecheck.pop(uid, None)
        await query.message.edit_text("✖️ Cancelled — nothing was deactivated.")
        await query.answer()
        return

    if action.startswith("t:"):
        # A toggle only ever edits the keyboard -- the state stays pending, so
        # this can't be mistaken for a confirmation.
        gid = int(action[len("t:"):])
        if gid in state["selected"]:
            state["selected"].discard(gid)
        else:
            state["selected"].add(gid)
        await query.message.edit_reply_markup(
            reply_markup=_titlecheck_kb(state["candidates"], state["selected"]))
        await query.answer()
        return

    # action == "ok"
    _pending_titlecheck.pop(uid, None)
    group_ids = list(state["selected"])
    if not group_ids:
        await query.message.edit_text("Nothing selected — nothing was deactivated.")
        await query.answer()
        return

    try:
        deactivated = await deactivate_group_ids(group_ids)
    except Exception:
        logging.exception("titlecheck bulk deactivate failed for admin %s", uid)
        await query.message.edit_text(
            "⚠️ <b>Something went wrong — nothing was deactivated.</b>\n"
            "Send /titlecheck again to retry.",
            parse_mode="HTML",
        )
        await query.answer()
        return

    await query.message.edit_text(
        f"💤 <b>{deactivated} group(s) deactivated.</b>\n"
        "Reactivate any of these from the web panel if a truck is still running.",
        parse_mode="HTML",
    )
    await query.answer()


def _retitle_button_label(r: dict, selected: bool) -> str:
    title = r["group"].get("title") or str(r["group"]["group_id"])
    mark = "✅" if selected else "⬜"
    label = f"{mark} {r['old']} → {r['new']} — {title}"
    return label if len(label) <= 40 else label[:39] + "…"


def _retitle_kb(candidates: list[dict], selected: set[int]) -> InlineKeyboardMarkup:
    """One toggle row per rename, same shape as _titlecheck_kb."""
    kb = InlineKeyboardMarkup()
    for r in candidates:
        gid = r["group"]["group_id"]
        kb.row(InlineKeyboardButton(
            _retitle_button_label(r, gid in selected),
            callback_data=f"rt:t:{gid}",
        ))
    kb.row(
        InlineKeyboardButton(f"🔄 Re-file selected ({len(selected)})", callback_data="rt:ok"),
        InlineKeyboardButton("✖️ Cancel", callback_data="rt:no"),
    )
    return kb


@dp.message_handler(commands=["retitle"], chat_type=types.ChatType.PRIVATE)
async def cmd_retitle(message: types.Message):
    """On-demand version of the title sweep's rename half.

    ``run_title_sweep`` only reaches this once a day; a title that changes just
    after a sweep otherwise waits until tomorrow to be re-filed. This runs
    the exact same check (``title_unit_changes``) whenever an admin wants it,
    and hands the result to a human who can deselect any candidate before
    confirming, same as /titlecheck.
    """
    if message.from_user.id not in _ADMIN_IDS:
        return

    status = await message.answer("🔎 Checking group titles for renames — this can "
                                  "take a minute for the full fleet…")
    groups = [g for g in await get_all_groups() if g.get("is_active", True)]
    fresh, skipped = await _refresh_titles(groups)
    renames = title_unit_changes(fresh)

    if not renames:
        text = ("🟢 No group's title names a different unit from the one it is "
                "filed under.")
        if skipped:
            text += f"\n\n<i>{skipped} group(s) couldn't be read and were skipped.</i>"
        await status.edit_text(text, parse_mode="HTML")
        return

    shown = renames[:_PREVIEW_LIMIT]
    selected = {r["group"]["group_id"] for r in shown}
    _pending_retitle[message.from_user.id] = {
        "candidates": shown,
        "selected": selected,
        "at": monotonic(),
    }
    lines = [f"🔄 <b>{len(renames)} active group(s)</b> whose title now names a "
             f"different unit:", ""]
    lines += [_rename_line(r) for r in shown]
    if len(renames) > len(shown):
        lines.append(f"…and {len(renames) - len(shown)} more -- re-run "
                     f"/retitle after these are handled to reach them.")
    if skipped:
        lines.append(f"\n<i>{skipped} group(s) couldn't be read and were left out.</i>")
    lines.append("\nTap a group to uncheck it if the rename is wrong. Nothing has "
                 "been re-filed yet.")
    await status.edit_text("\n".join(lines), parse_mode="HTML",
                           reply_markup=_retitle_kb(shown, selected))


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("rt:"))
async def retitle_callback(query: types.CallbackQuery):
    uid = query.from_user.id
    if uid not in _ADMIN_IDS:
        await query.answer("Not allowed.", show_alert=True)
        return

    state = _pending_retitle.get(uid)
    if state is None or monotonic() - state["at"] > _PENDING_TTL:
        _pending_retitle.pop(uid, None)
        await query.message.edit_text("That list expired — send /retitle again.")
        await query.answer()
        return

    action = query.data[len("rt:"):]

    if action == "no":
        _pending_retitle.pop(uid, None)
        await query.message.edit_text("✖️ Cancelled — nothing was re-filed.")
        await query.answer()
        return

    if action.startswith("t:"):
        gid = int(action[len("t:"):])
        if gid in state["selected"]:
            state["selected"].discard(gid)
        else:
            state["selected"].add(gid)
        await query.message.edit_reply_markup(
            reply_markup=_retitle_kb(state["candidates"], state["selected"]))
        await query.answer()
        return

    # action == "ok"
    _pending_retitle.pop(uid, None)
    chosen = [r for r in state["candidates"] if r["group"]["group_id"] in state["selected"]]
    if not chosen:
        await query.message.edit_text("Nothing selected — nothing was re-filed.")
        await query.answer()
        return

    pairs = [(r["group"]["group_id"], r["new"]) for r in chosen]
    try:
        refiled, _ = await apply_title_sweep(pairs, [])
    except Exception:
        logging.exception("retitle bulk rename failed for admin %s", uid)
        await query.message.edit_text(
            "⚠️ <b>Something went wrong — nothing was re-filed.</b>\n"
            "Send /retitle again to retry.",
            parse_mode="HTML",
        )
        await query.answer()
        return

    await query.message.edit_text(
        f"🔄 <b>{refiled} group(s) re-filed.</b>",
        parse_mode="HTML",
    )
    await query.answer()


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


async def title_sweep_loop():
    """Background loop: sweep the group titles once a day, and keep the
    message-count buckets from growing without bound.

    One loop rather than two tasks — the tick is six hours and every step is
    guarded, so a step that throws costs only itself.
    """
    while True:
        try:
            await run_title_sweep_if_due()
        except Exception:
            logging.exception("title sweep failed")
        try:
            await prune_group_message_days()
        except Exception:
            logging.exception("pruning group message buckets failed")
        await asyncio.sleep(CHECK_INTERVAL.total_seconds())
