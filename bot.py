import os
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from config import BOT_TOKEN, QUALITY_PRESETS, CLEANUP_TIMERS
from settings_manager import settings_manager
from downloader import downloader
from utils import is_youtube_url, cleanup_scheduler, format_duration

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# User state management
user_states = {}


# ==================== COMMAND HANDLERS ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    welcome_message = """
🎬 **Welcome to Advanced YouTube Downloader Bot!**

📋 **Features:**
✅ Download YouTube videos in any quality
✅ Convert videos to MP3
✅ Download thumbnails
✅ Search videos by name
✅ Customize your experience with settings

🔧 **Commands:**
/start - Show this message
/settings - Configure your preferences
/help - Get help

📥 **How to use:**
1️⃣ Send me a YouTube link
2️⃣ Choose what you want (Video/Audio/Thumbnail)
3️⃣ Select quality (if needed)
4️⃣ Wait for your file!

🔍 **Search:**
Just send me a song/video name to search!

Let's get started! 🚀
"""
    await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = """
📖 **Help & Guide**

**Downloading Videos:**
• Send any YouTube URL
• Choose Video option
• Select quality (or use your default)
• Receive as document

**Converting to MP3:**
• Send any YouTube URL
• Choose Audio (MP3) option
• Wait for conversion
• Receive as document

**Downloading Thumbnails:**
• Send any YouTube URL
• Choose Thumbnail option
• Receive image as document

**Searching Videos:**
• Send video/song name (not a URL)
• Choose from search results
• Continue normal flow

**Settings:**
Use /settings to customize:
• Default video quality
• Download mode (fixed/manual)
• Auto-cleanup timer

**Supported Sites:**
Currently supports YouTube (more coming soon!)

Need more help? Contact the developer!
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings command - show settings menu"""
    user_id = update.effective_user.id
    user_settings = await settings_manager.get_user_settings(user_id)
    
    keyboard = [
        [InlineKeyboardButton("🎬 Default Video Quality", callback_data="setting_quality")],
        [InlineKeyboardButton("🧹 Cleanup Timer", callback_data="setting_cleanup")],
        [InlineKeyboardButton("🔁 Download Mode", callback_data="setting_mode")],
        [InlineKeyboardButton("❌ Close", callback_data="setting_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    settings_text = f"""
⚙️ **Your Current Settings**

🎬 **Default Quality:** {user_settings['default_quality']}
🧹 **Cleanup Timer:** {user_settings['cleanup_timer']}
🔁 **Download Mode:** {user_settings['download_mode']}

Tap a setting to change it:
"""
    
    await update.message.reply_text(
        settings_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )


# ==================== MESSAGE HANDLER ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (URLs or search queries)"""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if is_youtube_url(text):
        await handle_youtube_url(update, context, text)
    else:
        await handle_search_query(update, context, text)


async def handle_youtube_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """Handle YouTube URL"""
    user_id = update.effective_user.id
    
    # Send processing message
    status_msg = await update.message.reply_text("🔍 Fetching video information...")
    
    try:
        # Extract video info
        info = await downloader.extract_info(url)
        
        if not info:
            await status_msg.edit_text("❌ Failed to extract video information.")
            return
        
        # Store info in user state
        user_states[user_id] = {
            'url': url,
            'info': info,
            'status_msg_id': status_msg.message_id
        }
        
        # Show video info and options
        title = info.get('title', 'Unknown')
        duration = format_duration(info.get('duration'))
        uploader = info.get('uploader', 'Unknown')
        
        info_text = f"""
📹 **Video Information**

**Title:** {title}
**Duration:** {duration}
**Uploader:** {uploader}

What would you like to download?
"""
        
        keyboard = [
            [InlineKeyboardButton("🎬 Video", callback_data="download_video")],
            [InlineKeyboardButton("🎵 Audio (MP3)", callback_data="download_audio")],
            [InlineKeyboardButton("🖼 Thumbnail", callback_data="download_thumbnail")],
            [InlineKeyboardButton("❌ Cancel", callback_data="download_cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await status_msg.edit_text(info_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")


async def handle_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    """Handle search query"""
    user_id = update.effective_user.id
    
    status_msg = await update.message.reply_text(f"🔍 Searching for: **{query}**...", parse_mode=ParseMode.MARKDOWN)
    
    try:
        results = await downloader.search_youtube(query, max_results=5)
        
        if not results:
            await status_msg.edit_text("❌ No results found. Try a different search term.")
            return
        
        # Show search results
        keyboard = []
        for idx, video in enumerate(results, 1):
            title = video.get('title', 'Unknown')[:50]  # Truncate long titles
            duration = format_duration(video.get('duration'))
            button_text = f"{idx}. {title} ({duration})"
            callback_data = f"search_select_{idx-1}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
        
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="search_cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Store search results in user state
        user_states[user_id] = {
            'search_results': results,
            'status_msg_id': status_msg.message_id
        }
        
        await status_msg.edit_text(
            f"🔍 **Search Results for:** {query}\n\nSelect a video:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Search error: {str(e)}")


# ==================== CALLBACK QUERY HANDLERS ====================

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    data = query.data
    
    # Settings callbacks
    if data.startswith("setting_"):
        await handle_setting_callback(update, context, data)
    
    # Download callbacks
    elif data.startswith("download_"):
        await handle_download_callback(update, context, data)
    
    # Quality selection callbacks
    elif data.startswith("quality_"):
        await handle_quality_callback(update, context, data)
    
    # Search callbacks
    elif data.startswith("search_"):
        await handle_search_callback(update, context, data)


async def handle_setting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Handle settings menu callbacks"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    if data == "setting_quality":
        keyboard = [
            [InlineKeyboardButton("360p", callback_data="setquality_360p")],
            [InlineKeyboardButton("480p", callback_data="setquality_480p")],
            [InlineKeyboardButton("720p", callback_data="setquality_720p")],
            [InlineKeyboardButton("1080p", callback_data="setquality_1080p")],
            [InlineKeyboardButton("Best Available", callback_data="setquality_Best Available")],
            [InlineKeyboardButton("« Back", callback_data="setting_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🎬 **Select Default Video Quality:**\n\nThis will be used when in Fixed Quality mode.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "setting_cleanup":
        keyboard = [
            [InlineKeyboardButton("5 Minutes", callback_data="setcleanup_5 Minutes")],
            [InlineKeyboardButton("10 Minutes", callback_data="setcleanup_10 Minutes")],
            [InlineKeyboardButton("15 Minutes", callback_data="setcleanup_15 Minutes")],
            [InlineKeyboardButton("30 Minutes", callback_data="setcleanup_30 Minutes")],
            [InlineKeyboardButton("♾ Never", callback_data="setcleanup_♾ Never")],
            [InlineKeyboardButton("« Back", callback_data="setting_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🧹 **Select Cleanup Timer:**\n\nFiles will be auto-deleted from server after this duration.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data == "setting_mode":
        keyboard = [
            [InlineKeyboardButton("✅ Fixed Quality", callback_data="setmode_Fixed Quality")],
            [InlineKeyboardButton("🎛 Manual Selection", callback_data="setmode_Manual Selection")],
            [InlineKeyboardButton("« Back", callback_data="setting_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🔁 **Select Download Mode:**\n\n"
            "**Fixed Quality:** Uses your default quality automatically\n"
            "**Manual Selection:** Shows quality options for each download",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif data.startswith("setquality_"):
        quality = data.replace("setquality_", "")
        await settings_manager.update_setting(user_id, "default_quality", quality)
        await query.answer(f"✅ Default quality set to {quality}", show_alert=True)
        await show_settings_menu(query)
    
    elif data.startswith("setcleanup_"):
        timer = data.replace("setcleanup_", "")
        await settings_manager.update_setting(user_id, "cleanup_timer", timer)
        await query.answer(f"✅ Cleanup timer set to {timer}", show_alert=True)
        await show_settings_menu(query)
    
    elif data.startswith("setmode_"):
        mode = data.replace("setmode_", "")
        await settings_manager.update_setting(user_id, "download_mode", mode)
        await query.answer(f"✅ Download mode set to {mode}", show_alert=True)
        await show_settings_menu(query)
    
    elif data == "setting_back":
        await show_settings_menu(query)
    
    elif data == "setting_close":
        await query.edit_message_text("⚙️ Settings closed.")


async def show_settings_menu(query):
    """Show main settings menu"""
    user_id = query.from_user.id
    user_settings = await settings_manager.get_user_settings(user_id)
    
    keyboard = [
        [InlineKeyboardButton("🎬 Default Video Quality", callback_data="setting_quality")],
        [InlineKeyboardButton("🧹 Cleanup Timer", callback_data="setting_cleanup")],
        [InlineKeyboardButton("🔁 Download Mode", callback_data="setting_mode")],
        [InlineKeyboardButton("❌ Close", callback_data="setting_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    settings_text = f"""
⚙️ **Your Current Settings**

🎬 **Default Quality:** {user_settings['default_quality']}
🧹 **Cleanup Timer:** {user_settings['cleanup_timer']}
🔁 **Download Mode:** {user_settings['download_mode']}

Tap a setting to change it:
"""
    
    await query.edit_message_text(
        settings_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )


async def handle_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Handle download option callbacks"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    if user_id not in user_states:
        await query.edit_message_text("❌ Session expired. Please send the URL again.")
        return
    
    state = user_states[user_id]
    
    if data == "download_video":
        # Check download mode
        download_mode = await settings_manager.get_setting(user_id, "download_mode")
        
        if download_mode == "Fixed Quality":
            # Use default quality directly
            default_quality = await settings_manager.get_setting(user_id, "default_quality")
            format_selector = downloader.get_format_selector(default_quality)
            
            # Start download
            await query.edit_message_text(f"📥 Downloading in {default_quality}...")
            await start_video_download(update, context, format_selector)
        else:
            # Show quality options
            await show_quality_options(update, context)
    
    elif data == "download_audio":
        await query.edit_message_text("🎵 Starting audio download and conversion...")
        await start_audio_download(update, context)
    
    elif data == "download_thumbnail":
        await query.edit_message_text("🖼 Downloading thumbnail...")
        await start_thumbnail_download(update, context)
    
    elif data == "download_cancel":
        await query.edit_message_text("❌ Download cancelled.")
        if user_id in user_states:
            del user_states[user_id]


async def show_quality_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available quality options"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    state = user_states.get(user_id)
    if not state:
        return
    
    info = state['info']
    formats = downloader.get_available_formats(info)
    
    if not formats:
        await query.edit_message_text("❌ No video formats available.")
        return
    
    # Create quality buttons
    keyboard = []
    for fmt in formats:
        button_text = f"{fmt['label']}"
        callback_data = f"quality_{fmt['height']}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    
    keyboard.append([InlineKeyboardButton("🌟 Best Available", callback_data="quality_best")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="download_cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🎬 **Select Video Quality:**",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )


async def handle_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Handle quality selection"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    quality = data.replace("quality_", "")
    
    if quality == "best":
        format_selector = "best"
        quality_label = "Best Available"
    else:
        format_selector = f"best[height<={quality}]"
        quality_label = f"{quality}p"
    
    await query.edit_message_text(f"📥 Downloading in {quality_label}...")
    await start_video_download(update, context, format_selector)


async def handle_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Handle search result selection"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    if data == "search_cancel":
        await query.edit_message_text("❌ Search cancelled.")
        if user_id in user_states:
            del user_states[user_id]
        return
    
    # Extract selected index
    index = int(data.replace("search_select_", ""))
    
    state = user_states.get(user_id)
    if not state or 'search_results' not in state:
        await query.edit_message_text("❌ Session expired. Please search again.")
        return
    
    selected_video = state['search_results'][index]
    video_url = selected_video.get('url') or f"https://youtube.com/watch?v={selected_video['id']}"
    
    # Process as normal YouTube URL
    await query.edit_message_text("🔍 Loading selected video...")
    
    try:
        info = await downloader.extract_info(video_url)
        
        # Update state
        user_states[user_id] = {
            'url': video_url,
            'info': info,
            'status_msg_id': query.message.message_id
        }
        
        # Show video options
        title = info.get('title', 'Unknown')
        duration = format_duration(info.get('duration'))
        
        info_text = f"""
📹 **Video Information**

**Title:** {title}
**Duration:** {duration}

What would you like to download?
"""
        
        keyboard = [
            [InlineKeyboardButton("🎬 Video", callback_data="download_video")],
            [InlineKeyboardButton("🎵 Audio (MP3)", callback_data="download_audio")],
            [InlineKeyboardButton("🖼 Thumbnail", callback_data="download_thumbnail")],
            [InlineKeyboardButton("❌ Cancel", callback_data="download_cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(info_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        await query.edit_message_text(f"❌ Error: {str(e)}")


# ==================== DOWNLOAD FUNCTIONS ====================

async def start_video_download(update: Update, context: ContextTypes.DEFAULT_TYPE, format_selector: str):
    """Start video download with progress updates"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    state = user_states.get(user_id)
    if not state:
        return
    
    url = state['url']
    
    # Progress callback
    async def progress_callback(message: str):
        try:
            await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass  # Ignore rate limit errors
    
    try:
        # Download video
        filepath = await downloader.download_video(url, format_selector, progress_callback)
        
        # Send as document
        await query.edit_message_text("📤 Uploading to Telegram...")
        
        with open(filepath, 'rb') as video_file:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=video_file,
                caption=f"🎬 {os.path.basename(filepath)}",
                reply_to_message_id=query.message.message_id
            )
        
        await query.edit_message_text("✅ Video sent successfully!")
        
        # Schedule cleanup
        cleanup_timer = await settings_manager.get_setting(user_id, "cleanup_timer")
        await cleanup_scheduler.schedule_cleanup(filepath, user_id, cleanup_timer)
        
    except Exception as e:
        await query.edit_message_text(f"❌ Download failed: {str(e)}")
    finally:
        if user_id in user_states:
            del user_states[user_id]


async def start_audio_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start audio download and conversion"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    state = user_states.get(user_id)
    if not state:
        return
    
    url = state['url']
    
    # Progress callback
    async def progress_callback(message: str):
        try:
            await query.edit_message_text(message, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
    
    try:
        # Download and convert to MP3
        filepath = await downloader.download_audio(url, progress_callback)
        
        # Send as document
        await query.edit_message_text("📤 Uploading MP3...")
        
        with open(filepath, 'rb') as audio_file:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=audio_file,
                caption=f"🎵 {os.path.basename(filepath)}",
                reply_to_message_id=query.message.message_id
            )
        
        await query.edit_message_text("✅ MP3 sent successfully!")
        
        # Schedule cleanup
        cleanup_timer = await settings_manager.get_setting(user_id, "cleanup_timer")
        await cleanup_scheduler.schedule_cleanup(filepath, user_id, cleanup_timer)
        
    except Exception as e:
        await query.edit_message_text(f"❌ Audio download failed: {str(e)}")
    finally:
        if user_id in user_states:
            del user_states[user_id]


      async def start_thumbnail_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start thumbnail download"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    state = user_states.get(user_id)
    if not state:
        return
    
    info = state['info']
    
    try:
        # Download thumbnail
        filepath = await downloader.download_thumbnail(info)
        
        # Send as document
        await query.edit_message_text("📤 Uploading thumbnail...")
        
        with open(filepath, 'rb') as thumb_file:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=thumb_file,
                caption=f"🖼 {os.path.basename(filepath)}",
                reply_to_message_id=query.message.message_id
            )
        
        await query.edit_message_text("✅ Thumbnail sent successfully!")
        
        # Schedule cleanup
        cleanup_timer = await settings_manager.get_setting(user_id, "cleanup_timer")
        await cleanup_scheduler.schedule_cleanup(filepath, user_id, cleanup_timer)
        
    except Exception as e:
        await query.edit_message_text(f"❌ Thumbnail download failed: {str(e)}")
    finally:
        if user_id in user_states:
            del user_states[user_id]


      # ==================== ERROR HANDLER ====================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ An unexpected error occurred. Please try again later."
        )


# ==================== MAIN ====================


def main():
    """Start the bot"""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(callback_query_handler))
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    # Start bot
    logger.info("🤖 Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
          
