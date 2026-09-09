# Standing up the bot for a new company

One company = one deployment. Same code, same `master` branch, different
Railway project: its own bot token, its own Postgres, its own Telegram user
sessions. Nothing in the bot is keyed on which fleet it serves, so there is no
code change to make — this document is the config and the order to do it in.

Never share a database, a bot token or a Telethon session between two fleets.
The session rule is not a preference: see [Userbot sessions](#3-userbot-sessions).

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
  web panel still runs, the "Open Web Panel" button in `/admin` is just hidden.

### Variables

Required — the process exits at import without them:

```
BOT_TOKEN=
DATABASE_URL=            # Railway Postgres reference
GEMINI_API_KEYS=         # comma-separated; GEMINI_API_KEY works for a single key
ADMINS=                  # comma-separated Telegram user ids
ip=127.0.0.1             # read by data/config.py and used nowhere; it has no
                         # default, so leaving it out is a startup crash
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
LOCAL_SERVER_URL=http://localhost:8081
```

Per-fleet decisions — every one has a default, so set only what differs:

| Variable | Default | Decide |
| --- | --- | --- |
| `ENFORCEMENT_ENABLED` | `false` | Whether the hourly loop nags overdue drivers in the group and summarises to admins. Start `false`; turn it on once the roster is actually right, or the first thing a new company sees is the bot chasing drivers it has mis-registered. |
| `PTI_AUTOCHECK_ENABLED` | `true` | `false` in production on the existing fleets. With it off, an inspection needs `/check` or a video replying to the bot — a stray dashcam clip does not start one. |
| `FLEET_TZ` | `America/New_York` | The zone the weekly PTI quota resets in (midnight Monday). |
| `PTI_MAX_CONCURRENCY` | `3` | Raise only if the container has CPU/memory headroom; each inspection is ffmpeg plus a worker thread. |
| `PTI_TIRE_PASS` | `true` | The second, tire-only Gemini pass. Leave on. |
| `SMTP_USER` / `SMTP_PASSWORD` / `ALERT_EMAIL_TO` | unset | Email on the overdue escalation. A silent no-op until all three are set. `SMTP_PASSWORD` must be a Gmail *App Password*. |
| `GROUP_QUIET_DAYS` / `GROUP_QUIET_MAX_MESSAGES` | `3` / `3` | Only affects the `/quiet` report. |

## 3. Userbot sessions

Two optional user accounts, each doing one thing the Bot API cannot:

- `TELEGRAM_SESSION` — reads a group's member list, which is what builds the
  onboarding driver picker and the web panel's member search. Read-only.
- `TELEGRAM_LOOKUP_SESSION` — resolves a phone number to an account (`/whois`,
  and the automatic onboarding path). Writes (a contact import, deleted again
  immediately) and is the most rate-limited thing an account can do, which is
  why it is a **separate account**, not just a separate session.

**Generate new sessions for the new project. Do not copy an existing fleet's.**
Telegram revokes an authorization key seen from two IP addresses at once and
*both* copies die — that took member lookup down across the fleet on
2026-08-09. Extra sessions on the same account are fine (that is what
Settings → Devices lists); one session in two places is not.

```
railway run py -3.11 scripts/tg_login.py --name bot_userbot
railway run py -3.11 scripts/tg_session_to_railway.py \
    --service <new-service> --session bot_userbot --var TELEGRAM_SESSION
```

`tg_login.py` is interactive (phone, code, 2FA) and needs a real terminal — it
cannot be run through an agent or a pipe. Neither script ever prints the
session value.

The account must be **a member of the new company's groups**. It can only read
a roster it is in; for a group it is not in, member lookup returns nothing.

Skipping both sessions is supported: onboarding degrades to "no member
buttons", `/whois` is off, and drivers are added by hand (`/adddriver`,
`/setunit`, or the web panel's search — which also needs the session, so on a
sessionless deployment the panel falls back to typing a user id).

## 4. Verify the first deploy

1. **Logs.** `Gemini model in use: <id>` on startup. Check it — a new Google
   project's keys do not serve every model the registry offers, and the
   failover switches in memory without saying so anywhere else.
2. **Admin DM.** The startup notice arrives; `/admin` opens the panel; the
   "Open Web Panel" button appears once `WEBAPP_URL` is set.
3. **A group.** Add the bot, make it an admin. It posts an intro and works the
   setup out on its own; if it cannot, the admins get a DM prompt. Nothing is
   written to the database until an admin presses Save on that prompt.
4. **An inspection.** Post a walkaround video and reply `/check` to it. A
   result posts back into the group.

## 5. Things that do not travel between fleets

- Telethon sessions (above).
- The database. There is nothing to migrate; a new fleet starts empty.
- `WEBAPP_URL` — each service has its own domain, and a wrong one points the
  Mini App at another company's panel.
- `TEST_GROUP_IDS` in `handlers/groups/pti.py` is a hardcoded pair of test
  groups that always auto-check. They belong to the original fleet and are
  inert elsewhere; a new fleet that wants a test group needs that set edited.
