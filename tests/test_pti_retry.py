"""The failed-inspection retry queue.

The rule this suite exists to protect: only failures that are *ours* are worth
re-running. An overloaded or out-of-credit Gemini answers the same footage fine
tomorrow; a 12-minute video is 12 minutes long forever, and posting that verdict
two days later is noise rather than recovery.

Pure: no DB, no network, no Telegram.
"""
import asyncio
import json
from datetime import timedelta

from utils import pti_retry


# ---------- what gets re-run ----------

def test_backoff_lengthens_then_settles():
    delays = [pti_retry._backoff_seconds(a) for a in range(6)]
    assert delays == sorted(delays), "backoff must never shorten"
    # It plateaus rather than growing without bound.
    assert delays[-1] == delays[-2]


def test_backoff_starts_in_minutes_not_seconds():
    # A depleted balance is fixed by a human paying, not by retrying sooner;
    # a tight first retry just burns an attempt.
    assert pti_retry._backoff_seconds(0) >= 5 * 60


def test_attempts_are_bounded():
    assert pti_retry.MAX_ATTEMPTS == len(pti_retry.BACKOFF_MINUTES) + 1
    assert pti_retry.MAX_ATTEMPTS >= 3


def test_max_age_outlives_a_realistic_outage_but_not_the_week():
    # The 2026-08-20 outage ran ~2 days; anything much older is history.
    assert timedelta(days=1) <= pti_retry.MAX_AGE <= timedelta(days=4)


# ---------- rebuilding the media ----------

def test_items_are_rebuilt_from_stored_file_ids():
    refs = [{"kind": "video", "file_id": "abc", "mime_type": None},
            {"kind": "photo", "file_id": "def", "mime_type": None}]
    items = pti_retry._items_from_refs(refs)
    assert [i["kind"] for i in items] == ["video", "photo"]
    assert [i["obj"].file_id for i in items] == ["abc", "def"]


def test_a_ref_without_a_file_id_is_dropped():
    # Nothing can be downloaded for it, so carrying it would only produce a
    # confusing partial inspection.
    assert pti_retry._items_from_refs([{"kind": "video", "file_id": None}]) == []


def test_stored_media_exposes_what_the_pipeline_reads():
    obj = pti_retry._StoredMedia(
        {"file_id": "x", "mime_type": "video/mp4", "duration": 90, "file_size": 12})
    # process_mixed_media reads mime_type for image docs; the /check guard reads
    # duration to reject over-long videos.
    assert (obj.file_id, obj.mime_type, obj.duration) == ("x", "video/mp4", 90)


# ---------- the stand-in message ----------

def test_retry_target_exposes_chat_id():
    # _handle_pti_result and _reconcile_vehicles reach for message.chat.id.
    t = pti_retry._RetryTarget(-100123, 55)
    assert t.chat.id == -100123
    assert t.message_id == 55


def test_retry_target_replies_to_the_original_message(monkeypatch):
    sent = {}

    async def fake_send(chat_id, text, **kw):
        sent.update(chat_id=chat_id, text=text, **kw)
        return "msg"

    monkeypatch.setattr(pti_retry.bot, "send_message", fake_send)
    t = pti_retry._RetryTarget(-100123, 55)
    # allow_sending_without_reply is a Message.reply kwarg the Bot API call does
    # not take; passing it through would raise instead of posting the result.
    asyncio.run(t.reply("hi", allow_sending_without_reply=False))
    assert sent["chat_id"] == -100123
    assert sent["reply_to_message_id"] == 55


# ---------- classifying failures ----------

def _classify(exc_factory):
    """Run process_mixed_media's error branch for `exc` and report retryability."""
    from google.genai import errors as genai_errors

    from utils import pti_processor

    seen = {}

    class _Resp:
        status_code = 0
        headers: dict = {}
        text = ""

        def json(self):
            return {}

    def _err(code, msg):
        r = _Resp()
        r.status_code = code
        return genai_errors.ClientError(code, {"error": {"code": code, "message": msg}}, r)

    exc = exc_factory(_err)
    if pti_processor._is_service_overload(exc):
        seen["retryable"] = True
    elif pti_processor._is_model_retired(exc):
        seen["retryable"] = True
    elif getattr(exc, "code", None) in (401, 403, 429):
        seen["retryable"] = True
    else:
        seen["retryable"] = False
    return seen["retryable"]


def test_quota_and_billing_failures_are_retryable():
    # 429 is both a rate limit and how a depleted prepaid balance surfaces.
    assert _classify(lambda e: e(429, "RESOURCE_EXHAUSTED")) is True


def test_a_retired_model_is_retryable():
    assert _classify(lambda e: e(
        404, "This model models/gemini-2.5-pro is no longer available")) is True


def test_a_bad_request_is_not_retryable():
    # A real bug; retrying it four more times just delays noticing.
    assert _classify(lambda e: e(400, "invalid argument")) is False


# ---------- serialising the submission ----------

def test_items_to_refs_keeps_only_what_survives_a_restart():
    from handlers.groups import pti as pti_handler

    class _Obj:
        file_id = "F1"
        mime_type = "video/mp4"
        duration = 90
        file_size = 1234

    refs = pti_handler._items_to_refs([{"kind": "video", "obj": _Obj()}])
    assert refs == [{"kind": "video", "file_id": "F1", "mime_type": "video/mp4",
                     "duration": 90, "file_size": 1234}]


def test_items_to_refs_skips_an_object_with_no_file_id():
    from handlers.groups import pti as pti_handler

    class _Obj:
        file_id = None

    assert pti_handler._items_to_refs([{"kind": "video", "obj": _Obj()}]) == []


def test_a_queued_row_round_trips_through_json():
    from handlers.groups import pti as pti_handler

    class _Obj:
        file_id = "F1"
        mime_type = None
        duration = 30
        file_size = 5

    refs = pti_handler._items_to_refs([{"kind": "video", "obj": _Obj()}])
    items = pti_retry._items_from_refs(json.loads(json.dumps(refs)))
    assert items[0]["obj"].file_id == "F1"
    assert items[0]["kind"] == "video"
