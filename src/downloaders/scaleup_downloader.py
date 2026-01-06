import logging
import re
import yt_dlp
from pathlib import Path
from urllib.parse import urlparse
import requests

from .base import BaseDownloader
from src.config.settings_manager import SettingsManager
from src.utils.retry import build_ytdlp_retry_config

class ScaleUpDownloader(BaseDownloader):
    """
    Downloader for ScaleUp/SmartPlayer videos.
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)
        self.settings = self.settings_manager.get_settings()

    def _extract_m3u8_url(self, embed_url: str, session: requests.Session) -> str | None:
        """
        Extracts the m3u8 URL from the ScaleUp embed page.
        """
        logging.info(f"[ScaleUp/Smartplayer/TEMP] Acessando Embed: {embed_url}...")

        headers = session.headers.copy()
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        })

        try:
            resp = session.get(embed_url, headers=headers, timeout=15)
            resp.raise_for_status()
            html = resp.text

            # Search for: 'video':'https://...'
            match = re.search(r"'video'\s*:\s*'(https?://[^']+)'", html)
            
            if match:
                video_url = match.group(1)
                logging.info(f"[ScaleUp] URL Extracted (Regex JSON): {video_url[:60]}...")
                return video_url

            match_fallback = re.search(r"file[\"']?\s*:\s*[\"'](https?://[^\"']+\.m3u8[^\"']*)[\"']", html)
            if match_fallback:
                video_url = match_fallback.group(1)
                logging.info(f"[ScaleUp] URL Extracted (Regex Fallback): {video_url[:60]}...")
                return video_url

            logging.warning("[ScaleUp] URL not found in embed HTML.")
            return None

        except Exception as e:
            logging.error(f"[ScaleUp Error] {e}")
            return None

    def download_video(self, url: str, session: requests.Session, download_path: Path, extra_props: dict = None) -> bool:
        """
        Downloads the video from the ScaleUp embed URL.
        """
        m3u8_url = self._extract_m3u8_url(url, session)
        if not m3u8_url:
            return False

        logging.info(f"[ScaleUp] Starting download from: {m3u8_url}")
        
        cookie_header = "; ".join([f"{c.name}={c.value}" for c in session.cookies])

        ydl_headers = {
            'User-Agent': session.headers.get('User-Agent', 'Mozilla/5.0'),
            'Cookie': cookie_header
        }

        if 'Referer' in session.headers:
            ydl_headers['Referer'] = session.headers['Referer']
        if 'Origin' in session.headers:
            ydl_headers['Origin'] = session.headers['Origin']

        retry_opts = build_ytdlp_retry_config(self.settings)
        ydl_opts = {
            'outtmpl': str(download_path),
            'noplaylist': True,
            'http_headers': ydl_headers,
            'quiet': True,
            'no_warnings': True,
            'progress': True,
            'concurrent_fragment_downloads': max(1, getattr(self.settings, 'max_concurrent_segment_downloads', 10)),
            **retry_opts,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([m3u8_url])
                return True
        except Exception as e:
            logging.error(f"[ScaleUp yt-dlp Error] {e}")
            return False
