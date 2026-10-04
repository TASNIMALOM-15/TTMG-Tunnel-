import os

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _ids(value: str) -> list[int]:
    return [int(x) for x in value.replace(";", ",").split(",") if x.strip()]


# --- Telegram ---
API_ID = int(_req("API_ID"))
API_HASH = _req("API_HASH")
BOT_TOKEN = _req("BOT_TOKEN")
OWNER_IDS = _ids(_req("OWNER_IDS"))

# --- Web server / keep-alive ---
PORT = int(os.getenv("PORT", "8080"))
APP_URL = (os.getenv("APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "600"))

# --- Queue / resources ---
WORKERS = max(1, int(os.getenv("WORKERS", "1")))
MAX_RETRIES = max(0, int(os.getenv("MAX_RETRIES", "1")))
TMP_DIR = os.getenv("TMP_DIR", "/tmp/hub")
DB_PATH = os.getenv("DB_PATH", "/tmp/hub-db/jobs.db")

# --- Destination folders ---
GDRIVE_FOLDER = os.getenv("GDRIVE_FOLDER", "TG-Hub").strip("/")
MEGA_FOLDER = os.getenv("MEGA_FOLDER", "TG-Hub").strip("/")
TERABOX_FOLDER = "/" + os.getenv("TERABOX_FOLDER", "TG-Hub").strip("/")

# --- TeraBox (unofficial, cookie based) ---
TERABOX_COOKIE = os.getenv("TERABOX_COOKIE", "").strip()

# --- Telegram limit for bot uploads (2000 MiB) ---
TG_MAX_BYTES = 2000 * 1024 * 1024
