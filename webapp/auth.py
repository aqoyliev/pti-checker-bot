"""Auth for the web admin panel (Telegram Mini App).

Telegram signs everything a Mini App knows about the user into an ``initData``
query string. We validate it per
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
(HMAC-SHA256 keyed off the bot token), so the API needs no login/session of its
own — a request is trusted iff Telegram signed it recently and the signed user
id resolves to an admin. Admin resolution mirrors handlers/admin/panel.py:
env ``ADMINS`` are always super-admins; everyone else is looked up in the
DB-backed admins table.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from data.config import ADMINS, BOT_TOKEN
from utils.db import get_admin

# initData is minted when the Mini App opens; reject blobs older than this so a
# leaked one can't be replayed forever. A day comfortably covers a panel tab
# left open.
MAX_AGE_SECONDS = 24 * 3600

_ADMIN_IDS = {str(a).strip() for a in ADMINS if str(a).strip()}


def parse_init_data(
    init_data: str,
    *,
    bot_token: str = BOT_TOKEN,
    max_age: int = MAX_AGE_SECONDS,
    now: float | None = None,
) -> dict | None:
    """Return initData's fields as a dict if its signature is valid and fresh,
    else None. Every field except ``hash`` participates in the check string."""
    if not init_data:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None

    try:
        auth_date = int(data.get("auth_date", ""))
    except ValueError:
        return None
    if now is None:
        now = time.time()
    if auth_date <= 0 or now - auth_date > max_age:
        return None
    return data


def extract_user(data: dict) -> dict | None:
    """The signed ``user`` field ({id, first_name, ...}) or None."""
    try:
        user = json.loads(data.get("user", ""))
    except (TypeError, ValueError):
        return None
    return user if isinstance(user, dict) and isinstance(user.get("id"), int) else None


async def resolve_admin(user_id: int) -> dict | None:
    """{'user_id', 'is_super_admin'} for admins, None for everyone else."""
    if str(user_id) in _ADMIN_IDS:
        return {"user_id": user_id, "is_super_admin": True}
    return await get_admin(user_id)
