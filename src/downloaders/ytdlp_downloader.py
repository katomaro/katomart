import logging
import re

from pathlib import Path
import requests
import yt_dlp
from .base import BaseDownloader
from .errors import PermanentDownloadError
from src.config.settings_manager import SettingsManager
from src.utils.retry import build_ytdlp_retry_config
from src.utils.filesystem import get_executable_path


# Why: yt-dlp surfaces failures as a single DownloadError whose *message* is the
# only reliable signal of whether the failure is permanent. These substrings
# mean "retrying will never help" — a dead/expired link, a non-video URL handed
# to the generic extractor (e.g. a community invite), DRM without a CDM, or a
# malformed URL. Matched case-insensitively against the exception text.
_PERMANENT_YTDLP_SIGNATURES = (
    "http error 404",
    "http error 410",
    "unsupported url",
    "is not a valid url",
    "unable to download webpage: http error 4",  # any 4xx fetching the page
    "requested format is not available",
    "this video is drm protected",
    "drm protected",
    "no video formats found",
    "video unavailable",
    "private video",
    "members-only content",
    "this video has been removed",
)


def _is_permanent_ytdlp_error(message: str) -> bool:
    """Return True when a yt-dlp error message indicates a non-retryable failure."""
    lowered = message.lower()
    if any(sig in lowered for sig in _PERMANENT_YTDLP_SIGNATURES):
        return True
    # A 4xx (other than 429 rate-limiting) reported as "HTTP Error <code>".
    match = re.search(r"http error (\d{3})", lowered)
    if match:
        code = int(match.group(1))
        if 400 <= code < 500 and code != 429:
            return True
    return False


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
        ydl_opts.update(self.build_js_runtime_opts(self.settings))

        if self.settings.user_agent:
            ydl_opts['user_agent'] = self.settings.user_agent

        cookies_path = getattr(self.settings, 'youtube_cookies_path', '')
        if cookies_path and Path(cookies_path).is_file():
            ydl_opts['cookiefile'] = cookies_path

        if ffmpeg_exe:
            ydl_opts['ffmpeg_location'] = str(Path(ffmpeg_exe).parent)

        if "vimeo" in url.lower():
            # Why: domain-restricted Vimeo embeds require the embedding site's
            # Referer from the very first request — without it Vimeo serves a
            # "not available" page that yt-dlp can't extract.
            referer = extra_props.get('referer') if extra_props else None
            vimeo_opts = ydl_opts.copy()
            if referer:
                vimeo_opts['http_headers'] = {'Referer': referer}
            try:
                with yt_dlp.YoutubeDL(vimeo_opts) as ydl:
                    ydl.download([url])
                return True
            except Exception as e:
                logging.error(f"Vimeo download failed: {e}")
                if _is_permanent_ytdlp_error(str(e)):
                    raise PermanentDownloadError(f"Vimeo: {e}") from e
                return False

        if extra_props and 'referer' in extra_props:
            ydl_opts['http_headers'] = {'Referer': extra_props['referer']}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return True
        except Exception as e:
            # Why: Cloudflare-fronted hosts reject the generic extractor's
            # default HTTP client; yt-dlp's own error message tells users to
            # retry with `generic:impersonate` so it routes through curl_cffi
            # with a browser TLS/JA3 fingerprint.
            message = str(e).lower()
            if 'impersonate' in message or 'cloudflare' in message or '403' in message:
                logging.warning(
                    f"yt-dlp blocked (likely Cloudflare); retrying with generic:impersonate. Original error: {e}"
                )
                fallback_opts = ydl_opts.copy()
                fallback_opts['extractor_args'] = {'generic': {'impersonate': ['']}}
                try:
                    with yt_dlp.YoutubeDL(fallback_opts) as ydl:
                        ydl.download([url])
                    return True
                except Exception as e2:
                    logging.error(f"Error downloading with yt-dlp (impersonate fallback): {e2}")
                    if _is_permanent_ytdlp_error(str(e2)):
                        raise PermanentDownloadError(str(e2)) from e2
                    return False
            logging.error(f"Error downloading with yt-dlp: {e}")
            if _is_permanent_ytdlp_error(str(e)):
                raise PermanentDownloadError(str(e)) from e
            return False
