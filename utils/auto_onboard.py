"""Decide whether a new group can be configured without asking an admin.

The fleet writes both drivers' phone numbers into the group's About text, and
a phone number resolves to a user_id (utils/phone_lookup). When everything
lines up, onboarding has all the facts it would otherwise ask for, and the
admin gets told what happened instead of being asked to do it.

Everything here is pure: the caller does the roster read, the lookup and the
writes. That keeps the *decision* -- the part that must not go wrong quietly --
testable without a session or a database.

The bar is deliberately high, because a wrong auto-save is silent. Every one of
these must hold, or the normal picker is sent instead:

  * a unit was parsed AND survived the active-units check (the caller's
    _checked_guess already applies that);
  * the About text yields exactly as many numbers as a group has drivers --
    three numbers means one of them belongs to dispatch, and guessing which
    is exactly the kind of silent misattribution this avoids;
  * every number resolves to an account;
  * every resolved account is a member of the group. An account that is not in
    the chat can never post a PTI, so registering it would create a driver who
    is permanently overdue;
  * the accounts are distinct, and none is a bot.

Anything short of that is not a failure -- it is the ordinary prompt, with a
line saying which check stopped it.
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_DRIVERS = 2


@dataclass(frozen=True)
class AutoPlan:
    """What to write, and what to tell the admin it came from."""
    unit: str
    drivers: list[tuple[int, str]]      # (user_id, label)
    sources: dict[int, str]             # user_id -> the phone it came from


def plan_auto_config(unit, phones, resolved, members,
                     max_drivers: int = MAX_DRIVERS) -> tuple[AutoPlan | None, str]:
    """Return (plan, "") to configure the group, or (None, reason) to ask.

    `resolved` maps phone -> Match|None (Match needs .user_id; None means the
    number named no account). `members` is the group roster.
    """
    if not unit:
        return None, "no unit number could be read from the title or About text"
    if not phones:
        return None, "no phone numbers in the About text"
    if len(phones) > max_drivers:
        return None, (f"{len(phones)} phone numbers in the About text — too many "
                      f"to tell which {max_drivers} are the drivers")

    by_id = {m.user_id: m for m in members}
    drivers: list[tuple[int, str]] = []
    sources: dict[int, str] = {}
    for phone in phones:
        match = resolved.get(phone)
        if match is None:
            return None, f"{phone} matched no Telegram account"
        member = by_id.get(match.user_id)
        if member is None:
            # The commonest real case: the number belongs to someone who was
            # never added to the chat, or who left it.
            return None, f"{phone} resolved to an account that is not in this group"
        if member.is_bot:
            return None, f"{phone} resolved to a bot"
        if match.user_id in sources:
            return None, f"{phone} resolved to the same account as an earlier number"
        drivers.append((match.user_id, member.label))
        sources[match.user_id] = phone

    return AutoPlan(unit=unit, drivers=drivers, sources=sources), ""
