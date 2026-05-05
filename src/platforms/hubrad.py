from __future__ import annotations

import base64
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)

API_BASE = "https://hubrad.io:3003/api/v1"
ORIGIN = "https://app.hubrad.io"
APP_VERSION = "1.2.49"
APP_LANGUAGE = "pt-BR"
APP_TIMEZONE = "America/Sao_Paulo"
TRANSLATE_HEADER = (
    "headings|sponsor|live|posts|content_type|categories|chanevt|"
    "chanevt_display|posts_display|relateds|notifications_headings|"
    "notifications_messages|notifications_system_messages|courses|"
    "feed_messages|articles_title"
)
COURSES_SECTION_TITLES = {"meus cursos", "my courses", "mis cursos", "mes cours"}


def _decode_jwt_device_uuid(token: str) -> Optional[str]:
    """Extracts the `device_uuid` claim from a HubRAD JWT without verifying its signature."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("device_uuid")
    except Exception:
        return None


class HubradPlatform(BasePlatform):
    """HubRAD (PersonalRad) platform implementation."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._device_uuid: str = ""
        self._classes_cache: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="device_uuid",
                label="Device UUID (opcional)",
                placeholder="Cabeçalho `Uuid` capturado no navegador",
                required=False,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes podem informar e-mail/senha — o sistema obtém o token automaticamente
e gera um Device UUID novo para esta sessão.

Para usuários que preferem o token (JWT) já obtido:
1) Acesse https://app.hubrad.io e faça login normalmente.
2) Abra o DevTools (F12) → aba Rede (Network).
3) Procure por uma requisição autenticada para `hubrad.io:3003/api/v1/...`.
4) Copie o valor do cabeçalho `authorization` (apenas a parte após "Bearer ") e cole no campo Token.
5) Copie também o valor do cabeçalho `Uuid` e cole em "Device UUID" — o token é vinculado a este UUID.
   (Se omitido, o sistema tenta extrair o UUID embutido no próprio JWT.)
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)

        device_uuid = (credentials.get("device_uuid") or "").strip()
        if not device_uuid:
            device_uuid = _decode_jwt_device_uuid(token) or self._device_uuid
        if not device_uuid:
            device_uuid = str(uuid.uuid4())
        self._device_uuid = device_uuid
        self._configure_session(token)

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        if not username or not password:
            raise ValueError("Informe e-mail e senha para autenticar na HubRAD.")

        device_uuid = (credentials.get("device_uuid") or "").strip() or str(uuid.uuid4())
        self._device_uuid = device_uuid

        try:
            response = requests.post(
                f"{API_BASE}/user/login",
                json={
                    "email": username,
                    "password": password,
                    "device_uuid": device_uuid,
                },
                headers=self._build_anonymous_headers(device_uuid),
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            logger.debug("HubRAD login payload: %s", data)
            token = data.get("access_token")
            if not token:
                raise ValueError("Resposta de login da HubRAD não retornou access_token.")
            return token
        except requests.RequestException as exc:
            raise ConnectionError(
                "Falha ao autenticar na HubRAD. Verifique e-mail e senha."
            ) from exc

    def _build_anonymous_headers(self, device_uuid: str) -> Dict[str, str]:
        return {
            "User-Agent": self._settings.user_agent,
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Language": APP_LANGUAGE,
            "Timezone": APP_TIMEZONE,
            "Translate": TRANSLATE_HEADER,
            "Uuid": device_uuid,
            "Version": APP_VERSION,
        }

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.user_agent,
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
            "Language": APP_LANGUAGE,
            "Timezone": APP_TIMEZONE,
            "Translate": TRANSLATE_HEADER,
            "Uuid": self._device_uuid,
            "Version": APP_VERSION,
        })

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        courses: Dict[str, Dict[str, Any]] = {}
        page = 1
        last_page = 1
        max_pages = 20

        while page <= last_page and page <= max_pages:
            response = self._session.get(
                f"{API_BASE}/feed", params={"per_page": 30, "page": page}
            )
            response.raise_for_status()
            payload = response.json()
            logger.debug("HubRAD feed page %s: %s", page, payload.get("meta"))

            try:
                last_page = int(payload.get("meta", {}).get("last_page", page))
            except (TypeError, ValueError):
                last_page = page

            for section in payload.get("data", []):
                section_title = (section.get("content_title") or "").strip().lower()
                if section_title not in COURSES_SECTION_TITLES:
                    continue
                for item in section.get("itensList", []):
                    if item.get("destination") != "course":
                        continue
                    course_id = str(item.get("id") or "")
                    if not course_id or course_id in courses:
                        continue
                    courses[course_id] = {
                        "id": course_id,
                        "name": item.get("title", "Curso"),
                        "seller_name": "PersonalRad",
                        "slug": course_id,
                        "image": item.get("img"),
                    }
            page += 1

        if not courses:
            logger.warning("HubRAD: nenhum curso encontrado em 'Meus Cursos'.")

        return sorted(courses.values(), key=lambda c: c.get("name", ""))

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = str(course["id"])

            detail_resp = self._session.get(f"{API_BASE}/course/{course_id}")
            if detail_resp.ok:
                detail = (detail_resp.json() or {}).get("course", {})
                if detail.get("long_name"):
                    course["long_name"] = detail["long_name"]
                if detail.get("description"):
                    course["description"] = detail["description"]
            else:
                logger.warning(
                    "HubRAD: falha ao obter detalhes do curso %s (HTTP %s)",
                    course_id,
                    detail_resp.status_code,
                )

            classes_resp = self._session.get(f"{API_BASE}/course/class/{course_id}")
            classes_resp.raise_for_status()
            classes = classes_resp.json().get("classes", [])
            logger.debug("HubRAD: curso %s possui %d aulas", course_id, len(classes))

            modules: Dict[str, Dict[str, Any]] = {}
            for index, klass in enumerate(classes, start=1):
                category = "Sem classificação"
                cats = klass.get("categories")
                if cats:
                    category = (cats[0] or {}).get("description") or category

                module = modules.setdefault(category, {
                    "id": category,
                    "title": category,
                    "lessons": [],
                })

                lesson_id = klass.get("id")
                if not lesson_id:
                    continue

                self._classes_cache[lesson_id] = klass

                module["lessons"].append({
                    "id": lesson_id,
                    "title": klass.get("content_title") or f"Aula {index}",
                    "description": klass.get("content_descr"),
                    "image": klass.get("content_img"),
                    "order": index,
                    "date": klass.get("date"),
                })

            course_with_modules = course.copy()
            course_with_modules["title"] = course.get("name", "Curso")
            course_with_modules["modules"] = list(modules.values())
            all_content[course_id] = course_with_modules

        return all_content

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        lesson_id = lesson.get("id")
        if not lesson_id:
            raise ValueError("Lesson ID ausente.")

        klass = self._classes_cache.get(lesson_id)
        if klass is None:
            classes_resp = self._session.get(f"{API_BASE}/course/class/{course_id}")
            classes_resp.raise_for_status()
            for k in classes_resp.json().get("classes", []):
                if k.get("id"):
                    self._classes_cache[k["id"]] = k
            klass = self._classes_cache.get(lesson_id, {})

        content = LessonContent()

        descr = klass.get("content_descr") or lesson.get("description")
        if descr:
            content.description = Description(text=descr, description_type="html")

        for index, media in enumerate(self._extract_media_entries(klass), start=1):
            if media.get("type") != "video":
                continue
            video_id = media.get("fileUrl")
            if not video_id:
                continue

            otp_payload = self._fetch_vdocipher_otp(video_id)
            if not otp_payload:
                continue

            otp = otp_payload.get("otp", "")
            playback_info = otp_payload.get("playbackInfo", "")
            otp_meta = otp_payload.get("meta") or {}
            dash = otp_meta.get("dash") or {}
            license_servers = dash.get("licenseServers") or {}

            duration = int(
                (media.get("meta") or {}).get("total_duration")
                or otp_meta.get("duration")
                or 0
            )
            title = (
                otp_meta.get("title")
                or klass.get("content_title")
                or lesson.get("title")
                or f"video-{index}"
            )

            embed_url = (
                f"https://player.vdocipher.com/v2/?otp={otp}&playbackInfo={playback_info}"
            )

            extra_props: Dict[str, Any] = {
                "video_id": video_id,
                "otp": otp,
                "playback_info": playback_info,
                "drm": "widevine",
                "manifest_url": dash.get("manifest"),
                "license_url": license_servers.get("com.widevine.alpha"),
                "fallback_license_urls": dash.get("fallbackLicenseServers") or [],
                "fps_manifest_url": (otp_meta.get("fps") or {}).get("manifest"),
                "referer": f"{ORIGIN}/",
            }

            content.videos.append(
                Video(
                    video_id=video_id,
                    url=embed_url,
                    order=index,
                    title=title,
                    size=0,
                    duration=duration,
                    extra_props=extra_props,
                )
            )

        return content

    def _extract_media_entries(self, klass: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = (klass.get("content") or {}).get("data") or {}
        if "pt-br" in data:
            entries = data["pt-br"]
        elif data:
            entries = next(iter(data.values()))
        else:
            entries = []
        return [entry.get("media", {}) for entry in entries if entry.get("media")]

    def _fetch_vdocipher_otp(self, video_id: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._session.get(f"{API_BASE}/vdo/otp/{video_id}/1")
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.error(
                "HubRAD: falha ao obter OTP do VdoCipher para %s: %s", video_id, exc
            )
            return None

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        logger.warning(
            "HubRAD: anexos não estão disponíveis nesta plataforma."
        )
        return False


PlatformFactory.register_platform("HubRAD", HubradPlatform)
