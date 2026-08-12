"""Phone number -> Telegram account, via a second user session.

Deliberately NOT part of utils/userbot.py. That module is strictly read-only,
and there is no read-only way to do this: Telegram will only tell you which
account owns a number if you *import it as a contact*
(contacts.importContacts), which writes to the account's contact list. Every
imported contact is deleted again before this module returns, so the list is
left as it was found, but the call is still a write and stays out of the
read-only client.

The second reason for a second account is rate limiting. Contact import is the
most aggressively limited thing a user account can do — a young account is
often refused outright — and the roster session is load-bearing for onboarding.
Getting *that* account limited would take out member lookup fleet-wide, so the
two never share an account.

Three outcomes are worth telling apart, and only two of them are visible:

  * a match — the number is on Telegram and findable;
  * no match — the number is not registered, OR the owner has "Who can find me
    by my phone number" set to Nobody/My Contacts. Telegram returns nothing in
    both cases and does not say which;
  * refused — Telegram keeps returning the number in `retry_contacts` without
    ever importing it, which is how a contact-import limit shows up. That is
    an account problem, not an answer about the number, so it raises
    LookupUnavailable rather than quietly reading as "no match".

Configuration (absent config simply disables the feature):
  TELEGRAM_API_ID / TELEGRAM_API_HASH        shared with the roster userbot
  TELEGRAM_LOOKUP_SESSION                    a Telethon StringSession
  TELEGRAM_LOOKUP_SESSION_FILE               path to a .session file (local dev)
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from data.config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_LOOKUP_SESSION,
    TELEGRAM_LOOKUP_SESSION_FILE,
)

_client = None
_connect_lock = asyncio.Lock()
# One lookup at a time: two concurrent imports would interleave their contact
# cleanup, and contact import is rate-limited anyway.
_lookup_lock = asyncio.Lock()

# The fleet is US-only, so a bare 10-digit number is a US number. Numbers are
# typed by hand ("786-488-2619"), and Telegram silently resolves nothing for a
# number without a country code — indistinguishable from "not on Telegram".
DEFAULT_COUNTRY_CODE = "1"

_MAX_ATTEMPTS = 3
_RETRY_PAUSE = 5.0


class LookupUnavailable(RuntimeError):
    """The lookup could not be performed — says nothing about the numbers."""


@dataclass(frozen=True)
class Match:
    phone: str
    user_id: int
    name: str
    username: str | None
    is_bot: bool

    @property
    def label(self) -> str:
        return self.name or (f"@{self.username}" if self.username else str(self.user_id))


def normalize_phone(raw: str, default_country: str = DEFAULT_COUNTRY_CODE) -> str | None:
    """'786-488-2619' -> '+17864882619'. None when it cannot be a phone number.

    An explicit '+' is trusted as already-international; a bare 10-digit number
    gets the default country code.
    """
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        return None
    if raw.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    return f"+{digits}"


# A run of 10-15 digits with the usual separators. Deliberately stricter than
# normalize_phone: a group's About text is free prose full of numbers -- unit
# numbers, trailer numbers, years, dollar amounts -- and the same "descriptions
# are parsed more strictly than titles" reasoning that governs unit parsing
# applies here. Ten digits is the shortest thing that is unambiguously a phone.
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{8,18}\d")


def extract_phones(text: str) -> list[str]:
    """Phone numbers found in free text, normalized and de-duplicated.

    Driver phone numbers live in the group's About text, which is where this
    feature gets its input: description -> number -> account -> user_id.
    """
    out: list[str] = []
    for chunk in _PHONE_RE.findall(text or ""):
        digits = re.sub(r"\D", "", chunk)
        if not 10 <= len(digits) <= 15:
            continue
        norm = normalize_phone(chunk)
        if norm and norm not in out:
            out.append(norm)
    return out


def is_configured() -> bool:
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH
                and (TELEGRAM_LOOKUP_SESSION or TELEGRAM_LOOKUP_SESSION_FILE))


async def _get_client():
    """Connect once, reuse. Returns None when unconfigured or unusable."""
    global _client
    if not is_configured():
        return None
    if _client is not None:
        return _client

    async with _connect_lock:
        if _client is not None:
            return _client
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            session = (StringSession(TELEGRAM_LOOKUP_SESSION) if TELEGRAM_LOOKUP_SESSION
                       else TELEGRAM_LOOKUP_SESSION_FILE)
            client = TelegramClient(session, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                logging.error("lookup session is not authorized — phone lookup disabled")
                await client.disconnect()
                return None
            _client = client
            logging.info("lookup session connected (phone lookup available)")
        except Exception:
            logging.exception("could not start the lookup session")
            return None
    return _client


async def _drop_client():
    """Forget the cached client so the next call rebuilds it.

    Same reasoning as utils/userbot._drop_client: a session that dies mid-flight
    otherwise leaves every later lookup failing until the process restarts.
    """
    global _client
    stale, _client = _client, None
    if stale is not None:
        try:
            await stale.disconnect()
        except Exception:
            pass


# What each imported contact is named on the lookup account. It is never a
# person's real name, and it is what Telegram hands back while the contact
# exists, so it doubles as the marker for "this name is ours, not theirs".
_CONTACT_NAME = "lookup"


def _display_name(user) -> str:
    return " ".join(filter(None, [user.first_name, user.last_name])).strip()


async def _with_real_name(client, m: Match) -> Match:
    """Replace a placeholder name with the account's own, once un-contacted.

    A saved contact is reported under *your* name for them, so the User that
    comes back from importContacts is called "lookup" -- the label this module
    imports it under. Re-reading the profile after the contact is deleted gives
    the real first/last name. If that re-read fails, the id and username are
    still correct, so the placeholder is dropped rather than shown as a name.
    """
    if m.name != _CONTACT_NAME:
        return m
    try:
        user = await client.get_entity(m.user_id)
    except Exception:
        logging.warning("could not re-read the profile for %s", m.user_id)
        return Match(phone=m.phone, user_id=m.user_id, name="",
                     username=m.username, is_bot=m.is_bot)
    name = _display_name(user)
    return Match(phone=m.phone, user_id=m.user_id,
                 name="" if name == _CONTACT_NAME else name,
                 username=user.username or m.username,
                 is_bot=bool(getattr(user, "bot", False)))


async def lookup(phones: list[str]) -> dict[str, Match | None]:
    """Resolve `phones` (as typed) to accounts.

    Returns {original_string: Match or None}. Unparseable input maps to None.
    Raises LookupUnavailable when the session is missing, unauthorized, flood-
    waited, or when Telegram refuses every import — cases where None would be a
    lie about the number rather than an answer.
    """
    from telethon.errors import FloodWaitError

    wanted = {raw: normalize_phone(raw) for raw in phones}
    results: dict[str, Match | None] = {raw: None for raw in phones}
    pending = {raw: p for raw, p in wanted.items() if p}
    if not pending:
        return results

    client = await _get_client()
    if client is None:
        raise LookupUnavailable(
            "no lookup session configured (TELEGRAM_LOOKUP_SESSION)")

    from telethon.tl.functions.contacts import (
        DeleteContactsRequest,
        ImportContactsRequest,
    )
    from telethon.tl.types import InputPhoneContact

    imported_users: list = []
    refused_every_time = True
    async with _lookup_lock:
        try:
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                if not pending:
                    break
                # client_id is our own correlation handle; Telegram echoes it
                # back in `imported` and `retry_contacts`.
                order = list(pending.items())
                res = await client(ImportContactsRequest([
                    InputPhoneContact(client_id=i, phone=phone,
                                      first_name=_CONTACT_NAME, last_name="")
                    for i, (_, phone) in enumerate(order)
                ]))
                # Correlate on user_id, not on the phone string: Telegram
                # returns numbers without the leading '+' and may normalize
                # them, whereas ImportedContact carries both ids verbatim.
                by_id = {u.id: u for u in res.users}
                for imp in res.imported:
                    refused_every_time = False
                    raw, phone = order[imp.client_id]
                    user = by_id.get(imp.user_id)
                    if user is None:
                        # Imported but not returned: nothing more to learn.
                        pending.pop(raw, None)
                        continue
                    imported_users.append(user)
                    # NB: user.first_name here is _CONTACT_NAME, not the
                    # account's own name -- see _real_name below.
                    results[raw] = Match(phone=phone, user_id=user.id,
                                         name=_display_name(user),
                                         username=user.username,
                                         is_bot=bool(getattr(user, "bot", False)))
                    pending.pop(raw, None)

                retry = {order[i][0] for i in res.retry_contacts
                         if 0 <= i < len(order)}
                # Anything neither imported nor flagged for retry has been
                # answered: no visible account.
                pending = {raw: p for raw, p in pending.items() if raw in retry}
                if pending and attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(_RETRY_PAUSE)
        except FloodWaitError as e:
            raise LookupUnavailable(
                f"Telegram rate-limited the lookup account for {e.seconds}s") from e
        except (ConnectionError, OSError) as e:
            await _drop_client()
            raise LookupUnavailable(f"lookup session connection failed: {e}") from e
        except Exception as e:
            logging.exception("phone lookup failed")
            raise LookupUnavailable(f"{type(e).__name__}: {e}") from e
        finally:
            # Always undo the write, even on the way out with an exception.
            if imported_users:
                try:
                    await client(DeleteContactsRequest(id=imported_users))
                except Exception:
                    logging.warning("could not delete %d imported contact(s) — the "
                                    "lookup account's contact list is dirty",
                                    len(imported_users))

        # Only now, with the contact gone, does Telegram report the account's
        # own name instead of ours.
        results = {raw: await _with_real_name(client, m) if m else None
                   for raw, m in results.items()}

    if pending and refused_every_time:
        raise LookupUnavailable(
            "Telegram kept asking to retry and imported nothing — the lookup "
            "account is almost certainly contact-import limited")
    return results


async def close():
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        finally:
            _client = None
