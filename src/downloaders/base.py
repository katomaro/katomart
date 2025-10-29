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
    def download_video(self, url: str, session: requests.Session) -> bool:
        """
        Downloads a video from a given URL.

        Args:
            url (str): The URL of the video to download.
            session (requests.Session): The requests session to use for downloading.

        Returns:
            bool: True if the download was successful, False otherwise.
        """
        pass
