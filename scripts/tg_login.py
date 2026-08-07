"""One-time interactive Telegram *user* login, used to audit which fleet groups
exist and whether the bot is in them.

Run this yourself in a real terminal -- it prompts for your phone, the login
code Telegram sends you, and your 2FA password if you have one. Nothing it
prints contains a secret.

    railway run --service pti-checker-bot py -3.11 scripts/tg_login.py

(`railway run` supplies TELEGRAM_API_ID / TELEGRAM_API_HASH, the same app
credentials the local Bot API server already uses. You can also export them
yourself instead.)

The session is written OUTSIDE the repo, to ~/.pti-tg/fleet_audit.session, so
it can never be committed. That file is equivalent to a logged-in Telegram
client for your account -- delete it when the audit is done, or revoke it from
Telegram > Settings > Devices.
"""
from __future__ import annotations

import os
from pathlib import Path

from telethon.sync import TelegramClient

SESSION_DIR = Path.home() / ".pti-tg"
SESSION = SESSION_DIR / "fleet_audit"


def main() -> None:
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH not set.\n"
            "Either run this under `railway run --service pti-checker-bot`, "
            "or get them from https://my.telegram.org and export them."
        )

    SESSION_DIR.mkdir(mode=0o700, exist_ok=True)

    with TelegramClient(str(SESSION), int(api_id), api_hash) as client:
        me = client.get_me()
        name = " ".join(filter(None, [me.first_name, me.last_name]))
        groups = sum(1 for d in client.iter_dialogs() if d.is_group)
        print(f"\nLogged in as {name} (id {me.id})")
        print(f"Visible groups: {groups}")
        print(f"Session saved to {SESSION}.session")
        print("\nTell Claude it's ready; it will run the read-only scan next.")


if __name__ == "__main__":
    main()
