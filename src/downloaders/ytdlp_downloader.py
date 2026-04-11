import logging

from pathlib import Path
import requests
import yt_dlp
from .base import BaseDownloader
from src.config.settings_manager import SettingsManager
from src.utils.retry import build_ytdlp_retry_config
from src.utils.filesystem import get_executable_path


class YtdlpDownloader(BaseDownloader):
    """
    A downloader that uses yt-dlp to download videos.
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)
        self.settings = self.settings_manager.get_settings()

    def download_video(self, url: str, session: requests.Session, download_path: Path, extra_props: dict = None) -> bool:
        """
        Downloads a video from a given URL using yt-dlp.

        Args:
            url (str): The URL of the video to download.
            session (requests.Session): The requests session (not used by yt-dlp).
            download_path (Path): The path to save the downloaded video.
            extra_props (dict, optional): Extra properties for the download, e.g. referer.

        Returns:
            bool: True if the download was successful, False otherwise.
        """
        output_template = self.build_ytdlp_output_template(download_path, self.settings)

        retry_opts = build_ytdlp_retry_config(self.settings)
        
        ffmpeg_exe = get_executable_path("ffmpeg", getattr(self.settings, "ffmpeg_path", None))

        ydl_opts = {
            'outtmpl': output_template,
            'noplaylist': True,
            'concurrent_fragment_downloads': max(1, self.settings.max_concurrent_segment_downloads),
            'quiet': True,
            'no_warnings': True,
            'progress': True,
            'socket_timeout': self.settings.timeout_seconds,
            **retry_opts,
        }

        ydl_opts.update(self.build_quality_opts(self.settings))

        if self.settings.user_agent:
            ydl_opts['user_agent'] = self.settings.user_agent

        cookies_path = getattr(self.settings, 'youtube_cookies_path', '')
        if cookies_path and Path(cookies_path).is_file():
            ydl_opts['cookiefile'] = cookies_path

        if ffmpeg_exe:
            ydl_opts['ffmpeg_location'] = str(Path(ffmpeg_exe).parent)

        if "vimeo" in url.lower():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                return True
            except Exception as e:
                logging.warning(f"Standard Vimeo download failed: {e}. Retrying with referer...")
                
                referer = extra_props.get('referer') if extra_props else None
                if not referer:
                    logging.error("No referer available for retry.")
                    return False

                opts_with_ref = ydl_opts.copy()
                opts_with_ref['http_headers'] = {'Referer': referer}

                try:
                    with yt_dlp.YoutubeDL(opts_with_ref) as ydl:
                        ydl.download([url])
                    return True
                except Exception as e2:
                    logging.error(f"Vimeo download with referer failed: {e2}")
                    return False

        if extra_props and 'referer' in extra_props:
            ydl_opts['http_headers'] = {'Referer': extra_props['referer']}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return True
        except Exception as e:
            logging.error(f"Error downloading with yt-dlp: {e}")
            return False
