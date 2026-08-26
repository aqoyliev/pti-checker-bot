"""Re-run inspections that failed because the bot was broken, not the video.

Why this exists: on 2026-08-20 every ``gemini-2.5-*`` model id started returning
404 and PTI checking stopped for two days. The submissions made in that window
left **no trace at all** — the handler returned before writing a ``pti_log``
row — so drivers had filmed their walkarounds, been told something went wrong,
and there was nothing left in the bot that knew it. Recovering them meant
reading Telegram history with a user account, by hand.

So a failure that is ours now goes on a queue (``pti_retry_queue``) and is
re-analysed here once the service works again. Three rules shape it:

**Only infrastructure failures are queued.** An overload, an exhausted quota, a
retired model or a network blip all succeed on the same footage later. A video
that is 12 minutes long, or one the model refused to answer about, fails
identically forever — re-posting that verdict days later is noise, not recovery.
``process_mixed_media`` classifies this; unclassified errors default to *not*
retryable, since an unknown failure is likelier a bug than a blip.

**A retry is the ordinary path, not a copy of it.** It re-drives
``process_mixed_media`` and ``_handle_pti_result`` through a stand-in for the
driver's original message, so the dedup guard, vehicle reconciliation, reminder
reset and pti_log write all behave exactly as they would have at the time. A
second implementation would drift from the real one precisely where it matters.

**A stale result is worse than none.** Nothing is posted for a submission older
than ``MAX_AGE``, and anything superseded by a real inspection in the meantime is
dropped rather than answered twice.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from loader import bot
from utils.db import (
    count_pending_pti_retries,
    defer_pti_retry,
    due_pti_retries,
    get_cached_check,
    get_group,
    get_recent_ptis,
    resolve_pti_retry,
)
from utils.pti_processor import deliver_result, process_mixed_media

# How often the queue is looked at. Minutes, not seconds: the failures this
# recovers from last hours, and a tight loop would mostly re-check an empty table.
CHECK_INTERVAL = timedelta(minutes=10)
# Wait after startup before the first pass. The bot has just come up; give the
# DB pool, the local Bot API server and Gemini a moment to settle rather than
# spending an attempt on a service that is still waking.
STARTUP_DELAY = timedelta(minutes=2)
# Backoff per attempt. The last entry repeats. Hours apart on purpose -- a
# depleted Gemini balance is fixed by a human noticing and paying, not by
# retrying sooner.
BACKOFF_MINUTES = (15, 60, 240, 720)
MAX_ATTEMPTS = len(BACKOFF_MINUTES) + 1
# Past this, a PTI result is history rather than news, and the driver has long
# since been told to re-send. Queued rows older than this are closed unanswered.
MAX_AGE = timedelta(days=2)
# One at a time. Live inspections share PTI_MAX_CONCURRENCY, and a backlog
# flushing all at once is exactly the burst that trips a rate limit.
BATCH = 5

_NOTE = ("\n\n<i>ℹ️ This inspection was delayed — the analysis service was "
         "unavailable when it was sent.</i>")


class _RetryTarget:
    """Stands in for the driver's original message.

    ``process_mixed_media`` and ``_handle_pti_result`` only ever need
    ``.reply()``, ``.answer()`` and ``.chat.id`` from it, so a retry can go
    through the same code as a live ``/check`` instead of a parallel
    implementation that would drift from it.
    """

    class _Chat:
        def __init__(self, chat_id: int):
            self.id = chat_id

    def __init__(self, chat_id: int, message_id: int):
        self.chat = self._Chat(chat_id)
        self.message_id = message_id

    async def reply(self, text: str, **kwargs):
        kwargs.pop("allow_sending_without_reply", None)
        return await bot.send_message(
            self.chat.id, text, reply_to_message_id=self.message_id, **kwargs)

    async def answer(self, text: str, **kwargs):
        return await bot.send_message(self.chat.id, text, **kwargs)


class _StoredMedia:
    """A file_id rebuilt into something ``process_mixed_media`` can download."""

    def __init__(self, ref: dict):
        self.file_id = ref["file_id"]
        self.mime_type = ref.get("mime_type")
        self.duration = ref.get("duration")
        self.file_size = ref.get("file_size")

    async def get_file(self):
        return await bot.get_file(self.file_id)


def _items_from_refs(refs: list[dict]) -> list[dict]:
    return [{"kind": r["kind"], "obj": _StoredMedia(r)} for r in refs
            if r.get("file_id")]


def _backoff_seconds(attempts: int) -> int:
    idx = min(attempts, len(BACKOFF_MINUTES) - 1)
    return BACKOFF_MINUTES[idx] * 60


async def _run_one(row: dict) -> str:
    """Re-analyse one queued submission. Returns the outcome recorded for it."""
    # Imported here: handlers/ imports utils/, so a module-level import would
    # close the circle.
    from handlers.groups.pti import _handle_pti_result

    group_id, retry_id = row["group_id"], row["id"]

    age = datetime.utcnow() - row["created_at"]
    if age > MAX_AGE:
        logging.info("PTI retry %s abandoned: submission is %s old", retry_id, age)
        return "stale"

    # Someone may have re-sent the PTI, or dispatch re-run /check, while this sat
    # in the queue. Answering it again would post a second verdict for one
    # walkaround and count it twice toward the driver's quota.
    if row["media_signature"] or row["content_signature"]:
        if await get_cached_check(group_id, row["media_signature"],
                                  row["content_signature"]):
            logging.info("PTI retry %s superseded by a completed check", retry_id)
            return "superseded"

    items = _items_from_refs(json.loads(row["items_json"]))
    if not items:
        return "gave_up"

    group = await get_group(group_id)
    if not group or not group.get("is_active", True):
        return "gave_up"

    history = await get_recent_ptis(group_id, limit=5)
    unit = group.get("unit_number")
    if unit:
        history = [h for h in history if h.get("unit_number") == unit]

    target = _RetryTarget(group_id, row["reply_message_id"])
    failure: dict = {}

    def _note(retryable: bool, error: str) -> None:
        failure.update(retryable=retryable, error=error)

    text, data, status_msg = await process_mixed_media(
        items, target, history=history, driver_name=row["driver_name"],
        on_failure=_note,
    )

    if text is None or data is None or status_msg is None:
        if failure.get("retryable"):
            raise RuntimeError(failure.get("error") or "retryable failure")
        return "gave_up"

    result_msg = await deliver_result(target, status_msg, text + _NOTE)

    await _handle_pti_result(
        target, text, data,
        driver_user_id=row["user_id"],
        driver_name=row["driver_name"],
        replied_message_id=row["reply_message_id"],
        media_signature=row["media_signature"],
        content_signature=row["content_signature"],
        result_message_id=getattr(result_msg, "message_id", None),
    )
    return "done"


async def process_retry_queue() -> int:
    """One pass over the due rows. Returns how many were answered."""
    rows = await due_pti_retries(BATCH)
    if not rows:
        return 0

    logging.info("PTI retry: %s submission(s) due", len(rows))
    answered = 0
    for row in rows:
        try:
            outcome = await _run_one(row)
        except Exception as e:
            attempts = row["attempts"]
            if attempts + 1 >= MAX_ATTEMPTS:
                logging.warning("PTI retry %s giving up after %s attempts: %s",
                                row["id"], attempts + 1, e)
                await resolve_pti_retry(row["id"], "gave_up")
            else:
                delay = _backoff_seconds(attempts)
                logging.info("PTI retry %s failed again (%s); next try in %s min",
                             row["id"], e, delay // 60)
                await defer_pti_retry(row["id"], delay, str(e))
            continue

        await resolve_pti_retry(row["id"], outcome)
        if outcome == "done":
            answered += 1
            logging.info("PTI retry %s completed for group %s",
                         row["id"], row["group_id"])
    return answered


async def pti_retry_loop():
    """Background loop: flush the failed-inspection queue as the service recovers.

    Guarded end to end — this runs unattended and its whole purpose is to behave
    well when things are already broken, so a failure inside it must cost only
    the current pass.
    """
    await asyncio.sleep(STARTUP_DELAY.total_seconds())
    while True:
        try:
            pending = await count_pending_pti_retries()
            if pending:
                logging.info("PTI retry queue: %s pending", pending)
                await process_retry_queue()
        except Exception:
            logging.exception("PTI retry loop pass failed")
        await asyncio.sleep(CHECK_INTERVAL.total_seconds())
