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

  * a unit was parsed from the group's title or description;
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

The About text also names the drivers, and that is the name stored -- see
utils/driver_names. It never decides anything; a name that can't be paired with
a number just leaves the Telegram name in its place.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_DRIVERS = 2


@dataclass(frozen=True)
class AutoPlan:
    """What to write, and what to tell the admin it came from."""
    unit: str
    drivers: list[tuple[int, str]]      # (user_id, name to store)
    sources: dict[int, str]             # user_id -> the phone it came from
    tg_labels: dict[int, str] = field(default_factory=dict)  # user_id -> Telegram name


def plan_auto_config(unit, phones, resolved, members, names=None,
                     max_drivers: int = MAX_DRIVERS) -> tuple[AutoPlan | None, str]:
    """Return (plan, "") to configure the group, or (None, reason) to ask.

    `resolved` maps phone -> Match|None (Match needs .user_id; None means the
    number named no account). `members` is the group roster. `names` is what
    parse_driver_names read from the same About text.
    """
    if not unit:
        return None, "no unit number could be read from the title or About text"
    if not phones:
        return None, "no phone numbers in the About text"
    if len(phones) > max_drivers:
        return None, (f"{len(phones)} phone numbers in the About text — too many "
                      f"to tell which {max_drivers} are the drivers")

    # The fleet's names are paired with the numbers by position, because that is
    # how the About text writes them -- first name to first number. Any other
    # count is not a pairing at all, so the Telegram names are kept rather than
    # guessing which name belongs to which number.
    fleet = list(names or [])
    if len(fleet) != len(phones):
        fleet = []

    by_id = {m.user_id: m for m in members}
    drivers: list[tuple[int, str]] = []
    sources: dict[int, str] = {}
    tg_labels: dict[int, str] = {}
    for i, phone in enumerate(phones):
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
        drivers.append((match.user_id, fleet[i] if fleet else member.label))
        sources[match.user_id] = phone
        tg_labels[match.user_id] = member.label

    return AutoPlan(unit=unit, drivers=drivers, sources=sources,
                    tg_labels=tg_labels), ""
