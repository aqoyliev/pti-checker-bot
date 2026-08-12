"""Resolve phone numbers to Telegram accounts from your own machine.

    railway run py -3.11 scripts/tg_phone_lookup.py 786-488-2619 561-674-7866
    railway run py -3.11 scripts/tg_phone_lookup.py --session lookup_local 786-488-2619

This is the local twin of /whois. It uses a LOCAL session file — never the one
deployed to Railway. An authorization key seen from two IP addresses at once is
revoked by Telegram and both copies die (2026-08-09), so the deployed lookup
account needs its own second session for local use:

    railway run py -3.11 scripts/tg_login.py --name lookup_local

What it does is a write, undone immediately: each number is imported as a
contact, read, and deleted again. See utils/phone_lookup.py.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

SESSION_DIR = Path.home() / ".pti-tg"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phones", nargs="+", help="phone numbers, any punctuation")
    ap.add_argument("--session", default="lookup_local",
                    help="session name under ~/.pti-tg (default: lookup_local). "
                         "Must NOT be the session deployed to Railway.")
    args = ap.parse_args()

    session = SESSION_DIR / args.session
    if not session.with_suffix(".session").exists():
        raise SystemExit(
            f"no session at {session}.session — create one with:\n"
            f"  railway run py -3.11 scripts/tg_login.py --name {args.session}")

    # data/config reads the environment at import time, so point the lookup
    # client at the local session file before importing anything that uses it.
    os.environ["TELEGRAM_LOOKUP_SESSION_FILE"] = str(session)
    os.environ.pop("TELEGRAM_LOOKUP_SESSION", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from utils import phone_lookup

    async def run():
        try:
            results = await phone_lookup.lookup(args.phones)
        except phone_lookup.LookupUnavailable as e:
            raise SystemExit(f"lookup unavailable — {e}")
        for raw in args.phones:
            m = results.get(raw)
            if m is None:
                print(f"{raw}: no account found (not on Telegram, or hidden by "
                      f"privacy settings)")
            else:
                uname = f"@{m.username}" if m.username else "(no username)"
                print(f"{raw}: {m.user_id:<14} {m.label}  {uname}"
                      f"{'  [bot]' if m.is_bot else ''}")
        await phone_lookup.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
