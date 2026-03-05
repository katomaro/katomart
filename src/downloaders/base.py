from abc import ABC, abstractmethod
from pathlib import Path
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
