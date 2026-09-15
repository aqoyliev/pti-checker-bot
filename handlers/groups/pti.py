from __future__ import annotations

import hashlib
import html
import json
import logging

from aiogram import types
from aiogram.types import ContentType

from data.config import PTI_AUTOCHECK_ENABLED, PTI_TEST_GROUP_IDS
from loader import bot_id, dp
from utils.db import (
    get_group, get_drivers, is_registered_driver, upsert_group,
    log_pti, get_cached_check, get_recent_ptis,
    reset_group_reminders,
)
from utils import pti_gate
from utils.pti_processor import deliver_result, process_mixed_media
from handlers.groups.monitoring import buffer_message, get_album_media

GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]

# **A PTI never decides what vehicle it was filmed on.** Reading the unit
# number, the plate or the trailer number off the footage and storing it is
# gone (2026-09-13, at the fleet's instruction). A stencilled number on a dirty
# panel is the least legible thing in a walkaround, and everything it could be
# written to already has a better source: the truck's unit comes from the chat
# title and is swept daily, and the drivers come from the roster. What is left
# is an inspection verdict, which is what this module is for. The model is
# still asked for a `vehicles` block — the prompt is not ours to edit — and the
# answer is simply not used: a split inspection drops it at the merge, a
# single-call one leaves it in `result_json`, and nothing reads either.

# Test groups (PTI_TEST_GROUP_IDS, env): the bot auto-inspects EVERYONE's video
# there (registered or not, forwarded or not), needs no group setup at all, and
# skips the recycled-video dedup so the same clip can be re-sent while testing.
TEST_GROUP_IDS = PTI_TEST_GROUP_IDS


async def _group_ready(message: types.Message) -> bool:
    # A test group needs no setup. It exists to try the inspection out, so
    # there is no truck to file the result under and nobody to be on a roster --
    # waiting for either would leave the bot silent through the one thing a
    # trial is for. `pti_log` still references the group's row, so the upsert
    # makes sure there is one: an id can be added to PTI_TEST_GROUP_IDS while
    # the bot is already sitting in the chat, in which case its join was never
    # seen and nothing ever created it.
    if message.chat.id in TEST_GROUP_IDS:
        await upsert_group(message.chat.id)
        return True

    group = await get_group(message.chat.id)

    if not group or not group["setup_complete"]:
        return False
    return True


def _items_from_reply(reply: types.Message) -> list[dict] | None:
    if reply.photo:
        return [{"kind": "photo", "obj": reply.photo[-1]}]
    if reply.document and (reply.document.mime_type or "").startswith("image/"):
        return [{"kind": "image_doc", "obj": reply.document}]
    if reply.video:
        return [{"kind": "video", "obj": reply.video}]
    if reply.video_note:
        return [{"kind": "video_note", "obj": reply.video_note}]
    if reply.document and (reply.document.mime_type or "").startswith("video/"):
        return [{"kind": "video_doc", "obj": reply.document}]
    return None


def _items_from_buffered(buf_item) -> dict | None:
    if buf_item.content_type == "photo" and buf_item.photo_size:
        return {"kind": "photo", "obj": buf_item.photo_size}
    if buf_item.content_type == "video" and buf_item.video:
        return {"kind": "video", "obj": buf_item.video}
    if buf_item.content_type == "video_note" and buf_item.video_note:
        return {"kind": "video_note", "obj": buf_item.video_note}
    if buf_item.content_type == "document" and buf_item.document:
        mime = (buf_item.mime_type or "")
        if mime.startswith("image/"):
            return {"kind": "image_doc", "obj": buf_item.document}
        if mime.startswith("video/"):
            return {"kind": "video_doc", "obj": buf_item.document}
    return None


def _signature_from_items(items: list[dict]) -> str | None:
    """Return a dedup signature for this submission.

    Hashes the sorted Telegram ``file_unique_id``s of every photo/video item.
    ``file_unique_id`` is globally stable per-file, so the same set of media —
    whether a single video, a photo album, or mixed — collapses to the same
    signature even across forwards and re-uploads by the same user.
    """
    uids: list[str] = []
    for it in items:
        obj = it["obj"]
        uid = getattr(obj, "file_unique_id", None)
        if uid:
            uids.append(uid)
    if not uids:
        return None
    uids.sort()
    h = hashlib.sha1("|".join(uids).encode()).hexdigest()[:20]
    return f"album:{h}"


_VIDEO_KINDS = ("video", "video_note", "video_doc")


def _content_signature_from_items(items: list[dict]) -> str | None:
    """Second dedup signature for video items, based on ``(file_size, duration)``.

    Telegram assigns a fresh ``file_unique_id`` on every upload session, so
    re-uploading the same video defeats the file_unique_id signature. Size and
    duration stay identical for a byte-for-byte re-upload, so this catches that
    case. Photos are excluded because Telegram re-compresses them client-side,
    making size unreliable. Returns None if no video items have usable metadata.
    """
    parts: list[tuple[int, int]] = []
    for it in items:
        if it["kind"] not in _VIDEO_KINDS:
            continue
        obj = it["obj"]
        size = getattr(obj, "file_size", None)
        duration = getattr(obj, "duration", 0) or 0
        if size:
            parts.append((size, duration))
    if not parts:
        return None
    parts.sort()
    h = hashlib.sha1(repr(parts).encode()).hexdigest()[:20]
    return f"content:{h}"


async def _handle_pti_result(
    message: types.Message,
    text: str | None,
    data: dict | None,
    driver_user_id: int,
    driver_name: str | None,
    replied_message_id: int | None = None,
    media_signature: str | None = None,
    content_signature: str | None = None,
    result_message_id: int | None = None,
):
    if not text or not data:
        return
    passed = data.get("status") == "PASS"
    # `unit_number` is deliberately not passed: it used to be whatever the video
    # showed, and nothing else is written into it. See `_inspect`'s history filter
    # for what leaving it empty keeps switched off, and the module comment above
    # for why the video is no longer a source. The inspection belongs to
    # `group_id` either way, which is what every report groups by.
    await log_pti(
        group_id=message.chat.id,
        user_id=driver_user_id,
        passed=passed,
        severity=data.get("severity", ""),
        result_json=json.dumps(data),
        result_text=text,
        replied_message_id=replied_message_id,
        media_signature=media_signature,
        driver_name=driver_name,
        content_signature=content_signature,
    )
    # A driver submitting any PTI clears the overdue/escalation reminders (#9).
    await reset_group_reminders(message.chat.id)


# ---------- /check ----------

@dp.message_handler(commands=["check"], chat_type=GROUP_TYPES)
async def handle_check_group(message: types.Message):
    if not await _group_ready(message):
        # Drivers are never asked to configure a group: the fleet's admins do
        # that from their side (onboarding prompt, web panel), and the setup
        # nag keeps reminding them until it is done.
        await message.answer(
            "This group isn't set up yet — the fleet admins have been asked to "
            "assign its unit and drivers. Once that's done, /check will work here.")
        return

    reply = message.reply_to_message
    if not reply:
        await message.answer("Reply to a video or photo with /check.")
        return

    direct_uid = reply.from_user.id if reply.from_user else None
    forward_uid = reply.forward_from.id if reply.forward_from else None
    driver_uid: int | None = None
    if message.chat.id in TEST_GROUP_IDS:
        # Nobody is registered in a test group -- anyone's video is inspected,
        # same as the auto-trigger below.
        driver_uid = direct_uid or forward_uid
    elif direct_uid and await is_registered_driver(message.chat.id, direct_uid):
        driver_uid = direct_uid
    elif forward_uid and await is_registered_driver(message.chat.id, forward_uid):
        driver_uid = forward_uid
    if driver_uid is None:
        await message.answer(
            "⚠️ This video isn't from a registered driver, so it can't be checked.\n"
            "Reply <code>/check</code> to a <b>registered driver's</b> video. If the "
            "driver is missing, a fleet admin can add them from the admin panel.",
            parse_mode="HTML",
        )
        return

    drivers = await get_drivers(message.chat.id)
    driver_row = next((d for d in drivers if d["user_id"] == driver_uid), None)
    driver_name = driver_row["name"] if driver_row else (
        reply.from_user.full_name if reply.from_user else None
    )

    await _run_pti(message, reply, driver_uid, driver_name)


async def _run_pti(
    message: types.Message,
    reply: types.Message,
    driver_uid: int,
    driver_name: str | None,
):
    """Gate the media in ``reply`` into an inspection, and post the result.

    Shared by the ``/check`` command and the standalone-video auto-trigger.
    Caller is responsible for resolving ``driver_uid``/``driver_name`` and for
    the group-ready check. Everything expensive is in `_inspect`, behind the
    two guards below.
    """
    items = _items_from_reply(reply)
    if items is None:
        await message.answer("The replied message is not a video or photo.")
        return

    for item in items:
        obj = item["obj"]
        duration = getattr(obj, "duration", None)
        if duration and duration > 900:
            await message.answer(
                f"⚠️ Video is {duration // 60} min long — too long for a PTI inspection. "
                "Please send a video under 15 minutes."
            )
            return

    if reply.media_group_id:
        seen_ids = {reply.message_id}
        for buf_item in get_album_media(message.chat.id, reply.media_group_id):
            if buf_item.message_id in seen_ids:
                continue
            seen_ids.add(buf_item.message_id)
            converted = _items_from_buffered(buf_item)
            if converted:
                items.append(converted)

    signature = _signature_from_items(items)
    content_sig = _content_signature_from_items(items)
    sigs = {s for s in (signature, content_sig) if s}

    # Two `/check`s replying to one video used to start two inspections. The
    # dedup inside `_inspect` reads `pti_log`, and that row is not written
    # until an inspection *finishes*, so for the minutes in between the second
    # command looked exactly like the first: both ran, both logged, and one
    # walkaround was counted as two PTIs. `utils/pti_gate` closes that window
    # in memory, before a single frame has been extracted.
    if not pti_gate.claim(message.chat.id, sigs):
        await message.reply(
            "⏳ This video is <b>already being inspected</b> — the result "
            "is on its way. No need to send /check again.",
            parse_mode="HTML",
        )
        return
    try:
        # One inspection at a time per group; anything else waits its turn. A
        # queued submission therefore starts only once the one ahead of it has
        # logged its row, which is what lets the dedup finally see a video that
        # was still in flight when it was first asked about. Taking turns is
        # also the cheaper order -- two clips of one walkaround stop competing
        # for the same frames budget and the same quota.
        async with pti_gate.group_lock(message.chat.id):
            await _inspect(message, reply, items, driver_uid, driver_name,
                           signature, content_sig)
    finally:
        pti_gate.release(message.chat.id, sigs)


async def _inspect(
    message: types.Message,
    reply: types.Message,
    items: list[dict],
    driver_uid: int,
    driver_name: str | None,
    signature: str | None,
    content_sig: str | None,
):
    """Inspect ``items`` and post the verdict, holding this group's turn.

    Split out of `_run_pti` so the gate in front of it reads as the gate: every
    expensive step lives in here, behind both the in-flight claim and the
    group's lock.
    """
    if message.chat.id not in TEST_GROUP_IDS and (signature or content_sig):
        # Dedup by (file_size, duration) — an old video re-uploaded keeps the same
        # size+length even though Telegram assigns it a fresh file id. A match is
        # rejected here BEFORE any inspection runs, so a recycled PTI never counts
        # toward the quota and never clears the overdue reminders.
        cached = await get_cached_check(message.chat.id, signature, content_sig)
        if cached:
            if cached["user_id"] == driver_uid:
                await message.reply(
                    "♻️ This is the <b>same inspection video you already sent</b> "
                    "(same size and length) — it doesn't count.\n"
                    "Please record and send a <b>new</b> pre-trip inspection.",
                    parse_mode="HTML",
                )
            else:
                drivers = await get_drivers(message.chat.id)
                original = next(
                    (d for d in drivers if d["user_id"] == cached["user_id"]),
                    None,
                )
                original_name = (
                    (original and original["name"])
                    or cached.get("driver_name")
                    or "another driver"
                )
                await message.answer(
                    f"♻️ This video was already submitted by {html.escape(original_name)} — "
                    f"it can't be reused. Please record your own inspection."
                )
            return

    # Previous inspections are **not** shown to the model, and this filter is
    # what keeps them from being. It was written to stop a truck's history
    # following the group onto a different truck, matching against the unit each
    # PTI recorded; nothing writes `pti_log.unit_number` any more, so every row
    # fails the match and `history` comes out empty for any configured group.
    # That is the state the fleet chose to keep on 2026-09-13 — the results it
    # gets today are the results with no history — so the two are left wired up
    # and inert rather than half-removed. Filling the column is the one line
    # that turns it on; read the column's comment in db.init_db first.
    group = await get_group(message.chat.id)
    history = await get_recent_ptis(message.chat.id, limit=5)
    current_unit = group.get("unit_number") if group else None
    if current_unit:
        history = [h for h in history if h.get("unit_number") == current_unit]
    # A failed inspection is not queued for a later re-run. process_mixed_media
    # has already told the driver what to do, and they can do it now rather
    # than read the same apology five more times over the next ninety minutes
    # (which is what the retry queue did on 2026-08-31, before it was removed).
    text, data, status_msg = await process_mixed_media(
        items, reply, history=history, driver_name=driver_name,
    )

    if text is None or data is None or status_msg is None:
        return  # error path; process_mixed_media already edited the status message

    # Nothing is ever held back waiting on something else to be decided — there
    # used to be a confirmation vote on a truck change, and there is no longer
    # even a truck change. The verdict goes out as a *new* reply quoting the
    # video, not as an edit of the progress message — see deliver_result.
    result_msg = await deliver_result(reply, status_msg, text)

    await _handle_pti_result(
        message, text, data,
        driver_user_id=driver_uid,
        driver_name=driver_name,
        replied_message_id=reply.message_id,
        media_signature=signature,
        content_signature=content_sig,
        result_message_id=getattr(result_msg, "message_id", None),
    )


# ---------- media buffering (so /check can find what it replies to) ----------

@dp.message_handler(content_types=[ContentType.PHOTO], chat_type=GROUP_TYPES)
async def handle_group_photo(message: types.Message):
    buffer_message(message)


# Auto-run a PTI only for a single standalone video — not photos, not albums.
# video_note (round messages) and non-video documents are excluded.
_AUTO_VIDEO_KINDS = ("video", "video_doc")


def _is_forwarded_from_other(message: types.Message, sender_uid: int) -> bool:
    """True if this message was forwarded from someone other than its sender.

    A PTI must be the driver's own fresh recording, so a forwarded clip — e.g. a
    driver forwarding another person's video — should be ignored by the
    auto-inspector. A driver forwarding their *own* earlier message still counts
    as their video and is allowed. When the original sender hid their account or
    it was forwarded from a channel/chat, the origin can't be attributed to the
    driver, so it's treated as "from someone else".
    """
    forwarded = bool(
        message.forward_date
        or message.forward_from
        or message.forward_from_chat
        or message.forward_sender_name
    )
    if not forwarded:
        return False
    if message.forward_from is not None:
        return message.forward_from.id != sender_uid
    return True


async def _replies_to_bot(message: types.Message) -> bool:
    """True if this message is a reply to one of *our* bot's messages.

    Matched on this bot's own id, not ``from_user.is_bot`` — another bot in the
    group posting a message would otherwise become a trigger for inspections.
    A failed id lookup answers False, so the worst case is the pre-existing
    behaviour rather than an exception on a message handler.
    """
    reply = message.reply_to_message
    if reply is None or reply.from_user is None:
        return False
    try:
        return reply.from_user.id == await bot_id()
    except Exception:
        logging.exception("could not resolve the bot's own id")
        return False


@dp.message_handler(
    content_types=[ContentType.VIDEO, ContentType.VIDEO_NOTE, ContentType.DOCUMENT],
    chat_type=GROUP_TYPES,
)
async def handle_group_video(message: types.Message):
    buffer_message(message)

    # Auto-inspect a standalone video sent by a registered driver: no /check
    # needed. Cheap guards first, DB lookups last. Stay silent (buffer only)
    # on anything that isn't an eligible standalone video so random group
    # media never triggers an inspection or an error reply.
    #
    # A video that REPLIES TO THE BOT is inspected even when the blanket
    # auto-inspector is off. Replying to the bot's reminder with a video is how
    # drivers actually answer it, and making them add /check turns a normal
    # reply into a silent no-op. The reply is a deliberate address to the bot,
    # so it doesn't reopen the "any video in the group runs an inspection"
    # behaviour that PTI_AUTOCHECK_ENABLED=false exists to prevent.
    if (
        not PTI_AUTOCHECK_ENABLED
        and message.chat.id not in TEST_GROUP_IDS
        and not await _replies_to_bot(message)
    ):
        return  # auto-inspector off — /check or a reply to the bot only
    if message.media_group_id:
        return  # part of an album — needs an explicit /check
    items = _items_from_reply(message)
    if not items or items[0]["kind"] not in _AUTO_VIDEO_KINDS:
        return
    if not await _group_ready(message):
        return
    uid = message.from_user.id if message.from_user else None
    if not uid:
        return
    # A forwarded video isn't the driver's own fresh recording — e.g. a driver
    # forwarding someone else's clip. Don't auto-inspect it and don't reply; stay
    # silent (buffer only). A driver forwarding their *own* earlier message still
    # counts. TEST groups keep the old behavior and inspect forwards too.
    if message.chat.id not in TEST_GROUP_IDS and _is_forwarded_from_other(message, uid):
        return
    # #4: the bot only AUTO-checks a registered driver's video. Anyone else must
    # use /check explicitly (#6) — except in the test groups, where any
    # member's video auto-checks.
    if message.chat.id not in TEST_GROUP_IDS and not await is_registered_driver(message.chat.id, uid):
        return

    drivers = await get_drivers(message.chat.id)
    driver_row = next((d for d in drivers if d["user_id"] == uid), None)
    driver_name = driver_row["name"] if driver_row else (
        message.from_user.full_name if message.from_user else None
    )
    await _run_pti(message, message, uid, driver_name)
