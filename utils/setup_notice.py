"""What a driver's own group is told when its setup finally lands.

Every part of configuring a group happens where the drivers cannot see it. The
unit is read off the chat title, the roster and the About text come over
MTProto, an admin taps names in a DM prompt or searches for them in the web
panel, and what was written is reported back to the admins. From inside the
chat the only visible trace is the refusal a driver gets for running /check too
early -- "this group isn't set up yet" -- and until now nothing ever came back
to say that it had landed. The drivers were left watching a bot that had told
them to wait for something they could not see arrive.

So the group is told once, when it *becomes* usable: a unit on file and at
least one registered driver, which is exactly the state /check demands.

Three rules hold it up:

* **Only on the transition.** The caller reads ``is_usable`` before its writes
  and hands it back to ``announce_if_now_usable`` after, which posts only when
  it moved. An admin correcting one pick in a group that already works posts
  nothing -- those drivers have been told, and a notice per correction turns
  the one message that matters into traffic.
* **The caller opts in**, which is why this is a pair of calls around the
  writes and not something ``utils/db`` does on its own.
  ``scripts/setup_groups.py`` deliberately announces nothing: it configures a
  whole fleet in one pass, and a write-level post would land in sixty driver
  groups at once. The two in-group commands (``/setunit``, ``/adddriver``) are
  left out for the opposite reason -- whoever typed them is standing in the
  group, and the bot's answer is already on screen for everyone in it.
* **A refused post is recorded, not raised.** The setup stands either way and
  the admins have already been told; but this is often the bot's first real
  message in that chat, and therefore the first chance to notice it cannot
  speak there (``utils/group_health.note_send_failure``).

Each driver is **tagged** rather than merely named. A ``tg://user`` link is the
only way to do that here: the label has to stay the *fleet's* name for them --
that is the whole point of reading it out of the About text -- and plenty of
these accounts have no @username to fall back on. The tag is also what puts the
message in front of the person it registered, the one reader who can tell that
the wrong name landed on them.

It is a statement, not a request: no commands, no buttons, nothing for a driver
to do about it. Correcting any of it is an admin's job, which is why the admin
notice and not this message carries the ``/onboard`` line.
"""
from __future__ import annotations

import logging
from html import escape

from loader import bot
from utils.db import get_drivers, get_group
from utils.group_health import note_send_failure

# Where the picks came from, in the group's own words. The drivers are the only
# people who can tell that a name landed on the wrong account, and what they
# need in order to judge it is which source claimed it.
FROM_ABOUT_TEXT = ("Read from the group's name and the phone numbers in its "
                   "About text. If anything here is wrong, let a fleet admin "
                   "know.")
FROM_AN_ADMIN = ("Set up by one of the fleet's admins. If anything here is "
                 "wrong, let them know.")


async def is_usable(group_id: int) -> bool:
    """Whether /check would actually run here: a unit, and a driver to file it to.

    Deliberately not ``groups.setup_complete``. ``set_group_unit`` flips that
    flag on its own, so a group can be "complete" with an empty roster -- which
    is a group where every video belongs to nobody and the compliance pass has
    no one to count. Announcing on that would tell the drivers they were set up
    while /check still had no name to log them under.
    """
    group = await get_group(group_id)
    if not group or not (group.get("unit_number") or "").strip():
        return False
    return bool(await get_drivers(group_id))


async def tell_the_group(group_id: int, source: str) -> None:
    """Post the one notice the drivers' group ever gets about its own setup."""
    group = await get_group(group_id) or {}
    drivers = await get_drivers(group_id)
    unit = (group.get("unit_number") or "?").strip() or "?"
    tagged = "\n".join(
        f'• <a href="tg://user?id={d["user_id"]}">'
        f'{escape(d.get("name") or str(d["user_id"]))}</a>'
        for d in drivers)
    try:
        await bot.send_message(
            group_id,
            f"✅ <b>Unit {escape(unit)}</b> — this group is set up.\n\n"
            f"Registered driver(s):\n{tagged}\n\n"
            f"<i>/check works here now. {source}</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        # Never fatal: the group is configured, and the admins know. But a post
        # that comes back "no rights" is worth recording -- the bot is sitting
        # in a chat it cannot speak in, and nothing else would say so until a
        # reminder next came due.
        logging.warning("could not announce the setup of %s: %s",
                        group_id, type(e).__name__)
        await note_send_failure(group_id, e)


async def announce_if_now_usable(group_id: int, was_usable: bool,
                                 source: str) -> None:
    """Tell the group if the writes just done are what made it usable.

    `was_usable` is read by the caller *before* its writes, which is the whole
    of the transition rule: reading the state on both sides is what keeps this
    to one message per group however the setup arrives -- the unit first and a
    driver after, or the other way round, from the picker or from the panel.

    Called last on purpose, after the admin has their own confirmation. That
    confirmation is the record of what was written and must not wait on a send
    into a group that may be muted.
    """
    if not was_usable and await is_usable(group_id):
        await tell_the_group(group_id, source)
