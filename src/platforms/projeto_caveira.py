from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

API_BASE = "https://api.caveira.com/api/v1"
APP_BASE = "https://app.caveira.com"

LOGIN_URL = f"{API_BASE}/auth/login"
PROFILE_URL = f"{API_BASE}/auth/profile"
COURSES_URL = f"{API_BASE}/skull/courses"
COURSE_DETAIL_URL = f"{API_BASE}/skull/courses/{{course_id}}"
COURSE_MODULES_URL = f"{API_BASE}/skull/courses/{{course_id}}/modules"
MODULE_VIDEOS_URL = f"{API_BASE}/skull/courses/{{course_id}}/modules/{{module_id}}/videos"
VIDEO_DETAIL_URL = f"{API_BASE}/skull/courses/{{course_id}}/modules/{{module_id}}/videos/{{video_id}}"
COURSE_FILE_URL = f"{API_BASE}/skull/downloads/course-file/{{file_id}}"


class CaveiraPlatform(BasePlatform):
    """Caveira (caveira.com) — Laravel JSON API with Bunny Stream player."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._access_token: Optional[str] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
A Caveira aceita login direto com e-mail e senha (a API expõe um token Bearer
após o POST /api/v1/auth/login).

Como obter o token manualmente (caso o login direto falhe por reCAPTCHA):
1) Acesse https://app.caveira.com e faça login normalmente.
2) Abra o DevTools (F12) e vá em Rede (Network).
3) Recarregue qualquer página interna; clique em uma requisição para
   api.caveira.com (por exemplo "profile" ou "courses").
4) Em Headers, copie o valor de "Authorization" SEM o prefixo "Bearer ".
5) Cole o conteúdo no campo "Token de Acesso".
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        token = self.resolve_access_token(credentials, self._fetch_token_with_credentials)

        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/plain, */*",
                "Origin": APP_BASE,
                "Referer": f"{APP_BASE}/",
                "User-Agent": self._settings.user_agent,
            }
        )

        probe = session.get(PROFILE_URL, timeout=20)
        if probe.status_code == 401:
            raise ValueError("Token inválido ou expirado para a Caveira.")
        probe.raise_for_status()

        self._session = session
        self._access_token = token
        logging.info("Sessão autenticada na Caveira (%s).", probe.json().get("email", "usuário"))

    def _fetch_token_with_credentials(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        if not username or not password:
            raise ValueError("Informe e-mail e senha para autenticar na Caveira.")

        local = requests.Session()
        local.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": APP_BASE,
                "Referer": f"{APP_BASE}/",
                "User-Agent": self._settings.user_agent,
            }
        )

        response = local.post(
            LOGIN_URL,
            json={"email": username, "password": password},
            timeout=30,
        )
        if response.status_code in (401, 403, 422):
            raise ValueError(
                "Falha na autenticação da Caveira. Verifique e-mail/senha. "
                "Se o erro persistir, use o token manual (a API pode estar exigindo reCAPTCHA)."
            )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise ValueError("Resposta de login da Caveira não retornou access_token.")
        return token

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        response = self._session.get(COURSES_URL, timeout=30)
        response.raise_for_status()
        payload = response.json()

        courses: List[Dict[str, Any]] = []
        for course in payload.get("courses", []):
            if not course.get("can_access") or not course.get("enrolled"):
                continue
            courses.append(
                {
                    "id": course.get("id"),
                    "name": course.get("name", "Curso Caveira"),
                    "slug": str(course.get("id")),
                    "seller_name": "Caveira",
                }
            )
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}
        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            modules = self._fetch_modules(course_id)
            content[str(course_id)] = {
                "id": course_id,
                "name": course.get("name", "Curso"),
                "slug": str(course_id),
                "title": course.get("name", "Curso"),
                "modules": modules,
            }
        return content

    def _fetch_modules(self, course_id: int) -> List[Dict[str, Any]]:
        response = self._session.get(COURSE_MODULES_URL.format(course_id=course_id), timeout=30)
        response.raise_for_status()
        raw_modules = response.json() or []

        modules: List[Dict[str, Any]] = []
        order_counter = 0
        for module_index, module in enumerate(raw_modules, start=1):
            module_id = module.get("id")
            if not module_id:
                continue
            module_name = module.get("name", f"Módulo {module_index}")

            try:
                submodules = self._fetch_submodules(course_id, module_id)
            except Exception as exc:
                logging.warning(
                    "[Caveira] Falha ao buscar submódulos do módulo %s (%s): %s",
                    module_id,
                    module_name,
                    exc,
                )
                submodules = []

            if not submodules:
                continue

            for sub_index, submodule in enumerate(submodules, start=1):
                order_counter += 1
                sub_id = submodule.get("id")
                sub_name = submodule.get("name", f"Aula {sub_index}")
                lessons = self._build_lessons(submodule.get("videos") or [], module_id)
                if not lessons:
                    continue
                modules.append(
                    {
                        "id": f"{module_id}-{sub_id}" if sub_id else f"{module_id}-{sub_index}",
                        "title": f"{module_index:02d}. {module_name} - {sub_index:02d}. {sub_name}",
                        "order": order_counter,
                        "lessons": lessons,
                        "locked": False,
                        "raw_module_id": module_id,
                    }
                )
        return modules

    def _fetch_submodules(self, course_id: int, module_id: int) -> List[Dict[str, Any]]:
        response = self._session.get(
            MODULE_VIDEOS_URL.format(course_id=course_id, module_id=module_id),
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json() or {}
        return (payload.get("data") or {}).get("submodules") or []

    def _build_lessons(self, videos: List[Dict[str, Any]], module_id: int) -> List[Dict[str, Any]]:
        lessons: List[Dict[str, Any]] = []
        for index, video in enumerate(videos, start=1):
            video_id = video.get("id")
            if not video_id:
                continue
            lessons.append(
                {
                    "id": video_id,
                    "title": video.get("name", f"Aula {index}"),
                    "order": index,
                    "locked": False,
                    "duration_text": video.get("time"),
                    "raw_module_id": module_id,
                }
            )
        return lessons

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content = LessonContent()
        video_id = lesson.get("id")
        raw_module_id = lesson.get("raw_module_id") or self._raw_module_id_from_compound(module_id)
        if not video_id or not raw_module_id:
            logging.warning("[Caveira] Aula sem IDs suficientes: %s", lesson)
            return content

        try:
            response = self._session.get(
                VIDEO_DETAIL_URL.format(
                    course_id=course_id,
                    module_id=raw_module_id,
                    video_id=video_id,
                ),
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            logging.error("[Caveira] Falha ao buscar aula %s: %s", video_id, exc)
            return content

        payload = response.json() or {}
        course_video = payload.get("course_video") or {}

        video_url = course_video.get("url")
        if video_url:
            content.videos.append(
                Video(
                    video_id=str(video_id),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=course_video.get("name") or lesson.get("title", "Aula"),
                    size=0,
                    duration=self._parse_duration(course_video.get("time")),
                    extra_props={"referer": f"{APP_BASE}/"},
                )
            )

        description_text = course_video.get("description") or course_video.get("note")
        if description_text:
            content.description = Description(text=description_text, description_type="text")

        for attachment_index, file_info in enumerate(course_video.get("files") or [], start=1):
            file_id = file_info.get("id")
            if not file_id:
                continue
            try:
                file_url, filename, extension = self._resolve_attachment(file_id, file_info.get("title", "Material"))
            except Exception as exc:
                logging.error("[Caveira] Falha ao resolver anexo %s: %s", file_id, exc)
                continue

            if not file_url:
                continue

            content.attachments.append(
                Attachment(
                    attachment_id=str(file_id),
                    url=file_url,
                    filename=filename,
                    order=attachment_index,
                    extension=extension,
                    size=0,
                )
            )

        return content

    def _resolve_attachment(self, file_id: int, title: str) -> tuple[str, str, str]:
        response = self._session.get(COURSE_FILE_URL.format(file_id=file_id), timeout=30)
        response.raise_for_status()
        download_url = (response.json() or {}).get("url") or ""
        if not download_url:
            return "", title.strip() or "Material", ""

        filename, extension = self._probe_attachment_filename(download_url, title)
        return download_url, filename, extension

    def _probe_attachment_filename(self, url: str, title: str) -> tuple[str, str]:
        """Hits the download URL with a streaming GET and recovers the real
        filename + extension from Content-Disposition / Content-Type / final
        redirected URL. Connection is closed as soon as headers are read."""

        safe_title = (title or "").strip() or "Material"

        try:
            with self._session.get(url, stream=True, allow_redirects=True, timeout=30) as probe:
                probe.raise_for_status()
                disposition = probe.headers.get("Content-Disposition", "")
                content_type = (probe.headers.get("Content-Type", "") or "").split(";", 1)[0].strip()
                final_url = probe.url
        except Exception as exc:
            logging.warning("[Caveira] Não foi possível inspecionar o anexo %s: %s", url, exc)
            return safe_title, ""

        disposition_name = self._filename_from_disposition(disposition)
        if disposition_name:
            stem = Path(disposition_name).stem
            ext = Path(disposition_name).suffix.lstrip(".")
            if ext:
                return f"{safe_title or stem}.{ext}", ext

        ext = Path(urlparse(final_url).path).suffix.lstrip(".")
        if not ext and content_type:
            guessed = mimetypes.guess_extension(content_type) or ""
            ext = guessed.lstrip(".")

        if ext:
            return f"{safe_title}.{ext}", ext
        return safe_title, ""

    @staticmethod
    def _filename_from_disposition(header_value: str) -> str:
        if not header_value:
            return ""
        match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", header_value, flags=re.IGNORECASE)
        if match:
            return unquote(match.group(1).strip().strip('"'))
        match = re.search(r'filename="?([^";]+)"?', header_value, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return ""

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")
        if not attachment.url:
            logging.error("[Caveira] Anexo sem URL: %s", attachment.filename)
            return False

        try:
            response = self._session.get(attachment.url, stream=True, allow_redirects=True, timeout=60)
            response.raise_for_status()
            with open(download_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
            return True
        except Exception as exc:
            logging.error("[Caveira] Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False

    @staticmethod
    def _raw_module_id_from_compound(module_id: str) -> Optional[str]:
        if not module_id:
            return None
        return str(module_id).split("-", 1)[0] or None

    @staticmethod
    def _parse_duration(time_text: Optional[str]) -> int:
        if not time_text:
            return 0
        parts = time_text.split(":")
        try:
            parts_int = [int(p) for p in parts]
        except ValueError:
            return 0
        if len(parts_int) == 3:
            h, m, s = parts_int
        elif len(parts_int) == 2:
            h, m, s = 0, parts_int[0], parts_int[1]
        else:
            return 0
        return h * 3600 + m * 60 + s


PlatformFactory.register_platform("Projeto Caveira", CaveiraPlatform)
