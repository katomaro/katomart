from .base import BaseDownloader
from .ytdlp_downloader import YtdlpDownloader
from .hotmart_video_downloader import HotmartDownloader
from .requests_downloader import RequestsDownloader
from .pandavideo_downloader import PandaVideoDownloader
from .scaleup_downloader import ScaleUpDownloader
from .safevideo_downloader import SafeVideoDownloader
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
        elif "pandavideo.com" in url:
            return PandaVideoDownloader(settings_manager)
        elif "player.scaleup.com.br" in url:
            return ScaleUpDownloader(settings_manager)
        elif "safevideo.com" in url:
            return SafeVideoDownloader(settings_manager)
        elif ".m3u8" in url:
            return YtdlpDownloader(settings_manager)
        else:
            return YtdlpDownloader(settings_manager)
            # return RequestsDownloader(settings_manager)  # Fallback to RequestsDownloader for direct file links, needs better URL detection
