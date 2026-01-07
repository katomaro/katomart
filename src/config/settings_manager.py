from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Dict

@dataclass
class AppSettings:
    """A dataclass to hold application settings for type safety."""
    download_path: str = "./downloads"
    video_quality: str = "highest"
    max_concurrent_segment_downloads: int = 1
    timeout_seconds: int = 30
    download_subtitles: bool = True
    download_podcasts: bool = True
    subtitle_language: str = "en"
    audio_language: str = "pt-BR"
    keep_audio_only: bool = False
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    download_retry_attempts: int = 0
    download_retry_delay_seconds: int = 60
    download_widevine: bool = False
    cdm_path: str = ""
    use_http_proxy: bool = False
    proxy_address: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    proxy_port: int = 0
    hardcode_subtitles: bool = False
    run_ffmpeg: bool = False
    ffmpeg_args: str = "-c copy"
    download_embedded_videos: bool = True
    auto_reauth_on_error: bool = False
    embed_domain_blacklist: list[str] = field(default_factory=lambda: [
        "docs.google.com",
        "drive.google.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "linkedin.com",
        "pinterest.com",
        "imgur.com",
        "whatsapp.com",
        "wa.me",
        "t.me",
        "telegram.me",
        "telegram.org",
        "discord.gg"
    ])
    use_whisper_transcription: bool = False
    whisper_model: str = "base"
    whisper_language: str = "auto"
    whisper_output_format: str = "srt"
    max_course_name_length: int = 40
    max_module_name_length: int = 60
    max_lesson_name_length: int = 60
    max_file_name_length: int = 30
    permissions: list[str] = field(default_factory=list)
    has_full_permissions: bool = False
    membership_email: str = ""
    membership_password: str = ""
    save_membership_password: bool = False
    membership_token: str = ""
    allowed_platforms: list[str] = field(default_factory=list)
    is_premium_member: bool = False
    create_resume_summary: bool = False
    delete_folder_on_error: bool = False
    allowed_attachment_extensions: list[str] = field(default_factory=list)
    ffmpeg_path: str = "./ffmpeg/bin"
    bento4_path: str = "./bento4/bin"
    lesson_access_delay: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppSettings":
        """Creates an instance from a dictionary, ignoring unknown keys."""
        known_keys = {f.name for f in fields(cls)}
        filtered_data = {k: v for k, v in data.items() if k in known_keys}
        return cls(**filtered_data)

class SettingsManager:
    """Handles loading and saving application settings to a JSON file."""

    def __init__(self, settings_path: Path) -> None:
        """
        Initializes the SettingsManager.

        Args:
            settings_path: The path to the settings.json file.
        """
        self._settings_path = settings_path
        self._settings = self._load_settings()

    def _load_settings(self) -> AppSettings:
        """Loads settings from the JSON file, using defaults if not found."""
        try:
            if self._settings_path.exists():
                with open(self._settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return AppSettings.from_dict(data)
        except (IOError, json.JSONDecodeError) as e:
            logging.error(f"Failed to load settings from {self._settings_path}: {e}")

        return AppSettings()

    def _apply_paid_defaults(self, settings: AppSettings) -> AppSettings:
        """Ensure paid-only values fall back to defaults for free users."""
        if settings.has_full_permissions:
            return settings

        default = AppSettings()
        paid_only_fields = {
            "user_agent": default.user_agent,
            "max_concurrent_segment_downloads": default.max_concurrent_segment_downloads,
            "download_retry_attempts": default.download_retry_attempts,
            "download_retry_delay_seconds": default.download_retry_delay_seconds,
            "download_widevine": default.download_widevine,
            "cdm_path": default.cdm_path,
            "use_http_proxy": default.use_http_proxy,
            "proxy_address": default.proxy_address,
            "proxy_username": default.proxy_username,
            "proxy_password": default.proxy_password,
            "proxy_port": default.proxy_port,
            "use_whisper_transcription": default.use_whisper_transcription,
            "whisper_model": default.whisper_model,
            "whisper_language": default.whisper_language,
            "whisper_output_format": default.whisper_output_format,
            "create_resume_summary": default.create_resume_summary,
            "allowed_attachment_extensions": default.allowed_attachment_extensions,
        }
        return replace(settings, **paid_only_fields)

    def get_settings(self, include_premium: bool = False) -> AppSettings:
        """Returns the current application settings.

        Args:
            include_premium: When True, returns the cached settings without
                applying paid defaults even if the user lacks permissions.
        """
        if include_premium:
            return self._settings

        return (
            self._settings
            if self._settings.has_full_permissions
            else self._apply_paid_defaults(self._settings)
        )

    def save_settings(self, settings: AppSettings) -> bool:
        """
        Saves the provided settings to the JSON file.

        Args:
            settings: The AppSettings object to save.

        Returns:
            True if saving was successful, False otherwise.
        """
        if settings.has_full_permissions:
            merged_settings = settings
        else:
            default = AppSettings()
            premium_fields = {
                "user_agent",
                "max_concurrent_segment_downloads",
                "download_retry_attempts",
                "download_retry_delay_seconds",
                "download_widevine",
                "cdm_path",
                "use_http_proxy",
                "proxy_address",
                "proxy_username",
                "proxy_password",
                "proxy_port",
                "use_whisper_transcription",
                "whisper_model",
                "whisper_language",
                "whisper_output_format",
                "create_resume_summary",
                "allowed_attachment_extensions",
            }

            cached_premium_values = {
                field: getattr(self._settings, field, getattr(default, field))
                for field in premium_fields
            }

            merged_settings = replace(settings, **cached_premium_values)

        self._settings = merged_settings
        persisted_data = asdict(merged_settings)

        sensitive_keys = {
            "membership_token",
            "allowed_platforms",
            "is_premium_member",
            "permissions",
            "has_full_permissions",
        }
        for key in sensitive_keys:
            persisted_data.pop(key, None)

        try:
            with open(self._settings_path, "w", encoding="utf-8") as f:
                json.dump(persisted_data, f, indent=4)
            return True
        except IOError as e:
            logging.error(f"Failed to save settings to {self._settings_path}: {e}")
            return False
