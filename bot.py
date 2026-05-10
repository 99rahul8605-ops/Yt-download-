#!/usr/bin/env python3
"""
Advanced Telegram YouTube Downloader Bot
- yt-dlp powered, Python 3.10+
- Per‑user settings (quality, mode, cleanup timer)
- Async, python-telegram-bot v20+
- Document‑only uploads
- FFmpeg mandatory
- Automatic format fallback if requested quality unavailable
- Cookies support via COOKIES_BASE64 env variable
- Reliable health server (no extra dependencies)
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, Optional, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

# ---------- Health check server (built‑in) ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_server(port: int):
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Health server running on port {port}")
    except Exception as e:
        logger.error(f"Failed to start health server on port {port}: {e}")

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Please set BOT_TOKEN environment variable")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Cookies Setup ----------
COOKIES_FILE = "cookies.txt"

def setup_cookies():
    cookies_b64 = os.environ.get("COOKIES_BASE64", "").strip()
    if cookies_b64:
        try:
            with open(COOKIES_FILE, "wb") as f:
                f.write(base64.b64decode(cookies_b64))
            logger.info("Cookies loaded from COOKIES_BASE64")
        except Exception as e:
            logger.error(f"Failed to decode COOKIES_BASE64: {e}")
    else:
        logger.info("No COOKIES_BASE64 variable set – using local cookies.txt if present")

# ---------- Global Settings Storage ----------
SETTINGS_FILE = Path("settings.json")
DEFAULT_SETTINGS = {
    "default_quality": "720p",
    "download_mode": "manual",
    "cleanup_timer": 10,
}
user_settings: Dict[int, dict] = {}
cleanup_tasks: Dict[int, asyncio.Task] = {}

def load_settings() -> None:
    global user_settings
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                user_settings = json.load(f)
                user_settings = {int(k): v for k, v in user_settings.items()}
            logger.info("Settings loaded from disk")
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
    else:
        user_settings = {}

def save_settings() -> None:
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(user_settings, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")

def get_user_settings(user_id: int) -> dict:
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
        save_settings()
    return user_settings[user_id]

def update_user_setting(user_id: int, key: str, value: Any) -> None:
    settings = get_user_settings(user_id)
    settings[key] = value
    save_settings()

# ---------- Format helpers ----------
QUALITY_TO_FORMAT = {
    "360p": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]",
    "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]",
    "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
    "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]",
    "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
}

def quality_to_ytdl_format(quality: str) -> str:
    return QUALITY_TO_FORMAT.get(quality, QUALITY_TO_FORMAT["best"])

YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w-]+"
)

def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_URL_RE.search(text))

# ---------- Progress Hook ----------
class ProgressHook:
    def __init__(self, bot, chat_id: int, message_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_text = ""

    def __call__(self, d: dict) -> None:
        if d["status"] == "downloading":
            percent = d.get("_percent_str", "N/A")
            speed = d.get("_speed_str", "N/A")
            eta = d.get("_eta_str", "N/A")
            text = f"⬇ Downloading…\n▸ {percent.strip()}\n🚀 {speed.strip()}\n⏳ ETA: {eta.strip()}"
        elif d["status"] == "finished":
            text = "✅ Download finished, processing..."
        else:
            return

        if text != self.last_text:
            self.last_text = text
            asyncio.run_coroutine_threadsafe(
                self._edit_message(text),
                self.bot.loop,
            )

    async def _edit_message(self, text: str) -> None:
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
        except Exception as e:
            logger.debug(f"Progress edit failed: {e}")

# ---------- Core Download Engine with format fallback ----------
async def download_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    media_type: str,
    quality: Optional[str] = None,
) -> None:
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    chat_id = update.effective_chat.id

    tmp_dir = tempfile.mkdtemp(prefix="ytdl_", dir=".")
    outtmpl = os.path.join(tmp_dir, "%(title).100s_%(id)s.%(ext)s")

    progress_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Starting download...",
    )
    progress_callback = ProgressHook(context.bot, chat_id, progress_msg.message_id)

    def build_opts(fmt: str) -> dict:
        opts = {
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_callback],
            "cookiefile": COOKIES_FILE,
            "merge_output_format": "mp4",
            "format": fmt,
        }
        if media_type == "audio":
            opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        elif media_type == "video":
            opts.setdefault("postprocessors", [])
            opts["postprocessors"].append({"key": "FFmpegVideoConvertor", "preferedformat": "mp4"})
        elif media_type == "thumbnail":
            opts.update({
                "writethumbnail": True,
                "skip_download": True,
            })
        return opts

    async def attempt_download(fmt: str) -> list:
        opts = build_opts(fmt)
        loop = asyncio.get_running_loop()
        with YoutubeDL(opts) as ydl:
            await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
        if media_type == "thumbnail":
            for f in os.listdir(tmp_dir):
                if f.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    return [os.path.join(tmp_dir, f)]
            return []
        files = []
        for f in os.listdir(tmp_dir):
            fp = os.path.join(tmp_dir, f)
            if os.path.isfile(fp) and not fp.endswith(".part") and not fp.endswith(".ytdl"):
                files.append(fp)
        return files

    try:
        target_fmt = quality_to_ytdl_format(quality) if quality else quality_to_ytdl_format("best")
        try:
            downloaded_files = await attempt_download(target_fmt)
        except (DownloadError, ExtractorError) as e:
            error_text = str(e)
            if "Requested format is not available" in error_text and media_type == "video":
                await progress_msg.edit_text("⚠ Requested quality not available, falling back to best...")
                target_fmt = quality_to_ytdl_format("best")
                downloaded_files = await attempt_download(target_fmt)
            else:
                raise

        if not downloaded_files:
            await progress_msg.edit_text("❌ Download succeeded but no file found.")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        for file_path in downloaded_files:
            with open(file_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=os.path.basename(file_path),
                    caption=f"📥 {media_type.capitalize()} from YouTube",
                )
        await progress_msg.delete()

        cleanup_minutes = settings.get("cleanup_timer")
        if cleanup_minutes is not None:
            if user_id in cleanup_tasks and not cleanup_tasks[user_id].done():
                cleanup_tasks[user_id].cancel()
            async def _cleanup():
                await asyncio.sleep(cleanup_minutes * 60)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                logger.info(f"Cleaned up files for user {user_id}")
            task = asyncio.create_task(_cleanup())
            cleanup_tasks[user_id] = task

    except (DownloadError, ExtractorError) as e:
        error_text = str(e)
        if "Private video" in error_text or "This video is private" in error_text:
            msg = "🔒 This video is private."
        elif "Video unavailable" in error_text or "This video is not available" in error_text:
            msg = "🚫 Video is unavailable (deleted or region‑blocked)."
        elif "sign in" in error_text.lower() or "confirm you're not a bot" in error_text.lower():
            msg = "🔑 YouTube requires authentication. Add valid cookies (use /help)."
        else:
            msg = f"❌ Download failed: {error_text[:200]}"
        await progress_msg.edit_text(msg)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as e:
        await progress_msg.edit_text(f"❌ Unexpected error: {str(e)[:200]}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.exception("Download error")

# ---------- Handlers (identical to previous version) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to the YouTube Downloader Bot!\n\n"
        "• Send a YouTube link to download video/audio/thumbnail.\n"
        "• Send a song name to search.\n"
        "• Use /settings to adjust quality, mode, and cleanup.\n"
        "• /help for FAQ."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔐 If downloads fail with 'Sign in to confirm', provide YouTube cookies.\n"
        "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp\n\n"
        "🎥 Format errors? The bot will automatically fall back to 'best' if the requested quality isn't available."
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🎬 Default Video Quality", callback_data="set_quality")],
        [InlineKeyboardButton("🔁 Download Mode", callback_data="set_mode")],
        [InlineKeyboardButton("🧹 Cleanup Timer", callback_data="set_cleanup")],
        [InlineKeyboardButton("❌ Close", callback_data="close_settings")],
    ]
    await update.message.reply_text("⚙ Settings:", reply_markup=InlineKeyboardMarkup(keyboard))

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    settings = get_user_settings(user_id)

    if data == "set_quality":
        current = settings["default_quality"]
        keyboard = [
            [InlineKeyboardButton(f"360p {'✅' if current=='360p' else ''}", callback_data="quality_360p")],
            [InlineKeyboardButton(f"480p {'✅' if current=='480p' else ''}", callback_data="quality_480p")],
            [InlineKeyboardButton(f"720p {'✅' if current=='720p' else ''}", callback_data="quality_720p")],
            [InlineKeyboardButton(f"1080p {'✅' if current=='1080p' else ''}", callback_data="quality_1080p")],
            [InlineKeyboardButton(f"Best Available {'✅' if current=='best' else ''}", callback_data="quality_best")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_settings")],
        ]
        await query.edit_message_text("🎬 Select default video quality:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("quality_"):
        quality = data.split("_", 1)[1]
        update_user_setting(user_id, "default_quality", quality)
        await query.edit_message_text(f"✅ Default quality set to: {quality}")
    elif data == "set_mode":
        current = settings["download_mode"]
        fixed_text = "✅ Fixed Quality (use default)" if current == "fixed" else "Fixed Quality (use default)"
        manual_text = "🎛 Manual Selection" if current == "manual" else "Manual Selection"
        keyboard = [
            [InlineKeyboardButton(fixed_text, callback_data="mode_fixed")],
            [InlineKeyboardButton(manual_text, callback_data="mode_manual")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_settings")],
        ]
        await query.edit_message_text("🔁 Choose download mode:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("mode_"):
        mode = data.split("_", 1)[1]
        update_user_setting(user_id, "download_mode", mode)
        mode_display = "Fixed Quality" if mode == "fixed" else "Manual Selection"
        await query.edit_message_text(f"✅ Download mode set to: {mode_display}")
    elif data == "set_cleanup":
        current = settings["cleanup_timer"]
        options = [5, 10, 15, 30, None]
        labels = {5: "5 Minutes", 10: "10 Minutes", 15: "15 Minutes", 30: "30 Minutes", None: "♾ Never"}
        keyboard = []
        for val in options:
            text = labels[val]
            if val == current:
                text = f"{text} ✅"
            keyboard.append([InlineKeyboardButton(text, callback_data=f"cleanup_{val}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_settings")])
        await query.edit_message_text("🧹 Auto‑delete files after:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("cleanup_"):
        val = data.split("_", 1)[1]
        cleanup = int(val) if val != "None" else None
        update_user_setting(user_id, "cleanup_timer", cleanup)
        await query.edit_message_text(f"✅ Cleanup timer set to: {'Never' if cleanup is None else f'{cleanup} min'}")
    elif data == "back_settings":
        await query.edit_message_text("Return to /settings to see the main menu again.")
        await query.delete_message()
    elif data == "close_settings":
        await query.edit_message_text("Settings closed.")
        await query.delete_message()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if is_youtube_url(text):
        keyboard = [
            [InlineKeyboardButton("🎬 Video", callback_data=f"type_video|{text}")],
            [InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"type_audio|{text}")],
            [InlineKeyboardButton("🖼 Thumbnail", callback_data=f"type_thumb|{text}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="type_cancel")],
        ]
        await update.message.reply_text("What would you like to download?", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        progress_msg = await update.message.reply_text("🔍 Searching YouTube...")
        try:
            loop = asyncio.get_running_loop()
            with YoutubeDL({"quiet": True, "extract_flat": True, "cookiefile": COOKIES_FILE}) as ydl:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(f"ytsearch5:{text}", download=False))
            entries = info.get("entries", [])
            if not entries:
                await progress_msg.edit_text("❌ No results found.")
                return
            keyboard = []
            for vid in entries[:5]:
                title = vid.get("title", "No Title")
                vid_url = vid.get("webpage_url") or f"https://youtu.be/{vid['id']}"
                if len(title) > 50:
                    title = title[:47] + "..."
                keyboard.append([InlineKeyboardButton(title, callback_data=f"search_result|{vid_url}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="search_cancel")])
            await progress_msg.edit_text("🎵 Top results:", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            await progress_msg.edit_text(f"❌ Search failed: {e}")

# FIXED media_type_callback
async def media_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data in ("type_cancel", "search_cancel"):
        await query.edit_message_text("❌ Cancelled.")
        return

    if data.startswith("type_"):
        parts = data.split("|", 1)
        if len(parts) != 2:
            await query.edit_message_text("❌ Invalid selection.")
            return
        type_part, url = parts
        media_type = type_part.split("_", 1)[1]
        user_id = query.from_user.id
        settings = get_user_settings(user_id)

        if media_type == "video":
            mode = settings["download_mode"]
            if mode == "fixed":
                quality = settings["default_quality"]
                await query.edit_message_text(f"⏳ Downloading video in {quality}...")
                await download_media(update, context, url, "video", quality=quality)
            else:
                await query.edit_message_text("🔍 Fetching available qualities...")
                try:
                    loop = asyncio.get_running_loop()
                    with YoutubeDL({"quiet": True, "cookiefile": COOKIES_FILE}) as ydl:
                        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                    formats = info.get("formats", [])
                    seen = set()
                    heights = []
                    for f in formats:
                        h = f.get("height")
                        if h and h not in seen and f.get("ext") == "mp4":
                            seen.add(h)
                            heights.append(h)
                    heights.sort()
                    if not heights:
                        await query.edit_message_text("⚠ No MP4 formats found, using best available.")
                        await download_media(update, context, url, "video", quality="best")
                        return
                    buttons = []
                    for h in heights:
                        buttons.append([InlineKeyboardButton(f"{h}p", callback_data=f"quality_manual|{url}|{h}")])
                    buttons.append([InlineKeyboardButton("Best Available", callback_data=f"quality_manual|{url}|best")])
                    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="type_cancel")])
                    await query.edit_message_text("🎬 Choose video quality:", reply_markup=InlineKeyboardMarkup(buttons))
                except Exception as e:
                    await query.edit_message_text(f"❌ Could not fetch formats: {e}")

        elif media_type == "audio":
            await query.edit_message_text("🎵 Downloading MP3...")
            await download_media(update, context, url, "audio")

        elif media_type == "thumb":
            await query.edit_message_text("🖼 Downloading thumbnail...")
            await download_media(update, context, url, "thumbnail")

    elif data.startswith("quality_manual"):
        _, url, quality = data.split("|", 2)
        await query.edit_message_text(f"⬇ Downloading video in {quality}...")
        await download_media(update, context, url, "video", quality=quality)

    elif data.startswith("search_result"):
        _, vid_url = data.split("|", 1)
        keyboard = [
            [InlineKeyboardButton("🎬 Video", callback_data=f"type_video|{vid_url}")],
            [InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"type_audio|{vid_url}")],
            [InlineKeyboardButton("🖼 Thumbnail", callback_data=f"type_thumb|{vid_url}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="type_cancel")],
        ]
        await query.edit_message_text("What would you like to download?", reply_markup=InlineKeyboardMarkup(keyboard))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ An internal error occurred. Please try again later.",
            )
        except Exception:
            pass

# ---------- MAIN ----------
async def main() -> None:
    setup_cookies()
    load_settings()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^(set_|quality_|mode_|cleanup_|back_settings|close_settings)"))
    app.add_handler(CallbackQueryHandler(media_type_callback, pattern="^(type_|search_|quality_manual|search_cancel|type_cancel)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # Robust health server start (built‑in, no aiohttp needed)
    if os.environ.get("ENABLE_HEALTH_SERVER", "").lower() == "true":
        port = int(os.environ.get("PORT", 10000))
        logger.info(f"Starting health server on port {port}...")
        # Run in a thread to not block the event loop
        asyncio.get_running_loop().run_in_executor(None, start_health_server, port)

    logger.info("Bot started. Press Ctrl+C to stop.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())