from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.exceptions import (
    ChatNotFound,
    MessageNotModified,
    MethodIsNotAvailable,
    Unauthorized,
)

from loader import dp, bot
from utils.db import (
    attach_proposal_message,
    bump_proposal_reminder,
    bump_setup_nag,
    cast_vote,
    count_votes,
    create_proposal,
    get_groups_needing_setup_nag,
    get_open_proposals,
    get_pti_log,
    get_proposal,
    mark_group_inactive,
    mark_unreachable,
    clear_unreachable,
    UNREACHABLE_LIMIT,
    mark_pti_failed,
    reset_setup_nag,
    set_proposal_status,
    set_trailer,
    set_truck_unit,
)
from utils.enforcement import handle_pti_passed
from handlers.admin.onboard import start_onboarding

CONFIRM_THRESHOLD = 3
REJECT_THRESHOLD = 3
REMINDER_INTERVAL = timedelta(minutes=10)
REMINDER_MAX = 3
# When False, proposal vote reminders ("still need votes") are never sent.
# The truck-change confirmation flow is active (#5), so reminders are ON — a
# stalled vote gets nudged and eventually expires instead of holding a PTI
# forever. Set False to silence the nags.
PROPOSAL_REMINDERS_ENABLED = True
SETUP_NAG_INTERVAL = timedelta(minutes=10)
# One prompt per group. The real ceiling lives in get_groups_needing_setup_nag
# (setup_nag_count < 1); this constant only documents it.
SETUP_NAG_MAX = 1

# When False (current), an unconfigured group is never nagged in-chat — the
# onboarding prompt is re-sent to the admins in DM instead. Flip to True to
# restore the old behaviour of asking whoever is in the group to run /setunit.
NAG_THE_GROUP = False

SETUP_NAG_MESSAGE = (
    "⏰ This group is still not configured. Anyone in the group can run:\n"
    "1. Have the driver send a message, then reply with: <code>/adddriver Driver Name</code>\n"
    "2. Set the unit number: <code>/setunit &lt;unit_number&gt;</code>"
)

_reminder_tasks: dict[int, asyncio.Task] = {}


def _vote_kb(proposal_id: int, confirms: int, rejects: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(
            f"✅ Confirm ({confirms}/{CONFIRM_THRESHOLD})",
            callback_data=f"pv:{proposal_id}:c",
        ),
        InlineKeyboardButton(
            f"❌ Reject ({rejects}/{REJECT_THRESHOLD})",
            callback_data=f"pv:{proposal_id}:r",
        ),
    )
    return kb


def _proposal_body(proposal_type: str, payload: dict) -> str:
    if proposal_type == "vehicle_change":
        kind = escape(payload.get("kind") or "vehicle")
        current_unit = escape(payload.get("current_unit") or "—")
        new_unit = escape(payload.get("new_unit") or "—")
        new_plate = payload.get("new_plate")
        plate_line = f" (plate <b>{escape(new_plate)}</b>)" if new_plate else ""
        bundled = payload.get("bundled_trailer")
        trailer_line = ""
        if bundled and bundled.get("unit"):
            t_unit = escape(bundled["unit"])
            t_plate = bundled.get("plate")
            t_plate_str = f" (plate <b>{escape(t_plate)}</b>)" if t_plate else ""
            trailer_line = f"\nTrailer in the same PTI: <b>{t_unit}</b>{t_plate_str}"
        return (
            f"🚨 <b>{kind.capitalize()} change detected</b>\n\n"
            f"This PTI shows {kind} unit <b>{new_unit}</b>{plate_line}, but the registered "
            f"{kind} is <b>{current_unit}</b>.{trailer_line}\n\n"
            f"If this is a real {kind} switch, confirm to update (both truck and trailer "
            f"will be saved). If it's a wrong PTI, reject — the PTI will be marked failed and "
            f"the registered vehicles stay.\n\n"
            f"Needs <b>{CONFIRM_THRESHOLD} confirmations</b>."
        )
    return "Proposal pending."


async def _send_proposal(chat_id: int, proposal_type: str, payload: dict, proposer_id: int | None) -> int:
    proposal_id = await create_proposal(chat_id, proposal_type, payload, proposer_id)
    body = _proposal_body(proposal_type, payload)
    sent = await bot.send_message(
        chat_id,
        body,
        parse_mode="HTML",
        reply_markup=_vote_kb(proposal_id, 0, 0),
    )
    await attach_proposal_message(proposal_id, sent.message_id)
    _schedule_reminder(proposal_id, REMINDER_INTERVAL.total_seconds())
    return proposal_id


def _cancel_reminder(proposal_id: int):
    task = _reminder_tasks.pop(proposal_id, None)
    if task and not task.done():
        task.cancel()


def _schedule_reminder(proposal_id: int, delay_seconds: float):
    if not PROPOSAL_REMINDERS_ENABLED:
        return
    existing = _reminder_tasks.get(proposal_id)
    if existing and not existing.done():
        existing.cancel()
    _reminder_tasks[proposal_id] = asyncio.create_task(
        _reminder_loop(proposal_id, delay_seconds)
    )


async def _reminder_loop(proposal_id: int, delay_seconds: float):
    try:
        await asyncio.sleep(max(delay_seconds, 1))
        proposal = await get_proposal(proposal_id)
        if not proposal or proposal["status"] != "open":
            return

        confirms, rejects = await count_votes(proposal_id)
        if confirms >= CONFIRM_THRESHOLD or rejects >= REJECT_THRESHOLD:
            return  # callback handler will finalize on next click; nothing to do

        new_count = await bump_proposal_reminder(proposal_id)
        chat_id = proposal["group_id"]
        body = _proposal_body(proposal["proposal_type"], proposal["payload"])
        header = f"🔔 <b>Reminder ({new_count}/{REMINDER_MAX})</b> — still need votes:\n\n"
        try:
            sent = await bot.send_message(
                chat_id,
                header + body,
                parse_mode="HTML",
                reply_markup=_vote_kb(proposal_id, confirms, rejects),
            )
            await attach_proposal_message(proposal_id, sent.message_id)
        except Exception:
            logging.exception("Failed to send reminder for proposal %s", proposal_id)

        if new_count >= REMINDER_MAX:
            await set_proposal_status(proposal_id, "expired")
            try:
                await bot.send_message(
                    chat_id,
                    "⏱️ Proposal expired — no quorum after reminders. Re-run the command to try again.",
                )
            except Exception:
                logging.exception("Failed to send expiration notice for proposal %s", proposal_id)
            return

        _schedule_reminder(proposal_id, REMINDER_INTERVAL.total_seconds())
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("Reminder loop failed for proposal %s", proposal_id)


async def setup_nag_loop():
    """Periodically nag groups whose setup is incomplete and that have no open proposal."""
    while True:
        try:
            await _run_setup_nag_pass()
        except Exception:
            logging.exception("setup nag pass failed")
        await asyncio.sleep(60)


# Errors that mean the group is gone for good (kicked/blocked/deleted/not found).
# Unauthorized is the parent of BotKicked/BotBlocked and also covers the bare
# "Forbidden: the group chat was deleted" case. Marking the group inactive drops
# it from get_groups_needing_setup_nag() so we stop retrying it every pass.
_UNREACHABLE = (Unauthorized, ChatNotFound, MethodIsNotAvailable)


async def _run_setup_nag_pass():
    now = datetime.utcnow()
    for g in await get_groups_needing_setup_nag():
        last = g.get("last_setup_nag_at")
        if last is not None and (now - last) < SETUP_NAG_INTERVAL:
            continue
        try:
            if NAG_THE_GROUP:
                await bot.send_message(g["group_id"], SETUP_NAG_MESSAGE, parse_mode="HTML")
            else:
                # Drivers are never asked to configure their own group; the
                # reminder goes to the admins who actually do the onboarding.
                # If none of them is reachable the group gets nothing — silence
                # is the intended behaviour, not a fallback to nagging drivers.
                chat = await bot.get_chat(g["group_id"])
                if not await start_onboarding(
                        g["group_id"], chat.title or str(g["group_id"])):
                    logging.warning(
                        "setup nag for %s reached no admin; group left untouched",
                        g["group_id"],
                    )
            await bump_setup_nag(g["group_id"])
            await clear_unreachable(g["group_id"])
        except _UNREACHABLE as e:
            # Same rule as the reminder path: one failure is not proof the group
            # is gone (the local Bot API server forgets chats on restart), so
            # deactivate only after a sustained streak.
            strikes = await mark_unreachable(g["group_id"])
            if strikes >= UNREACHABLE_LIMIT:
                logging.warning(
                    "Group %s unreachable %s times in a row during setup nag (%s)"
                    " — deactivating", g["group_id"], strikes, type(e).__name__,
                )
                await mark_group_inactive(g["group_id"])
            else:
                logging.warning(
                    "Group %s unreachable during setup nag (%s), strike %s/%s",
                    g["group_id"], type(e).__name__, strikes, UNREACHABLE_LIMIT,
                )
        except Exception:
            logging.exception("failed to send setup nag to %s", g["group_id"])


async def schedule_pending_reminders():
    """Re-arm reminders after bot restart for any still-open proposals."""
    now = datetime.utcnow()
    for proposal in await get_open_proposals():
        created = proposal["created_at"]
        sent_reminders = proposal.get("reminder_count") or 0
        next_due = created + REMINDER_INTERVAL * (sent_reminders + 1)
        delay = max((next_due - now).total_seconds(), 1.0)
        _schedule_reminder(proposal["id"], delay)


async def propose_vehicle_change(
    chat_id: int,
    kind: str,
    current_unit: str,
    new_unit: str,
    new_plate: str | None,
    pti_log_id: int,
    result_message_id: int | None = None,
    bundled_trailer: dict | None = None,
) -> int:
    return await _send_proposal(
        chat_id,
        "vehicle_change",
        {
            "kind": kind,
            "current_unit": current_unit,
            "new_unit": new_unit,
            "new_plate": new_plate,
            "pti_log_id": pti_log_id,
            "result_message_id": result_message_id,
            "bundled_trailer": bundled_trailer,
        },
        proposer_id=None,
    )


# ---------- execution on confirm / reject ----------

async def _on_confirmed(chat_id: int, proposal: dict):
    ptype = proposal["proposal_type"]
    payload = proposal["payload"]

    if ptype == "vehicle_change":
        kind = payload["kind"]
        new_unit = payload["new_unit"]
        new_plate = payload.get("new_plate")
        if kind == "truck":
            await set_truck_unit(chat_id, new_unit, new_plate)
        else:
            await set_trailer(chat_id, new_unit, new_plate)
        plate_note = f" (plate <b>{escape(new_plate)}</b>)" if new_plate else ""
        confirm_lines = [f"✅ Updated {escape(kind)} to unit <b>{escape(new_unit)}</b>{plate_note}."]
        bundled = payload.get("bundled_trailer")
        if bundled and bundled.get("unit"):
            t_unit = bundled["unit"]
            t_plate = bundled.get("plate")
            await set_trailer(chat_id, t_unit, t_plate)
            t_plate_note = f" (plate <b>{escape(t_plate)}</b>)" if t_plate else ""
            confirm_lines.append(f"✅ Trailer also updated to unit <b>{escape(t_unit)}</b>{t_plate_note}.")
        await bot.send_message(chat_id, "\n".join(confirm_lines), parse_mode="HTML")
        # Restore the held PTI result, and trigger enforcement if it passed.
        pti_log_id = payload.get("pti_log_id")
        result_message_id = payload.get("result_message_id")
        if pti_log_id:
            pti = await get_pti_log(int(pti_log_id))
            if pti and result_message_id:
                try:
                    await bot.edit_message_text(
                        pti["result_text"],
                        chat_id=chat_id,
                        message_id=int(result_message_id),
                        parse_mode="HTML",
                    )
                except Exception:
                    logging.exception("Failed to restore PTI result message")
            if pti:
                try:
                    await handle_pti_passed(
                        chat_id,
                        int(pti["user_id"]),
                        pti.get("driver_name") or str(pti["user_id"]),
                    )
                except Exception:
                    logging.exception("Failed to run handle_pti_passed after vehicle confirm")
        return


async def _on_rejected(chat_id: int, proposal: dict):
    ptype = proposal["proposal_type"]
    if ptype == "vehicle_change":
        payload = proposal["payload"]
        kind = payload.get("kind") or "vehicle"
        current_unit = payload.get("current_unit") or "—"
        new_unit = payload.get("new_unit") or "—"
        pti_log_id = payload.get("pti_log_id")
        result_message_id = payload.get("result_message_id")
        if pti_log_id:
            try:
                await mark_pti_failed(int(pti_log_id))
            except Exception:
                logging.exception("Failed to mark pti_log %s as failed", pti_log_id)
        if result_message_id:
            try:
                await bot.edit_message_text(
                    f"❌ <b>PTI marked FAILED.</b> Members rejected the {escape(kind)} change "
                    f"to <b>{escape(new_unit)}</b>; registered {escape(kind)} stays "
                    f"<b>{escape(current_unit)}</b>.",
                    chat_id=chat_id,
                    message_id=int(result_message_id),
                    parse_mode="HTML",
                )
            except Exception:
                logging.exception("Failed to edit held PTI result on reject")
        await bot.send_message(
            chat_id,
            f"❌ {escape(kind.capitalize())} change rejected. Keeping <b>{escape(current_unit)}</b>.",
            parse_mode="HTML",
        )
        return


# ---------- callback handler ----------

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("pv:"))
async def vote_callback(query: types.CallbackQuery):
    try:
        _, pid_str, vote_code = query.data.split(":")
        proposal_id = int(pid_str)
    except (ValueError, AttributeError):
        await query.answer("Invalid vote.", show_alert=False)
        return

    if vote_code == "c":
        vote = "confirm"
    elif vote_code == "r":
        vote = "reject"
    else:
        await query.answer("Unknown vote.", show_alert=False)
        return

    proposal = await get_proposal(proposal_id)
    if not proposal:
        await query.answer("This proposal no longer exists.", show_alert=False)
        return
    if proposal["status"] != "open":
        await query.answer(f"This proposal is already {proposal['status']}.", show_alert=False)
        return

    chat_id = proposal["group_id"]
    if query.message and query.message.chat.id != chat_id:
        await query.answer("Wrong chat.", show_alert=False)
        return

    await cast_vote(proposal_id, query.from_user.id, vote)
    confirms, rejects = await count_votes(proposal_id)

    body = _proposal_body(proposal["proposal_type"], proposal["payload"])

    if confirms >= CONFIRM_THRESHOLD:
        await set_proposal_status(proposal_id, "confirmed")
        _cancel_reminder(proposal_id)
        await reset_setup_nag(chat_id)
        try:
            await bot.edit_message_text(
                f"{body}\n\n✅ <b>Confirmed</b> ({confirms} confirmations).",
                chat_id=chat_id,
                message_id=proposal["message_id"],
                parse_mode="HTML",
            )
        except MessageNotModified:
            pass
        await query.answer("Confirmed.")
        try:
            await _on_confirmed(chat_id, proposal)
        except Exception:
            logging.exception("Failed to execute confirmed proposal %s", proposal_id)
        return

    if rejects >= REJECT_THRESHOLD:
        await set_proposal_status(proposal_id, "rejected")
        _cancel_reminder(proposal_id)
        try:
            await bot.edit_message_text(
                f"{body}\n\n❌ <b>Rejected</b> ({rejects} rejections).",
                chat_id=chat_id,
                message_id=proposal["message_id"],
                parse_mode="HTML",
            )
        except MessageNotModified:
            pass
        await query.answer("Rejected.")
        try:
            await _on_rejected(chat_id, proposal)
        except Exception:
            logging.exception("Failed to handle rejected proposal %s", proposal_id)
        return

    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=proposal["message_id"],
            reply_markup=_vote_kb(proposal_id, confirms, rejects),
        )
    except MessageNotModified:
        pass
    await query.answer("Vote recorded.")
