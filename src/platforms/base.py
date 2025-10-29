from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Type, Any
from pathlib import Path
from src.app.models import LessonContent, Attachment

from src.app.api_service import ApiService
from src.config.settings_manager import SettingsManager
import requests

class BasePlatform(ABC):
    """
    Abstract base class for a scraping platform.
    Defines the interface that all platform implementations must follow.
    """
    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        self._api_service = api_service
        self._settings = settings_manager.get_settings()
        self._session: Optional[requests.Session] = None

    @abstractmethod
    def authenticate(self, credentials: Dict[str, str]) -> None:
        """Authenticates on the platform and configures the session."""
        pass

    @abstractmethod
    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Fetches the list of available courses."""
        pass

    @abstractmethod
    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fetches the modules and lessons for the selected courses."""
        pass

    @abstractmethod
    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        """Fetches detailed information for a single lesson, like video URLs."""
        pass

    @abstractmethod
    def download_attachment(self, attachment: "Attachment", download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        """Downloads an attachment using platform-specific logic."""
        pass

    def get_session(self) -> Optional[requests.Session]:
        """Returns the authenticated requests session."""
        return self._session


PLATFORM_REGISTRY = {
}

def register_platform(name: str):
    """Decorator to register a platform class."""
    def decorator(cls):
        PLATFORM_REGISTRY[name] = cls
        return cls
    return decorator

class PlatformFactory:
    """A factory to create platform instances."""
    _platforms: Dict[str, Type[BasePlatform]] = {}

    @classmethod
    def register_platform(cls, name: str, platform_class: Type[BasePlatform]) -> None:
        """Registers a new platform class."""
        cls._platforms[name] = platform_class

    @classmethod
    def get_platform_names(cls) -> List[str]:
        """Returns a list of registered platform names."""
        return list(cls._platforms.keys())

    @classmethod
    def create_platform(cls, name: str, settings_manager: SettingsManager) -> Optional[BasePlatform]:
        """Creates a platform instance by name."""
        platform_class = cls._platforms.get(name)
        if platform_class:
            api_service = ApiService(settings_manager.get_settings())
            return platform_class(api_service, settings_manager)
        return None
