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
# pass juggles 8 areas over 150+ frames and reliably overlooks a single worn
# tire (attention dilution); a focused pass catches it. Costs one extra Gemini
# call per inspection — set False if Gemini quota is tight.
PTI_TIRE_PASS = env.bool("PTI_TIRE_PASS", default=True)

# Compliance enforcement. When False (default), the hourly loop never mutes
# drivers and sends no overdue reminders to the group or to admins — it only
# lifts any restrictions left over from when enforcement was on. Set True to
# re-enable muting + reminders.
ENFORCEMENT_ENABLED = env.bool("ENFORCEMENT_ENABLED", default=False)
