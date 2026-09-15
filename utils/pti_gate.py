"""One inspection at a time per group, and never two over the same video.

Two `/check` replies to the same clip used to start two full inspections. The
recycled-video dedup could not stop it: that reads `pti_log`, and the row is
only written once an inspection *finishes*, so for the minutes in between the
second command looked exactly like the first. Both ran, both logged, and the
day's count read as two PTIs from a driver who filmed one -- which is how
drivers who send one walkaround a day ended up with six submissions against
their name on the fleet report.

So the guard lives in memory, where the in-flight work does:

* `claim` reserves a submission's media signatures before anything expensive
  starts, and refuses a second claim on any of them. It is **synchronous on
  purpose** -- an `await` between asking and reserving is the race it exists to
  close, and the two `/check`s that cause this arrive milliseconds apart.
* `group_lock` then lets one inspection run at a time per group; the rest wait
  their turn. By the time a queued submission starts, the one ahead of it has
  logged its row, so the ordinary dedup can finally see it. Queueing is also
  the cheaper order -- two clips of one walkaround take turns instead of
  competing for the same frames budget and the same Gemini quota.

One process per fleet, so a module-level dict is the whole of the state, and
nothing here survives a restart -- which is right: neither does an inspection
that was in flight.
"""

from __future__ import annotations

import asyncio

# chat_id -> the media signatures currently being inspected in that chat.
_inflight: dict[int, set[str]] = {}

# chat_id -> its turn-taking lock. Never pruned: it is one small object per
# group the bot has inspected since boot (~150 for the biggest fleet), and
# dropping a lock somebody is still queued on would strand them on an object
# no new caller can find.
_locks: dict[int, asyncio.Lock] = {}


def claim(chat_id: int, signatures: set[str]) -> bool:
    """Reserve ``signatures`` for this chat. False if any is already running.

    Must stay synchronous -- see the module docstring. An empty set always
    succeeds and reserves nothing: media with no usable signature still gets
    the group's turn-taking, which is all that can be offered for it.
    """
    running = _inflight.setdefault(chat_id, set())
    if signatures & running:
        return False
    running |= signatures
    return True


def release(chat_id: int, signatures: set[str]) -> None:
    """Give up a claim. Safe to call for signatures that were never reserved."""
    running = _inflight.get(chat_id)
    if running is None:
        return
    running -= signatures
    if not running:
        del _inflight[chat_id]


def is_running(chat_id: int, signatures: set[str]) -> bool:
    """True while any of ``signatures`` is being inspected in this chat."""
    return bool(signatures & _inflight.get(chat_id, set()))


def group_lock(chat_id: int) -> asyncio.Lock:
    """This chat's turn: one inspection at a time, the rest queue on it."""
    lock = _locks.get(chat_id)
    if lock is None:
        lock = _locks[chat_id] = asyncio.Lock()
    return lock
