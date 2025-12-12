import logging
import re

from pathlib import Path
import requests
import yt_dlp
from .base import BaseDownloader
from src.config.settings_manager import SettingsManager
from src.utils.retry import build_ytdlp_retry_config


class YtdlpDownloader(BaseDownloader):
    """
    A downloader that uses yt-dlp to download videos.
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)
        self.settings = self.settings_manager.get_settings()

    def download_video(self, url: str, session: requests.Session, download_path: Path) -> bool:
        """
        Downloads a video from a given URL using yt-dlp.

        Args:
            url (str): The URL of the video to download.
            session (requests.Session): The requests session (not used by yt-dlp).
            download_path (Path): The path to save the downloaded video.

        Returns:
            bool: True if the download was successful, False otherwise.
        """
        def _has_extension(path: Path) -> bool:
            """Return True when the path ends with a likely file extension."""

            suffix = path.suffix
            return bool(suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,5}", suffix))

        output_template = str(download_path)
        if not _has_extension(download_path):
            output_template += ".%(ext)s"

        retry_opts = build_ytdlp_retry_config(self.settings)
        ydl_opts = {
            'outtmpl': output_template,
            'noplaylist': True,
            'concurrent_fragment_downloads': max(1, self.settings.max_concurrent_segment_downloads),
            'quiet': True,
            'no_warnings': True,
            'progress': True,
            **retry_opts,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return True
        except Exception as e:
            logging.error(f"Error downloading with yt-dlp: {e}")
            return False
