from environs import Env

env = Env()
env.read_env()

BOT_TOKEN = env.str("BOT_TOKEN")
ADMINS = env.list("ADMINS")
IP = env.str("ip")
LOCAL_SERVER_URL = env.str("LOCAL_SERVER_URL", default="")
PTI_FRAMES = env.int("PTI_FRAMES", default=7)
DATABASE_URL = env.str("DATABASE_URL")
