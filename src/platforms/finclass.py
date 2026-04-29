from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory, sanitize_token

logger = logging.getLogger(__name__)

API_BASE = "https://core.finclass.com"
LOGIN_URL = f"{API_BASE}/users/user/login"
ME_URL = f"{API_BASE}/users/user/readMe"
COURSES_URL = f"{API_BASE}/learning/courses"
COURSE_DETAIL_URL = f"{API_BASE}/learning/courses/{{course_id}}"
CONTENT_FOLLOW_URL = f"{API_BASE}/learning/content-follow"

WEB_ORIGIN = "https://app.finclass.com"
GPRIM_SPECVER = "2.2.1"

# Pagination wired but disabled — single page covers full catalog as of capture.
# Flip to True if FinClass starts paginating.
_PAGINATION_ENABLED = False
_PAGE_SIZE = 100

# Whitelist of course statuses to surface. Extend as new statuses appear.
_ALLOWED_COURSE_STATUSES = {"published"}


class FinClassPlatform(BasePlatform):
    """
    FinClass platform integration.

    Auth: POST core.finclass.com/users/user/login -> response header `sessionid`.
    All authed requests carry `sessionid` + X-gprim-spec{dev,nav,ver} headers.

    Catalog: GET /learning/courses (single page, paginates trivially if needed).
    Course detail: GET /learning/courses/{id} returns moduleEntities w/ full lessons.
    Video: lessonMedia.mediaDash (DASH, no DRM observed) primary;
           mediaSource (direct MP4) and mediaHls available as fallbacks.
    Attachments: lessonFiles[].fileAddress (public URLs on assets.finclass.com).
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._sessionid: Optional[str] = None
        self._user_id: Optional[str] = None
        # Mark-watched gated until per-platform settings exist (see mark_lesson_watched).
        # TODO: expose via SettingsManager per-platform settings once supported.
        self._mark_watched_enabled: bool = False

    # ------------------------------------------------------------------ auth

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como autenticar no FinClass:

Opção 1 — Usuário e senha (recomendado):
1) Informe o e-mail e a senha cadastrados na FinClass.
2) O Katomart fará login em https://core.finclass.com/users/user/login e
   capturará o cabeçalho `sessionid` da resposta automaticamente.

Opção 2 — Colar o `sessionid` manualmente (caso já tenha capturado):
1) Abra https://app.finclass.com em seu navegador e faça login normalmente.
2) Abra as Ferramentas de Desenvolvedor (F12) -> aba Rede (Network).
3) Selecione qualquer requisição feita para o domínio `core.finclass.com`.
4) Na aba "Cabeçalhos" (Headers) -> "Cabeçalhos da requisição" copie o valor
   do header `sessionid` (formato UUID, ex.: `e5ae88ee-4217-43b4-8979-b4de160b4517`).
5) Cole esse valor no campo "Token de Acesso".

Observação: a FinClass não oferece 2FA ou OTP no momento.
"""

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        token = sanitize_token((credentials.get("token") or "").strip())
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        sessionid: Optional[str] = None

        if username and password:
            sessionid = self._login_with_credentials(username, password)
            self.credentials = credentials
        elif token:
            sessionid = token
        else:
            raise ValueError("Informe e-mail e senha ou um token (sessionid) válido.")

        if not sessionid:
            raise ConnectionError("Não foi possível obter o sessionid da FinClass.")

        self._configure_session(sessionid)

        try:
            self._user_id = self._fetch_user_id()
            if self._user_id:
                logger.info("FinClass: userID resolvido (%s).", self._user_id[:8])
        except Exception as exc:
            logger.warning("FinClass: falha ao resolver userID via /users/user/readMe: %s", exc)

    def refresh_auth(self) -> None:
        if self.credentials:
            self.authenticate(self.credentials)
        else:
            raise ValueError("Sem credenciais armazenadas para renovar a sessão FinClass.")

    def _login_with_credentials(self, email: str, password: str) -> str:
        payload = {"email": email, "password": password}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": f"{WEB_ORIGIN}/",
            "User-Agent": self._settings.user_agent,
            "X-gprim-specdev": "WEB",
            "X-gprim-specnav": self._settings.user_agent,
            "X-gprim-specver": GPRIM_SPECVER,
        }
        try:
            response = requests.post(LOGIN_URL, json=payload, headers=headers, timeout=30)
        except requests.RequestException as exc:
            logger.error("FinClass: erro de rede no login: %s", exc)
            raise ConnectionError(f"Falha ao autenticar no FinClass: {exc}") from exc

        if response.status_code == 401 or response.status_code == 403:
            raise ValueError("Credenciais FinClass inválidas.")
        if not response.ok:
            raise ConnectionError(
                f"Login FinClass retornou status {response.status_code}: {response.text[:200]}"
            )

        # sessionid is delivered in the response *headers*, not in the body.
        sessionid = response.headers.get("sessionid") or response.headers.get("Sessionid")
        if not sessionid:
            raise ConnectionError(
                "Login FinClass não retornou o cabeçalho `sessionid`. "
                "Verifique se há proxy/CDN reescrevendo headers."
            )
        return sanitize_token(sessionid.strip())

    def _configure_session(self, sessionid: str) -> None:
        self._sessionid = sessionid
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Origin": WEB_ORIGIN,
            "Referer": f"{WEB_ORIGIN}/",
            "User-Agent": self._settings.user_agent,
            "sessionid": sessionid,
            "X-gprim-specdev": "WEB",
            "X-gprim-specnav": self._settings.user_agent,
            "X-gprim-specver": GPRIM_SPECVER,
        })

    def _fetch_user_id(self) -> Optional[str]:
        if not self._session:
            return None
        response = self._session.get(ME_URL, timeout=30)
        if not response.ok:
            return None
        try:
            data = response.json().get("data") or {}
        except ValueError:
            return None
        return data.get("userID") or data.get("userId") or data.get("id")

    # ------------------------------------------------- catalog (Batch 01.2)

    def fetch_courses(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("FinClass.fetch_courses pendente (Batch 01.2).")

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError("FinClass.fetch_course_content pendente (Batch 01.2).")

    # -------------------------------------------- lesson details (Batch 01.3)

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        raise NotImplementedError("FinClass.fetch_lesson_details pendente (Batch 01.3).")

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        raise NotImplementedError("FinClass.download_attachment pendente (Batch 01.3).")

    # ------------------------------------------- mark watched (Batch 01.4)

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        # Gated by self._mark_watched_enabled. Until per-platform settings exist,
        # default behaviour is to skip without erroring.
        # TODO: expose self._mark_watched_enabled via SettingsManager per-platform
        #       settings once supported, allowing users to opt in.
        if not self._mark_watched_enabled:
            logger.info(
                "FinClass: mark_lesson_watched desativado por padrão; ignorando aula %s.",
                lesson.get("id"),
            )
            return
        raise NotImplementedError("FinClass.mark_lesson_watched ativa pendente (Batch 01.4).")


PlatformFactory.register_platform("FinClass", FinClassPlatform)
