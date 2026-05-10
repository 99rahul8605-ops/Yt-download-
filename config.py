import os
from dotenv import load_dotenv

load_dotenv()

# Bot Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN not found in environment variables!")

# Download Configuration
DOWNLOAD_DIR = "downloads"
SETTINGS_FILE = "user_settings.json"
MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2GB Telegram limit

# Quality presets
QUALITY_PRESETS = {
    "360p": "best[height<=360]",
    "480p": "best[height<=480]",
    "720p": "best[height<=720]",
    "1080p": "best[height<=1080]",
    "Best Available": "best"
}

# Cleanup timers (in seconds)
CLEANUP_TIMERS = {
    "5 Minutes": 300,
    "10 Minutes": 600,
    "15 Minutes": 900,
    "30 Minutes": 1800,
    "♾ Never": None
}

# yt-dlp options template
YT_DLP_OPTIONS = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'geo_bypass': True,
    'nocheckcertificate': True,
}
