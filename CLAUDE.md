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
- **`handlers/groups/proposals.py`** — group voting/proposal flow + a "nag" loop.
- **`middlewares/throttling.py`** — anti-flood for text messages.

The **live PTI path** is `handlers/groups/pti.py` → `pti_processor.process_mixed_media`.
The other `process_*` functions in `pti_processor.py` are legacy/unused.

## Runtime requirements

- `ffmpeg` (+ `ffprobe`) on PATH — used to extract video frames.
- PostgreSQL via `DATABASE_URL`.
- `GEMINI_API_KEY`.
- Optional local [Bot API server](https://github.com/tdlib/telegram-bot-api) via
  `LOCAL_SERVER_URL` to lift the 20 MB file limit (the Dockerfile builds this).

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

## Behavior under high load

`PTI_MAX_CONCURRENCY` (env, default **3**) caps how many inspections run
concurrently. Each inspection is CPU-heavy (ffmpeg) and uses a worker thread +
Gemini quota, so an unbounded burst could exhaust threads/memory or trip rate
limits. Excess submissions queue on an `asyncio.Semaphore` in
`pti_processor._get_analysis_slot()` and show the driver a "queued" notice
instead of piling on. Gemini calls also retry with backoff and surface a friendly
"overloaded" message on 5xx/429. Tune `PTI_MAX_CONCURRENCY` up only if the host
has CPU/memory headroom.
