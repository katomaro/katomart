from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type
from pathlib import Path
from src.app.models import LessonContent, Attachment

from src.app.api_service import ApiService
from src.config.settings_manager import SettingsManager
import requests

class AuthFieldType(Enum):
    """Supported input widget types for authentication fields."""

    TEXT = "text"
    PASSWORD = "password"
    MULTILINE = "multiline"
    KEY_VALUE_LIST = "key_value_list"
    CHECKBOX = "checkbox"


@dataclass(frozen=True)
class AuthField:
    """Metadata that describes an authentication field required by a platform."""

    name: str
    label: str
    field_type: AuthFieldType = AuthFieldType.TEXT
    placeholder: str = ""
    key_label: str = "Chave"
    key_placeholder: str = ""
    value_label: str = "Valor"
    value_placeholder: str = ""
    required: bool = True
    requires_membership: bool = False


class BasePlatform(ABC):
    """
    Abstract base class for a scraping platform.
    Defines the interface that all platform implementations must follow.
    """
    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        self._api_service = api_service
        self._settings = settings_manager.get_settings()
        self._session: Optional[requests.Session] = None
        self.credentials: Dict[str, Any] = {}

    def refresh_auth(self) -> None:
        """
        Refreshes the authentication session.
        By default, it re-authenticates using the stored credentials.
        Subclasses can override this to use refresh tokens if available.
        """
        if self.credentials:
            self.authenticate(self.credentials)
        else:
            raise ValueError("No credentials available for refresh.")

    def resolve_access_token(
        self,
        credentials: Dict[str, Any],
        credential_token_provider: Callable[[str, str, Dict[str, Any]], str],
    ) -> str:
        """
        Determines whether to authenticate via token or via credentials.

        If the user has the katomart.FULL permission and both username and
        password are present, the provided credential_token_provider will be
        used to obtain the platform token. Otherwise, a filled token is used
        directly. Raises an error when neither option is viable.
        """

        token = (credentials.get("token") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()
        use_browser_emulation = bool(credentials.get("browser_emulation"))

        if self._settings.has_full_permissions:
            if username and password:
                return credential_token_provider(username, password, credentials)

            if use_browser_emulation:
                return credential_token_provider(username, password, credentials)

        if token:
            return token

        if self._settings.has_full_permissions and (username or password):
            raise ValueError("Informe usuário e senha completos ou utilize um token de acesso.")

        raise ValueError("Informe um token ou credenciais válidas para autenticação.")

    @abstractmethod
    def authenticate(self, credentials: Dict[str, Any]) -> None:
        """Authenticates on the platform and configures the session."""
        pass

    @abstractmethod
    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Fetches the list of available courses."""
        pass

    def search_courses(self, query: str) -> List[Dict[str, Any]]:
        """
        Searches for courses matching the query.
        Default implementation fetches all courses and filters locally.
        Subclasses should override this if the platform supports server-side search.
        """
        all_courses = self.fetch_courses()
        query_lower = query.lower()
        return [
            c for c in all_courses 
            if query_lower in c.get("name", "").lower() 
            or query_lower in c.get("seller_name", "").lower()
        ]

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

    @classmethod
    def token_field(cls) -> AuthField:
        """Returns the default optional token field."""
        return AuthField(
            name="token",
            label="Token de Acesso",
            field_type=AuthFieldType.PASSWORD,
            placeholder="Cole aqui o token obtido na plataforma",
            required=False,
        )

    @classmethod
    def membership_fields(cls) -> List[AuthField]:
        """Returns the default username/password fields for subscribers."""
        return [
            AuthField(
                name="username",
                label="Usuário / Email",
                placeholder="Digite o usuário da plataforma",
                requires_membership=True,
            ),
            AuthField(
                name="password",
                label="Senha",
                field_type=AuthFieldType.PASSWORD,
                placeholder="Digite a senha da plataforma",
                requires_membership=True,
            ),
            AuthField(
                name="browser_emulation",
                label="Emular Navegador (2FA/Captcha)",
                field_type=AuthFieldType.CHECKBOX,
                required=False,
                requires_membership=True,
            ),
        ]

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        """Platform-specific additional fields (e.g., 2FA or headers)."""
        return []

    @classmethod
    def all_auth_fields(cls) -> List[AuthField]:
        """Combines the default token/member fields with platform-specific ones."""
        fields: List[AuthField] = [cls.token_field(), *cls.membership_fields()]
        fields.extend(cls.auth_fields())
        return fields

    @classmethod
    def auth_instructions(cls) -> str:
        """Returns platform specific instructions for collecting credentials."""
        return "Nenhuma instrução disponível para esta plataforma."

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
    def get_platform_class(cls, name: str) -> Optional[Type[BasePlatform]]:
        """Returns the platform class for the provided name, if registered."""
        return cls._platforms.get(name)

    @classmethod
    def create_platform(cls, name: str, settings_manager: SettingsManager) -> Optional[BasePlatform]:
        """Creates a platform instance by name."""
        platform_class = cls._platforms.get(name)
        if platform_class:
            api_service = ApiService(settings_manager.get_settings())
            return platform_class(api_service, settings_manager)
        return None
