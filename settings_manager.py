import json
import os
import asyncio
from typing import Dict, Any, Optional
from config import SETTINGS_FILE

class SettingsManager:
    """Manages persistent user settings"""
    
    def __init__(self):
        self.settings: Dict[int, Dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self._load_settings()
    
    def _load_settings(self):
        """Load settings from JSON file"""
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r') as f:
                    data = json.load(f)
                    # Convert string keys back to integers
                    self.settings = {int(k): v for k, v in data.items()}
            except Exception as e:
                print(f"⚠️ Error loading settings: {e}")
                self.settings = {}
        else:
            self.settings = {}
    
    async def _save_settings(self):
        """Save settings to JSON file"""
        async with self.lock:
            try:
                with open(SETTINGS_FILE, 'w') as f:
                    # Convert int keys to strings for JSON
                    data = {str(k): v for k, v in self.settings.items()}
                    json.dump(data, f, indent=2)
            except Exception as e:
                print(f"⚠️ Error saving settings: {e}")
    
    async def get_user_settings(self, user_id: int) -> Dict[str, Any]:
        """Get settings for a user, create defaults if not exist"""
        if user_id not in self.settings:
            self.settings[user_id] = {
                "default_quality": "720p",
                "cleanup_timer": "10 Minutes",
                "download_mode": "Manual Selection"
            }
            await self._save_settings()
        return self.settings[user_id]
    
    async def update_setting(self, user_id: int, key: str, value: Any):
        """Update a specific setting for a user"""
        if user_id not in self.settings:
            await self.get_user_settings(user_id)
        self.settings[user_id][key] = value
        await self._save_settings()
    
    async def get_setting(self, user_id: int, key: str, default=None) -> Any:
        """Get a specific setting value"""
        settings = await self.get_user_settings(user_id)
        return settings.get(key, default)

# Global instance
settings_manager = SettingsManager()
