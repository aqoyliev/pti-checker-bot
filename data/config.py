from environs import Env

env = Env()
env.read_env()

BOT_TOKEN = env.str("BOT_TOKEN")
ADMINS = env.list("ADMINS")
IP = env.str("ip")
LOCAL_SERVER_URL = env.str("LOCAL_SERVER_URL", default="")
PTI_FRAMES = env.int("PTI_FRAMES", default=7)
DATABASE_URL = env.str("DATABASE_URL")

# Max number of PTI inspections analyzed concurrently. Each inspection runs
# ffmpeg frame extraction (CPU) and a Gemini call (network + worker thread),
# so an unbounded burst of submissions can exhaust the thread pool, spike
# memory/disk, and trip Gemini rate limits. Excess submissions queue and wait
# for a free slot instead of piling on. Tune up only if the host has headroom.
PTI_MAX_CONCURRENCY = env.int("PTI_MAX_CONCURRENCY", default=3)

# Run a second, tire-only Gemini pass over the same frames and merge any
# out-of-service (bald/tread-gone) tire it finds into the result. The broad PTI
# pass juggles 8 areas over 150+ frames and can overlook a single worn tire
# (attention dilution); a focused pass catches it.
# The focused pass used to over-flag INNER duals seen side-on in wide walkaround
# frames (foreshortened/shadowed behind the outer dual, reading as falsely bald).
# Mitigated by a CLOSE-UP / head-on requirement in test_pti.TIRE_SYSTEM_PROMPT:
# it now only flags a worn dual whose center tread face is clearly visible in a
# close, roughly head-on shot (as when a driver films a problem tire up close) —
# the real worn tires show up that way, the false positives were distant/oblique.
# Set False to skip it if Gemini quota is tight.
PTI_TIRE_PASS = env.bool("PTI_TIRE_PASS", default=True)

# Split a single inspection's frames across multiple Gemini API keys and analyze
# the chunks in parallel (e.g. 210 frames over 3 keys = 70 each), then merge the
# per-chunk results into one verdict. Kicks in whenever GEMINI_API_KEYS has more
# than one key and the inspection has 2+ images; otherwise it's a single
# whole-footage call. NOTE: each chunk sees only part of the walkaround, so
# completeness is rebuilt at merge time (an area filmed in ANY chunk counts as
# filmed). Set False to always send the whole inspection in one call. (default: true)
PTI_SPLIT_FRAMES = env.bool("PTI_SPLIT_FRAMES", default=True)

# Auto-inspect a standalone video from a registered driver without a /check
# command (handlers/groups/pti.py:handle_group_video). When False, the bot
# never auto-runs a PTI on group videos — every inspection must be requested
# explicitly with /check (in-reply). Buffering, dedup, and /check are unaffected.
# The hardcoded TEST groups (pti.TEST_GROUP_IDS) always auto-check regardless.
# Set False to turn the auto-inspector off everywhere except TEST groups. (default: true)
PTI_AUTOCHECK_ENABLED = env.bool("PTI_AUTOCHECK_ENABLED", default=True)

# Overdue reminders. The bot never restricts a driver under any setting — this
# only controls whether the hourly loop sends overdue reminders to the group and
# a summary to admins. False (default) = the loop does nothing at all.
ENFORCEMENT_ENABLED = env.bool("ENFORCEMENT_ENABLED", default=False)

# Timezone the weekly PTI quota is anchored to. The week resets at midnight
# Monday in this zone (DST-aware), not UTC — otherwise drivers' Sunday-evening
# and Monday-morning submissions land in the wrong week. Timestamps stay stored
# as UTC; only the week boundary shifts.
FLEET_TZ = env.str("FLEET_TZ", default="America/New_York")

# Web admin panel (Telegram Mini App). The bot always starts a small aiohttp
# server (webapp/server.py) that serves the panel UI + JSON API on WEBAPP_PORT —
# on Railway the injected PORT wins, so generating a service domain "just works".
# Set WEBAPP_URL to that public HTTPS URL (e.g. https://<app>.up.railway.app) to
# show the "Open Web Panel" button in /admin; Telegram requires HTTPS for Mini
# Apps, and until WEBAPP_URL is set the button is hidden (the server still runs).
WEBAPP_URL = env.str("WEBAPP_URL", default="").strip().rstrip("/")
WEBAPP_PORT = env.int("PORT", default=env.int("WEBAPP_PORT", default=8080))

# Email alerts for the overdue escalation (#9). When a group enters the every-12h
# overdue phase, the bot emails ALERT_EMAIL_TO alongside the group nag, so a
# fleet manager hears about a non-compliant driver without watching the chat.
# Sent via Gmail SMTP: set SMTP_USER to the Gmail address and SMTP_PASSWORD to a
# Gmail *App Password* (16 chars, needs 2-Step Verification on the account) —
# a normal login password won't work. Until SMTP_USER + SMTP_PASSWORD are set the
# feature is a silent no-op (see utils/email_alerts.email_configured). Port 465 =
# implicit SSL, else STARTTLS. Gmail sends From = the authenticated account.
SMTP_HOST = env.str("SMTP_HOST", default="smtp.gmail.com")
SMTP_PORT = env.int("SMTP_PORT", default=587)
SMTP_USER = env.str("SMTP_USER", default="")
SMTP_PASSWORD = env.str("SMTP_PASSWORD", default="").replace(" ", "")
SMTP_FROM = env.str("SMTP_FROM", default="") or SMTP_USER
ALERT_EMAIL_TO = env.str("ALERT_EMAIL_TO", default="")

# --- userbot (member lookup) ---
# The Bot API cannot list group members, so onboarding borrows a *user* session
# purely to read the roster. All three are optional: without them the feature
# degrades to "no member buttons" rather than failing. See utils/userbot.py.
TELEGRAM_API_ID = env.str("TELEGRAM_API_ID", default="")
TELEGRAM_API_HASH = env.str("TELEGRAM_API_HASH", default="")
# A Telethon StringSession — how the session travels to Railway (a .session file
# would not survive a redeploy). Treat it like a password: it is full access to
# the account it was made from.
TELEGRAM_SESSION = env.str("TELEGRAM_SESSION", default="")
TELEGRAM_SESSION_FILE = env.str("TELEGRAM_SESSION_FILE", default="")
