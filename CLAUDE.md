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
- **`utils/scheduler.py` + `utils/enforcement.py` + `utils/reminders.py`** —
  the hourly loop: the 3-day overdue escalation (`reminders.py`, always on)
  and the weekly-quota check + admin summary (`enforcement.py`, behind
  `ENFORCEMENT_ENABLED`). **The bot never restricts a driver** (see Conventions).
- **`utils/admins.py`** — who counts as an admin: env `ADMINS` are
  super-admins, the `admins` table holds the rest. Every admin check (the web
  panel, `/admin`, the group setup commands) goes through it.
- **`webapp/`** — the web admin panel (Telegram Mini App) — **the** admin
  panel; there is no inline copy of it any more. `server.py` is an aiohttp app
  started from `on_startup` (listens on `PORT`/`WEBAPP_PORT`, default 8080);
  `auth.py` validates the Mini App's signed `initData`; `static/index.html`
  is the whole UI. `handlers/admin/panel.py` is just the door: `/admin` sends
  a web_app button and sets the chat menu button, both pointing at
  `WEBAPP_URL` (public HTTPS URL). The panel is also where a group gets
  **configured by hand** — unit, plus drivers picked by searching the roster
  (below). Timestamps in its JSON are already converted to `FLEET_TZ`. There
  is no separate stats screen: the fleet-wide counts it carried are the line
  above the groups list, and its list of overdue drivers is the **Due**
  filter plus the names already on every row.
- **`handlers/groups/setup_nag.py`** — the "nag" loop for still-unconfigured
  groups (it re-sends the onboarding prompt to admins in DM; it does not
  message the group).
- **`handlers/admin/onboard.py`** — admin-driven group onboarding (below), plus
  `/onboard <group_id>` to re-open the prompt for a group.
- **`utils/unit_parse.py`** — group title/description → unit-number *guess*.
- **`utils/driver_names.py`** — the fleet's driver name: parsing it out of a
  group's About text, pairing it to a registered `user_id` (`/fixnames`,
  `handlers/admin/names.py`) and, via `parse_driver_contacts`, to the phone
  number written beside it (below).
- **`utils/phones.py`** — finding and normalizing a phone number in free
  text. Split out of `phone_lookup` so *reading* a number costs no session,
  no API id and no rate-limit budget; `phone_lookup` re-exports it.
- **`utils/group_health.py`** — "the bot is muted in this group" vs. "the
  bot was removed", and the DM to the admins when the first one starts or
  stops (below).
- **`utils/userbot.py`** — read-only Telethon client, logged in as the bot
  itself over MTProto. It exists for the one thing the Bot API cannot do: list
  a group's members (plus the About text). No separate account needed — see
  "Group onboarding" below.
- **`utils/phone_lookup.py`** — a separate, write-capable *user* session: phone
  number → account (`/whois`, `scripts/tg_phone_lookup.py`). Separate account on
  purpose (below).
- **`utils/group_activity.py` + `middlewares/group_activity.py`** — the derived
  "has this group gone quiet?" report (below). (There is no anti-flood
  middleware: the bot handles no free text in groups, and the template one
  that used to be here replied "Too many requests!" into a driver group
  whenever someone forwarded a few messages at once.)

The **live PTI path** is `handlers/groups/pti.py` → `pti_processor.process_mixed_media`.
The other `process_*` functions in `pti_processor.py` are legacy/unused.

## Runtime requirements

- `ffmpeg` (+ `ffprobe`) on PATH — used to extract video frames.
- `chromium` on PATH (or one of `CHROME_CANDIDATES` in `scripts/fleet_report.py`,
  which also lists the usual Windows Chrome paths so the CLI runs on a dev box)
  — headless-renders the web panel's report PDFs. The Dockerfile installs it,
  plus `fonts-dejavu-core`, since a slim image otherwise has no font at all;
  without Chromium the Tools tab's report buttons fail with a clear error
  instead of a crash.
- PostgreSQL via `DATABASE_URL`.
- `GEMINI_API_KEY`.
- Optional local [Bot API server](https://github.com/tdlib/telegram-bot-api) via
  `LOCAL_SERVER_URL` to lift the 20 MB file limit (the Dockerfile builds this).
- Optional `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` (shared with the local Bot
  API server) for the onboarding member picker — it logs the bot itself into
  MTProto, no separate session. Without them onboarding still runs, just with
  no member buttons. See "Group onboarding" below.

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

One company = one deployment: same `master`, its own Railway project, bot token,
Postgres and Telegram sessions. `docs/deploy-new-fleet.md` is the runbook for
standing one up.

## Conventions

- Everything is `async`. Offload blocking work (ffmpeg, Gemini SDK) with
  `asyncio.to_thread`.
- All Telegram messages use HTML parse mode — **escape** any user/model text
  (`format_result` uses `html.escape`).
- Route DB access through `utils/db.py` helpers.
- **The bot never restricts, mutes or otherwise silences a driver.** Overdue
  compliance is answered with a reminder in the group and a summary to admins —
  never by taking away someone's ability to post. `utils/enforcement.py` makes
  no `restrict_chat_member` call at all — not to mute, and (since 2026-09-13)
  not to unmute either: the unmute path only lifted restrictions from before
  this rule, ran only behind `ENFORCEMENT_ENABLED`, and no fleet has ever
  turned that on. A test asserts no mute helper exists and the call is absent
  from the source. `ENFORCEMENT_ENABLED` only toggles the weekly-quota
  reminders. Don't reintroduce muting behind a config flag.
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
and **no button** on the passive path (the bot joining, or the setup nag) — a
setup that went right is news, not a question, and an Edit button on every one
of them invites a tap on the ones that were correct.

Changing an automatic setup is `/onboard <group_id>`, named in the notice
itself, which re-reads the roster and the About text instead of reopening a
picker built from a stale snapshot -- and unlike the passive path, it *does*
add the Edit button (`ob:e:`), because running the command is itself the
question: an admin who typed it is already looking for something to fix, and
had no way to act on that short of manual `/adddriver` commands in the group.
`start_onboarding`'s `manual` flag is the only difference between the two call
sites; a notice from before this distinction existed still carries the old
button either way.

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

**Everyone else in the chat is recorded as a non-driver** (as of 2026-09-16).
This path knows who the drivers are from the fleet's own phone numbers rather
than from someone tapping names, so the rest of the roster is dispatch, safety
or a mechanic — better evidence than a picker tap, not worse — and recording it
is what stops the same handful of office people being offered in every later
group's prompt. Two things follow from there being no screen here:

- **the whole roster is judged**, not the first `MEMBER_BUTTONS` of it. That cap
  exists only because an admin can only judge what fitted on their screen;
- **a driver of any other group is left alone** (`get_registered_driver_ids`,
  read *after* the write so this group's own drivers are in the set). A team
  driver who changed trucks sits in two chats, and hiding them fleet-wide would
  cost the next setup its buttons.

The admin notice says how many rows were written, because this is a fleet-wide
exclusion and that notice is the only place it is visible. It stays reversible
the same three ways as always — picking someone clears their row, "Show hidden"
reveals them for one prompt, `/nondrivers clear` empties the table.

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

### `scripts/setup_groups.py`: the groups that were asked too early

The automatic path declines a group whose About-text numbers resolve to people
who are not in the chat yet, and that is the normal state of a fleet being
stood up: the bot is added to sixty groups on one afternoon and the drivers
are added over the following days. Every one of those groups fell through to
the picker, and the setup nag sends that picker **once per group** -- so the
answer arrives after the only question was asked, and nothing retries.

The script is the retry, over every unconfigured active group
(`get_unconfigured_groups` -- the nag's query, without the one-prompt
ceiling). It decides nothing of its own: it reads the roster and the About
text the way onboarding does and hands them to the same `plan_auto_config`,
then calls the same `_apply_auto_config`. Three things it does carry:

- **preview is the default**; `--apply` writes. It runs unattended over a whole
  live fleet, which is the same reason `/titlecheck` and `/fixnames` show their
  work first.
- **the title is read fresh from Telegram**, never from `groups.title` -- the
  cache is refreshed by traffic, so the stalest titles belong to the quietest
  groups, and the unit is parsed out of it.
- **it gives up after `LOOKUP_FAILURE_LIMIT` refusals in a row**
  (`LOOKUP_UNAVAILABLE`, named in `onboard.py` so both sides agree on it).
  Contact import is the most rate-limited call the lookup account has; once it
  stops answering, the remaining groups were never really asked, and filing
  them all as "declined" would hide that.

It needs the bot's own credentials (bot token for the roster, the lookup
session for the numbers), so unlike `scripts/fleet_report.py` it runs on the
deployment — `railway ssh -- python /app/scripts/setup_groups.py`.

**`--suggest` is for what is left over, and it never writes.** Measured on
Cross USA on 2026-09-16: of 41 groups, 22 configured themselves and 16 declined
because *one of the two numbers matched no Telegram account* — the driver is in
the chat, they simply cannot be found by phone, which is a privacy setting and
theirs to keep. No amount of retrying moves those, so the mode does the reading
a person would otherwise do: it pairs the names the fleet wrote against the
member list with `match_names_to_drivers` — the same proven-only rule
`/fixnames` uses — and prints the pairs, the shared word that proved each one,
and the candidates for any name it would have had to guess at. Three rules:

- **it writes nothing and spends no phone lookup** (`--suggest --apply` is
  refused outright): every pair is confirmed by a person, in the panel or
  through `/onboard`;
- **the About text is read first, the title only as a fallback**
  (`_names_from_title`). About half these groups name the drivers in the title
  and nowhere a parser can see them — no label, no phone line beneath — so a
  title read is better than nothing, but it is the weaker source and the report
  says which one it used;
- **it flags a suggested person who is on the non-driver list.** After a
  fleet-wide setup that is common, and the picker hides them behind "Show N
  hidden" until someone asks.

**`--pair=GID:UID --apply` writes one pair an operator has read and approved.**
(The `=` is required: a group id starts with a minus, which argparse otherwise
reads as another option.)
It is the only place a *name* match reaches `set_group_unit`, so it is fenced
in: the pairing is recomputed at write time (the report is minutes old, the
roster is live), the person must still be the proven match, a unit must parse
out of the title, and the evidence must be `STRONG_SHARED_WORDS` (2) — a first
and a last name agreeing, not the single "mohamed" that pairs two unrelated
men. It registers one driver, sets the unit, clears that person's non-driver
row, and **sweeps nothing**: confirming one pick is not a judgement on the
other seventeen members, the same view the panel's driver search takes.

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
confirm, like `/titlecheck`, and the confirmed write is one transaction
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

**Titles are swept once a day.** `run_title_sweep` (keyed on the UTC date, from
`title_sweep_loop`) re-checks every active group's title and reports only when
something changed. The posting-permission sweep rides the same daily gate for
the same reason — one throttled pass over every active group, once — but is
guarded separately so a failure there can't undo a title sweep that already ran:

| The title now names | Result |
| --- | --- |
| a different unit | re-filed under it |
| no unit at all, or INACTIVE / moved | deactivated |
| the same unit it always did | silent |

It writes unattended, so four things hold it up:

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

`title_deactivations` **reads the title alone**, and one rule is already a wide
net: ~20% of fleet titles carry no parseable number. Reversing a retirement is a
manual panel decision, same as any other reactivation — nothing here ever
reactivates a group on its own.

A rename needs the group to be already configured and its title not to read as
retired. Collisions — two titles claiming one unit, or a unit another active
group already holds — are dropped rather than guessed at: two groups under one
unit is a broken compliance denominator, not a worse guess. Making the title
authoritative on 2026-08-26 turned 1 re-file into 3 across both fleets, and one
of the two new ones was a misparse (`SUB-Unit# 543659 - 488090` read as 543659;
`_SUB` now swallows that "unit" so a sublease number can't be claimed by
`_LABELLED`).

**There is no active-units list any more.** An admin used to paste the fleet's
live unit numbers weekly (`/units`): onboarding refused any unit missing from
that list, and every group filed under a unit that fell off it was retired,
behind a preview and a confirmation. It was **removed on 2026-08-31 at the
fleet's instruction — the list was never trustworthy.** It had already lost the
unattended half of the job on 2026-08-17, when "retire a group whose title names
a unit not on the list" stacked a ~79.5%-accurate title parse against a list with
holes in it and retired live groups three times a week; it lost the rename gate
on 2026-08-26, because the list went stale between pastes (JRD's was twelve days
old) and a truck that had just arrived was never on it. What was left could still
retire running trucks off one truncated paste. Gone with it: the `active_units`
table, `/units` and its confirmation flow, the weekly nag, `groups_to_deactivate`,
`units_without_groups`, `scripts/load_fleet_roster.py` and `utils/fleet_roster.py`.
**Don't reintroduce any of it** — absence from a roster is not evidence a truck is
gone. A unit is decided from the group's own title and the driver's own video.

> The table is left in place on the live databases rather than dropped. It is
> unread, and dropping a column of fleet history is not something a deploy
> should do on its own.

`/adddriver`, `/setunit` and `/removedriver` still work as a manual escape
hatch — **for the fleet's admins only** (`utils/admins.is_admin`; anyone else
gets a one-line refusal). Left open to every member, a driver could re-file the
truck with one `/setunit` or drop the co-driver out of compliance with
`/removedriver`, so that refusal is the whole guard: `/setunit` and `/adddriver`
are **in** the group command menu, because an admin configuring a group by hand
is standing in the group rather than in the panel, and a command nothing lists
has to be typed from memory. `/removedriver` is not listed — `/adddriver`'s own
reply names it in the single case it is needed, a group that already has two
drivers. A driver who runs
`/check` in an unconfigured group is told the admins have been asked to set
it up, not handed setup commands.

**The roster read no longer needs a user session at all.** Before 2026-09-12
this ran on a *user* account (`TELEGRAM_SESSION`), and inherited the same
"one session per host" fragility the lookup account still has below —
Telegram revokes an authorization key seen from two IP addresses at once
(`AuthKeyDuplicatedError`), which took out member lookup fleet-wide on
2026-08-09 when the Railway session was also used by a local script. Measured
against six live fleet groups (both chat shapes, none with the bot as admin),
`channels.getParticipants` answers fully for a bot that is merely a member —
admin rights are not required — so `utils/userbot.py` now logs in as the bot
itself (`BOT_TOKEN`) over a `MemorySession`, the same credential the Bot API
polling already uses. A bot token has no single-authorization-key limit, so
there is no "don't reuse this session" rule to maintain, no `.session` file to
generate or ship to Railway, and no dialog-cache warming — the entity is built
directly from the id (`_peer`) instead of walked via `iter_dialogs`. The one
thing this does not reach is `/whois`: `contacts.importContacts` is closed to
bots, so phone lookup keeps needing the separate user account below.

The userbot is strictly read-only (never sends, joins, leaves or edits) and
connects lazily, so a bot that never onboards a group never opens this MTProto
client at all. Every failure path degrades — MTProto unconfigured, or a group
the bot is not in — to "no member buttons", not an exception.

### The web panel's driver picker: a search, not a keyboard

The same roster, asked a different way. `GET /api/groups/{gid}/members` returns
every non-bot member flagged `is_driver` / `is_non_driver`, and the panel's
group page picks from it by name. Four writes sit on top of it: add
(`POST …/drivers`), rename (`POST …/drivers/{uid}/name`), swap
(`POST …/drivers/{uid}/replace`) and the existing remove. Together with the unit
field that is a full manual setup path, so an un-onboarded group no longer has
to wait for a DM prompt — the groups list carries a **Needs setup** filter
because such a group has no unit and no last PTI and therefore sorts to the
bottom of every other ordering. **Solo driver** is the same argument one step
on: a unit registered with exactly one driver reads as normal everywhere — it
has a unit, a driver and recent PTIs — but the About text names two numbers in
144 of 147 groups, so one is either a team that lost a driver or an onboarding
that half landed, and nothing else in the panel makes it visible.

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

### Each driver's phone number, on their own row

The number an admin wants when a driver stops filing PTIs is already written in
the group's About text, and the panel fetches that text anyway — `getChat`
returns `description` alongside the title, so `_chat_info` caches both from the
one response and there is no extra call. It is deliberately **not** stored in
the database: it is the fleet's own record of who to call, and a stale copy of a
phone number is worse than no copy.

Landing it on the right row needs two pairings to hold at once, and both can
refuse:

1. **name ↔ number**, from the About text (`parse_driver_contacts`). Three
   layouts, tried in order of how much guessing they cost: name and number on
   the *same line* (`Driver 1 - ZAMA, EMILE - 718-864-1154` — the fleet wrote
   the pairing, so it survives a line being added or reordered); a names line
   *directly above* a numbers line with a matching `/`-separated count (the
   layout with no label at all, which `parse_driver_names` refuses on its own
   and should — sitting above a matching count of numbers is the evidence it
   otherwise lacks); and whole-document positional, which is what
   `utils/auto_onboard` already does. Any mismatched count pairs nothing.
   Measured across 60 live groups on 2026-09-13: all 60 paired.
2. **name ↔ `user_id`**, by `match_names_to_drivers` — the same proven-only
   pairing `/fixnames` uses, so two drivers sharing a surname pair to neither.

Numbers left over are shown under the driver list as the **group's** numbers
rather than guessed at. An admin would rather see two unattributed numbers than
none, and a wrong number on a driver row reads as authoritative in exactly the
way a wrong name does — except this one has someone call the wrong person.

The number is displayed **as the fleet typed it** (`find_phones`), not
normalized: that is the form they recognise and dial. `extract_phones` still
returns `+1…` for the lookup, and both agree on what counts as a phone number.

## Phone number → account: the lookup userbot

`utils/phone_lookup.py` answers "whose Telegram account owns this number?",
used by `/whois <phone…>` (admin DM) and `scripts/tg_phone_lookup.py`. Driver
lists arrive as names and phone numbers while everything here is keyed on
`user_id`, so this is the bridge between the two; a resolved id is also checked
against `group_drivers` (`get_driver_memberships`) to answer "are they already
registered somewhere?".

*Reading* a number out of free text is not this and lives in `utils/phones.py`,
which has no config, no session and no rate limit to spend — the panel and
`utils/driver_names` want only that half.

It is a **separate module on a separate account** (`TELEGRAM_LOOKUP_SESSION`),
and both halves of that matter:

- **It writes.** There is no read-only way to do this — Telegram only names the
  owner of a number if you import it as a contact (`contacts.importContacts`).
  Every imported contact is deleted again in a `finally`, so the contact list is
  left as found, but the call is still a write and must not live in
  `utils/userbot.py`. A test asserts that module stays free of writes.
- **It is the most rate-limited thing a user account can do.** That is also
  why it could never move to the bot token the way the roster read did —
  `contacts.importContacts` is closed to bots outright, not just rate-limited —
  so `/whois` is the one piece of onboarding still tied to a user account at all.

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

The one-session-per-host rule still applies to this account: `lookup_userbot`
is the Railway session, `lookup_local` is for `scripts/tg_phone_lookup.py`, and
they are different sessions. (The roster read no longer has a session exposed
to this at all — it runs on the bot token instead, above.)

```bash
railway run py -3.11 scripts/tg_login.py --name lookup_userbot
railway run py -3.11 scripts/tg_session_to_railway.py \
    --session lookup_userbot --var TELEGRAM_LOOKUP_SESSION
```

## Retired vs. quiet groups

Two different things, deliberately kept apart:

- **`groups.is_active`** is an administrative switch — the daily title sweep and
  the panel's Deactivate/Reactivate set it. It says whether a group *should*
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
It used to ride along with the weekly units ask — detached on 2026-08-17, and
the ask itself is gone as of 2026-08-31. A quiet truck is not a retired one: a
driver who films his PTI and says nothing else is indistinguishable from an idle
truck, so the list sat next to a decision it cannot answer. Don't attach it to a
retirement decision anywhere else either. Two rules:

- It never writes `is_active`. Deactivation belongs to the title sweep and
  `/titlecheck`, where a human confirms it; quiet is evidence, not a decision.
- It never gates a reminder or a broadcast. A group nobody has posted in for
  three days is exactly the one the overdue reminder is for, and a silent truck
  is a missing inspection — so quiet groups stay in the compliance denominator.

## Muted vs. removed: when the bot can't post in a group

A group admin who takes away the bot's **Send Messages** permission breaks it
*silently*. The bot still receives everything, so nothing looks wrong from the
inside: `/check` runs the whole inspection, the result just never lands, and the
overdue reminder fails the same way. The drivers see a bot that stopped
answering and the fleet sees a unit that stopped inspecting.
`utils/group_health.py` turns that into a DM to the super-admins.

**Being kicked is deliberately not this.** It is a different situation with a
different answer — the bot has to be re-added — and `mark_unreachable` already
handles it, slowly on purpose (three consecutive failures, because the local Bot
API server forgets every chat when it restarts). `is_post_denied` is the whole
distinction and it is pure: an `Unauthorized` (which covers BotKicked and
BotBlocked) is never a mute, and neither is `ChatNotFound`. A mute arrives as a
bare `BadRequest` — aiogram has no class for it — so it is matched on the
wording, loosely, because the Bot API has phrased it more than one way.

Two detectors feed one recorder:

- **Reactive** — free and immediate, but only fires when there was something to
  send: `reminders._send`, the PTI result delivery (`deliver_result` — the worst
  case, since the frames were already uploaded and paid for) and the panel's
  broadcast.
- **Proactive** — one `getChatMember` per active group, once a day, riding the
  same daily budget as the title sweep. ~150 calls catches a restriction the day
  it happens instead of whenever a reminder next comes due. It reads the bot's
  own membership rather than trying a message, so nothing is posted into a
  driver's group to find out; a plain member holds the group's *default*
  permissions, so those are what decide for it. A group that can't be read is
  skipped, same as in the title sweep: "couldn't ask" is never an answer.

Both go through `record_post_access`, which writes `groups.post_blocked` and
alerts **only when that value changes** — started, and fixed. An hourly loop over
150 groups would otherwise repeat the same alert all week, and an alert repeated
hourly is an alert nobody reads. The panel shows the same state: a 🔇 chip on the
row, a "Can't post" filter, and a banner on the group saying who can fix it,
since nothing in the panel itself can.

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

`_replies_to_bot` matches **this bot's own id** (`loader.bot_id()`, one
`get_me` cached for the process), not `from_user.is_bot`; otherwise another bot
in the group could make its messages into inspection triggers. Every other
guard still applies to rules 2 and 3: registered driver, not forwarded from
someone else, not an album, group setup-complete.

`PTI_TEST_GROUP_IDS` (env, comma-separated chat ids, default empty) names test
groups where every member's video auto-checks, the recycled-video dedup is
skipped, and **no setup is required** — `_group_ready` answers yes for them
outright, and `/check` there accepts anyone's video too. A trial group has no
truck behind it, so waiting for a unit and a roster would leave the bot silent
through the one thing the trial is for. It still upserts the `groups` row,
since `pti_log` references it and an id can be configured while the bot is
already sitting in the chat, its join long since missed. It used to be a
hardcoded pair in `pti.py` belonging to one fleet; this repo serves several, so
nothing fleet-specific may be hardcoded.

### One inspection at a time, and never two of the same video

Two `/check` replies to one video used to start two full inspections, and the
recycled-video dedup structurally could not stop it: that reads `pti_log`, and
the row is only written once an inspection *finishes*, so through the minutes
in between the second command looked exactly like the first. Both ran, both
logged, and one walkaround was counted as two PTIs — which is how drivers who
send one video a day showed up on the fleet report with six submissions.

`utils/pti_gate` holds the window the database cannot, in memory, and the two
halves do different jobs:

- **`claim`** reserves the submission's media signatures before a single frame
  is extracted, and refuses a second claim on any of them — the driver is told
  the video is already being inspected, and nothing runs twice. It is
  **synchronous on purpose**: an `await` between asking and reserving is the
  race it exists to close, and the two commands that cause this arrive
  milliseconds apart. Don't make it a coroutine.
- **`group_lock`** then lets one inspection run at a time per group; the rest
  wait their turn. A queued submission therefore starts only after the one
  ahead of it has logged its row, which is what lets the ordinary dedup finally
  see a video that was still in flight when it was first asked about. Taking
  turns is also the cheaper order — two clips of one walkaround stop competing
  for the same frames budget and the same Gemini quota.

One process per fleet, so a module-level dict is the whole of the state, and
nothing survives a restart — which is right: neither does an inspection that
was in flight. The locks dict is never pruned (one small object per group seen
since boot); dropping a lock somebody is still queued on would strand them on
an object no new caller can find. `tests/test_pti_gate.py` pins both halves.

## A PTI never decides what vehicle it was filmed on

**Removed 2026-09-13, at the fleet's instruction.** Reading the truck's unit
number, its plate or the trailer number off the footage and storing it is gone:
`_extract_vehicles`, `truck_verdict`, `_reconcile_vehicles`, `set_trailer`,
`set_truck_plate` and `set_truck_unit` are all deleted, and so is the
`vehicles` key in `merge_frame_passes`. An inspection produces a verdict; that
is the whole of it.

A stencilled number on a dirty panel is the least legible thing in a
walkaround, and everything a reading could be written to already has a better
source — **the truck's unit comes from the chat title and is re-checked by the
daily sweep, the drivers come from the roster.** A video-driven write would also
have fought that sweep: it re-files a group from its title every day, so a unit
adopted from footage would be undone within a day and spend the hours in between
filing inspections under a number nothing else agreed with.

The model is still *asked* for a `vehicles` block — the prompt is not ours to
edit — and the answer is simply not used. A single-call inspection keeps it in
`result_json`; a split one (which is every inspection in production) drops it at
the merge. Nothing reads either.

Gone with it, on the live databases only: `groups.truck_plate`,
`groups.trailer_unit`, `groups.trailer_plate` and `pti_log.plate`. They are not
created on a fresh database and nothing reads them on an old one — same
treatment as the retired tables, since a deploy should not delete fleet history
on its own. The panel no longer offers a Truck-plate or Trailer row either: a
field nothing maintains reads as missing data rather than as absent data.

`tests/test_no_vehicle_reads.py` pins all three ways it could creep back — a
helper that reads the block, a merge that carries it, a write that stores it.

### `pti_log.unit_number` is deliberately empty, and that is a switch

Nothing writes it. Filling it from the group's registered unit would be correct
and is one line — and it would **turn the previous-inspection history back on
for the model**, which is not a side effect to discover by accident:

`_run_pti` passes the last five inspections to `call_gemini_photos(history=…)`,
filtered to rows whose `unit_number` matches the group's current one. The filter
exists so a truck's history cannot follow a group onto a different truck. With
the column never written, every row fails the match and `history` comes out
empty for any configured group — which is every group that can file a PTI.

**It has always been empty in practice**: the same merge bug that dropped
`vehicles` meant only 3 of dmworld's 405 logged inspections ever carried a unit
number, so the filter discarded the rest. The fleet's results, which it is happy
with, are the results with no history. Asked on 2026-09-13 whether to let it
switch on, the answer was no — the prompt already says "check whether prior
issues are now fixed", so turning it on changes what the model sees on every
inspection across four fleets, and a model reminded of last week's cracked rim
may well flag it again.

So the two pieces are left wired up and inert rather than half-removed. Don't
"fix" the empty column, and don't delete the filter — deleting it switches the
history on just as surely as filling the column does.

## The tire pass: it observes in one call and decides in another

`PTI_TIRE_PASS` (default on) runs a second Gemini pass over the same frames that
judges **only** tread wear, because the broad pass juggles 8 areas across 150+
frames and overlooks a single worn tire. That much is old. What is new — and is
the part to leave alone — is that the pass is **two calls**, and only the first
one sees the frames.

**Asked to inspect, the model under-reports.** On JRD unit 2456's 2026-08-26
clip, a trailer axle worn until the rib grooves were hairlines flush with the
tread, an inspector-framed pass returned `tire_defect: false` over all 325
frames, over a 41-frame close-up window, over 19 frames, over 6, and over a
2-frame pair against the deepest tire in the clip. Rewriting the carve-outs did
not fix it; neither did cutting the frame count. Asked to **describe** the same
frames with no verdict to reach, the same model called those grooves "almost
flush on a flat, smoothed tread face with virtually no open channel shadow" and
ranked them last of every tire in the walkaround, every run.

So `call_gemini_tires` is: `TIRE_SURVEY_PROMPT` over the frames (observe and
rank, decide nothing) → `TIRE_DECIDE_PROMPT` over the survey's own JSON (apply
the policy, no images). The decision costs ~1k tokens against the survey's ~350k.
End to end on that clip it went from 0/3 to 3/3, with no healthy tire flagged.
Four things hold it up, and `tests/test_tire_two_call_pass.py` pins them:

- **The survey names no verdict.** No "report", "flag", "defect", "out-of-service"
  or CFR citation — that vocabulary is what flipped the same observation from
  "flush" to "open channel".
- **The decision never gets the frames back.** Re-looking is the step that fails.
- **Center tread and shoulder are separate fields.** With one field they merge,
  and the outermost rib — smoother by design on every commercial tire — reads as
  wear: a healthy stack of spares came back "shallow and close to flush", word
  for word what the worn axle got. The decision may never report on the strength
  of `shoulder_note`.
- **Depth, never presence.** A rib groove leaves a traceable wavy line right up
  until it is gone, so the old rib-tire carve-out ("you can still see a groove")
  is precisely what read a worn-out rib tire as fine. An open channel is dark,
  wide and shadowed inside; a worn-out one is a faint line flush with a flat face.

Two older guards are deliberately gone. The contrast no longer has to be against
**the adjacent dual in the same frame** — a close-up of a worn tire never has
that frame, so the one case this pass exists for was the case it structurally
could not report — and "never flag all tires worn" is gone too, because an axle
wears out as a set and a matched pair is exactly what a driver films up close.
What they guarded against, a uniform rib set that merely *looks* shallow, is
covered instead by the depth test and by the survey's rule to skip distant,
oblique and wet tires. Promoted findings are still forced to `oos=false`
(`merge_tire_pass`) — worn tread is an advisory, never out-of-service.

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

**The web panel's Tools tab generates the same two PDFs on demand** — pick any
date range (two date pickers, no preset shortcuts to maintain alongside them)
and download either one. The end date is inclusive in the UI, same as picking
a day on a calendar; the API adds the one day back on to keep the rest of the
pipeline working with the half-open `[since, until)` range `fleet_report.py`
already expects, so the chosen end day is never dropped from the count.
`webapp/server.py`'s `/api/reports/{which}.pdf` calls `fetch`/`build`/
`stats_html`/`driver_html`/`to_pdf` directly rather than shelling out (the
web panel already holds every credential the CLI avoids needing), running
`to_pdf`'s blocking Chromium subprocess in a thread so it doesn't stall the
event loop. **`FLEET_NAME` is the company's name and is printed as the
wordmark at the top of both sheets** — not `--fleet`, since one deployment
already serves one fleet. It defaults to `"Fleet"`, which is a placeholder,
not a name: an unset `FLEET_NAME` puts "FLEET" on every page the company
sends out, so setting it is part of standing up a deployment. It is free text
an operator typed, so nothing downstream may assume it is short or clean —
`wordmark()` steps the type down and ultimately cuts it (a long name
otherwise squeezes the title, grows the masthead and pushes the one-page
sheet onto a second page), and the download filename is slugified, since a
quote in the name would close the filename early inside the
`Content-Disposition` header.
This is *why the Dockerfile installs `chromium`* now: before this, a missing
Chromium binary only broke a script nobody ran unattended; now it breaks a
button in production, so `to_pdf`'s `SystemExit` is caught and turned into a
plain "PDF rendering isn't available" panel error instead of a 500 with no
explanation.

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
- **Inactive groups are excluded everywhere, the window count included.** The
  silent list and the driver list always skipped them; the window count did
  not, so "units that submitted" was measured over the whole history while
  "active units" was measured over the running fleet — one fraction, two
  fleets, and a coverage headline that can read over 100%. `build` now drops a
  retired group's submissions outright and counts `drivers_total` over active
  groups only. A window row whose `groups` row is missing entirely is *kept*:
  unknown is not the same as retired, and dropping it loses a real inspection.
  `tests/test_fleet_report_build.py` pins all of that.
- An unreadable `result_json` scores 0 with every area counted missing, because
  a submission the pipeline could not read is not evidence of a walkaround.

CSVs of the same numbers land beside the PDFs, unrounded — and they keep both
counts the sheets no longer print side by side: how many inspections, and how
many clips those inspections arrived in.

### An inspection is a walkaround, not an upload

Some drivers film the PTI in two or three passes and post the clips one after
another. Scored one at a time each clip covers a third of the truck, so every
one of them lands as a Partial and not one clears the real-PTI bar — while
between them the driver filmed everything. That is backwards: they did the
walkaround, just not in one take. So `sessions_from` groups a driver's clips in
one group into a **session** while each is within `SESSION_GAP_MINUTES` (30) of
the one before it, and `report_scoring.merge_session` scores the union:

- an area is unfilmed only when **no** clip showed it (the intersection of the
  clips' missing lists);
- the extinguisher counts as shown if **any** clip showed it;
- a sub-item counts as not visible only when **every** clip said so — a clip
  that never pointed at the trailer is no evidence about its tape;
- and PASS follows the coverage, since the bot's own rule is "was every
  required area filmed".

Two rules that are easy to undo:

- **A session of one is that submission untouched.** `merge_session` returns
  the very same `Score` object. Nearly every driver sends a single video, and
  the published per-driver numbers must not move because the report learned to
  read the ones who don't.
- **The gap rolls, clip to clip — it is not a fixed bucket.** A walkaround
  filmed over twenty minutes holds together; a genuine second PTI that
  afternoon stays a second PTI. Widening it to "same day" would quietly
  collapse a team driver's two walkarounds into one.

A merged row says so on its own line ("3 clips, scored together"): a number
that came from three clips must not look like one that came from a single take.
The session also absorbs the other way one walkaround used to count twice — a
`/check` run again on a video already inspected. That is refused up front now
(`utils/pti_gate`), but the rows it already wrote are in the database and every
report over a past window still reads them. `tests/test_session_merge.py` pins
the arithmetic and the grouping.

**The driver table prints one count, not two.** `Sub` was every submission and
`Real` the ones that were a walkaround; with clips merged the two ran within a
hair of each other, and a pair of near-identical columns invites the question
of what the difference is rather than answering it. What is printed is the real
ones, headed **`Subs`** — the legend says so, the CSV has both. The same word
means the same thing on the driver report's index and cards; don't let "subs"
mean sessions in one place and real PTIs in another.

### How the two sheets are laid out

Neither is a web page: both are fixed-size paper, so columns are sized in px
against the printable box (letter at 96dpi leaves 965×733 landscape, 725 wide
portrait) and the charts are emitted at exactly the width they occupy —
`viewBox`, `width` and `height` all agreeing. An SVG stretched to fit a
flexible column rescales its own labels, which is how chart text ends up
smaller than everything around it.

**Inspections per day is one bar.** It used to be two, the second counting the
distinct units behind the day's submissions — dropped 2026-09-15 at the
fleet's instruction, because the sheet is read as "how much came in today" and
a second bar three quarters the height of the first invites the question of
what the difference means rather than answering it. One series also gets the
width the pair shared, so the bars and their value labels survive a window
about twice as long as before.

- **The document carries its own typeface.** `scripts/report_fonts/` holds
  Inter (SIL OFL, licence beside the files) and `font_css()` base64s it into
  the page. Rendering happens on whatever Chromium the host has, and this is a
  *slim* image where Debian's chromium only **recommends** a font package —
  which `--no-install-recommends` skips — so there was no guarantee any face
  existed to render with. `fonts-dejavu-core` is in the Dockerfile as the
  fallback for glyphs Inter's latin/latin-ext subsets miss (a Cyrillic
  Telegram profile name, say), not as the report's typeface. A test asserts
  the generated HTML contains no `http://` or `https://` at all: the renderer
  has no reason to have outbound access, and a report that needs it fails
  silently and invisibly.
- **The statistics sheet is one page, and that is a budget, not a preference.**
  Masthead, headline row, charts, the two tables and the legend add up to
  exactly the printable height; `STATS_ROWS` is what is left for the two
  bottom tables once everything above them is paid for. Raise the chart, the
  type scale or the legend and rows have to come off the bottom to match, or
  the legend silently lands alone on a second page. **The legend is four
  columns, and a fifth is not free**: `flex:1` narrows all four, every one of
  them wraps onto a fourth line, and the sheet goes to two pages — which is
  exactly what a fifth entry for the session rule did before it was folded
  into the one that defines the `Subs` column. The driver report's intro, which
  has the room, carries the long version.
- **The driver report paginates itself.** Each index block is sized *under* a
  page and forced to break after it, because how many rows fit depends on how
  many names wrap to a second line — a block sized to the page exactly spills
  two rows onto the next one, which then carries a stranded stub above the
  block that belongs there. Each index page names the slice of the ranking it
  carries, since Chromium cannot number printed pages from CSS. A driver's
  card is `break-inside: avoid`: a table header alone at the top of a page
  belongs to nobody.
- **Nothing depends on colour alone.** Every plotted bar prints its value and
  every score bar sits beside its own number, so the sheets survive greyscale
  and a phone screen.

## Behavior under high load

`PTI_MAX_CONCURRENCY` (env, default **3**) caps how many inspections run
concurrently. Each inspection is CPU-heavy (ffmpeg) and uses a worker thread +
Gemini quota, so an unbounded burst could exhaust threads/memory or trip rate
limits. Excess submissions queue on an `asyncio.Semaphore` in
`pti_processor._get_analysis_slot()` and show the driver a "queued" notice
instead of piling on. Gemini calls also retry with backoff and surface a friendly
"overloaded" message on 5xx/429. Tune `PTI_MAX_CONCURRENCY` up only if the host
has CPU/memory headroom.

**There is no failed-inspection retry queue.** A failed inspection is reported
to the driver and forgotten; they re-send `/check`. One existed
(`utils/pti_retry.py`, `pti_retry_queue`, removed 2026-09-13 — the table is
left on the live databases, unread): it re-ran submissions that failed on an
overload or an out-of-credit key. Its re-runs were louder than the failures
they recovered — every attempt posted its own status message, so one Gemini
outage on 2026-08-31 left five "the analysis service is overloaded" messages
in a DM World group over 90 minutes, for one walkaround and not one of them a
result. The case it was built for is real (2026-08-20: every key locked out of
the model for two days, so re-sending never worked either); if it ever comes
back, it has to be a retry that fails *silently* and posts only a result.
Error messages the driver sees never carry the raw exception text — that used
to print API JSON and file paths into the group.
