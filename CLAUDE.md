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
  (public HTTPS URL) is set. It is also where a group gets **configured by
  hand** — unit, plus drivers picked by searching the roster (below).
- **`handlers/groups/proposals.py`** — the "nag" loop for still-unconfigured
  groups (the nag re-sends the onboarding prompt to admins in DM; it does not
  message the group). It also still holds the 3-vote proposal flow, which is
  **no longer reached** — vehicle changes are decided from the video (below).
- **`handlers/admin/onboard.py`** — admin-driven group onboarding (below), plus
  `/onboard <group_id>` to re-open the prompt for a group.
- **`utils/unit_parse.py`** — group title/description → unit-number *guess*.
- **`utils/driver_names.py`** — the fleet's driver name: parsing it out of a
  group's About text, and pairing it to a registered `user_id` (`/fixnames`,
  `handlers/admin/names.py`).
- **`utils/userbot.py`** — read-only Telethon *user* session. It exists for the
  one thing the Bot API cannot do: list a group's members.
- **`utils/phone_lookup.py`** — a *second*, write-capable user session: phone
  number → account (`/whois`, `scripts/tg_phone_lookup.py`). Separate account on
  purpose (below).
- **`middlewares/throttling.py`** — anti-flood for text messages.
- **`utils/group_activity.py` + `middlewares/group_activity.py`** — the derived
  "has this group gone quiet?" report (below).

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
- **One reminder per unit per 24 hours**, whatever kind it is. Both loops run
  hourly, so the cap lives in the data, not in the cadence: `groups.last_reminder_at`
  is stamped by every sender and checked through `reminder_logic.may_remind`. The
  cap is per *unit* — two overdue drivers share one message naming both, not one
  each. A fresh PTI still clears the overdue state inside that window (`reset`
  writes nothing to the chat), and the admin report is not a reminder: it still
  lists every overdue driver every pass.
- **A `MigrateToChat` is a move, not a failure.** When a basic group is upgraded
  to a supergroup Telegram issues a brand-new chat id, and it is not just the
  reminders that break: a PTI posted in the new chat finds no `groups` row, so
  `_group_ready` refuses it *silently*. `db.migrate_group_id` moves the whole
  history in one transaction (`handlers/groups/registration.on_chat_migrated`
  catches the service message; the two hourly senders catch the exception in
  case that update was missed). Neither sender re-sends after migrating: every
  stamp the caller writes is keyed on the id that just moved, so the send waits
  for the next pass rather than going out with its 24-hour slot unstamped. It
  must never reach the unreachable/deactivate path — the chat moved, it is live.
- **A dropped Gemini upload comes back as a 400, and is still transient.**
  `Upload has already been terminated.` means the resumable session died
  mid-transfer, not that the file was refused. `_upload_one` retries it (a long
  PTI is hundreds of frames, 8 at a time, so one dropped session must not sink
  the inspection) and `pti_processor._is_transient` counts it, so an exhausted
  retry fails over to the next API key and shows the try-again message instead
  of printing raw API JSON into the driver's group. Don't fold it back into a
  plain 400.
- **There is no calendar-based "nudge" reminder — only the overdue one.** A
  twice-weekly nudge (#8, `decide_weekly`) used to fire every Monday and Thursday
  at 14:00 UTC regardless of same-day activity, so a driver who had already
  submitted a PTI that morning still got told to "please send your PTI video"
  that afternoon. Removed 2026-08-20 after exactly that complaint. The only
  reminder left in `utils/reminders.py` is the #9 overdue escalation, which is
  driven off the *actual* last PTI (`get_last_pti_for_group`), not a schedule.
  Don't reintroduce a reminder that fires on a fixed cadence without checking
  whether a PTI already came in.

## Group onboarding

**Drivers are never asked to register or configure anything.** When the bot is
added to a group it posts only an intro (what it does, how `/check` works) and
then works the setup out on its own:

1. guess the unit from the chat **title**, falling back to the **description**;
2. read the member roster through `utils/userbot.py`;
3. resolve the phone numbers in the About text into accounts, and configure the
   group outright if everything checks out (below);
4. otherwise DM the admins the title, the About text, the unit guess *and where
   it came from*, the reason step 3 declined, and one button per member.

The admin taps the drivers (this is how their `user_id` is captured), confirms
the unit and presses Save. **Nothing is written to the DB until Save** — on the
picker path.

### Configuring from the About text

The fleet writes both drivers' phone numbers into the group's About text (144 of
147 active groups, almost always exactly two), and a number resolves to a
`user_id` through `utils/phone_lookup.py`. `utils/auto_onboard.plan_auto_config`
decides whether that is enough to skip the admin, and the admin is *told* rather
than asked: a DM naming the unit, both drivers and the number each came from,
and **no button** — a setup that went right is news, not a question, and an
Edit button on every one of them invites a tap on the ones that were correct.
Changing an automatic setup is `/onboard <group_id>`, named in the notice
itself, which re-reads the roster and the About text instead of reopening a
picker built from a stale snapshot. The `ob:e:` callback still works (notices
already sitting in a DM carry the old button) but nothing offers it any more.

The decision is pure — the caller does the roster read, the lookup and the
writes — because it is the part that must not go wrong quietly. **Every one of
these must hold, or the picker is sent instead:** a unit parsed *and* on the
active list; exactly as many numbers in the About text as a group has drivers;
every number resolving to an account; every account being a member of the group
and not a bot; the accounts distinct. Three numbers means one belongs to
dispatch and guessing which is the failure this exists to avoid; an account that
is not in the chat can never post a PTI, so registering it would create a driver
who is permanently overdue.

Declining is not a failure — it is the ordinary prompt with a line saying which
check stopped it. That includes `LookupUnavailable`: a rate-limited lookup
account may cost an automatic setup, never a wrong one. With no lookup session
configured the whole step is skipped silently.

Nobody is marked as a non-driver on this path — only people actually shown a
picker count as passed over.

**The stored name is the fleet's, not Telegram's.** The About text names the
drivers on their own line (`Name: ZAMA, EMILE / FLEURMOND, JACQUES`), and
`utils/driver_names.parse_driver_names` reads it: a Telegram profile says
"Emile ✈️" or `@jacques_f`, which nobody can match against a driver list. Names
pair with phone numbers **by position**, so any other count is not a pairing at
all and the Telegram names are kept instead of guessing — the name is a label,
never a reason to decline an otherwise-clean setup. The admin notice shows the
Telegram name beside the stored one when they differ, because that is the line
on which a swapped pair becomes visible. The picker's Save keeps whatever name a
driver is already stored with, so editing one pick can't quietly swap the other
back to a Telegram handle.

### `/fixnames`: the backfill for groups configured earlier

Groups set up before that are filed under Telegram names, and re-resolving every
phone number to fix them is not a trade worth making — contact import is the
most rate-limited thing a user account does, and spending the lookup account
fleet-wide for a display name risks the member lookup onboarding depends on. So
`handlers/admin/names.py` re-reads each active group's About text and pairs the
names against the drivers *already registered*, by their words
(`match_names_to_drivers`): a pairing counts only when a shared word ≥3 letters
picks out exactly one driver and no driver is claimed twice, plus the one free
case of a single name and a single driver. Two drivers sharing a surname pair to
neither. A name that can't be placed is **reported, not guessed** — a wrong name
on a `user_id` reads as authoritative — and the fix for those is a per-group
`/onboard <group_id>`, which resolves the numbers properly. Preview then
confirm, like `/units`, and the confirmed write is one transaction
(`set_driver_names`).

Three rules that are easy to undo by accident:

- **The parsed unit is a suggestion, never a value.** Measured across the 158
  groups whose `unit_number` was already known, a naive digit-run regex scored
  79.5% — and six titles yielded a *different valid unit* rather than nothing. A
  wrong unit silently misattributes inspections, so it is only ever written
  without a human when the auto-config path's *other* checks corroborate it —
  two phone numbers that resolve to two members of that very group. Do not wire
  `parse_unit` straight into `set_group_unit`.
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

**Filter the roster, then slice it — never the other way round.**
`MEMBER_BUTTONS` (40) is how many buttons a Telegram message holds, and it is
applied *last*, by `_shown()`, to the already-filtered list. Slicing the roster
on the way into the state instead produced a prompt with no names on it at all:
in a group whose first 40 members are office staff, every one of them is a known
non-driver, so the drivers at position 41+ were discarded before the non-driver
filter ever ran and the admin was told "40 known non-driver(s) hidden" above an
empty keyboard. Three things read from `_shown()` for the same reason —
the buttons, the overflow note, and `_passed_over()`, since someone who never
appeared on screen must not be swept into a fleet-wide non-driver decision.
Everything about *membership* still reads the full roster.

The unit guess is also checked against `active_units` before being offered, so a
title naming a retired truck reads as "not found". An admin refreshes that list
weekly with `/units …` (replaced wholesale); an empty table disables the check
rather than rejecting every unit.

**The weekly list first re-files, then retires.** `/units` runs
`title_unit_changes` before `groups_to_deactivate`, because the fleet renames a
group when its truck changes — so a group still filed under the old number would
be retired for a unit that merely moved. Both land in one `apply_units_sweep`
transaction, renames applied first.

**Titles are also swept on their own, once a day.** Between weekly lists,
`run_title_sweep` (daily, keyed on the UTC date, from `units_refresh_loop`)
re-checks every active group's title and reports only when something changed:

| The title now names | Result |
| --- | --- |
| a different unit | re-filed under it |
| no unit at all, or INACTIVE / moved | deactivated |
| the same unit it always did | silent, even if that unit left the list |

The last row belongs to `/units`, where a human confirms it. It writes
unattended, so four things hold it up:

- **Titles are read fresh from Telegram** (`get_chat`), not from the `groups.title`
  cache — the cache is refreshed opportunistically from the message middleware,
  so the stalest titles belong to the quietest groups, which is exactly the state
  a retired truck is in.
- **A group that can't be read is dropped, not defaulted.** "Couldn't fetch" must
  never be mistaken for "the title lost its unit"; that reading is how the fleet
  was mass-deactivated once before.
- **Un-onboarded groups are never retired** — no stored unit means no truck, and
  an unparseable title there is the question onboarding is waiting to ask.
- **A title still printing the stored unit is never retired**, whatever
  `parse_unit` made of it (`title_names_unit`). "Does this title name *a* unit?"
  is a guess about a format; "is *my* number still on it?" is not. On
  2026-08-26 the daily sweep retired a running JRD truck titled
  `T-120 QUINTERO, JOHN / ...` — the number was right there and only the regex
  could not read a hyphenated prefix. Unit numbers are **not always digits**:
  `T-120`, `F9121`, `ML2432` and `1002FT` are all real, so `_UNIT` in
  `utils/unit_parse.py` allows one or two letters glued on either side and a
  hyphen after a letter prefix — never across a space, or "1136 LORISTON"
  would parse as "1136 LO". A `looks_retired` marker still overrides the veto:
  the fleet leaves the number on those titles.

**`title_deactivations` reads the title alone — it never consults
`active_units`.** It used to have a second rule (retire a group whose title
names a unit that is *not* on the stored list), added 2026-08-13 and **removed
2026-08-17: the fleet's weekly list is not trustworthy, it omits trucks that are
running.** That rule stacked two unreliable inputs with nothing to catch the
result — a ~79.5%-accurate title parse against a list with holes in it — and
wrote unattended three times a week, so either input being wrong retired a live
group. Don't reintroduce it. Absence from that list is not evidence a truck is
gone, and the one place it may still be read that way is `/units`, where a human
previews the casualties and confirms them.

What remains is still a much wider net than the weekly list's: ~20% of fleet
titles carry no parseable number. Reversing one is a manual panel decision, same
as any other reactivation.

A rename needs the group to be already configured and its title not to read as
retired. It used to need one more thing — **the parsed number on the incoming
list** — and that was **removed 2026-08-26 at the fleet's instruction: a title
naming a new unit *is* the truck changing.** The list is pasted in by hand and
goes stale between pastes (JRD's was twelve days old), so requiring it skipped,
in silence, exactly the case a rename exists for: a truck that had just
arrived. The cost is real and was measured before the change — across both
fleets it turned 1 re-file into 3, and one of the two new ones was a misparse
(`SUB-Unit# 543659 - 488090` read as 543659; `_SUB` now swallows that "unit"
so the sublease number can't be claimed by `_LABELLED`). Collisions (two titles
claiming one unit, or a unit another active group already holds) are still
dropped rather than guessed at — two groups under one unit is a broken
compliance denominator, not a worse guess.

**The weekly list also retires groups.** Any active group whose `unit_number` is
missing from the new list is deactivated (`groups_to_deactivate` →
`apply_units_sweep`). Three rules keep that from going wrong:

- **Preview, then confirm — then one transaction.** One pasted message
  deactivating groups fleet-wide is precisely how `is_active` once went FALSE
  across the fleet, so `/units` shows what would be retired and writes *nothing*
  — not even the list — until the admin confirms. The confirmed write stores the
  list and retires the groups in a **single transaction** (`apply_units_sweep`),
  so the stored state can never disagree with what the admin was told happened.
- **Deactivate only.** A unit reappearing on a later list never reactivates its
  group; that stays a manual panel decision.
- **No unit ⇒ untouched.** A group still awaiting onboarding has no unit to
  match, and "not in the list" must not mean "retired" for it.

**And the reverse question.** `units_without_groups` answers "which units on
this list have no active group?" — trucks whose chat was never created, never
had the bot added, or is still un-onboarded with no unit stored. Every other
report is driven off the groups the bot already knows, so those trucks are
invisible to all of them, which is exactly what makes them worth naming: nothing
can be inspected for them. A unit held only by a *deactivated* group is listed
with that group attached, because the fix there is a reactivation, not a new
chat. It is reported on both paths, including the quiet one where nothing is
written — a truck with no chat is news either way.

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

### The web panel's driver picker: a search, not a keyboard

The same roster, asked a different way. `GET /api/groups/{gid}/members` returns
every non-bot member flagged `is_driver` / `is_non_driver`, and the panel's
group page picks from it by name. Four writes sit on top of it: add
(`POST …/drivers`), rename (`POST …/drivers/{uid}/name`), swap
(`POST …/drivers/{uid}/replace`) and the existing remove. Together with the unit
field that is a full manual setup path, so an un-onboarded group no longer has
to wait for a DM prompt — the groups list carries a **Needs setup** filter
because such a group has no unit and no last PTI and therefore sorts to the
bottom of every other ordering.

Three rules it does *not* share with the Telegram picker:

- **It hides nobody.** Known non-drivers are badged and sorted last, never
  filtered out. Hiding is what leaves the keyboard empty; in a list you find
  people by typing, so there is nothing to protect them from.
- **It marks nobody.** Searching for one person is not a judgement on the rest
  of the roster, so nothing here writes `non_drivers` — only a picker Save does.
  Adding or swapping *in* a driver still clears their row, as everywhere else.
- **A swap is one transaction** (`swap_driver`). Remove-then-add leaves a window
  where the unit is short a driver, which the hourly compliance pass can read.
  It refuses to swap onto someone who already drives the group, since the
  DELETE + upsert would quietly collapse two drivers into one.

Degrading works as it does for onboarding: no session, or an account that is not
in the group, yields `available: false` plus the reason, and the panel falls back
to a typed user id.

## Phone number → account: the second userbot

`utils/phone_lookup.py` answers "whose Telegram account owns this number?",
used by `/whois <phone…>` (admin DM) and `scripts/tg_phone_lookup.py`. Driver
lists arrive as names and phone numbers while everything here is keyed on
`user_id`, so this is the bridge between the two; a resolved id is also checked
against `group_drivers` (`get_driver_memberships`) to answer "are they already
registered somewhere?".

It is a **separate module on a separate account** (`TELEGRAM_LOOKUP_SESSION`),
and both halves of that matter:

- **It writes.** There is no read-only way to do this — Telegram only names the
  owner of a number if you import it as a contact (`contacts.importContacts`).
  Every imported contact is deleted again in a `finally`, so the contact list is
  left as found, but the call is still a write and must not live in
  `utils/userbot.py`. A test asserts that module stays free of writes.
- **It is the most rate-limited thing a user account can do.** The roster
  session is load-bearing for onboarding; a contact-import limit picked up while
  answering `/whois` must not be able to take member lookup down with it.

Three outcomes, and conflating the last two is the bug to avoid:

| Telegram's response | Meaning |
| --- | --- |
| imported, user returned | match |
| neither imported nor `retry_contacts` | no visible account — not registered, **or** hidden by "Who can find me by my phone number". Indistinguishable. |
| `retry_contacts` forever, nothing imported | the *lookup* failed (account is contact-import limited) → `LookupUnavailable`, never "no match" |

Reporting a refusal as "not on Telegram" would send an admin chasing a driver
who is perfectly reachable, so the refusal raises. Observed live on 2026-08-12:
the `Safety` account (`8554521339`) returns `retry_contacts` for every number
including a control, which is why lookup gets its own, older account.

The one-session-per-host rule applies here too: `lookup_userbot` is the Railway
session, `lookup_local` is for `scripts/tg_phone_lookup.py`, and they are
different sessions.

```bash
railway run py -3.11 scripts/tg_login.py --name lookup_userbot
railway run py -3.11 scripts/tg_session_to_railway.py \
    --session lookup_userbot --var TELEGRAM_LOOKUP_SESSION
```

## Retired vs. quiet groups

Two different things, deliberately kept apart:

- **`groups.is_active`** is an administrative switch — the weekly `/units` sweep
  and the panel's Deactivate/Reactivate set it. It says whether a group *should*
  still be running, not whether anyone is using it.
- **Quiet** is derived from traffic: at most `GROUP_QUIET_MAX_MESSAGES` (env,
  default **3**) human messages in `GROUP_QUIET_DAYS` (env, default **3**) days.
  `middlewares/group_activity.py` counts one per human message into
  `group_message_days`; `utils/group_activity.py` holds the pure threshold half.

The threshold is a **count, not zero**: a stray "ok" or a sticker is not evidence
a truck is in service. Bot chatter is excluded (it would make every nagged group
look alive), as are join/leave/pin service messages — but an anonymous admin
counts (`from_user` is GroupAnonymousBot **with** `sender_chat`).

Storage is one row per group per day, not per message: the only question ever
asked is "how many in the last few days", so a daily counter answers it with one
small row instead of thousands, and pruning is a single `DELETE`. That is also
why the middleware is **not** throttled — a count needs every message.

Quiet is a **reporting** status, surfaced by `/quiet` in DM **on demand only**.
It used to ride along with the weekly units ask; that was removed on 2026-08-17,
because a quiet truck is not a retired one — a driver who films his PTI and says
nothing else is indistinguishable from an idle truck — so the list sat directly
above "paste this week's active units", next to a decision it cannot answer.
Don't re-attach it there. Two rules:

- It never writes `is_active`. Deactivation belongs to the `/units` sweep, where
  a human confirms it; quiet is evidence, not a decision.
- It never gates a reminder or a broadcast. A group nobody has posted in for
  three days is exactly the one the overdue reminder is for, and a silent truck
  is a missing inspection — so quiet groups stay in the compliance denominator.

## What triggers an inspection

Three ways, in `handlers/groups/pti.py`:

1. `/check` replying to a video or photo — always works.
2. A registered driver's standalone video, when `PTI_AUTOCHECK_ENABLED` is on.
   **It is off in production**, so this is normally inert.
3. A registered driver's video **replying to one of the bot's messages** — this
   works regardless of `PTI_AUTOCHECK_ENABLED`.

Rule 3 exists because replying to the bot's reminder with a video is how drivers
actually answer it; requiring `/check` turned a natural reply into a silent
no-op. It does not reopen blanket auto-checking — a reply is a deliberate
address to the bot, whereas the flag is off precisely so that *any* video in the
group doesn't start an inspection.

`_replies_to_bot` matches **this bot's own id** (cached from `get_me`), not
`from_user.is_bot`; otherwise another bot in the group could make its messages
into inspection triggers. Every other guard still applies to rules 2 and 3:
registered driver, not forwarded from someone else, not an album, group
setup-complete.

## Vehicle changes: decided by the plate, not by a vote

A truck or trailer swap is applied straight from the PTI — there is no
confirmation vote. Trailers were always applied immediately; trucks now are too,
guarded by `truck_verdict` in `handlers/groups/pti.py`:

| Registered vs. filmed | Result |
| --- | --- |
| unit differs, **plate identical** | **misread — store nothing** |
| unit differs, plate differs *or* no plate filmed | real change — store unit + plate |
| unit same, plate differs | store the plate |

The misread rule is the point of the whole thing. A plate is a far more legible
marking than a stencilled unit number, so a matching plate outweighs a differing
unit: that is one truck filmed badly, and adopting the unit would silently
misattribute every later inspection in the group. It stays silent in the chat —
nothing about the driver's inspection changed — and logs instead.

A differing unit with *no* plate to compare is treated as a real change on
purpose: genuine swaps are often filmed without a clear plate shot, and
demanding plate evidence would strand those groups on the old truck.

Because the change resolves from the video, the driver's result is never held
back. Don't reintroduce the vote: `PROPOSAL_REMINDERS_ENABLED` and the `pv:`
callbacks in `proposals.py` are dead for vehicle changes.

Both admin surfaces used to describe their edits as "skips the 3-vote flow",
which read as though a vote were still waiting somewhere. Nothing in the panels
says that any more — an admin edit is simply immediate. Don't reintroduce the
phrase in user-facing copy either.

## The fleet reports

`scripts/fleet_report.py` builds the two PDFs the fleet is used to seeing — a
one-page **fleet inspection statistics** sheet and a multi-page **driver
inspection report** — for any fleet and any window:

```bash
python scripts/fleet_report.py --fleet jrd-pti --last-week
python scripts/fleet_report.py --fleet jrd-pti --since 2026-08-17 --until 2026-08-24
```

It needs `DATABASE_URL` (or `--database-url`) and nothing else. Rendering is
headless Chromium (`--print-to-pdf`) over generated HTML, so there is no PDF
library to keep current. Every statement it runs is a SELECT.

- **It does not import the `utils` package.** `data/config.py` demands
  `BOT_TOKEN` and every other bot secret at import time, so the scoring module
  is loaded by path. A job that prints a PDF must not need credentials it cannot
  use — and must not be blocked from running because a bot secret is absent.
- **The window is half-open `[since, until)` in fleet-local time**, so
  `--last-week` is the most recently *completed* Monday 00:00 → Monday 00:00.
  Run on a Monday it reports the week that just ended, never the one in
  progress. `pti_log.submitted_at` is naive UTC and is converted with `--tz`
  (default `FLEET_TZ`) before days are bucketed; get that wrong and every
  submission near midnight lands on the wrong day.
- **The completeness score is fixed by what the fleet has already been shown**
  (`utils/report_scoring.py`): 85 pts for required areas filmed — 8, or 9 once
  the optional under-hood check appears in the footage — 5 for the fire
  extinguisher, 10 less 2 per "not visible" sub-item. `tests/test_report_scoring.py`
  pins it to rows copied out of the 6 Aug 2026 gurman report, so a change that
  moves a published number fails the suite. The score is **not** the verdict:
  PASS/FAIL is still only "was every required area filmed", and the
  extinguisher never fails an inspection.
- **A silent unit is the headline, not a silent driver** — two drivers share a
  truck and often only one uses the app. The stats sheet counts active groups
  that submitted nothing in the window, and says separately how many of those
  have never sent one *at all*, which a windowed count cannot tell you.
- **A driver who submitted nothing still appears**, greyed, with `—` for an
  average rather than 0% — a missing inspection is the report's subject, and 0%
  reads as a bad walkaround instead of no walkaround.
- Inactive groups are excluded everywhere; an unreadable `result_json` scores 0
  with every area counted missing, because a submission the pipeline could not
  read is not evidence of a walkaround.

CSVs of the same numbers land beside the PDFs, unrounded.

## Behavior under high load

`PTI_MAX_CONCURRENCY` (env, default **3**) caps how many inspections run
concurrently. Each inspection is CPU-heavy (ffmpeg) and uses a worker thread +
Gemini quota, so an unbounded burst could exhaust threads/memory or trip rate
limits. Excess submissions queue on an `asyncio.Semaphore` in
`pti_processor._get_analysis_slot()` and show the driver a "queued" notice
instead of piling on. Gemini calls also retry with backoff and surface a friendly
"overloaded" message on 5xx/429. Tune `PTI_MAX_CONCURRENCY` up only if the host
has CPU/memory headroom.
