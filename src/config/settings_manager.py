import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict

@dataclass
class AppSettings:
    """A dataclass to hold application settings for type safety."""
    download_path: str = "./downloads"
    video_quality: str = "highest"
    max_concurrent_segment_downloads: int = 3
    timeout_seconds: int = 30
    download_subtitles: bool = True
    subtitle_language: str = "en"
    audio_language: str = "pt-BR"
    keep_audio_only: bool = False
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    hardcode_subtitles: bool = False
    run_ffmpeg: bool = False
    ffmpeg_args: str = "-c copy"
    download_embedded_videos: bool = True
    max_course_name_length: int = 40
    max_module_name_length: int = 60
    max_lesson_name_length: int = 60
    max_file_name_length: int = 30

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

    def get_settings(self) -> AppSettings:
        """Returns the current application settings."""
        return self._settings

    def save_settings(self, settings: AppSettings) -> bool:
        """
        Saves the provided settings to the JSON file.

        Args:
            settings: The AppSettings object to save.

        Returns:
            True if saving was successful, False otherwise.
        """
        self._settings = settings
        try:
            with open(self._settings_path, "w", encoding="utf-8") as f:
                json.dump(asdict(self._settings), f, indent=4)
            return True
        except IOError as e:
            logging.error(f"Failed to save settings to {self._settings_path}: {e}")
            return False
