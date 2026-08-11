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
- **`utils/gemini.py`** — the low-level Gemini/ffmpeg functions (`extract_frames`,
  `call_gemini`, `call_gemini_photos`, `parse_result`), the model registry and the
  API-key failover, plus a CLI for manually checking a single video:
  `python -m utils.gemini <video.mp4>` (needs a Gemini key).
- **`utils/scheduler.py` + `utils/enforcement.py`** — hourly compliance loop.
  **The bot never restricts a driver** (see Conventions).
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
- **`utils/group_activity.py` + `middlewares/group_activity.py`** — the derived
  "is this group still in use?" status (below).

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
- **The bot never restricts, mutes or otherwise silences a driver.** Overdue
  compliance is answered with a reminder in the group and a summary to admins —
  never by taking away someone's ability to post. `utils/enforcement.py` has no
  `mute_driver()` and no muted-permission set, and a test asserts they stay
  absent; `unmute_driver()` exists only to *lift* restrictions left over from
  before this rule. `ENFORCEMENT_ENABLED` only toggles the reminders. Don't
  reintroduce muting behind a config flag.

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

Saving also records everyone who was on screen and *not* picked as a fleet-wide
non-driver (`non_drivers`), so dispatchers and safety staff stop being offered in
the next group. That exclusion is global, so it is kept reversible three ways:
picking someone as a driver clears their row, a "Show N hidden" button reveals
them for one prompt, and `/nondrivers clear` empties the table. Someone hidden is
never swept into a fresh non-driver decision — only people actually displayed
count as "passed over".

The unit guess is also checked against `active_units` before being offered, so a
title naming a retired truck reads as "not found". An admin refreshes that list
weekly with `/units …` (replaced wholesale); an empty table disables the check
rather than rejecting every unit.

`/adddriver` and `/setunit` still work as a manual escape hatch; they are simply
not advertised to the group any more.

**One session per host.** Telegram revokes an authorization key seen from two IP
addresses at once (`AuthKeyDuplicatedError`) and *both* copies die — this took
out member lookup on 2026-08-09, when the session deployed to Railway was also
used by a local script. `~/.pti-tg/fleet_audit` is for local tooling,
`~/.pti-tg/bot_userbot` is for Railway, and they must never be the same file.
Create one with `scripts/tg_login.py --name <n>`; recovery from a revoked key
means moving the dead `.session` aside (Telethon retries it instead of
prompting) and logging in again.

Telethon also cannot address a chat by bare id on a fresh session — the access
hash is only learned by walking the dialog list, so `get_entity(-100…)` raises
`ValueError` and member lookup silently returns `[]`. `utils/userbot.py` warms
the dialog cache once on the first unresolved chat; don't remove that.

The userbot is strictly read-only (never sends, joins, leaves or edits) and
connects lazily, so a bot that never onboards a group never opens the session.
Every failure path degrades — a missing session, an unauthorized account, or a
group the account is not in all yield "no member buttons", not an exception.
`TELEGRAM_SESSION` is full access to the account it was made from: use a
dedicated account and move it with `scripts/tg_session_to_railway.py`, which
never prints the value.

## Active vs. dormant groups

Two different things, deliberately kept apart:

- **`groups.is_active`** is an administrative switch (the panel's
  Deactivate/Reactivate, `mark_unreachable`'s strike limit). It is a poor
  measure of whether a truck's chat is in use — a send failure once flipped it
  FALSE across the fleet, and 19 of 30 "inactive" groups still had the bot in
  them.
- **Dormant** is derived from traffic: no message from a *human* in
  `GROUP_INACTIVE_DAYS` days (env, default **3**), the same rule
  `scripts/tg_scan.py` / `tg_rehome.py --stale-days` already use offline.
  `middlewares/group_activity.py` stamps `groups.last_human_message_at` from
  live updates — the bot's own results and reminders don't count (they'd make
  every nagged group look alive), nor do join/leave/pin service messages, but an
  anonymous admin does (`from_user` is GroupAnonymousBot **with** `sender_chat`).
  Writes are throttled to one per group per 5 min. `utils/group_activity.py`
  holds the pure "how old is too old" half.

Dormancy is a **reporting** status — the admin panel and web panel show 🌙 and a
"quiet 6d" chip, and stats count live/dormant separately. Two rules:

- It never writes `is_active`. Deactivation stays a human decision; this exists
  precisely because an automatic flag got it wrong before.
- It never gates a reminder or a broadcast. A group with no human message for
  three days is exactly the one the overdue reminder is for — filtering on it
  would silence the drivers who most need nudging. For the same reason dormant
  groups stay in the compliance denominator: a silent truck is a missing
  inspection, not a group to hide.

On first deploy the column is backfilled from `MAX(pti_log.submitted_at)` (a
submitted inspection is a human message). A group that has never spoken falls
back to `created_at`, so one added this morning reads as alive rather than dead.

## Behavior under high load

`PTI_MAX_CONCURRENCY` (env, default **3**) caps how many inspections run
concurrently. Each inspection is CPU-heavy (ffmpeg) and uses a worker thread +
Gemini quota, so an unbounded burst could exhaust threads/memory or trip rate
limits. Excess submissions queue on an `asyncio.Semaphore` in
`pti_processor._get_analysis_slot()` and show the driver a "queued" notice
instead of piling on. Gemini calls also retry with backoff and surface a friendly
"overloaded" message on 5xx/429. Tune `PTI_MAX_CONCURRENCY` up only if the host
has CPU/memory headroom.
