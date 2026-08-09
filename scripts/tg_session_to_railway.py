"""Convert the local .session file into a Telethon StringSession and store it
as the TELEGRAM_SESSION Railway variable.

A .session file cannot travel to Railway -- the container filesystem is
rebuilt on every deploy -- so the session has to live in an env var.

The value is never printed. It is read, converted and handed to `railway
variables` inside this process, so the credential does not appear in your
terminal, your shell history or an agent transcript.

SECURITY: TELEGRAM_SESSION is full access to the Telegram account it was
created from -- messages, contacts, groups, everything. Anyone with access to
the Railway project can read it. Use an account that only exists for this, not
a personal one, and revoke it from Telegram > Settings > Devices when done.

NEVER ship a session you also use locally. Telegram revokes an authorization
key seen from two IP addresses at once (AuthKeyDuplicatedError), killing both
copies -- which is what happened on 2026-08-09 when the `fleet_audit` session
was sent to Railway and then used from a local script. Hence the default here
is `bot_userbot`, a session that exists only to be deployed:

    railway run py -3.11 scripts/tg_login.py --name bot_userbot
    railway run py -3.11 scripts/tg_session_to_railway.py

    py -3.11 scripts/tg_session_to_railway.py [--service pti-checker-bot]
                                              [--session bot_userbot]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

SESSION_DIR = Path.home() / ".pti-tg"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", default="pti-checker-bot")
    ap.add_argument("--session", default="bot_userbot",
                    help="session name under ~/.pti-tg. Must NOT be one you use "
                         "locally (default: bot_userbot)")
    ap.add_argument("--print-only", action="store_true",
                    help="print the value instead of setting it (avoid: it is a credential)")
    args = ap.parse_args()

    session = SESSION_DIR / args.session
    if not session.with_suffix(".session").exists():
        raise SystemExit(
            f"no session at {session}.session — create one with:\n"
            f"  railway run py -3.11 scripts/tg_login.py --name {args.session}"
        )

    import os
    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TELEGRAM_API_ID / TELEGRAM_API_HASH not set (use `railway run`).")

    with TelegramClient(str(session), int(api_id), api_hash) as client:
        me = client.get_me()
        value = StringSession.save(client.session)

    print(f"session belongs to {me.first_name} (id {me.id}); {len(value)} chars")

    if args.print_only:
        print(value)
        return

    proc = subprocess.run(
        ["railway", "variables", "--service", args.service,
         "--set", f"TELEGRAM_SESSION={value}"],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    if proc.returncode != 0:
        # Never echo the command: it contains the session.
        print("railway variables failed:", proc.stderr.strip()[:400])
        raise SystemExit(1)
    print(f"TELEGRAM_SESSION set on {args.service}. Railway will redeploy.")


if __name__ == "__main__":
    main()
