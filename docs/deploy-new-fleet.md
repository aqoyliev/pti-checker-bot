# Standing up the bot for a new company

One company = one deployment. Same code, same `master` branch, different
Railway project: its own bot token, its own Postgres, its own Telethon lookup
session. Nothing in the bot is keyed on which fleet it serves, so there is no
code change to make — this document is the config and the order to do it in.

Never share a database, a bot token or a Telethon session between two fleets.
The session rule is not a preference: see [The lookup userbot](#3-the-lookup-userbot).

## 1. Before you touch Railway

| What | Where from |
| --- | --- |
| Bot token | BotFather → `/newbot`. Then `/setprivacy` → **Disable**, or the bot cannot see videos posted in a group. |
| Gemini API key(s) | A Google AI Studio project. Several keys are worth having — they are used for failover *and* for splitting one inspection's frames across keys in parallel. |
| Admin user ids | Each admin must press **Start** in the bot's DM once. A bot cannot open a DM with someone who never started it, so an admin who skips this gets no onboarding prompts and no startup notice. |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | https://my.telegram.org/apps. Reusing the values from another fleet is fine — these are app credentials, not a session. |

## 2. The Railway service

- **Builder: Dockerfile.** Not Nixpacks — `ffmpeg` and the local Bot API server
  binary come from the Dockerfile, and without the local server Telegram caps
  downloads at 20 MB, which is most of a walkaround video.
- **Branch: `master`.**
- **Add a Postgres database** to the project and point `DATABASE_URL` at it
  (Railway reference variable). The schema is created on first boot by
  `init_db()` — there is no migration step and nothing to import.
- Generate a service domain, then set `WEBAPP_URL` to it. Until it is set the
  web panel still runs, but `/admin` has nothing to point at and says so.

### Variables

Required — the process exits at import without them:

```
BOT_TOKEN=
DATABASE_URL=            # Railway Postgres reference
GEMINI_API_KEYS=         # comma-separated; GEMINI_API_KEY works for a single key
ADMINS=                  # comma-separated Telegram user ids
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
LOCAL_SERVER_URL=http://localhost:8081
```

Per-fleet decisions — every one has a default, so set only what differs:

| Variable | Default | Decide |
| --- | --- | --- |
| `ENFORCEMENT_ENABLED` | `false` | Whether the hourly loop nags overdue drivers in the group and summarises to admins. Start `false`; turn it on once the roster is actually right, or the first thing a new company sees is the bot chasing drivers it has mis-registered. |
| `PTI_AUTOCHECK_ENABLED` | `true` | `false` in production on the existing fleets. With it off, an inspection needs `/check` or a video replying to the bot — a stray dashcam clip does not start one. |
| `PTI_TEST_GROUP_IDS` | unset | Comma-separated chat ids of test groups: every member's video auto-checks there and the same clip may be re-sent. Leave unset unless the company keeps a test group. |
| `FLEET_TZ` | `America/New_York` | The zone the weekly PTI quota resets in (midnight Monday). |
| `FLEET_NAME` | `Fleet` | **The company's name**, printed as the wordmark at the top of both report PDFs (Tools tab). The default is a placeholder — leave it unset and every report the company sends out is headed "FLEET". |
| `PTI_MAX_CONCURRENCY` | `3` | Raise only if the container has CPU/memory headroom; each inspection is ffmpeg plus a worker thread. |
| `PTI_TIRE_PASS` | `true` | The second, tire-only Gemini pass. Leave on. |
| `SMTP_USER` / `SMTP_PASSWORD` / `ALERT_EMAIL_TO` | unset | Email on the overdue escalation. A silent no-op until all three are set. `SMTP_PASSWORD` must be a Gmail *App Password*. |
| `GROUP_QUIET_DAYS` / `GROUP_QUIET_MAX_MESSAGES` | `3` / `3` | Only affects the `/quiet` report. |

## 3. The lookup userbot

Member lookup (the onboarding driver picker, the web panel's member search)
reads a group's roster over MTProto **as the bot itself** — `TELEGRAM_API_ID`/
`TELEGRAM_API_HASH` above plus `BOT_TOKEN` are all it needs, no separate
session to generate. It works for a bot that is merely a member of the group;
admin rights are not required.

One optional *user* account remains, for the one thing a bot token cannot do:

- `TELEGRAM_LOOKUP_SESSION` — resolves a phone number to an account (`/whois`,
  and the automatic onboarding path). Writes (a contact import, deleted again
  immediately) and is the most rate-limited thing an account can do, which is
  why it is a dedicated account, used for nothing else.

**Generate a new session for the new project. Do not copy an existing fleet's.**
Telegram revokes an authorization key seen from two IP addresses at once and
*both* copies die — that took member lookup down across the fleet on
2026-08-09 (back when it was also a user session). Extra sessions on the same
account are fine (that is what Settings → Devices lists); one session in two
places is not.

```
railway run py -3.11 scripts/tg_login.py --name lookup_userbot
railway run py -3.11 scripts/tg_session_to_railway.py \
    --service <new-service> --session lookup_userbot --var TELEGRAM_LOOKUP_SESSION
```

`tg_login.py` is interactive (phone, code, 2FA) and needs a real terminal — it
cannot be run through an agent or a pipe. Neither script ever prints the
session value.

The account must be a member of the groups it needs to resolve numbers for.

Skipping it is supported: `/whois` and the phone-based auto-config path are
off, and drivers are added by hand — the web panel's member search (which
still works off the bot-token roster either way), or an admin running
`/adddriver` / `/setunit` in the group.

## 4. Verify the first deploy

1. **Logs.** `Gemini model in use: <id>` on startup. Check it — a new Google
   project's keys do not serve every model the registry offers, and the
   failover switches in memory without saying so anywhere else.
2. **Admin DM.** The startup notice arrives; `/admin` answers with the panel
   button once `WEBAPP_URL` is set (and says so when it isn't).
3. **A group.** Add the bot, make it an admin. It posts an intro and works the
   setup out on its own; if it cannot, the admins get a DM prompt. Nothing is
   written to the database until an admin presses Save on that prompt.
4. **An inspection.** Post a walkaround video and reply `/check` to it. A
   result posts back into the group.

## 4b. The groups the bot joined before the drivers did

Standing a fleet up does not happen in onboarding's order. The bot is added to
sixty groups on one afternoon; the drivers are added to them over the days
after. At join time each About text's phone numbers resolved to accounts that
were not in the chat yet, which the automatic path refuses to configure from --
an account that is not in the group can never post a PTI -- so every group fell
through to the admin picker, and that picker is sent **once per group**. Nothing
asks again on its own.

`scripts/setup_groups.py` is the second ask, for the whole fleet at once. It
runs where the bot's credentials live, previews by default, and configures only
the groups the ordinary decision accepts:

```bash
railway ssh -- python /app/scripts/setup_groups.py            # preview
railway ssh -- python /app/scripts/setup_groups.py --apply
```

Groups it declines are printed with an `/onboard <group_id>` line each; those
need a person. Run it again whenever another batch of drivers has been added.

Most declines are one of the two numbers matching no Telegram account — the
driver is in the chat but cannot be found by phone, which is their own privacy
setting. For those, ask for the reading rather than the write:

```bash
railway ssh -- python /app/scripts/setup_groups.py --suggest --sleep 1
```

It pairs the names the fleet wrote against each member list and prints only the
pairs it can prove, with the word that proved them. Nothing is written; confirm
each one in the panel's driver search, through `/onboard <group_id>`, or by
handing the pairs back to the script:

```bash
railway ssh -- python /app/scripts/setup_groups.py --apply     --pair=-1004438996513:7242667900 --pair=-5594909794:5016375864
```

The `=` is required (a group id starts with a minus). Each pair is re-checked
before it is written — still the proven match, a unit in the title, and at
least two shared words — so a stale report cannot write anything.

## 5. Things that do not travel between fleets

- Telethon sessions (above).
- The database. There is nothing to migrate; a new fleet starts empty.
- `WEBAPP_URL` — each service has its own domain, and a wrong one points the
  Mini App at another company's panel.
- `PTI_TEST_GROUP_IDS` — a test group is one company's chat; set it per
  deployment, never in code.
