"""Two `/check`s on one video must not start two inspections.

Pure: no network, no DB. What is pinned here is the window the `pti_log` dedup
structurally cannot close -- the row is written only when an inspection
*finishes*, so between two commands seconds apart there is nothing in the
database to match against. `utils/pti_gate` holds that window in memory, and
its claim is deliberately synchronous: an `await` between asking and reserving
is the race itself.
"""
import asyncio

import pytest

from utils import pti_gate

CHAT = -1001234567890
OTHER = -1009876543210


@pytest.fixture(autouse=True)
def _clean():
    pti_gate._inflight.clear()
    pti_gate._locks.clear()
    yield
    pti_gate._inflight.clear()
    pti_gate._locks.clear()


def test_second_claim_on_the_same_video_is_refused():
    sigs = {"album:aaa", "content:bbb"}
    assert pti_gate.claim(CHAT, sigs) is True
    assert pti_gate.claim(CHAT, sigs) is False


def test_one_shared_signature_is_enough_to_refuse():
    """A re-upload keeps its (size, duration) even with a fresh file id."""
    assert pti_gate.claim(CHAT, {"album:aaa", "content:bbb"}) is True
    assert pti_gate.claim(CHAT, {"album:zzz", "content:bbb"}) is False


def test_a_different_video_in_the_same_group_still_claims():
    assert pti_gate.claim(CHAT, {"album:aaa"}) is True
    assert pti_gate.claim(CHAT, {"album:bbb"}) is True


def test_the_same_video_in_another_group_is_not_this_group_s_business():
    assert pti_gate.claim(CHAT, {"album:aaa"}) is True
    assert pti_gate.claim(OTHER, {"album:aaa"}) is True


def test_release_reopens_the_video_and_leaves_no_row_behind():
    sigs = {"album:aaa", "content:bbb"}
    pti_gate.claim(CHAT, sigs)
    pti_gate.release(CHAT, sigs)
    assert CHAT not in pti_gate._inflight
    assert pti_gate.claim(CHAT, sigs) is True


def test_release_of_something_never_claimed_is_harmless():
    pti_gate.release(CHAT, {"album:aaa"})
    pti_gate.claim(CHAT, {"album:aaa"})
    pti_gate.release(CHAT, {"album:zzz"})
    assert pti_gate.is_running(CHAT, {"album:aaa"}) is True


def test_media_with_no_signature_still_gets_through():
    """Nothing to reserve is not a reason to refuse the inspection."""
    assert pti_gate.claim(CHAT, set()) is True
    assert pti_gate.claim(CHAT, set()) is True


def test_claim_is_synchronous_so_there_is_no_await_to_race_in():
    assert not asyncio.iscoroutinefunction(pti_gate.claim)


def test_a_group_runs_one_inspection_at_a_time():
    order = []

    async def inspect(tag, hold):
        async with pti_gate.group_lock(CHAT):
            order.append(f"{tag} start")
            await asyncio.sleep(hold)
            order.append(f"{tag} done")

    async def main():
        await asyncio.gather(inspect("a", 0.02), inspect("b", 0))

    asyncio.run(main())
    assert order == ["a start", "a done", "b start", "b done"]


def test_two_groups_do_not_wait_on_each_other():
    running = []

    async def inspect(chat):
        async with pti_gate.group_lock(chat):
            running.append(chat)
            await asyncio.sleep(0.02)

    async def main():
        await asyncio.wait_for(
            asyncio.gather(inspect(CHAT), inspect(OTHER)), timeout=1)

    asyncio.run(main())
    assert sorted(running) == sorted([CHAT, OTHER])
