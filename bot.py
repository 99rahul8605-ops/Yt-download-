#!/usr/bin/env python3
"""
Advanced Telegram YouTube Downloader Bot
- yt-dlp powered
- Per‑user settings (quality, mode, cleanup timer)
- Async, python-telegram-bot v20+
- Document‑only uploads
- FFmpeg mandatory
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
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

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Please set BOT_TOKEN environment variable")

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Global Settings Storage ----------
SETTINGS_FILE = Path("settings.json")

DEFAULT_SETTINGS = {
    "default_quality": "720p",       # 360p, 480p, 720p, 1080p, best
    "download_mode": "manual",       # fixed or manual
    "cleanup_timer": 10,             # minutes (None = never)
}

# In‑memory cache: user_id -> settings dict
user_settings: Dict[int, dict] = {}

# Track active cleanup tasks: {user_id: asyncio.Task}
cleanup_tasks: Dict[int, asyncio.Task] = {}


def load_settings() -> None:
    """Load settings from JSON file into global cache."""
    global user_settings
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                user_settings = json.load(f)
                # Convert string keys back to int (JSON keys are strings)
                user_settings = {int(k): v for k, v in user_settings.items()}
            logger.info("Settings loaded from disk")
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
    else:
        user_settings = {}


def save_settings() -> None:
    """Persist current settings cache to JSON file."""
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(user_settings, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")


def get_user_settings(user_id: int) -> dict:
    """Return settings for a user, filling defaults if missing."""
    if user_id not in user_settings:
        user_settings[user_id] = DEFAULT_SETTINGS.copy()
        save_settings()
    return user_settings[user_id]


def update_user_setting(user_id: int, key: str, value: Any) -> None:
    """Update a single setting and persist."""
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
    """Translate a quality label to a yt-dlp format string."""
    return QUALITY_TO_FORMAT.get(quality, QUALITY_TO_FORMAT["best"])


# ---------- YouTube URL & Search ----------
YOUTUBE_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w-]+"
)

YT_SEARCH_PREFIX = "ytsearch5:"


def is_youtube_url(text: str) -> bool:
    """Check if text is a YouTube video URL."""
    return bool(YOUTUBE_URL_RE.search(text))


# ---------- Progress Hook (thread‑safe) ----------
class ProgressHook:
    """Thread‑safe progress hook that edits a Telegram message."""

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
            text = (
                f"⬇ Downloading…\n"
                f"▸ {percent.strip()}\n"
                f"🚀 {speed.strip()}\n"
                f"⏳ ETA: {eta.strip()}"
            )
        elif d["status"] == "finished":
            text = "✅ Download finished, processing..."
        else:
            return

        if text != self.last_text:
            self.last_text = text
            # Schedule the message edit in the main event loop
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


# ---------- Core Download Engine ----------
async def download_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    media_type: str,          # "video", "audio", "thumbnail"
    quality: Optional[str] = None,  # only for video, e.g. "720p"
) -> None:
    """
    Download media in background, send as document, apply cleanup.
    """
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    chat_id = update.effective_chat.id

    # Create temporary directory for this download
    tmp_dir = tempfile.mkdtemp(prefix="ytdl_", dir=".")
    # Unique file name base (yt-dlp will append extensions)
    outtmpl = os.path.join(tmp_dir, "%(title).100s_%(id)s.%(ext)s")

    # Build progress message
    progress_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Starting download...",
    )
    progress_callback = ProgressHook(context.bot, chat_id, progress_msg.message_id)

    # yt-dlp options
    ydl_opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_callback],
        "cookiefile": "cookies.txt",  # optional, for age‑restricted content
        "merge_output_format": "mp4",  # ensure final merge is mp4
    }

    if media_type == "audio":
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        })
    elif media_type == "video":
        # If quality specified, use that; otherwise best
        fmt = quality_to_ytdl_format(quality) if quality else quality_to_ytdl_format("best")
        ydl_opts["format"] = fmt
        ydl_opts.setdefault("postprocessors", [])
        # Ensure we merge video+audio if needed
        ydl_opts["postprocessors"].append({"key": "FFmpegVideoConvertor", "preferedformat": "mp4"})
    elif media_type == "thumbnail":
        # Download only thumbnail
        ydl_opts.update({
            "writethumbnail": True,
            "skip_download": True,  # don't download video
        })
    else:
        raise ValueError("Invalid media type")

    try:
        # Run yt-dlp in a thread to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        with YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))

        # Find downloaded file(s)
        downloaded_files = []
        if media_type == "thumbnail":
            # Thumbnail file is named like the video title + ".jpg" (or .png)
            for f in os.listdir(tmp_dir):
                if f.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    downloaded_files.append(os.path.join(tmp_dir, f))
                    break
        else:
            # All other files in tmp_dir
            for f in os.listdir(tmp_dir):
                fp = os.path.join(tmp_dir, f)
                if os.path.isfile(fp):
                    downloaded_files.append(fp)

        if not downloaded_files:
            await progress_msg.edit_text("❌ Download succeeded but no file found.")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        # Send each file as document
        for file_path in downloaded_files:
            with open(file_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=os.path.basename(file_path),
                    caption=f"📥 {media_type.capitalize()} from YouTube",
                )

        await progress_msg.delete()

        # Schedule cleanup based on user settings
        cleanup_minutes = settings.get("cleanup_timer")
        if cleanup_minutes is not None:
            # Cancel any previous cleanup task for the user (just in case)
            if user_id in cleanup_tasks and not cleanup_tasks[user_id].done():
                cleanup_tasks[user_id].cancel()
            # Schedule deletion
            async def _cleanup():
                await asyncio.sleep(cleanup_minutes * 60)
                shutil.rmtree(tmp_dir, ignore_errors=True)
                logger.info(f"Cleaned up files for user {user_id}")

            task = asyncio.create_task(_cleanup())
            cleanup_tasks[user_id] = task
        else:
            # "Never" – leave files
            logger.info(f"Skipping cleanup for user {user_id} (timer = Never)")

    except (DownloadError, ExtractorError) as e:
        error_text = str(e)
        if "Private video" in error_text or "This video is private" in error_text:
            msg = "🔒 This video is private."
        elif "Video unavailable" in error_text or "This video is not available" in error_text:
            msg = "🚫 Video is unavailable (deleted or region‑blocked)."
        elif "sign in" in error_text.lower():
            msg = "🔑 Age‑restricted or sign‑in required. Provide cookies.txt to bypass."
        else:
            msg = f"❌ Download failed: {error_text[:200]}"
        await progress_msg.edit_text(msg)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as e:
        await progress_msg.edit_text(f"❌ Unexpected error: {str(e)[:200]}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.exception("Download error")


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message."""
    await update.message.reply_text(
        "👋 Welcome to the YouTube Downloader Bot!\n\n"
        "• Send a YouTube link to download video/audio/thumbnail.\n"
        "• Send a song name to search.\n"
        "• Use /settings to adjust quality, mode, and cleanup."
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display main settings menu."""
    keyboard = [
        [InlineKeyboardButton("🎬 Default Video Quality", callback_data="set_quality")],
        [InlineKeyboardButton("🔁 Download Mode", callback_data="set_mode")],
        [InlineKeyboardButton("🧹 Cleanup Timer", callback_data="set_cleanup")],
        [InlineKeyboardButton("❌ Close", callback_data="close_settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚙ Settings:", reply_markup=reply_markup)


# ---------- Settings Callback Handlers ----------
async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route settings sub‑menus."""
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
        quality = data.split("_", 1)[1]  # e.g., "720p"
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
        options = [5, 10, 15, 30, None]  # None = never
        labels = {
            5: "5 Minutes",
            10: "10 Minutes",
            15: "15 Minutes",
            30: "30 Minutes",
            None: "♾ Never",
        }
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
        # This approach won't work directly because we need to send a new message.
        # Instead, we delete the current message and rely on user to re‑open /settings.
        await query.edit_message_text("Return to /settings to see the main menu again.")
        # Alternatively, we can just delete it.
        await query.delete_message()

    elif data == "close_settings":
        await query.edit_message_text("Settings closed.")
        await query.delete_message()


# ---------- URL / Search Handler ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive text messages and decide action."""
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if is_youtube_url(text):
        # Show media type selection
        keyboard = [
            [InlineKeyboardButton("🎬 Video", callback_data=f"type_video|{text}")],
            [InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"type_audio|{text}")],
            [InlineKeyboardButton("🖼 Thumbnail", callback_data=f"type_thumb|{text}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="type_cancel")],
        ]
        await update.message.reply_text(
            "What would you like to download?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        # Search YouTube for the query
        progress_msg = await update.message.reply_text("🔍 Searching YouTube...")
        try:
            loop = asyncio.get_running_loop()
            with YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
                # ytsearch5: returns up to 5 results with basic info
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(f"ytsearch5:{text}", download=False))
            entries = info.get("entries", [])
            if not entries:
                await progress_msg.edit_text("❌ No results found.")
                return

            keyboard = []
            for vid in entries[:5]:
                title = vid.get("title", "No Title")
                vid_url = vid.get("webpage_url") or f"https://youtu.be/{vid['id']}"
                # Truncate long titles
                if len(title) > 50:
                    title = title[:47] + "..."
                keyboard.append([InlineKeyboardButton(title, callback_data=f"search_result|{vid_url}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="search_cancel")])
            await progress_msg.edit_text(
                "🎵 Top results:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        except Exception as e:
            await progress_msg.edit_text(f"❌ Search failed: {e}")


# ---------- Media Type / Search Selection Callback (FIXED) ----------
async def media_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the initial video/audio/thumbnail choice or search result click."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "type_cancel" or data == "search_cancel":
        await query.edit_message_text("❌ Cancelled.")
        return

    if data.startswith("type_"):
        # FIX: data format is "type_video|URL" (2 parts), not 3.
        parts = data.split("|", 1)
        if len(parts) != 2:
            await query.edit_message_text("❌ Invalid selection.")
            return
        type_part, url = parts
        media_type = type_part.split("_", 1)[1]  # "video", "audio", "thumb"
        user_id = query.from_user.id
        settings = get_user_settings(user_id)

        if media_type == "video":
            mode = settings["download_mode"]
            if mode == "fixed":
                quality = settings["default_quality"]
                await query.edit_message_text(f"⏳ Downloading video in {quality}...")
                await download_media(update, context, url, "video", quality=quality)
            else:
                # Manual: fetch dynamic qualities
                await query.edit_message_text("🔍 Fetching available qualities...")
                try:
                    loop = asyncio.get_running_loop()
                    with YoutubeDL({"quiet": True}) as ydl:
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
                    await query.edit_message_text(
                        "🎬 Choose video quality:",
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )
                except Exception as e:
                    await query.edit_message_text(f"❌ Could not fetch formats: {e}")

        elif media_type == "audio":
            await query.edit_message_text("🎵 Downloading MP3...")
            await download_media(update, context, url, "audio")

        elif media_type == "thumb":
            await query.edit_message_text("🖼 Downloading thumbnail...")
            await download_media(update, context, url, "thumbnail")

    elif data.startswith("quality_manual"):
        # data: "quality_manual|url|height"
        _, url, quality = data.split("|", 2)
        await query.edit_message_text(f"⬇ Downloading video in {quality}...")
        await download_media(update, context, url, "video", quality=quality)

    elif data.startswith("search_result"):
        # data: "search_result|url"
        _, vid_url = data.split("|", 1)
        keyboard = [
            [InlineKeyboardButton("🎬 Video", callback_data=f"type_video|{vid_url}")],
            [InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"type_audio|{vid_url}")],
            [InlineKeyboardButton("🖼 Thumbnail", callback_data=f"type_thumb|{vid_url}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="type_cancel")],
        ]
        await query.edit_message_text(
            "What would you like to download?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


# ---------- Error Handler (to prevent "No error handlers" log) ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a friendly message to the user."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    # If possible, notify the user
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ An internal error occurred. Please try again later.",
            )
        except Exception:
            pass


# ---------- Main ----------
def main() -> None:
    """Start the bot."""
    # Load existing settings
    load_settings()

    # Build application
    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^(set_|quality_|mode_|cleanup_|back_settings|close_settings)"))
    app.add_handler(CallbackQueryHandler(media_type_callback, pattern="^(type_|search_|quality_manual|search_cancel|type_cancel)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Register the error handler
    app.add_error_handler(error_handler)

    # Start polling
    logger.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()