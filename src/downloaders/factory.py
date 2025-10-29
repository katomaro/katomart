from .base import BaseDownloader
from .ytdlp_downloader import YtdlpDownloader
from .hotmart_video_downloader import HotmartDownloader
from .requests_downloader import RequestsDownloader
from src.config.settings_manager import SettingsManager

class DownloaderFactory:
    """
    Factory for creating video downloaders.
    """

    @staticmethod
    def get_downloader(url: str, settings_manager: SettingsManager) -> BaseDownloader:
        """
        Gets the appropriate downloader based on the video URL.

        Args:
            url (str): The URL of the video.
            settings_manager (SettingsManager): The application settings manager.

        Returns:
            BaseDownloader: An instance of the appropriate downloader.
        """
        if "youtube.com" in url or "youtu.be" in url or "vimeo.com" in url:
            return YtdlpDownloader(settings_manager)
        elif "cf-embed.play.hotmart.com" in url:
            return HotmartDownloader(settings_manager)
        else:
            return RequestsDownloader(settings_manager)
