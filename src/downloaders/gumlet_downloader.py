import logging
import re
import yt_dlp
from pathlib import Path
import requests

from .base import BaseDownloader
from src.config.settings_manager import SettingsManager
from src.utils.retry import build_ytdlp_retry_config


class GumletDownloader(BaseDownloader):
    """
    Downloader for Gumlet videos (play.gumlet.io).
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)
        self.settings = self.settings_manager.get_settings()

    def _extract_m3u8_url(self, embed_url: str, session: requests.Session, extra_props: dict = None) -> str | None:
        """
        Extracts the m3u8 URL from the Gumlet embed page.
        """
        logging.info(f"[Gumlet] Fetching embed page: {embed_url}...")

        headers = session.headers.copy()
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        })

        if extra_props and extra_props.get('referer'):
            headers['Referer'] = extra_props['referer']

        try:
            resp = session.get(embed_url, headers=headers, timeout=15)
            resp.raise_for_status()
            html = resp.text

            # Search for m3u8 URL: https://video.gumlet.io/{collection_id}/{video_id}/main.m3u8
            match = re.search(r'(https://video\.gumlet\.io/[a-f0-9]+/[a-f0-9]+/main\.m3u8)', html)
            if match:
                m3u8_url = match.group(1)
                logging.info(f"[Gumlet] M3U8 URL extracted: {m3u8_url}")
                return m3u8_url

            logging.warning("[Gumlet] M3U8 URL not found in embed page.")
            return None

        except Exception as e:
            logging.error(f"[Gumlet Error] {e}")
            return None

    def download_video(self, url: str, session: requests.Session, download_path: Path, extra_props: dict = None) -> bool:
        """
        Downloads the video from the Gumlet embed URL.
        """
        m3u8_url = self._extract_m3u8_url(url, session, extra_props)
        if not m3u8_url:
            return False

        logging.info(f"[Gumlet] Starting download from: {m3u8_url}")

        cookie_header = "; ".join([f"{c.name}={c.value}" for c in session.cookies])

        ydl_headers = {
            'User-Agent': session.headers.get('User-Agent', 'Mozilla/5.0'),
            'Cookie': cookie_header
        }

        if extra_props and extra_props.get('referer'):
            ydl_headers['Referer'] = extra_props['referer']
        elif 'Referer' in session.headers:
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

        if getattr(self.settings, 'keep_audio_only', False):
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            quality = getattr(self.settings, 'video_quality', 'highest')
            if quality == "Mais alta" or quality == "highest":
                ydl_opts['format'] = 'bestvideo+bestaudio/best'
            elif quality == "Mais baixa" or quality == "lowest":
                ydl_opts['format'] = 'worstvideo+bestaudio/worst'
            else:
                try:
                    target_height = int(str(quality).replace('p', ''))
                    ydl_opts['format'] = f"bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]"
                except Exception:
                    ydl_opts['format'] = 'bestvideo+bestaudio/best'

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([m3u8_url])
                return True
        except Exception as e:
            logging.error(f"[Gumlet yt-dlp Error] {e}")
            return False
