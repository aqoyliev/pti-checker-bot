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
# DISABLED by default: in practice the focused pass over-flags INNER duals —
# filmed from the side, an inner dual is foreshortened/shadowed behind the outer
# and reads as "worn smooth" even when fine. This produced repeated false
# positives on good tires that no prompt rule could separate from real wear
# (the model perceives the artifact as genuine), so the broad pass alone (which
# still catches clearly worn/damaged tires) is the better tradeoff for trust.
# Set True to re-enable the backstop if you accept the inner-dual false positives.
PTI_TIRE_PASS = env.bool("PTI_TIRE_PASS", default=False)

# Split a single inspection's frames across multiple Gemini API keys and analyze
# the chunks in parallel (e.g. 210 frames over 3 keys = 70 each), then merge the
# per-chunk results into one verdict. Only kicks in when GEMINI_API_KEYS has more
# than one key and there are enough frames; otherwise it's a single whole-footage
# call. NOTE: each chunk sees only part of the walkaround, so completeness is
# rebuilt at merge time (an area filmed in ANY chunk counts as filmed). Set False
# to always send the whole inspection in one call. (default: true)
PTI_SPLIT_FRAMES = env.bool("PTI_SPLIT_FRAMES", default=True)

# Minimum number of images (photos + video frames) before splitting is worth it.
# Below this, the whole inspection goes in a single call to the first key (failover
# still covers it); at or above it — and with >1 key — the frames are split. Keeps
# short clips as one sharp call instead of tiny per-key chunks. (default: 30)
PTI_SPLIT_MIN_FRAMES = env.int("PTI_SPLIT_MIN_FRAMES", default=30)

# Compliance enforcement. When False (default), the hourly loop never mutes
# drivers and sends no overdue reminders to the group or to admins — it only
# lifts any restrictions left over from when enforcement was on. Set True to
# re-enable muting + reminders.
ENFORCEMENT_ENABLED = env.bool("ENFORCEMENT_ENABLED", default=False)
