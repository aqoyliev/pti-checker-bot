#!/usr/bin/env python3
"""Standalone SMTP test for the overdue-alert email (Gmail).

Sends ONE test email and prints the exact result/error, so an SMTP problem can
be diagnosed without waiting for a real overdue escalation. Uses only the Python
stdlib, so it runs without installing the bot's dependencies. Reads SMTP_* /
ALERT_EMAIL_TO from the project .env automatically (real env vars override it).

Usage:
    py -3 scripts/test_email_smtp.py

Override the recipient for a one-off test without touching .env:
    $env:ALERT_EMAIL_TO="you@example.com"; py -3 scripts/test_email_smtp.py
"""
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path


def _load_dotenv() -> None:
    """Load SMTP_* / ALERT_EMAIL_TO from the project .env if present.

    Real env vars win over .env (so a manual override still works). Kept as a
    tiny stdlib parser so the script stays dependency-free - no python-dotenv.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
PORT = int(os.environ.get("SMTP_PORT", "587"))
USER = os.environ.get("SMTP_USER", "")
PASSWORD = os.environ.get("SMTP_PASSWORD", "").replace(" ", "")
FROM = os.environ.get("SMTP_FROM", "") or USER
TO = os.environ.get("ALERT_EMAIL_TO", "")


def main() -> int:
    print(f"Host:   {HOST}:{PORT}")
    print(f"User:   {USER or '(none)'}")
    print(f"From:   {FROM}")
    print(f"To:     {TO}")
    print(f"Mode:   {'implicit SSL (465)' if PORT == 465 else 'STARTTLS'}")
    print("-" * 50)

    if not (HOST and USER and PASSWORD and TO):
        print("MISSING CONFIG: SMTP_HOST, SMTP_USER, SMTP_PASSWORD and ALERT_EMAIL_TO are all required.")
        return 2

    msg = EmailMessage()
    msg["Subject"] = "PTI bot - SMTP test"
    msg["From"] = FROM
    msg["To"] = TO
    msg.set_content("This is a test of the PTI overdue-alert email path. If you got this, SMTP works.")

    try:
        ctx = ssl.create_default_context()
        if PORT == 465:
            with smtplib.SMTP_SSL(HOST, PORT, context=ctx, timeout=30) as s:
                s.set_debuglevel(1)
                s.login(USER, PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(HOST, PORT, timeout=30) as s:
                s.set_debuglevel(1)
                s.starttls(context=ctx)
                s.login(USER, PASSWORD)
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        print(f"\nAUTH FAILED ({e.smtp_code}): {e.smtp_error!r}")
        print("-> Wrong SMTP_USER/SMTP_PASSWORD. For Gmail, SMTP_PASSWORD must be a 16-char")
        print("   App Password (Google Account > Security > App passwords), not the account")
        print("   password, and 2-Step Verification must be enabled on the account.")
        return 1
    except smtplib.SMTPSenderRefused as e:
        print(f"\nSENDER REFUSED: {e!r}")
        print(f"-> The server won't send as '{FROM}'. For Gmail, SMTP_FROM should equal SMTP_USER.")
        return 1
    except Exception as e:  # noqa: BLE001 - diagnostic, show everything
        print(f"\nFAILED: {type(e).__name__}: {e}")
        return 1

    print("\nSUCCESS - test email accepted by the server. Check the inbox (and spam).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
