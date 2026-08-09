"""One-time interactive Telegram *user* login.

Run this yourself in a real terminal -- it prompts for your phone, the login
code Telegram sends you, and your 2FA password if you have one. Nothing it
prints contains a secret.

    railway run py -3.11 scripts/tg_login.py                    # local scripts
    railway run py -3.11 scripts/tg_login.py --name bot_userbot # for the bot

ONE SESSION PER PLACE IT RUNS. An authorization key used from two IP addresses
at once is revoked by Telegram (AuthKeyDuplicatedError) and both copies die --
which is exactly what happened on 2026-08-09, when the session powering the
deployed bot was also used by a local script. Keep `fleet_audit` for your
machine and `bot_userbot` for Railway; never point both at one file. Extra
sessions on the same account are fine, that is what Settings > Devices lists.

(`railway run` supplies TELEGRAM_API_ID / TELEGRAM_API_HASH, the same app
credentials the local Bot API server already uses. You can also export them
yourself instead.)

Sessions are written OUTSIDE the repo, to ~/.pti-tg/<name>.session, so they can
never be committed. Each file is equivalent to a logged-in Telegram client for
your account -- delete it when done, or revoke it from Telegram > Settings >
Devices.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from telethon.sync import TelegramClient

SESSION_DIR = Path.home() / ".pti-tg"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="fleet_audit",
                    help="session file name under ~/.pti-tg (default: fleet_audit)")
    args = ap.parse_args()
    session = SESSION_DIR / args.name

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH not set.\n"
            "Either run this under `railway run --service pti-checker-bot`, "
            "or get them from https://my.telegram.org and export them."
        )

    SESSION_DIR.mkdir(mode=0o700, exist_ok=True)

    with TelegramClient(str(session), int(api_id), api_hash) as client:
        me = client.get_me()
        name = " ".join(filter(None, [me.first_name, me.last_name]))
        groups = sum(1 for d in client.iter_dialogs() if d.is_group)
        print(f"\nLogged in as {name} (id {me.id})")
        print(f"Visible groups: {groups}")
        print(f"Session saved to {session}.session")
        print("\nUse this session from ONE place only — sharing an auth key "
              "across two IPs revokes it.")


if __name__ == "__main__":
    main()
