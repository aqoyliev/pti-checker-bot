# pti-checker-bot

A Telegram bot (aiogram 2.x) that runs AI **pre-trip inspections (PTI)** on
truck/trailer photos and videos. Media is sampled into frames with `ffmpeg`,
sent to **Google Gemini**, and the structured verdict (PASS/FAIL + severity +
issues) is posted back into the group.

## Architecture

- **`app.py`** — entrypoint; `executor.start_polling`. `on_startup` inits the DB,
  sets bot commands, and launches the background loops.
- **`loader.py`** — the shared `bot` and `dp` (FSM uses in-memory storage).
- **`data/config.py`** — all env config (read via `environs`). See `.env.example`.
- **`handlers/`** — aiogram handlers grouped by chat type (`groups/`, `users/`,
  `admin/`, `channels/`, `errors/`). Handlers register via import side effects
  (`handlers/__init__.py` etc.), so the unused-import "warnings" there are intentional.
- **`utils/db.py`** — the asyncpg pool **and every DB query**. Add new queries here
  as helper functions; don't inline SQL in handlers.
- **`utils/pti_processor.py`** — the media → frames → Gemini → formatted-result
  pipeline. Includes Gemini retry/backoff, "service overloaded" handling, a
  hallucination filter, and the concurrency gate (below).
- **`test_pti.py`** — the low-level Gemini/ffmpeg functions (`extract_frames`,
  `call_gemini`, `call_gemini_photos`, `parse_result`) plus a CLI for manually
  checking a single video: `python test_pti.py <video.mp4>` (needs a Gemini key).
- **`utils/scheduler.py` + `utils/enforcement.py`** — hourly compliance loop.
- **`webapp/`** — the web admin panel (Telegram Mini App). `server.py` is an
  aiohttp app started from `on_startup` (listens on `PORT`/`WEBAPP_PORT`,
  default 8080); `auth.py` validates the Mini App's signed `initData` (admins =
  env `ADMINS` ∪ admins table, same as the inline panel); `static/index.html`
  is the whole UI. `/admin` shows an "Open Web Panel" button once `WEBAPP_URL`
  (public HTTPS URL) is set.
- **`handlers/groups/proposals.py`** — group voting/proposal flow + a "nag" loop
  for still-unconfigured groups (the nag re-sends the onboarding prompt to
  admins in DM; it does not message the group).
- **`handlers/admin/onboard.py`** — admin-driven group onboarding (below), plus
  `/onboard <group_id>` to re-open the prompt for a group.
- **`utils/unit_parse.py`** — group title/description → unit-number *guess*.
- **`utils/userbot.py`** — read-only Telethon *user* session. It exists for the
  one thing the Bot API cannot do: list a group's members.
- **`middlewares/throttling.py`** — anti-flood for text messages.

The **live PTI path** is `handlers/groups/pti.py` → `pti_processor.process_mixed_media`.
The other `process_*` functions in `pti_processor.py` are legacy/unused.

## Runtime requirements

- `ffmpeg` (+ `ffprobe`) on PATH — used to extract video frames.
- PostgreSQL via `DATABASE_URL`.
- `GEMINI_API_KEY`.
- Optional local [Bot API server](https://github.com/tdlib/telegram-bot-api) via
  `LOCAL_SERVER_URL` to lift the 20 MB file limit (the Dockerfile builds this).
- Optional Telethon *user* session for the onboarding member picker:
  `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` (shared with the local Bot API server)
  and `TELEGRAM_SESSION`. Without them onboarding still runs, just with no
  member buttons. See "Group onboarding" below.

## Dev workflow

```bash
pip install -r requirements.txt -r requirements-dev.txt   # ffmpeg must be installed too
ruff check .        # lint (config in ruff.toml — conservative: real errors only)
pytest              # unit tests in tests/ (pure functions; no secrets/network needed)
python app.py       # run the bot (needs a populated .env)
docker compose up   # or run the full stack (bot + local Bot API server)
```

> On Claude Code on the web, `.claude/hooks/session-start.sh` installs all of the
> above automatically at session start.

`tests/conftest.py` sets dummy env vars so importing the bot modules doesn't
require real secrets. Keep new unit tests pure (no network / no DB).

## Conventions

- Everything is `async`. Offload blocking work (ffmpeg, Gemini SDK) with
  `asyncio.to_thread`.
- All Telegram messages use HTML parse mode — **escape** any user/model text
  (`format_result` uses `html.escape`).
- Route DB access through `utils/db.py` helpers.

## Group onboarding

**Drivers are never asked to register or configure anything.** When the bot is
added to a group it posts only an intro (what it does, how `/check` works) and
then works the setup out on its own:

1. guess the unit from the chat **title**, falling back to the **description**;
2. read the member roster through `utils/userbot.py`;
3. DM the admins the title, the About text, the unit guess *and where it came
   from*, and one button per member.

The admin taps the drivers (this is how their `user_id` is captured), confirms
the unit and presses Save. **Nothing is written to the DB until Save.**

Three rules that are easy to undo by accident:

- **The parsed unit is a suggestion, never a value.** Measured across the 158
  groups whose `unit_number` was already known, a naive digit-run regex scored
  79.5% — and six titles yielded a *different valid unit* rather than nothing. A
  wrong unit silently misattributes inspections, so a human confirms it. Do not
  wire `parse_unit` straight into `set_group_unit`.
- **Descriptions are parsed more strictly than titles** — labelled forms only
  (`UNIT 1216`, `TRUCK# 147085`, `SUB x // y`). About text is free prose, where
  the title's bare-leading-number rule would read a phone number, a street
  address or "Established 2019" as a unit.
- **No admin reachable ⇒ silence.** A bot cannot open a DM with someone who
  never started it. `start_onboarding` returns `False` in that case and the
  group is deliberately left alone rather than being asked to run `/setunit` —
  it stays unconfigured and surfaces via the setup nag or `/onboard <group_id>`.

`/adddriver` and `/setunit` still work as a manual escape hatch; they are simply
not advertised to the group any more.

The userbot is strictly read-only (never sends, joins, leaves or edits) and
connects lazily, so a bot that never onboards a group never opens the session.
Every failure path degrades — a missing session, an unauthorized account, or a
group the account is not in all yield "no member buttons", not an exception.
`TELEGRAM_SESSION` is full access to the account it was made from: use a
dedicated account and move it with `scripts/tg_session_to_railway.py`, which
never prints the value.

## Behavior under high load

`PTI_MAX_CONCURRENCY` (env, default **3**) caps how many inspections run
concurrently. Each inspection is CPU-heavy (ffmpeg) and uses a worker thread +
Gemini quota, so an unbounded burst could exhaust threads/memory or trip rate
limits. Excess submissions queue on an `asyncio.Semaphore` in
`pti_processor._get_analysis_slot()` and show the driver a "queued" notice
instead of piling on. Gemini calls also retry with backoff and surface a friendly
"overloaded" message on 5xx/429. Tune `PTI_MAX_CONCURRENCY` up only if the host
has CPU/memory headroom.
