from environs import Env

# environs kutubxonasidan foydalanish
env = Env()
env.read_env()

# .env fayl ichidan quyidagilarni o'qiymiz
BOT_TOKEN = env.str("BOT_TOKEN")
ADMINS = env.list("ADMINS")
IP = env.str("ip")
LOCAL_SERVER_URL = env.str("LOCAL_SERVER_URL", default="")
LOCAL_BOT_API_DIR = env.str("LOCAL_BOT_API_DIR", default="/var/lib/telegram-bot-api")
PTI_FRAMES = env.int("PTI_FRAMES", default=7)
