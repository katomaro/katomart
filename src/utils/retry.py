from __future__ import annotations

from typing import Any, Dict

from src.config.settings_manager import AppSettings


def build_ytdlp_retry_config(settings: AppSettings) -> Dict[str, Any]:
    """Build yt-dlp retry options based on application settings.

    Args:
        settings: The application settings containing retry parameters.

    Returns:
        A dictionary with retry configuration keys supported by yt-dlp.
    """

    attempts = max(0, getattr(settings, "download_retry_attempts", 0))
    delay_seconds = max(0, getattr(settings, "download_retry_delay_seconds", 0))

    retry_opts: Dict[str, Any] = {
        "retries": attempts,
        "fragment_retries": attempts,
    }

    if delay_seconds:
        retry_opts["retry_sleep"] = {
            "http": delay_seconds,
            "fragment": delay_seconds,
        }

    return retry_opts
