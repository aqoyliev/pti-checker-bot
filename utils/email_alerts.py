"""Email alerts for the overdue PTI escalation (#9).

A small, dependency-free SMTP sender: the bot emails a single global address
(``ALERT_EMAIL_TO``) when a group enters the every-12-hours overdue phase, so a
fleet manager learns about a non-compliant driver without watching the chat.

Sends through Gmail SMTP (``smtp.gmail.com``) — authenticating as the Gmail
account means the message passes SPF/DKIM/DMARC and actually delivers, unlike a
third-party relay sending "as" a Gmail address. ``SMTP_PASSWORD`` must be a Gmail
*App Password* (not the account password), which requires 2-Step Verification.

Sending is blocking (stdlib ``smtplib``), so callers go through
``send_overdue_alert``, which offloads to a worker thread and never raises — a
mail failure must not break the reminder pass. The feature is a silent no-op
unless ``SMTP_USER``, ``SMTP_PASSWORD`` and ``ALERT_EMAIL_TO`` are all set.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

from data.config import (
    ALERT_EMAIL_TO,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)


def email_configured() -> bool:
    """True when overdue alert emails can be sent.

    Requires a host, a recipient, and Gmail credentials (address + App
    Password). Until all are supplied in the env, sending is a silent no-op."""
    return bool(SMTP_HOST and ALERT_EMAIL_TO and SMTP_USER and SMTP_PASSWORD)


def _send_blocking(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = ALERT_EMAIL_TO
    msg.set_content(body)

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context()) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)


async def send_overdue_alert(unit_number: str | None, driver_names: str, days_overdue: int) -> None:
    """Email the global alert address that a group's PTI is overdue.

    No-op (and never raises) when email isn't configured or the send fails; the
    reminder pass must keep going regardless.
    """
    if not email_configured():
        return

    unit = unit_number or "unknown"
    subject = f"PTI overdue - unit {unit} ({driver_names})"
    body = (
        f"No pre-trip inspection has been submitted for {days_overdue} day(s).\n\n"
        f"Unit: {unit}\n"
        f"Driver(s): {driver_names}\n\n"
        "The driver is being reminded in the group every 12 hours until a PTI video "
        "is submitted. This email repeats on the same 12-hour cadence."
    )
    try:
        await asyncio.to_thread(_send_blocking, subject, body)
    except Exception:
        logging.exception("Failed to send overdue alert email for unit %s", unit)
