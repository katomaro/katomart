from __future__ import annotations

import base64
import gzip
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, AuxiliaryURL, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)

API_BASE = "https://portal.fullcycle.com.br"
PLATFORM_BASE = "https://plataforma.fullcycle.com.br"
LOGIN_URL = f"{API_BASE}/api/login_check"
MY_COURSES_URL = f"{API_BASE}/api/cursos/my.json"
LEARNING_PATHS_URL = f"{API_BASE}/api/learning_paths/categories/{{category_id}}/classrooms/{{classroom_id}}"
LEARNING_PATH_URL = f"{API_BASE}/api/learning_paths/{{lp_id}}/classrooms/{{classroom_id}}"
CAPITULOS_URL = f"{API_BASE}/api/cursos/turma/{{classroom_id}}/curso/{{course_id}}/capitulos.json?expand_contents=1"
CONTENT_URL = f"{API_BASE}/api/cursos/conteudo/{{content_id}}.json"

CONTENT_TYPE_VIDEO = 12
CONTENT_TYPE_TEXT = 2
CONTENT_TYPE_LINK = 8


def _decode_gzipped_payload(response_json: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Fullcycle wraps large JSON payloads as `{<key>: "<gzipped-base64>"}`."""
    payload = response_json.get(key)
    if not payload:
        raise ValueError(f"Campo '{key}' ausente ou vazio na resposta da Fullcycle.")
    decoded = gzip.decompress(base64.b64decode(payload))
    return json.loads(decoded)


class FullcyclePlatform(BasePlatform):
    """Implements the Fullcycle (portal.fullcycle.com.br) platform."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._course_meta: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Use com delays.
1) Abra https://plataforma.fullcycle.com.br e faça login.
2) Abra o DevTools (F12) → aba Rede / Network.
3) Recarregue a página e procure por qualquer requisição para portal.fullcycle.com.br/api/.
4) Na aba Headers, copie o valor do cabeçalho "Authorization" (algo como "Bearer eyJhbGciOi...").
5) Cole apenas a parte do token (sem a palavra "Bearer ") no campo acima.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": PLATFORM_BASE,
                "Referer": f"{PLATFORM_BASE}/",
            }
        )

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        """Posts credentials to /api/login_check and returns the JWT."""
        if not username or not password:
            raise ConnectionError("Usuário e senha são obrigatórios para login automático.")

        try:
            response = requests.post(
                LOGIN_URL,
                files={
                    "_username": (None, username),
                    "_password": (None, password),
                },
                headers={
                    "User-Agent": self._settings.user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Origin": PLATFORM_BASE,
                    "Referer": f"{PLATFORM_BASE}/",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise ConnectionError(f"Falha ao autenticar na Fullcycle: {exc}") from exc

        token = data.get("token")
        if not token:
            raise ConnectionError("Resposta de login da Fullcycle não contém token.")
        return token

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        response = self._session.get(MY_COURSES_URL, timeout=30)
        response.raise_for_status()
        categories = response.json()

        courses: List[Dict[str, Any]] = []
        seen_course_ids: set = set()

        for entry in categories:
            category = entry.get("category") or {}
            classroom = entry.get("classroom") or {}
            category_id = category.get("id")
            classroom_id = classroom.get("id")
            category_name = category.get("name", "Full Cycle")

            if not category_id or not classroom_id:
                continue

            try:
                lp_resp = self._session.get(
                    LEARNING_PATHS_URL.format(category_id=category_id, classroom_id=classroom_id),
                    timeout=30,
                )
                lp_resp.raise_for_status()
                lp_data = lp_resp.json()
            except requests.RequestException as exc:
                logger.warning("Fullcycle: falha ao listar learning_paths de %s: %s", category_id, exc)
                continue

            for lp in lp_data.get("learning_paths", []):
                lp_id = lp.get("id")
                lp_name = lp.get("name", "")
                if not lp_id:
                    continue

                try:
                    detail_resp = self._session.get(
                        LEARNING_PATH_URL.format(lp_id=lp_id, classroom_id=classroom_id),
                        timeout=30,
                    )
                    detail_resp.raise_for_status()
                    lp_detail = detail_resp.json()
                except requests.RequestException as exc:
                    logger.warning("Fullcycle: falha ao carregar learning_path %s: %s", lp_id, exc)
                    continue

                for course in lp_detail.get("courses", []):
                    course_id = course.get("id")
                    if not course_id or course_id in seen_course_ids:
                        continue
                    seen_course_ids.add(course_id)

                    seller = f"{category_name} > {lp_name}" if lp_name else category_name
                    courses.append(
                        {
                            "id": course_id,
                            "name": course.get("name", "Curso"),
                            "seller_name": seller,
                            "slug": str(course_id),
                            "category_id": category_id,
                            "classroom_id": classroom_id,
                            "learning_path_id": lp_id,
                        }
                    )

        return sorted(courses, key=lambda c: (c.get("seller_name", ""), c.get("name", "")))

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            classroom_id = course.get("classroom_id")
            if not course_id or not classroom_id:
                continue

            self._course_meta[str(course_id)] = {
                "classroom_id": classroom_id,
                "category_id": course.get("category_id"),
                "learning_path_id": course.get("learning_path_id"),
            }

            try:
                resp = self._session.get(
                    CAPITULOS_URL.format(classroom_id=classroom_id, course_id=course_id),
                    timeout=60,
                )
                resp.raise_for_status()
                payload = _decode_gzipped_payload(resp.json(), "course")
            except (requests.RequestException, ValueError) as exc:
                logger.error("Fullcycle: falha ao obter capítulos de %s: %s", course_id, exc)
                continue

            logger.debug("Fullcycle capítulos curso %s: %s", course_id, payload.get("name"))

            modules: List[Dict[str, Any]] = []
            for module_index, chapter in enumerate(payload.get("chapters", []), start=1):
                lessons: List[Dict[str, Any]] = []
                for lesson_index, content in enumerate(chapter.get("contents", []), start=1):
                    lessons.append(
                        {
                            "id": content.get("id"),
                            "title": content.get("title", f"Aula {lesson_index}"),
                            "order": lesson_index,
                            "item_type": content.get("type"),
                            "url": content.get("url"),
                            "link": content.get("link"),
                            "text": content.get("text"),
                            "transcription": content.get("transcription"),
                            "duration": content.get("duration"),
                            "supplementary_materials": content.get("supplementary_materials") or [],
                        }
                    )

                modules.append(
                    {
                        "id": chapter.get("id"),
                        "title": chapter.get("name", f"Módulo {module_index}"),
                        "order": module_index,
                        "lessons": lessons,
                    }
                )

            course_entry = course.copy()
            course_entry["title"] = payload.get("name", course.get("name", "Curso"))
            course_entry["enable_drm"] = bool(payload.get("enable_drm"))
            course_entry["modules"] = modules
            all_content[str(course_id)] = course_entry

        return all_content

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        content = LessonContent()
        order = lesson.get("order", 1)
        title = lesson.get("title", f"Aula {order}")
        item_type = lesson.get("item_type")
        url = lesson.get("url")

        description_html = lesson.get("transcription") or lesson.get("text")
        if description_html:
            content.description = Description(text=description_html, description_type="html")

        if item_type == CONTENT_TYPE_VIDEO and url and "iframe.mediadelivery.net" in url:
            duration_seconds = self._parse_duration(lesson.get("duration"))
            content.videos.append(
                Video(
                    video_id=str(lesson.get("id") or ""),
                    url=url,
                    order=order,
                    title=title,
                    size=0,
                    duration=duration_seconds,
                    extra_props={"enable_drm": True},
                )
            )

        elif item_type == CONTENT_TYPE_LINK:
            link = lesson.get("link") or url
            if link:
                content.auxiliary_urls.append(
                    AuxiliaryURL(
                        url_id=str(lesson.get("id") or ""),
                        url=link,
                        order=order,
                        title=title,
                        description=link,
                    )
                )

        for material_index, material in enumerate(lesson.get("supplementary_materials") or [], start=1):
            file_url = material.get("url") or material.get("file_url")
            filename = material.get("filename") or material.get("name") or f"material_{material_index}"
            if not file_url:
                continue
            extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
            content.attachments.append(
                Attachment(
                    attachment_id=str(material.get("id") or f"{lesson.get('id')}-{material_index}"),
                    url=file_url,
                    filename=filename,
                    order=material.get("order", material_index),
                    extension=extension,
                    size=material.get("size", 0) or 0,
                )
            )

        return content

    @staticmethod
    def _parse_duration(raw: Any) -> int:
        """Parses strings like '07:53' or '01:02:03' into total seconds."""
        if not raw or not isinstance(raw, str):
            return 0
        parts = raw.split(":")
        try:
            numbers = [int(p) for p in parts]
        except ValueError:
            return 0
        total = 0
        for n in numbers:
            total = total * 60 + n
        return total

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")
        if not attachment.url:
            return False

        try:
            with self._session.get(attachment.url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(download_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except requests.RequestException as exc:
            logger.error("Fullcycle: falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Fullcycle", FullcyclePlatform)
