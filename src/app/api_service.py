import requests
from typing import Optional
from src.config.settings_manager import AppSettings

class ApiService:
    """
    Manages the creation of a requests session for API communication.
    
    This service is intended to be passed to background workers that need to
    make authenticated network requests.
    """
    def __init__(self, settings: AppSettings) -> None:
        self._session: Optional[requests.Session] = None
        self._settings = settings

    def create_session(self, token: str) -> None:
        """
        Creates and configures a requests Session with the provided auth token.

        Args:
            token: The authentication token for the API.
        """
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "Origin": "https://consumer.hotmart.com",
            "Referer": "https://consumer.hotmart.com/",
            "User-Agent": self._settings.user_agent
        })

    def get_session(self) -> Optional[requests.Session]:
        """
        Returns the currently active requests Session.

        Returns:
            The configured requests.Session object, or None if not created.
        """
        return self._session
