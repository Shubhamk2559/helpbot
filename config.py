import os


def _req(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# both   = ek hi service me sab kuch (default, recommended)
# main   = manager bot (admin commands, uploads, health check)
# worker = secondary delivery bots + Mini App (ad verification)
ROLE = os.getenv("ROLE", "both").strip().lower()
if ROLE not in ("main", "worker", "both"):
    raise RuntimeError("ROLE must be 'both', 'main' or 'worker'")

MAIN_BOT_TOKEN = _req("MAIN_BOT_TOKEN")
OWNER_ID = int(_req("OWNER_ID"))
MONGO_URI = _req("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "filebot")
FERNET_KEY = _req("FERNET_KEY")
WEBHOOK_SECRET = _req("WEBHOOK_SECRET")

BASE_URL = _req("BASE_URL").rstrip("/")
LINK_BASE_URL = os.getenv("LINK_BASE_URL", BASE_URL).rstrip("/")

PRIMARY_CHANNEL = int(_req("PRIMARY_CHANNEL"))
BACKUP_CHANNEL = int(_req("BACKUP_CHANNEL"))
CHANNELS = [PRIMARY_CHANNEL, BACKUP_CHANNEL]

HEALTH_INTERVAL = int(os.getenv("HEALTH_INTERVAL", "300"))
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "60"))

# ---- optional buttons on the bot menu ----
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()

# ---- ad verification (worker) ----
VERIFY_REQUIRED = _bool("VERIFY_REQUIRED", True)
ADS_REQUIRED = int(os.getenv("ADS_REQUIRED", "1"))
MIN_AD_SECONDS = int(os.getenv("MIN_AD_SECONDS", "5"))
SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))

# ---- direct Mini App launch ----
# False (default) = link -> bot -> 'Verify & Get Video' button -> Mini App me turant ad.
# True = link seedha Mini App kholta hai (button nahi).
# Iske liye har delivery bot me BotFather -> Bot Settings -> Main Mini App set hona chahiye
# (URL: BASE_URL/app). Agar short name wala app bana hai (/newapp) to uska short name yahan daalo.
DIRECT_MINIAPP = _bool("DIRECT_MINIAPP", False)
MINIAPP_SHORT_NAME = os.getenv("MINIAPP_SHORT_NAME", "").strip()

# ---- TADS (tads.me) ----
# Dashboard -> Widgets -> FULLSCREEN widget ka ID
TADS_WIDGET_ID = int(os.getenv("TADS_WIDGET_ID", "13070"))
TADS_SDK_URL = os.getenv("TADS_SDK_URL", "https://w.tads.me/widget.js").strip()
# True = dev mode (asli ads nahi, data collect nahi). Production me False.
TADS_DEBUG = _bool("TADS_DEBUG", False)
