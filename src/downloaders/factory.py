from .base import BaseDownloader
from .ytdlp_downloader import YtdlpDownloader
from .hotmart_video_downloader import HotmartDownloader
from .requests_downloader import RequestsDownloader
from .pandavideo_downloader import PandaVideoDownloader
from .scaleup_downloader import ScaleUpDownloader
from .safevideo_downloader import SafeVideoDownloader
from .udemy_video_downloader import UdemyDownloader
from .gumlet_downloader import GumletDownloader
from .spalla_downloader import SpallaDownloader
from .bunnystream_video_downloader import BunnyStreamDownloader
from src.config.settings_manager import SettingsManager

class DownloaderFactory:
    """
    Factory for creating video downloaders.
    """

    @staticmethod
    def get_downloader(url: str, settings_manager: SettingsManager, extra_props: dict = None) -> BaseDownloader:
        """
        Gets the appropriate downloader based on the video URL and extra properties.

        Args:
            url (str): The URL of the video.
            settings_manager (SettingsManager): The application settings manager.
            extra_props (dict): Extra properties that may influence downloader selection.

        Returns:
            BaseDownloader: An instance of the appropriate downloader.
        """
        extra_props = extra_props or {}

        if extra_props.get("is_encrypted") and extra_props.get("media_license_token"):
            return UdemyDownloader(settings_manager)

        # Udemy non-DRM media (HLS) is served from authenticated, Cloudflare-fronted
        # udemy hosts. The generic yt-dlp path 403s (wrong headers + the udemy:course
        # extractor hijacks the URL); UdemyDownloader._download_regular_video forwards
        # only browser-style headers and forces the generic extractor.
        if "udemy.com" in url or "udemycdn.com" in url:
            return UdemyDownloader(settings_manager)

        if "youtube.com" in url or "youtu.be" in url or "vimeo.com" in url:
            return YtdlpDownloader(settings_manager)
        elif "cf-embed.play.hotmart.com" in url:
            return HotmartDownloader(settings_manager)
        elif "pandavideo.com" in url:
            return PandaVideoDownloader(settings_manager)
        elif "player.scaleup.com.br" in url or "smartplayer.io" in url:
            return ScaleUpDownloader(settings_manager)
        elif "play.gumlet.io" in url:
            return GumletDownloader(settings_manager)
        elif "safevideo.com" in url:
            return SafeVideoDownloader(settings_manager)
        elif "spalla.io" in url:
            return SpallaDownloader(settings_manager)
        elif "iframe.mediadelivery.net" in url or "mediadelivery.net/embed" in url:
            return BunnyStreamDownloader(settings_manager)
        elif ".m3u8" in url:
            return YtdlpDownloader(settings_manager)
        else:
            return YtdlpDownloader(settings_manager)
