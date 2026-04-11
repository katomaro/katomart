from abc import ABC, abstractmethod
from pathlib import Path
import re
import requests
from src.config.settings_manager import SettingsManager


class BaseDownloader(ABC):
    """
    Abstract base class for a video downloader.
    """

    def __init__(self, settings_manager: SettingsManager):
        """
        Initializes the downloader with a settings manager.

        Args:
            settings_manager (SettingsManager): The application settings manager.
        """
        self.settings_manager = settings_manager

    @staticmethod
    def build_quality_opts(settings) -> dict:
        """Build yt-dlp format/postprocessor options based on quality settings."""
        opts: dict = {}

        if getattr(settings, 'keep_audio_only', False):
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
            return opts

        quality = getattr(settings, 'video_quality', 'Mais alta')
        if quality in ("Mais alta", "highest"):
            opts['format'] = 'bestvideo+bestaudio/best'
        elif quality in ("Mais baixa", "lowest"):
            opts['format'] = 'worstvideo+bestaudio/worst'
        else:
            try:
                target_height = int(str(quality).replace('p', ''))
                opts['format'] = (
                    f"bestvideo[height<={target_height}]+bestaudio"
                    f"/best[height<={target_height}]"
                    f"/worstvideo+bestaudio/worst"
                )
            except (ValueError, AttributeError):
                opts['format'] = 'bestvideo+bestaudio/best'

        return opts

    @staticmethod
    def _has_likely_extension(path: Path) -> bool:
        suffix = path.suffix
        return bool(suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,5}", suffix))

    def build_ytdlp_output_template(self, download_path: Path, settings) -> str:
        """Build yt-dlp outtmpl while preserving numbered prefixes when requested."""

        prefer_original_name = bool(getattr(settings, "try_keep_original_video_name", False))

        if prefer_original_name:
            stem = download_path.name
            match = re.match(r"^(\d+\.\s*)", stem)
            if match:
                output_stem = f"{match.group(1)}%(title).200B"
            else:
                output_stem = "%(title).200B"
            output_template = str(download_path.with_name(output_stem))
        else:
            output_template = str(download_path)

        if not self._has_likely_extension(download_path):
            output_template += ".%(ext)s"

        return output_template

    @abstractmethod
    def download_video(self, url: str, session: requests.Session, download_path: Path, extra_props: dict = None) -> bool:
        """
        Downloads a video from a given URL and saves it to `download_path`.

        Args:
            url (str): The URL of the video to download.
            session (requests.Session): The requests session to use for downloading.
            download_path (Path): The destination file or template for the download.
            extra_props (dict, optional): Extra properties for the download.

        Returns:
            bool: True if the download was successful, False otherwise.
        """
        raise NotImplementedError()
