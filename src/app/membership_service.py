from __future__ import annotations

from dataclasses import dataclass
from typing import List

import requests


@dataclass(frozen=True)
class MembershipInfo:
    """Represents the response from the membership authentication API."""

    token: str
    allowed_platforms: List[str]
    is_premium: bool


class MembershipService:
    """Client to authenticate the user with the Katomart membership backend."""

    def __init__(self, base_url: str, timeout: int = 15) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = timeout

    def authenticate(self, email: str, password: str) -> MembershipInfo:
        """Authenticates the user and returns the membership info."""
        if not self._base_url:
            raise ValueError("Nenhum endpoint configurado para autenticação do software.")

        url = f"{self._base_url}/auth/login"
        payload = {"email": email, "password": password}

        response = requests.post(url, json=payload, timeout=self._timeout)
        response.raise_for_status()

        data = response.json()
        token = (data.get("token") or "").strip()
        if not token:
            raise ValueError("A API não retornou um token de autenticação.")

        allowed = data.get("allowedPlatforms") or data.get("platforms") or []
        if not isinstance(allowed, list):
            allowed = []

        plan_label = str(data.get("plan") or data.get("membership") or "").lower()
        is_premium = bool(data.get("isPremium")) or plan_label in {"premium", "paid", "pro"}

        return MembershipInfo(token=token, allowed_platforms=allowed, is_premium=is_premium)
