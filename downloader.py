import os
import asyncio
from typing import Dict, List, Optional, Callable
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError
from config import DOWNLOAD_DIR, YT_DLP_OPTIONS, QUALITY_PRESETS, MAX_FILE_SIZE
from utils import sanitize_filename, format_bytes

class ProgressTracker:
    """Track download progress and report via callback"""
    
    def __init__(self, callback: Callable):
        self.callback = callback
        self.last_update = 0
    
    async def __call__(self, d):
        """yt-dlp progress hook"""
        if d['status'] == 'downloading':
            # Throttle updates to every 2 seconds
            current_time = asyncio.get_event_loop().time()
            if current_time - self.last_update < 2:
                return
            self.last_update = current_time
            
            percent = d.get('_percent_str', 'N/A')
            speed = d.get('_speed_str', 'N/A')
            eta = d.get('_eta_str', 'N/A')
            
            message = f"⬇️ **Downloading...**\n\n"
            message += f"Progress: {percent}\n"
            message += f"Speed: {speed}\n"
            message += f"ETA: {eta}"
            
            await self.callback(message)
        
        elif d['status'] == 'finished':
            await self.callback("✅ Download complete! Processing...")


class YouTubeDownloader:
    """Handles all YouTube download operations using yt-dlp"""
    
    def __init__(self):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    async def extract_info(self, url: str) -> Optional[Dict]:
        """Extract video metadata without downloading"""
        try:
            ydl_opts = {
                **YT_DLP_OPTIONS,
                'skip_download': True,
            }
            
            loop = asyncio.get_event_loop()
            with YoutubeDL(ydl_opts) as ydl:
                info = await loop.run_in_executor(
                    None, 
                    lambda: ydl.extract_info(url, download=False)
                )
                return info
        except ExtractorError as e:
            raise Exception(f"❌ Extraction failed: {str(e)}")
        except DownloadError as e:
            raise Exception(f"❌ Download error: {str(e)}")
        except Exception as e:
            raise Exception(f"❌ Unexpected error: {str(e)}")
    
    async def search_youtube(self, query: str, max_results: int = 5) -> List[Dict]:
        """Search YouTube and return results"""
        try:
            search_query = f"ytsearch{max_results}:{query}"
            ydl_opts = {
                **YT_DLP_OPTIONS,
                'skip_download': True,
            }
            
            loop = asyncio.get_event_loop()
            with YoutubeDL(ydl_opts) as ydl:
                result = await loop.run_in_executor(
                    None,
                    lambda: ydl.extract_info(search_query, download=False)
                )
                
                if 'entries' in result:
                    return result['entries']
                return []
        except Exception as e:
            raise Exception(f"❌ Search failed: {str(e)}")
    
    def get_available_formats(self, info: Dict) -> List[Dict]:
        """Extract available video formats with resolution"""
        formats = []
        seen_heights = set()
        
        for f in info.get('formats', []):
            # Only video formats with height and video codec
            if f.get('vcodec') != 'none' and f.get('height'):
                height = f['height']
                if height not in seen_heights:
                    formats.append({
                        'format_id': f['format_id'],
                        'height': height,
                        'ext': f.get('ext', 'mp4'),
                        'filesize': f.get('filesize', 0),
                        'label': f"{height}p"
                    })
                    seen_heights.add(height)
        
        # Sort by height
        formats.sort(key=lambda x: x['height'])
        return formats
    
    def get_format_selector(self, quality_setting: str) -> str:
        """Get yt-dlp format selector from quality setting"""
        return QUALITY_PRESETS.get(quality_setting, "best")
    
    async def download_video(
        self, 
        url: str, 
        format_selector: str,
        progress_callback: Optional[Callable] = None
    ) -> str:
        """Download video and return filepath"""
        try:
            output_template = os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s')
            
            ydl_opts = {
                **YT_DLP_OPTIONS,
                'format': f'{format_selector}+bestaudio/best',
                'outtmpl': output_template,
                'merge_output_format': 'mp4',
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }],
            }
            
            # Add progress hook if callback provided
            if progress_callback:
                progress_hook = ProgressTracker(progress_callback)
                ydl_opts['progress_hooks'] = [
                    lambda d: asyncio.create_task(progress_hook(d))
                ]
            
            loop = asyncio.get_event_loop()
            with YoutubeDL(ydl_opts) as ydl:
                info = await loop.run_in_executor(
                    None,
                    lambda: ydl.extract_info(url, download=True)
                )
                
                # Get the actual filename
                filename = ydl.prepare_filename(info)
                
                # Check if file exists (might have different extension after processing)
                if not os.path.exists(filename):
                    # Try .mp4 extension
                    base = os.path.splitext(filename)[0]
                    filename = f"{base}.mp4"
                
                if not os.path.exists(filename):
                    raise Exception("Downloaded file not found")
                
                # Check file size
                filesize = os.path.getsize(filename)
                if filesize > MAX_FILE_SIZE:
                    os.remove(filename)
                    raise Exception(f"File too large ({format_bytes(filesize)}). Telegram limit is 2GB.")
                
                return filename
                
        except Exception as e:
            raise Exception(f"❌ Video download failed: {str(e)}")
    
    async def download_audio(
        self,
        url: str,
        progress_callback: Optional[Callable] = None
    ) -> str:
        """Download and convert to MP3"""
        try:
            output_template = os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s')
            
            ydl_opts = {
                **YT_DLP_OPTIONS,
                'format': 'bestaudio/best',
                'outtmpl': output_template,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
            
            if progress_callback:
                progress_hook = ProgressTracker(progress_callback)
                ydl_opts['progress_hooks'] = [
                    lambda d: asyncio.create_task(progress_hook(d))
                ]
            
            loop = asyncio.get_event_loop()
            with YoutubeDL(ydl_opts) as ydl:
                info = await loop.run_in_executor(
                    None,
                    lambda: ydl.extract_info(url, download=True)
                )
                
                # Get filename with mp3 extension
                base_filename = ydl.prepare_filename(info)
                filename = os.path.splitext(base_filename)[0] + '.mp3'
                
                if not os.path.exists(filename):
                    raise Exception("Audio file not found after conversion")
                
                # Check file size
                filesize = os.path.getsize(filename)
                if filesize > MAX_FILE_SIZE:
                    os.remove(filename)
                    raise Exception(f"File too large ({format_bytes(filesize)}). Telegram limit is 2GB.")
                
                return filename
                
        except Exception as e:
            raise Exception(f"❌ Audio download failed: {str(e)}")
    
    async def download_thumbnail(self, info: Dict) -> str:
        """Download video thumbnail"""
        try:
            import aiohttp
            
            thumbnail_url = info.get('thumbnail')
            if not thumbnail_url:
                raise Exception("No thumbnail available")
            
            title = sanitize_filename(info.get('title', 'thumbnail'))
            filepath = os.path.join(DOWNLOAD_DIR, f"{title}.jpg")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(thumbnail_url) as resp:
                    if resp.status == 200:
                        with open(filepath, 'wb') as f:
                            f.write(await resp.read())
                        return filepath
                    else:
                        raise Exception(f"Failed to download thumbnail: HTTP {resp.status}")
                        
        except Exception as e:
            raise Exception(f"❌ Thumbnail download failed: {str(e)}")

# Global instance
downloader = YouTubeDownloader()
