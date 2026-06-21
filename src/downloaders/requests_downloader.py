
import logging
from pathlib import Path
import requests
from .base import BaseDownloader
from .errors import PermanentDownloadError
from src.config.settings_manager import SettingsManager


class RequestsDownloader(BaseDownloader):
    """
    A downloader that uses the requests library for custom video downloads.
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)

    def download_video(self, url: str, session: requests.Session, download_path: Path, extra_props: dict = None) -> bool:
        """
        Downloads a video from a given URL using the requests library.

        Args:
            url (str): The URL of the video to download.
            session (requests.Session): The requests session to use for downloading.
            download_path (Path): The path to save the downloaded video.
            extra_props (dict, optional): Extra properties for the download.

        Returns:
            bool: True if the download was successful, False otherwise.
        """
        try:
            with session.get(url, stream=True) as r:
                r.raise_for_status()
                with open(download_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except requests.exceptions.HTTPError as e:
            logging.error(f"Error downloading with requests: {e}")
            # A 4xx (other than 429) is permanent — the resource is gone or
            # forbidden; retrying only wastes backoff time.
            status = e.response.status_code if e.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                raise PermanentDownloadError(f"HTTP {status}: {url}") from e
            return False
        except requests.exceptions.RequestException as e:
            logging.error(f"Error downloading with requests: {e}")
            return False
