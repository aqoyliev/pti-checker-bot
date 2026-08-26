"""The finished verdict is a new reply to the video, not an edit of the progress
message.

An inspection takes minutes. Editing the "Analyzing…" message meant the verdict
appeared wherever that message had ended up in the scrollback, with no
notification — drivers only saw a result if they went looking for it. So
``deliver_result`` posts a fresh message quoting the video and then removes the
progress message.

The ordering is the part worth pinning down: **send, then delete.** Deleting
first and failing to send would leave the driver with nothing at all.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from utils.pti_processor import deliver_result


def _target(events, *, reply_exc=None, answer_exc=None):
    async def reply(text, **kwargs):
        events.append("reply")
        if reply_exc:
            raise reply_exc
        return SimpleNamespace(message_id=222)

    async def answer(text, **kwargs):
        events.append("answer")
        if answer_exc:
            raise answer_exc
        return SimpleNamespace(message_id=333)

    return SimpleNamespace(reply=reply, answer=answer)


def _progress(events, *, delete_exc=None):
    async def delete():
        events.append("delete")
        if delete_exc:
            raise delete_exc

    return SimpleNamespace(message_id=111, delete=delete, edit_text=AsyncMock())


def test_result_is_a_new_reply_and_the_progress_message_goes():
    events = []
    progress = _progress(events)

    sent = asyncio.run(deliver_result(_target(events), progress, "PASS"))

    assert events == ["reply", "delete"]  # send first, delete second
    assert sent.message_id == 222
    progress.edit_text.assert_not_awaited()


def test_quote_failure_falls_back_to_a_plain_send():
    """An anonymous admin's post can't be quoted, nor can a deleted video."""
    events = []
    progress = _progress(events)

    sent = asyncio.run(
        deliver_result(_target(events, reply_exc=RuntimeError("no quote")),
                       progress, "PASS")
    )

    assert events == ["reply", "answer", "delete"]
    assert sent.message_id == 333


def test_progress_message_survives_when_nothing_can_be_sent():
    """No new message reached the chat, so it is the only place left to say it."""
    events = []
    progress = _progress(events)

    sent = asyncio.run(
        deliver_result(
            _target(events, reply_exc=RuntimeError("x"), answer_exc=RuntimeError("y")),
            progress, "PASS",
        )
    )

    assert "delete" not in events
    assert sent is progress
    progress.edit_text.assert_awaited_once_with("PASS", parse_mode="HTML")


def test_a_delete_that_fails_does_not_lose_the_result():
    """Bots can't always delete their own posts; a stray "Analyzing…" is noise."""
    events = []
    progress = _progress(events, delete_exc=RuntimeError("too old"))

    sent = asyncio.run(deliver_result(_target(events), progress, "PASS"))

    assert sent.message_id == 222
