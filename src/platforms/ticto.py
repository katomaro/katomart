from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, AuxiliaryURL, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import (
    AuthField,
    AuthFieldType,
    BasePlatform,
    PlatformFactory,
    sanitize_token,
)

logger = logging.getLogger(__name__)

API_BASE = "https://mozart-api.ticto.cloud/api/v1/private"


class TictoPlatform(BasePlatform):
    """Ticto / Mozart EAD — multi-tenant platform identified by `portal-code` UUID."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._portal_url: str = ""
        self._portal_code: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="portal_url",
                label="URL do portal (ex: plataforma.suaescola.com.br)",
                field_type=AuthFieldType.TEXT,
                placeholder="plataforma.suaescola.com.br",
                required=True,
            ),
            AuthField(
                name="portal_code",
                label="Portal Code (UUID)",
                field_type=AuthFieldType.TEXT,
                placeholder="00000000-0000-0000-0000-000000000000",
                required=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
A Ticto/Mozart EAD identifica cada escola por um Portal Code (UUID) e usa uma API compartilhada
em mozart-api.ticto.cloud. Informe o domínio do seu portal (sem https:// e sem barra final) e o
Portal Code da sua escola.

Como obter o Portal Code:
1) Acesse seu portal e faça login normalmente.
2) Abra as Ferramentas de Desenvolvedor (F12) → aba Rede (Network).
3) Procure qualquer requisição para "mozart-api.ticto.cloud" e clique nela.
4) Na aba Headers/Cabeçalhos, localize o header "portal-code" — o valor é um UUID. Copie-o.

Token de Acesso (gratuito):
1) Na mesma aba de rede, com login feito, clique em uma requisição para mozart-api.ticto.cloud.
2) Na aba Headers, copie o valor do header "Authorization" (formato "Bearer <id>|<segredo>").
3) Cole apenas a parte após "Bearer " no campo Token.

Assinantes podem informar e-mail/senha — o sistema autentica via API REST (POST /ead/login)
usando o Portal Code informado.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        portal_url = (credentials.get("portal_url") or "").strip()
        portal_code = (credentials.get("portal_code") or "").strip()
        if not portal_url:
            raise ValueError("Informe a URL do portal Ticto/Mozart EAD.")
        if not portal_code:
            raise ValueError("Informe o Portal Code (UUID) da sua escola.")

        self._portal_url = self._normalize_portal_url(portal_url)
        self._portal_code = portal_code

        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)
        self._validate_session()

    @staticmethod
    def _normalize_portal_url(value: str) -> str:
        value = value.strip().rstrip("/")
        if "://" not in value:
            value = f"https://{value}"
        parsed = urlparse(value)
        return f"{parsed.scheme}://{parsed.netloc}".lower()

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        if not username or not password:
            raise ValueError("Informe e-mail e senha para autenticar via credenciais.")

        try:
            response = requests.post(
                f"{API_BASE}/ead/login",
                json={
                    "email": username,
                    "password": password,
                    "portal_code": self._portal_code,
                },
                headers={
                    "User-Agent": self._settings.user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Origin": self._portal_url,
                    "Referer": f"{self._portal_url}/",
                    "portal-code": self._portal_code,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ConnectionError(f"Falha ao contatar a API Ticto: {exc}") from exc

        if response.status_code != 200:
            raise ConnectionError(
                f"Falha no login Ticto (HTTP {response.status_code}). Verifique e-mail, senha e Portal Code."
            )

        data = response.json() or {}
        access_token = (data.get("accessToken") or {}).get("token")
        if not access_token:
            raise ConnectionError("Resposta de login Ticto sem accessToken.")
        return access_token

    def _configure_session(self, token: str) -> None:
        token = sanitize_token(token)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "portal-code": self._portal_code,
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": self._portal_url,
                "Referer": f"{self._portal_url}/",
            }
        )

    def _validate_session(self) -> None:
        if not self._session:
            raise ConnectionError("Sessão Ticto não autenticada.")
        response = self._session.get(f"{API_BASE}/ead/verify-subscription", timeout=30)
        if response.status_code == 401:
            raise ConnectionError("Token Ticto inválido ou expirado.")
        if response.status_code >= 400:
            raise ConnectionError(
                f"Falha ao validar sessão Ticto (HTTP {response.status_code})."
            )

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessão Ticto não autenticada.")

        response = self._session.get(f"{API_BASE}/ead/contents", timeout=30)
        response.raise_for_status()
        raw = response.json() or []

        courses: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            course_id = item.get("id")
            if not course_id:
                continue
            courses.append(
                {
                    "id": str(course_id),
                    "name": item.get("name") or f"Curso {course_id}",
                    "seller_name": "",
                    "slug": str(course_id),
                    "has_active_access": item.get("has_active_access", True),
                }
            )
        return sorted(courses, key=lambda c: c.get("name", ""))

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessão Ticto não autenticada.")

        all_content: Dict[str, Any] = {}
        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            modules_resp = self._session.get(
                f"{API_BASE}/ead/contents/{course_id}/modules",
                params={"no-pagination": "true"},
                timeout=30,
            )
            modules_resp.raise_for_status()
            raw_modules = modules_resp.json() or []

            processed_modules: List[Dict[str, Any]] = []
            for raw_module in raw_modules:
                if not isinstance(raw_module, dict):
                    continue
                module_id = raw_module.get("id")
                if not module_id:
                    continue

                lessons = self._fetch_module_lessons(str(module_id))

                processed_modules.append(
                    {
                        "id": str(module_id),
                        "title": raw_module.get("name") or f"Módulo {module_id}",
                        "order": raw_module.get("order", 0),
                        "lessons": lessons,
                        "locked": bool(raw_module.get("is_blocked")),
                    }
                )

            course_entry = course.copy()
            course_entry["title"] = course.get("name", f"Curso {course_id}")
            course_entry["modules"] = processed_modules
            all_content[str(course_id)] = course_entry

        return all_content

    def _fetch_module_lessons(self, module_id: str) -> List[Dict[str, Any]]:
        if not self._session:
            return []

        lessons: List[Dict[str, Any]] = []
        page = 1
        while True:
            resp = self._session.get(
                f"{API_BASE}/ead/modules/{module_id}/items",
                params={"page": page},
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json() or {}
            data = payload.get("data") or []
            for item in data:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "lesson":
                    continue
                lesson_id = item.get("item_id") or (item.get("itemable") or {}).get("id")
                if not lesson_id:
                    continue
                lessons.append(
                    {
                        "id": str(lesson_id),
                        "title": item.get("name") or f"Aula {lesson_id}",
                        "order": item.get("item_order") or item.get("order") or 0,
                        "slug": item.get("slug"),
                        "is_viewed": bool(item.get("is_viewed")),
                        "locked": bool(item.get("is_blocked")),
                    }
                )

            current_page = payload.get("current_page") or page
            last_page = payload.get("last_page") or current_page
            if current_page >= last_page or not data:
                break
            page = current_page + 1

        return lessons

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessão Ticto não autenticada.")

        lesson_id = lesson.get("id")
        if not lesson_id:
            raise ValueError("ID da aula Ticto não encontrado.")

        resp = self._session.get(
            f"{API_BASE}/ead/lessons/{lesson_id}/elements", timeout=30
        )
        resp.raise_for_status()
        elements = resp.json() or []

        content = LessonContent()
        description_chunks: List[str] = []
        video_index = 0
        attachment_index = 0

        for element in elements:
            if not isinstance(element, dict):
                continue
            etype = element.get("type")
            contents = element.get("contents") or {}
            order = element.get("order") or 0
            name = element.get("name") or lesson.get("title") or "Aula"

            if etype == "video":
                video_index += 1
                url = contents.get("url") or ""
                if not url:
                    continue
                content.videos.append(
                    Video(
                        video_id=str(element.get("id") or lesson_id),
                        url=url,
                        order=order or video_index,
                        title=name,
                        size=0,
                        duration=0,
                        extra_props={"referer": f"{self._portal_url}/"},
                    )
                )
            elif etype == "text":
                text = (contents.get("text") or "").strip()
                if text:
                    description_chunks.append(text)
            elif etype == "attachment":
                attachment_index += 1
                url = contents.get("url") or ""
                if not url:
                    continue
                filename = (
                    contents.get("fileName")
                    or self._filename_from_url(url)
                    or f"{name}"
                )
                extension = filename.split(".")[-1] if "." in filename else ""
                content.attachments.append(
                    Attachment(
                        attachment_id=str(element.get("id") or attachment_index),
                        url=url,
                        filename=filename,
                        order=order or attachment_index,
                        extension=extension,
                        size=0,
                    )
                )

        if description_chunks:
            content.description = Description(
                text="\n".join(description_chunks), description_type="html"
            )

        self._extract_auxiliary_urls(description_chunks, content)

        return content

    @staticmethod
    def _filename_from_url(url: str) -> str:
        try:
            path = urlparse(url).path
            return Path(path).name
        except Exception:
            return ""

    @staticmethod
    def _extract_auxiliary_urls(html_chunks: List[str], content: LessonContent) -> None:
        if not html_chunks:
            return
        pattern = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
        seen: set = set()
        idx = 0
        for chunk in html_chunks:
            for match in pattern.finditer(chunk):
                url = match.group(1).strip()
                if not url or url.startswith(("#", "javascript:", "mailto:")):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                idx += 1
                content.auxiliary_urls.append(
                    AuxiliaryURL(
                        url_id=f"link-{idx}",
                        url=url,
                        order=idx,
                        title=url,
                        description="",
                    )
                )

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessão Ticto não autenticada.")

        url = attachment.url
        if not url:
            logger.error("Anexo Ticto sem URL: %s", attachment.filename)
            return False

        # S3 signed URLs from elements may have expired (5-min TTL). Refresh if so.
        if "amazonaws.com" in urlparse(url).netloc and attachment.attachment_id:
            refreshed = self._refresh_attachment_url(attachment.attachment_id)
            if refreshed:
                url = refreshed

        try:
            headers = {
                "User-Agent": self._settings.user_agent,
                "Accept": "*/*",
                "Referer": f"{self._portal_url}/",
            }
            if "amazonaws.com" in urlparse(url).netloc:
                response = requests.get(url, stream=True, headers=headers, timeout=120)
            else:
                response = self._session.get(url, stream=True, timeout=120)
            response.raise_for_status()

            with open(download_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
            return True
        except Exception as exc:
            logger.error("Falha ao baixar anexo Ticto %s: %s", attachment.filename, exc)
            return False

    def _refresh_attachment_url(self, attachment_id: str) -> Optional[str]:
        if not self._session:
            return None
        try:
            resp = self._session.get(
                f"{API_BASE}/ead/attachment/{attachment_id}/", timeout=30
            )
            resp.raise_for_status()
            data = resp.json() or {}
            return (data.get("contents") or {}).get("url")
        except requests.RequestException as exc:
            logger.debug("Ticto: falha ao renovar URL do anexo %s: %s", attachment_id, exc)
            return None

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        if not self._session:
            return
        if not watched:
            logger.info("Ticto: API não expõe 'desmarcar aula'; ignorando.")
            return
        lesson_id = lesson.get("id")
        if not lesson_id:
            return
        try:
            self._session.post(
                f"{API_BASE}/ead/lessons/{lesson_id}/mark-as-viewed", timeout=30
            )
        except requests.RequestException as exc:
            logger.debug("Ticto: falha ao marcar aula %s: %s", lesson_id, exc)


PlatformFactory.register_platform("Ticto (Mozart EAD)", TictoPlatform)
