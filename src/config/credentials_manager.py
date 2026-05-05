import json
import logging
from pathlib import Path
from typing import Dict, Optional

class CredentialsManager:
    """Handles loading and saving user credentials to a JSON file."""

    def __init__(self, credentials_path: Path = Path("credentials.json")) -> None:
        self._credentials_path = credentials_path
        self._credentials: Dict[str, Dict[str, str]] = self._load_credentials()

    def _load_credentials(self) -> Dict[str, Dict[str, str]]:
        """Loads credentials from the JSON file."""
        if not self._credentials_path.exists():
            return {}
        try:
            with open(self._credentials_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logging.error(f"Failed to load credentials from {self._credentials_path}: {e}")
            return {}

    def save_credentials(self, platform: str, email: str, password: str) -> None:
        """Saves credentials for a specific platform."""
        self._credentials[platform] = {"email": email, "password": password}
        self._save_to_file()

    def get_credentials(self, platform: str) -> Optional[Dict[str, str]]:
        """Retrieves credentials for a specific platform."""
        return self._credentials.get(platform)

    def clear_credentials(self) -> None:
        """Clears all saved credentials."""
        self._credentials = {}
        self._save_to_file()
        if self._credentials_path.exists():
             try:
                self._credentials_path.unlink()
             except OSError as e:
                logging.error(f"Failed to delete credentials file: {e}")


    def _save_to_file(self) -> None:
        """Writes the current credentials to the JSON file."""
        try:
            with open(self._credentials_path, "w", encoding="utf-8") as f:
                json.dump(self._credentials, f, indent=4)
        except IOError as e:
            logging.error(f"Failed to save credentials to {self._credentials_path}: {e}")
