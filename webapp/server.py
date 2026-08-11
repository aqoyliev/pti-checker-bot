"""aiohttp server for the web admin panel (Telegram Mini App).

Runs inside the bot process (started from ``app.py:on_startup``) and exposes the
same capabilities as the inline /admin panel, as JSON under ``/api/*`` plus the
single-page UI at ``/``. Every API request must carry the Mini App's signed
``initData`` in an ``Authorization: tma <initData>`` header; webapp/auth.py
validates the signature and resolves the user to an admin, so there is no
separate login. aiohttp is already a dependency (aiogram runs on it).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from functools import partial
from html import escape
from pathlib import Path

from aiohttp import web

from data.config import WEBAPP_PORT
from loader import bot
from utils.db import (
    add_admin,
    get_active_group_ids,
    get_admins,
    get_all_drivers_by_group,
    get_all_groups,
    get_drivers,
    get_group,
    get_last_pti,
    get_last_pti_per_group,
    get_pti_count_this_week,
    get_recent_ptis,
    get_weekly_pti_stats,
    normalize_unit,
    remove_admin,
    remove_driver,
    set_group_active,
    set_group_notifications,
    set_group_title,
    set_group_unit,
    set_setting,
)
from utils.enforcement import REQUIRED_PER_WEEK, compliance_verdict
from utils.gemini import AVAILABLE_GEMINI_MODELS, get_active_model, set_active_model
from webapp.auth import extract_user, parse_init_data, resolve_admin

_INDEX_HTML = Path(__file__).parent / "static" / "index.html"

# Same admin-facing model hints as the inline panel.
MODEL_HINTS = {
    "gemini-2.5-pro": "2.5 — most accurate, slowest",
    "gemini-2.5-flash": "2.5 — faster, cheaper",
    "gemini-2.5-flash-lite": "2.5 — fastest, cheapest",
    "gemini-3-pro-preview": "3.0 Pro (preview)",
    "gemini-3-flash-preview": "3.0 Flash (preview)",
    "gemini-3.1-pro-preview": "3.1 Pro (preview)",
    "gemini-3.1-flash-lite": "3.1 Flash-Lite",
    "gemini-3.5-flash": "3.5 Flash — newest, lots of capacity",
}

_dumps = partial(json.dumps, default=str)  # asyncpg rows carry datetimes


def _json(payload, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=_dumps)


def _err(status: int, message: str) -> web.Response:
    return _json({"error": message}, status=status)


# ---------- chat-title cache ----------
# Titles live in groups.title (kept fresh by the group_title middleware), so
# listing groups normally needs no Telegram calls. get_chat only fills gaps —
# groups with no stored title yet — and its result is persisted; the short
# in-memory cache just absorbs rapid re-fetches (including failures for dead
# group ids).

_TITLE_TTL = 600.0
_title_cache: dict[int, tuple[float, str]] = {}


async def _chat_title(group_id: int, stored: str | None = None) -> str:
    hit = _title_cache.get(group_id)
    if hit and time.monotonic() - hit[0] < _TITLE_TTL:
        return hit[1]
    try:
        chat = await bot.get_chat(group_id)
        title = chat.title or str(group_id)
        if title != stored:
            try:
                await set_group_title(group_id, title)
            except Exception:
                logging.exception("web panel: failed to store title for %s", group_id)
    except Exception:
        title = stored or str(group_id)
    _title_cache[group_id] = (time.monotonic(), title)
    return title


async def _chat_titles(group_ids: list[int]) -> dict[int, str]:
    sem = asyncio.Semaphore(8)

    async def one(gid: int) -> tuple[int, str]:
        async with sem:
            return gid, await _chat_title(gid)

    return dict(await asyncio.gather(*(one(g) for g in group_ids)))


# ---------- middleware ----------

@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)

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
        # Non-compliant driver count, so the list can sort/badge "due" groups.
        # Same eligibility rule as api_stats: only active, configured groups.
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
            "truck_plate": g.get("truck_plate"),
            "trailer_unit": g.get("trailer_unit"),
            "trailer_plate": g.get("trailer_plate"),
            "is_active": g.get("is_active", True),
            "setup_complete": bool(g.get("setup_complete")),
            "notifications_disabled": bool(g.get("notifications_disabled")),
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

    drivers = []
    for d in await get_drivers(gid):
        count = await get_pti_count_this_week(gid, d["user_id"])
        last = await get_last_pti(gid, d["user_id"])
        ok, reason = compliance_verdict(count, last["submitted_at"] if last else None)
        drivers.append({
            "user_id": d["user_id"],
            "name": d["name"],
            "ptis_this_week": count,
            "compliant": ok,
            "reason": None if ok else reason,
        })

    return _json({
        "group_id": gid,
        "title": await _chat_title(gid, g.get("title")),
        "unit_number": g.get("unit_number"),
        "truck_plate": g.get("truck_plate"),
        "trailer_unit": g.get("trailer_unit"),
        "trailer_plate": g.get("trailer_plate"),
        "is_active": g.get("is_active", True),
        "setup_complete": bool(g.get("setup_complete")),
        "notifications_disabled": bool(g.get("notifications_disabled")),
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


async def api_stats(request: web.Request) -> web.Response:
    groups = await get_all_groups()
    drivers_by_group = await get_all_drivers_by_group()
    weekly = await get_weekly_pti_stats()
    active = [g for g in groups if g.get("is_active", True)]
    setup_done = [g for g in active if g.get("setup_complete")]

    total_drivers = compliant = 0
    non_compliant = []
    for g in setup_done:
        gid = g["group_id"]
        for d in drivers_by_group.get(gid, []):
            total_drivers += 1
            s = weekly.get((gid, d["user_id"]))
            ok, reason = compliance_verdict(
                s["week_count"] if s else 0, s["last_at"] if s else None
            )
            if ok:
                compliant += 1
            else:
                non_compliant.append({
                    "group_id": gid,
                    "unit": g.get("unit_number") or str(gid),
                    "name": d["name"],
                    "reason": reason,
                })

    return _json({
        "groups_total": len(groups),
        "groups_active": len(active),
        "groups_configured": len(setup_done),
        "groups_inactive": len(groups) - len(active),
        "drivers_total": total_drivers,
        "drivers_compliant": compliant,
        "required_per_week": REQUIRED_PER_WEEK,
        "non_compliant": non_compliant,
    })


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
    return _json([
        {"user_id": a["user_id"], "is_super_admin": bool(a.get("is_super_admin"))}
        for a in await get_admins()
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
            await bot.send_message(gid, escape(text))  # plain text, like the inline panel
            sent += 1
        except Exception:
            failed += 1
            logging.exception("web panel: broadcast to %s failed", gid)
    logging.info("web panel: admin %s broadcast to %s groups (%s failed)",
                 request["admin"]["user_id"], sent, failed)
    return _json({"ok": True, "sent": sent, "failed": failed})


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
    app.router.add_delete("/api/groups/{gid}/drivers/{uid}", api_remove_driver)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/model", api_model)
    app.router.add_post("/api/model", api_model_set)
    app.router.add_get("/api/admins", api_admins)
    app.router.add_post("/api/admins", api_admins_add)
    app.router.add_delete("/api/admins/{uid}", api_admins_remove)
    app.router.add_post("/api/broadcast", api_broadcast)
    return app


async def start_webapp() -> None:
    """Start serving the panel; called once from on_startup."""
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT).start()
    logging.info("web admin panel listening on port %s", WEBAPP_PORT)
