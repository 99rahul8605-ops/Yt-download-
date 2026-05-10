import re
import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from config import CLEANUP_TIMERS

class FileCleanupScheduler:
    """Handles automatic file cleanup based on user settings"""
    
    def __init__(self):
        self.scheduled_cleanups = {}
    
    async def schedule_cleanup(self, filepath: str, user_id: int, timer_setting: str):
        """Schedule a file for deletion"""
        delay = CLEANUP_TIMERS.get(timer_setting)
        
        if delay is None:  # Never delete
            return
        
        if not os.path.exists(filepath):
            return
        
        # Cancel existing cleanup for this file
        if filepath in self.scheduled_cleanups:
            self.scheduled_cleanups[filepath].cancel()
        
        # Schedule new cleanup
        task = asyncio.create_task(self._cleanup_file(filepath, delay))
        self.scheduled_cleanups[filepath] = task
    
    async def _cleanup_file(self, filepath: str, delay: int):
        """Delete file after delay"""
        try:
            await asyncio.sleep(delay)
            if os.path.exists(filepath):
                os.remove(filepath)
                print(f"🧹 Cleaned up: {filepath}")
        except Exception as e:
            print(f"⚠️ Cleanup error for {filepath}: {e}")
        finally:
            if filepath in self.scheduled_cleanups:
                del self.scheduled_cleanups[filepath]

# Global instance
cleanup_scheduler = FileCleanupScheduler()


def is_youtube_url(url: str) -> bool:
    """Validate if string is a YouTube URL"""
    youtube_regex = r'(https?://)?(www\.)?(youtube\.com|youtu\.be)/.+'
    return bool(re.match(youtube_regex, url))


def sanitize_filename(filename: str) -> str:
    """Remove invalid characters from filename"""
    return re.sub(r'[<>:"/\\|?*]', '', filename)


def format_bytes(bytes: int) -> str:
    """Format bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024.0:
            return f"{bytes:.2f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.2f} TB"


def format_duration(seconds: int) -> str:
    """Format seconds to HH:MM:SS"""
    if seconds is None:
        return "Unknown"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
