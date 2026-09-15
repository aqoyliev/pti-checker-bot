"""aiohttp server for the web admin panel (Telegram Mini App).

Runs inside the bot process (started from ``app.py:on_startup``) and exposes
everything an admin can do, as JSON under ``/api/*`` plus the single-page UI
at ``/``. Every API request must carry the Mini App's signed ``initData`` in an
``Authorization: tma <initData>`` header; webapp/auth.py validates the
signature and utils/admins.py resolves the user to an admin, so there is no
separate login. The one exception is the report PDFs, which are opened with a
plain navigation instead of a header-carrying fetch -- see the one-time
download tokens below ``_report_pdf``. aiohttp is already a dependency
(aiogram runs on it).
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from functools import partial
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web

from data.config import DATABASE_URL, FLEET_NAME, FLEET_TZ, WEBAPP_PORT
from loader import bot
from scripts import fleet_report as _report
from utils import userbot
from utils.admins import resolve_admin
from utils.driver_names import match_names_to_drivers, parse_driver_contacts
from utils.group_health import note_send_failure, note_send_ok
from utils.phones import find_phones
from utils.db import (
    add_admin,
    add_driver,
    get_active_group_ids,
    get_admins,
    get_all_drivers_by_group,
    get_all_groups,
    get_drivers,
    get_group,
    get_last_pti,
    get_last_pti_per_group,
    get_non_driver_ids,
    get_pti_count_this_week,
    get_recent_ptis,
    get_weekly_pti_stats,
    normalize_unit,
    remove_admin,
    remove_driver,
    set_driver_names,
    set_group_active,
    set_group_notifications,
    set_group_title,
    set_group_unit,
    set_setting,
    swap_driver,
    unmark_non_drivers,
)
from utils.enforcement import REQUIRED_PER_WEEK, compliance_verdict
from utils.gemini import (AVAILABLE_GEMINI_MODELS, MODEL_HINTS, get_active_model,
                          set_active_model)
from webapp.auth import extract_user, parse_init_data

_INDEX_HTML = Path(__file__).parent / "static" / "index.html"

_TZ = ZoneInfo(FLEET_TZ)


def _json_default(o):
    """asyncpg hands back naive UTC datetimes; the admin reads them in the
    fleet's own zone, so they are converted here rather than shown four hours
    off. Fixed-width "YYYY-MM-DD HH:MM" keeps the panel's string sort working."""
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        return o.astimezone(_TZ).strftime("%Y-%m-%d %H:%M")
    return str(o)


_dumps = partial(json.dumps, default=_json_default)


def _json(payload, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=_dumps)


def _err(status: int, message: str) -> web.Response:
    return _json({"error": message}, status=status)


# ---------- chat cache ----------
# Titles live in groups.title (kept fresh by the group_title middleware), so
# listing groups normally needs no Telegram calls. get_chat only fills gaps —
# groups with no stored title yet — and its result is persisted; the short
# in-memory cache just absorbs rapid re-fetches (including failures for dead
# group ids).
#
# The same call answers for the About text, which is where the drivers' phone
# numbers live, so both are cached from one response rather than fetched twice.
# The About text is deliberately NOT stored in the database: it is the fleet's
# own record of who to call, and a stale copy of a phone number is worse than
# no copy.

_TITLE_TTL = 600.0
_chat_cache: dict[int, tuple[float, str, str]] = {}   # gid -> (at, title, about)


async def _chat_info(group_id: int, stored: str | None = None) -> tuple[str, str]:
    """(title, About text). Both degrade to the stored title / "" on failure."""
    hit = _chat_cache.get(group_id)
    if hit and time.monotonic() - hit[0] < _TITLE_TTL:
        return hit[1], hit[2]
    about = ""
    try:
        chat = await bot.get_chat(group_id)
        title = chat.title or str(group_id)
        about = getattr(chat, "description", None) or ""
        if title != stored:
            try:
                await set_group_title(group_id, title)
            except Exception:
                logging.exception("web panel: failed to store title for %s", group_id)
    except Exception:
        title = stored or str(group_id)
    _chat_cache[group_id] = (time.monotonic(), title, about)
    return title, about


async def _chat_title(group_id: int, stored: str | None = None) -> str:
    return (await _chat_info(group_id, stored))[0]


async def _chat_titles(group_ids: list[int]) -> dict[int, str]:
    sem = asyncio.Semaphore(8)

    async def one(gid: int) -> tuple[int, str]:
        async with sem:
            return gid, await _chat_title(gid)

    return dict(await asyncio.gather(*(one(g) for g in group_ids)))


def _driver_phones(about: str, drivers: list[dict]) -> tuple[dict[int, str], list[str]]:
    """({user_id: phone as written}, numbers that belong to nobody in particular).

    The About text pairs a name with a number; `group_drivers` pairs a name with
    a `user_id`. Both pairings have to hold for a number to land on a driver
    row, and `match_names_to_drivers` is the half that refuses to guess — two
    drivers sharing a surname pair to neither. Whatever is left over is shown
    as the group's numbers instead, because an admin looking up who to call
    would rather see two numbers than none.
    """
    contacts = parse_driver_contacts(about)
    by_name = {name: phone for name, phone in contacts if phone}
    placed, _ = match_names_to_drivers([n for n, _ in contacts], drivers)

    phones = {uid: by_name[name] for uid, name in placed.items() if name in by_name}
    taken = set(phones.values())
    spare = [p for p in find_phones(about) if p not in taken]
    return phones, spare


# ---------- user-profile cache ----------
# The admins table stores ids only, so a name has to come from Telegram.
# get_chat answers for anyone who has ever opened a DM with the bot, which is
# every admin — that DM is how onboarding reaches them. It is still one network
# call per admin, so results are cached, and a failure is cached too: an admin
# the bot can't resolve degrades to a bare id, never to an error.

_USER_TTL = 600.0
_user_cache: dict[int, tuple[float, dict]] = {}


async def _user_card(user_id: int) -> dict:
    """{name, username} for a user id. Both may be None."""
    hit = _user_cache.get(user_id)
    if hit and time.monotonic() - hit[0] < _USER_TTL:
        return hit[1]
    card: dict = {"name": None, "username": None}
    try:
        chat = await bot.get_chat(user_id)
        card = {
            "name": " ".join(filter(None, [chat.first_name, chat.last_name])).strip() or None,
            "username": chat.username or None,
        }
    except Exception:
        logging.info("web panel: no Telegram profile for user %s", user_id)
    _user_cache[user_id] = (time.monotonic(), card)
    return card


async def _user_cards(user_ids: list[int]) -> dict[int, dict]:
    sem = asyncio.Semaphore(8)

    async def one(uid: int) -> tuple[int, dict]:
        async with sem:
            return uid, await _user_card(uid)

    return dict(await asyncio.gather(*(one(u) for u in user_ids)))


# ---------- middleware ----------

@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)

    # The report PDFs are opened with a plain navigation (see
    # _download_tokens below), which can't carry the Authorization header --
    # a one-time token in the query string stands in for it there instead.
    token = request.query.get("token") if request.path.startswith("/api/reports/") else None
    if token:
        admin = _consume_download_token(token)
        if admin is None:
            return _err(401, "Link expired — generate the report again.")
    else:
        auth = request.headers.get("Authorization", "")
        scheme, _, init_data = auth.partition(" ")
        if scheme.lower() != "tma":
            return _err(401, "Open this panel from Telegram.")
        data = parse_init_data(init_data.strip())
        if data is None:
            return _err(401, "Session invalid or expired — reopen the panel from Telegram.")
        user = extract_user(data)
        if user is None:
            return _err(401, "No user in session data.")
        admin = await resolve_admin(user["id"])
        if admin is None:
            return _err(403, "Not authorized.")

    request["admin"] = admin
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        logging.exception("web admin panel: %s %s failed", request.method, request.path)
        return _err(500, "Internal error — check the bot logs.")


def _require_super(request: web.Request) -> web.Response | None:
    if not request["admin"].get("is_super_admin"):
        return _err(403, "Super-admins only.")
    return None


def _int_param(request: web.Request, name: str) -> int:
    try:
        return int(request.match_info[name])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text=f"bad {name}")


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="expected a JSON body")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return data


# ---------- routes ----------

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(_INDEX_HTML, headers={"Cache-Control": "no-cache"})


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def api_me(request: web.Request) -> web.Response:
    return _json({
        "user_id": request["admin"]["user_id"],
        "is_super_admin": bool(request["admin"].get("is_super_admin")),
        "required_per_week": REQUIRED_PER_WEEK,
    })


async def api_groups(request: web.Request) -> web.Response:
    groups = await get_all_groups()
    drivers = await get_all_drivers_by_group()
    last_ptis = await get_last_pti_per_group()
    weekly = await get_weekly_pti_stats()
    titles = {g["group_id"]: g["title"] for g in groups if g.get("title")}
    titles.update(await _chat_titles([g["group_id"] for g in groups if not g.get("title")]))

    out = []
    for g in groups:
        gid = g["group_id"]
        last = last_ptis.get(gid)
        # Non-compliant driver count, so the list can sort, badge and filter
        # "due" groups. Only active, configured groups are eligible: a group
        # with no unit files no PTIs and no reminder covers it, so counting it
        # as overdue would put every un-onboarded group in the same list as the
        # drivers who actually skipped a walkaround.
        due = 0
        if g.get("is_active", True) and g.get("setup_complete"):
            for d in drivers.get(gid, []):
                s = weekly.get((gid, d["user_id"]))
                ok, _ = compliance_verdict(
                    s["week_count"] if s else 0, s["last_at"] if s else None
                )
                if not ok:
                    due += 1
        out.append({
            "group_id": gid,
            "title": titles.get(gid, str(gid)),
            "unit_number": g.get("unit_number"),
            "is_active": g.get("is_active", True),
            "setup_complete": bool(g.get("setup_complete")),
            "notifications_disabled": bool(g.get("notifications_disabled")),
            "post_blocked": bool(g.get("post_blocked")),
            "drivers": drivers.get(gid, []),
            "drivers_due": due,
            "last_pti": last and {"passed": last["passed"], "submitted_at": last["submitted_at"]},
        })
    return _json(out)


async def api_group(request: web.Request) -> web.Response:
    gid = _int_param(request, "gid")
    g = await get_group(gid)
    if not g:
        return _err(404, "Group not found.")

    title, about = await _chat_info(gid, g.get("title"))
    registered = await get_drivers(gid)
    phones, spare_phones = _driver_phones(about, registered)

    drivers = []
    for d in registered:
        count = await get_pti_count_this_week(gid, d["user_id"])
        last = await get_last_pti(gid, d["user_id"])
        ok, reason = compliance_verdict(count, last["submitted_at"] if last else None)
        drivers.append({
            "user_id": d["user_id"],
            "name": d["name"],
            "phone": phones.get(d["user_id"]),
            "ptis_this_week": count,
            "compliant": ok,
            "reason": None if ok else reason,
        })

    return _json({
        "group_id": gid,
        "title": title,
        "spare_phones": spare_phones,
        "unit_number": g.get("unit_number"),
        "is_active": g.get("is_active", True),
        "setup_complete": bool(g.get("setup_complete")),
        "notifications_disabled": bool(g.get("notifications_disabled")),
        "post_blocked": bool(g.get("post_blocked")),
        "drivers": drivers,
    })


async def api_group_ptis(request: web.Request) -> web.Response:
    gid = _int_param(request, "gid")
    ptis = await get_recent_ptis(gid, 20)
    return _json([
        {
            "id": p["id"],
            "submitted_at": p["submitted_at"],
            "passed": p["passed"],
            "severity": p.get("severity"),
            "driver_name": p.get("driver_name") or str(p["user_id"]),
            "unit_number": p.get("unit_number"),
            "result_text": p.get("result_text"),
        }
        for p in ptis
    ])


async def api_group_set_unit(request: web.Request) -> web.Response:
    gid = _int_param(request, "gid")
    unit = normalize_unit(str((await _body(request)).get("unit", "")))
    if not unit:
        return _err(400, "Unit number can't be empty.")
    if not await get_group(gid):
        return _err(404, "Group not found.")
    await set_group_unit(gid, unit)
    logging.info("web panel: admin %s set unit=%r for group %s",
                 request["admin"]["user_id"], unit, gid)
    return _json({"ok": True})


async def api_group_notifications(request: web.Request) -> web.Response:
    gid = _int_param(request, "gid")
    disabled = bool((await _body(request)).get("disabled"))
    await set_group_notifications(gid, disabled)
    logging.info("web panel: admin %s set notifications_disabled=%s for group %s",
                 request["admin"]["user_id"], disabled, gid)
    return _json({"ok": True})


async def api_group_active(request: web.Request) -> web.Response:
    gid = _int_param(request, "gid")
    active = bool((await _body(request)).get("active"))
    await set_group_active(gid, active)
    logging.info("web panel: admin %s set is_active=%s for group %s",
                 request["admin"]["user_id"], active, gid)
    return _json({"ok": True})


async def api_remove_driver(request: web.Request) -> web.Response:
    gid = _int_param(request, "gid")
    uid = _int_param(request, "uid")
    removed = await remove_driver(gid, uid)
    logging.info("web panel: admin %s removed driver %s from group %s (found=%s)",
                 request["admin"]["user_id"], uid, gid, removed)
    return _json({"ok": True, "removed": removed})


# ---------- driver picking ----------
# The panel's answer to "this group has the wrong driver registered", and the
# way an un-onboarded group gets configured without waiting for a DM prompt.
# It reads the same roster the onboarding picker does, but presents it as a
# *searchable* list rather than a page of buttons: a search box has no room to
# run out of, so nothing here is hidden or capped.


async def api_group_members(request: web.Request) -> web.Response:
    """The group's roster, flagged for who is already a driver.

    Degrades exactly like the onboarding prompt: no session, no membership or a
    refused participant list all yield available=false plus a reason, never an
    error. The caller falls back to entering a user id by hand.
    """
    gid = _int_param(request, "gid")
    if not await get_group(gid):
        return _err(404, "Group not found.")
    if not userbot.is_configured():
        return _json({"available": False, "members": [], "reason":
                      "Member lookup isn't configured, so member lists are "
                      "unavailable."})

    roster = [m for m in await userbot.list_members(gid) if not m.is_bot]
    if not roster:
        return _json({"available": False, "members": [], "reason":
                      "No member list — the bot is probably not in this group."})

    driver_ids = {d["user_id"] for d in await get_drivers(gid)}
    non_drivers = await get_non_driver_ids()
    members = [{
        "user_id": m.user_id,
        "name": m.name or "",
        "username": m.username,
        "label": m.label,
        "is_driver": m.user_id in driver_ids,
        "is_non_driver": m.user_id in non_drivers,
    } for m in roster]
    # Known non-drivers are shown here, not hidden. Hiding them is what leaves
    # the Telegram picker with nothing to tap; in a list you search by name,
    # being able to find the person is the whole point. They sort last and
    # carry a badge instead.
    members.sort(key=lambda m: (m["is_non_driver"], m["label"].lower()))
    return _json({"available": True, "members": members, "reason": None})


def _driver_name(body: dict) -> str:
    return str(body.get("name") or "").strip()


async def api_add_driver(request: web.Request) -> web.Response:
    gid = _int_param(request, "gid")
    body = await _body(request)
    try:
        uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        return _err(400, "user_id must be a numeric Telegram id.")
    name = _driver_name(body)
    if not name:
        return _err(400, "Driver name can't be empty.")
    if not await get_group(gid):
        return _err(404, "Group not found.")
    if not await add_driver(gid, uid, name):
        return _err(400, "That person is already a driver in this group.")
    # Being chosen as a driver outranks a stale fleet-wide "not a driver" row,
    # exactly as it does on the onboarding Save path. Nobody is marked as a
    # non-driver from here: searching for one person is not a judgement on
    # everyone else in the roster.
    await unmark_non_drivers([uid])
    logging.info("web panel: admin %s added driver %s (%r) to group %s",
                 request["admin"]["user_id"], uid, name, gid)
    return _json({"ok": True})


async def api_rename_driver(request: web.Request) -> web.Response:
    gid = _int_param(request, "gid")
    uid = _int_param(request, "uid")
    name = _driver_name(await _body(request))
    if not name:
        return _err(400, "Driver name can't be empty.")
    changed = await set_driver_names([(gid, uid, name)])
    if not changed and not any(d["user_id"] == uid for d in await get_drivers(gid)):
        return _err(404, "That driver isn't registered in this group.")
    logging.info("web panel: admin %s renamed driver %s in group %s to %r",
                 request["admin"]["user_id"], uid, gid, name)
    # changed=False also means "the name already said that", which is a no-op
    # rather than a failure — the caller only needs to know it went through.
    return _json({"ok": True, "changed": bool(changed)})


async def api_replace_driver(request: web.Request) -> web.Response:
    """Swap one registered driver for another member of the same group."""
    gid = _int_param(request, "gid")
    old_uid = _int_param(request, "uid")
    body = await _body(request)
    try:
        new_uid = int(body.get("user_id"))
    except (TypeError, ValueError):
        return _err(400, "user_id must be a numeric Telegram id.")
    name = _driver_name(body)
    if not name:
        return _err(400, "Driver name can't be empty.")

    # Swapping onto someone who already drives here would delete one row and
    # update another, quietly turning two drivers into one.
    if new_uid != old_uid and any(d["user_id"] == new_uid
                                  for d in await get_drivers(gid)):
        return _err(400, "That person is already a driver in this group.")
    if not await swap_driver(gid, old_uid, new_uid, name):
        return _err(404, "That driver isn't registered here — reload the panel.")
    await unmark_non_drivers([new_uid])
    logging.info("web panel: admin %s replaced driver %s with %s (%r) in group %s",
                 request["admin"]["user_id"], old_uid, new_uid, name, gid)
    return _json({"ok": True})


async def api_model(request: web.Request) -> web.Response:
    return _json({
        "active": get_active_model(),
        "available": [{"id": m, "hint": MODEL_HINTS.get(m, "")} for m in AVAILABLE_GEMINI_MODELS],
    })


async def api_model_set(request: web.Request) -> web.Response:
    if resp := _require_super(request):
        return resp
    model = str((await _body(request)).get("model", ""))
    if not set_active_model(model):
        return _err(400, "Unknown model.")
    await set_setting("gemini_model", model)  # persist across restarts
    logging.info("web panel: admin %s switched Gemini model to %s",
                 request["admin"]["user_id"], model)
    return _json({"ok": True, "active": model})


async def api_admins(request: web.Request) -> web.Response:
    if resp := _require_super(request):
        return resp
    admins = await get_admins()
    cards = await _user_cards([a["user_id"] for a in admins])
    return _json([
        {"user_id": a["user_id"],
         "is_super_admin": bool(a.get("is_super_admin")),
         **cards[a["user_id"]]}
        for a in admins
    ])


async def api_admins_add(request: web.Request) -> web.Response:
    if resp := _require_super(request):
        return resp
    try:
        uid = int((await _body(request)).get("user_id"))
    except (TypeError, ValueError):
        return _err(400, "user_id must be a numeric Telegram id.")
    await add_admin(uid, is_super_admin=False)
    logging.info("web panel: super-admin %s added admin %s", request["admin"]["user_id"], uid)
    return _json({"ok": True})


async def api_admins_remove(request: web.Request) -> web.Response:
    if resp := _require_super(request):
        return resp
    uid = _int_param(request, "uid")
    removed = await remove_admin(uid)  # refuses super-admins
    if not removed:
        return _err(400, "Not removable (unknown id or super-admin).")
    logging.info("web panel: super-admin %s removed admin %s", request["admin"]["user_id"], uid)
    return _json({"ok": True})


async def api_broadcast(request: web.Request) -> web.Response:
    text = str((await _body(request)).get("text", "")).strip()
    if not text:
        return _err(400, "Message can't be empty.")
    sent = failed = 0
    for gid in await get_active_group_ids():
        try:
            await bot.send_message(gid, escape(text))  # sent as plain text
            sent += 1
            await note_send_ok(gid)
        except Exception as e:
            failed += 1
            # A broadcast touches every group at once, so it is the cheapest
            # sweep there is for "which of these can the bot still post in".
            await note_send_failure(gid, e)
            logging.exception("web panel: broadcast to %s failed", gid)
    logging.info("web panel: admin %s broadcast to %s groups (%s failed)",
                 request["admin"]["user_id"], sent, failed)
    return _json({"ok": True, "sent": sent, "failed": failed})


# ---------- fleet report PDFs ----------
# scripts/fleet_report.py is also a standalone CLI (kept free of `utils` so it
# never needs a bot token to print a PDF); this just calls its pure pieces
# directly instead of shelling out, since the web panel already has every
# credential it needs.
#
# The admin always picks an explicit range -- there is no "last week"/"last 7
# days" shortcut. Those covered a fraction of what an arbitrary range does and
# just added a second control to read, so the panel keeps one: two dates.

def _slug(name: str) -> str:
    """A company name safe to put in a download filename. FLEET_NAME is free
    text an operator typed, so it can hold spaces -- or a quote, which would
    end the filename early inside the Content-Disposition header."""
    out = "".join(c if c.isalnum() else "-" for c in (name or "").strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "fleet"


# One Chromium at a time. A render is a whole browser process on a small
# container, and two admins (or one impatient one) tapping both report buttons
# at once is exactly how it runs out of memory mid-inspection.
_render_lock = asyncio.Semaphore(1)


# ---------- one-time download tokens ----------
# The report buttons used to fetch the PDF as a blob and trigger the save with
# a synthetic <a download> click -- reliable on desktop, but on a phone that
# click lands inside the sandboxed WebView the Mini App itself runs in, which
# has nowhere to put a downloaded file. `tg.openLink()` hands the URL to
# Telegram's own in-app browser instead, a real one with a working download
# flow -- but that is a plain navigation with no custom header, so the
# Authorization header can't ride along. This token stands in for it on that
# one GET: minted from an already-authenticated call, single-use, and expiring
# almost immediately, so it is not the standing credential initData is.
_DOWNLOAD_TOKEN_TTL = 120
_download_tokens: dict[str, tuple[float, dict]] = {}


def _mint_download_token(admin: dict) -> str:
    now = time.monotonic()
    for k, (expires, _) in list(_download_tokens.items()):
        if now > expires:
            del _download_tokens[k]
    token = secrets.token_urlsafe(24)
    _download_tokens[token] = (now + _DOWNLOAD_TOKEN_TTL, admin)
    return token


def _consume_download_token(token: str) -> dict | None:
    entry = _download_tokens.pop(token, None)
    if entry is None:
        return None
    expires, admin = entry
    return admin if time.monotonic() <= expires else None


async def api_report_token(request: web.Request) -> web.Response:
    return _json({"token": _mint_download_token(request["admin"])})


async def _report_pdf(which: str, since_s: str, until_s: str) -> tuple[bytes, str]:
    tz = _TZ
    since = date.fromisoformat(since_s)
    # `until_s` is the last day the admin picked, inclusive -- one day is
    # added here so the rest of the pipeline can keep working with a
    # half-open [since, until) range, same as --since/--until on the CLI.
    until = date.fromisoformat(until_s) + timedelta(days=1)
    if until <= since:
        raise ValueError("the end date must be on or after the start date")
    since_utc = datetime.combine(since, datetime.min.time(), tz).astimezone(
        timezone.utc).replace(tzinfo=None)
    until_utc = datetime.combine(until, datetime.min.time(), tz).astimezone(
        timezone.utc).replace(tzinfo=None)

    data = await _report.fetch(DATABASE_URL, since_utc, until_utc)
    agg = _report.build(data, tz, since, until)
    meta = {
        # FLEET_NAME is the company, and it is printed as the wordmark at the
        # top of both sheets -- so `scope` says what the document is instead
        # of repeating the name back on the line underneath it.
        "fleet": _report.display_name(FLEET_NAME),
        "scope": "Pre-trip inspection compliance",
        "since": since, "until": until,
        "pulled": datetime.now(tz).date(),
        "tz": FLEET_TZ,
        "title": f"{FLEET_NAME} fleet inspection statistics",
        "title_d": f"{FLEET_NAME} driver inspection report",
    }
    html_text = (_report.stats_html(agg, meta) if which == "stats"
                else _report.driver_html(agg, meta))

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.pdf"
        # Shells out to headless Chromium; keep it off the event loop.
        async with _render_lock:
            await asyncio.to_thread(_report.to_pdf, html_text, out)
        pdf_bytes = out.read_bytes()

    tag = f"{_slug(FLEET_NAME)}-{until - timedelta(days=1):%Y%m%d}"
    fname = f"{tag}.pdf" if which == "stats" else f"{tag}-driver-report.pdf"
    return pdf_bytes, fname


async def api_report_pdf(request: web.Request) -> web.Response:
    which = request.match_info["which"]
    if which not in ("stats", "driver"):
        return _err(404, "Unknown report.")
    since_s = request.query.get("since")
    until_s = request.query.get("until")
    if not (since_s and until_s):
        return _err(400, "Pick a start and end date.")
    try:
        pdf_bytes, fname = await _report_pdf(which, since_s, until_s)
    except ValueError as e:
        return _err(400, str(e) or "Bad date range.")
    except SystemExit as e:
        # to_pdf raises this (not a normal Exception) when no Chromium binary
        # is on the box -- a CLI-style error the panel has to translate.
        return _err(500, str(e) or "PDF rendering isn't available on this server.")
    logging.info("web panel: admin %s generated the %s report (%s to %s)",
                 request["admin"]["user_id"], which, since_s, until_s)
    return web.Response(
        body=pdf_bytes, content_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/groups", api_groups)
    app.router.add_get("/api/groups/{gid}", api_group)
    app.router.add_get("/api/groups/{gid}/ptis", api_group_ptis)
    app.router.add_post("/api/groups/{gid}/unit", api_group_set_unit)
    app.router.add_post("/api/groups/{gid}/notifications", api_group_notifications)
    app.router.add_post("/api/groups/{gid}/active", api_group_active)
    app.router.add_get("/api/groups/{gid}/members", api_group_members)
    app.router.add_post("/api/groups/{gid}/drivers", api_add_driver)
    app.router.add_post("/api/groups/{gid}/drivers/{uid}/name", api_rename_driver)
    app.router.add_post("/api/groups/{gid}/drivers/{uid}/replace", api_replace_driver)
    app.router.add_delete("/api/groups/{gid}/drivers/{uid}", api_remove_driver)
    app.router.add_get("/api/model", api_model)
    app.router.add_post("/api/model", api_model_set)
    app.router.add_get("/api/admins", api_admins)
    app.router.add_post("/api/admins", api_admins_add)
    app.router.add_delete("/api/admins/{uid}", api_admins_remove)
    app.router.add_post("/api/broadcast", api_broadcast)
    app.router.add_post("/api/reports/token", api_report_token)
    app.router.add_get("/api/reports/{which}.pdf", api_report_pdf)
    return app


async def start_webapp() -> None:
    """Start serving the panel; called once from on_startup."""
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT).start()
    logging.info("web admin panel listening on port %s", WEBAPP_PORT)
