from __future__ import annotations

from dataclasses import dataclass
from typing import List

import requests

from src.platforms.base import PlatformFactory



PEDIDO = """
Se você está lendo essa mensagem, você muito provavelmente está vendo uma forma de burlar o acesso a alguma funcionalidade paga.
Entendo que nem todo mundo pode pagar, mas eu mantenho esse software desde 2021 e é um trabalho extenso.
Tem quem cobra assinaturas de 300 reais por mes para uma unica plataforma, eu mantenho tudo em código aberto e dou suporte ativo ao pessoal.
Não tem nada que vai te impedir de essencialmente crackear, se fosse meu propósito eu teria usado minha licença do pyarmor.
Considere dar alguma forma de apoio, e no mínimo, não compartilhe versão desbloqueada. Obrigado."""


MEMBERSHIP_BASE_URL = "https://katomaro.com"


@dataclass(frozen=True)
class MembershipInfo:
    """Represents the response from the membership authentication API."""

    token: str
    allowed_platforms: List[str]
    is_premium: bool
    permissions: List[str]
    user_email: str


class MembershipService:
    """Client to authenticate the user with the Katomart membership backend."""

    def __init__(self, timeout: int = 15) -> None:
        self._base_url = MEMBERSHIP_BASE_URL.rstrip("/")
        self._timeout = timeout

    def authenticate(self, email: str, password: str) -> MembershipInfo:
        """Authenticates the user and returns the membership info."""
        if not self._base_url:
            raise ValueError("Nenhum endpoint configurado para autenticação do software.")

        url = f"{self._base_url}/api/permissions"
        payload = {"email": email, "password": password}

        response = requests.post(url, json=payload, timeout=self._timeout)
        response.raise_for_status()

        data = response.json()
        permissions = data.get("permissions") or []
        if not isinstance(permissions, list):
            permissions = []

        user_info = data.get("user") or {}
        user_email = str(user_info.get("email") or "").strip()

        has_full_permission = "katomart.FULL" in permissions or "katomart.downloader" in permissions
        allowed = PlatformFactory.get_platform_names() if has_full_permission else []

        token = str(data.get("token") or "").strip()

        return MembershipInfo(
            token=token,
            allowed_platforms=allowed,
            is_premium=has_full_permission,
            permissions=permissions,
            user_email=user_email or email,
        )
