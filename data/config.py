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
