from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)

API_BASE = "https://api.cakto.com.br"
COURSES_URL = f"{API_BASE}/api/members/courses/"
TOKEN_URL = f"{API_BASE}/api/members/courses/token/"
FIREBASE_BASE = "https://us-central1-cakto2.cloudfunctions.net"
LESSON_URL = f"{FIREBASE_BASE}/api/aulas/{{lesson_id}}/assistir"
STREAM_BASE = "https://stream.cakto.com.br"
SESSION_COOKIE = "sessionid"


class CaktoMembersPlatform(BasePlatform):
    """
    Cakto Members variant (members.cakto.com.br).

    Uses api.cakto.com.br for course listing and a Firebase backend
    for lesson details.  Videos are HLS streams on stream.cakto.com.br.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._jwt: str = ""
        # Cache of full course data (with inline lessons) from the courses endpoint
        self._members_courses: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como obter o token da Cakto Members:
1) Acesse https://app.cakto.com.br e faca login normalmente.
2) Abra o DevTools (F12) > aba Application > Cookies > api.cakto.com.br.
3) Copie o valor do cookie "sessionid" e cole no campo de token acima.
""".strip()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        raise ConnectionError(
            "Login automatico nao e suportado para Cakto Members. "
            "Faca login em https://app.cakto.com.br e copie o cookie 'sessionid'."
        )

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()
        self._session.cookies.set(SESSION_COOKIE, token, domain="api.cakto.com.br", path="/")
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        # Fetch JWT for Firebase API calls
        try:
            resp = self._session.get(TOKEN_URL, timeout=30)
            resp.raise_for_status()
            self._jwt = resp.json().get("accessToken", "")
            if not self._jwt:
                raise ValueError("Token JWT vazio")
            logger.debug("CaktoMembers: JWT obtained")
        except Exception as exc:
            raise ConnectionError(
                "Falha ao obter JWT da Cakto Members. Verifique o cookie sessionid."
            ) from exc

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    # ------------------------------------------------------------------
    # Courses
    # ------------------------------------------------------------------

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        response = self._session.get(COURSES_URL, timeout=30)
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list):
            data = data.get("data", [])

        courses: List[Dict[str, Any]] = []
        for course in data:
            external_id = course.get("externalId") or course.get("id", "")
            name = course.get("nome", "Curso")

            # Cache full inline data (modules + lessons) keyed by externalId
            self._members_courses[str(external_id)] = course

            courses.append({
                "id": external_id,
                "name": name,
                "slug": external_id,
                "seller_name": "",
                "image": course.get("capa", ""),
            })

        logger.debug("CaktoMembers: found %d courses", len(courses))
        return sorted(courses, key=lambda c: c.get("name", ""))

    # ------------------------------------------------------------------
    # Course content
    # ------------------------------------------------------------------

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = str(course.get("id", ""))
            cached = self._members_courses.get(course_id, {})
            modulos = cached.get("modulos", [])

            # If no cached data, try Firebase API
            if not modulos and self._jwt:
                modulos = self._fetch_course_from_firebase(course_id)

            processed_modules = []
            for mod_idx, mod in enumerate(modulos, start=1):
                aulas = mod.get("aulas", [])
                processed_lessons = []

                for les_idx, aula in enumerate(aulas, start=1):
                    video_uuid = aula.get("video", "")
                    video_url = f"{STREAM_BASE}/{video_uuid}/playlist.m3u8" if video_uuid else ""

                    processed_lessons.append({
                        "id": aula.get("id", ""),
                        "title": aula.get("nome", f"Aula {les_idx}"),
                        "order": aula.get("posicao", les_idx),
                        "locked": False,
                        "video_url": video_url,
                        "video_uuid": video_uuid,
                        "description": aula.get("descricao", ""),
                        "files": aula.get("files", []),
                        "module_id": mod.get("id", ""),
                        "_has_inline_data": bool(video_uuid or aula.get("descricao")),
                    })

                processed_modules.append({
                    "id": mod.get("id", ""),
                    "title": mod.get("nome", f"Modulo {mod_idx}"),
                    "order": mod.get("posicao", mod_idx),
                    "lessons": processed_lessons,
                    "locked": False,
                })

            course_entry = course.copy()
            course_entry["title"] = cached.get("nome", course.get("name", f"Curso {course_id}"))
            course_entry["modules"] = processed_modules
            all_content[course_id] = course_entry

        return all_content

    def _fetch_course_from_firebase(self, course_id: str) -> List[Dict[str, Any]]:
        """Fallback: fetch course structure from Firebase API."""
        url = f"{FIREBASE_BASE}/api/cursos/{course_id}/"
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {self._jwt}"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", data).get("modulos", [])
        except Exception as exc:
            logger.error("CaktoMembers: failed to fetch course %s from Firebase: %s", course_id, exc)
            return []

    # ------------------------------------------------------------------
    # Lesson details
    # ------------------------------------------------------------------

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        content = LessonContent()

        # If the inline data was incomplete, fetch from Firebase
        if not lesson.get("_has_inline_data") and self._jwt:
            lesson = self._fetch_lesson_from_firebase(lesson)

        description_text = lesson.get("description") or lesson.get("descricao", "")
        if description_text:
            content.description = Description(text=description_text, description_type="html")

        video_url = lesson.get("video_url", "")
        if video_url:
            content.videos.append(
                Video(
                    video_id=lesson.get("video_uuid") or lesson.get("id", "video"),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=lesson.get("title") or lesson.get("nome", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": "https://members.cakto.com.br/"},
                )
            )

        files = lesson.get("files", [])
        for file_idx, file_info in enumerate(files, start=1):
            filename = file_info.get("name") or file_info.get("nome", f"file_{file_idx}")
            file_id = str(file_info.get("id", file_idx))
            extension = filename.rsplit(".", 1)[-1] if "." in filename else ""

            content.attachments.append(
                Attachment(
                    attachment_id=file_id,
                    url=file_info.get("url", ""),
                    filename=filename,
                    order=file_idx,
                    extension=extension,
                    size=file_info.get("size", 0),
                )
            )

        return content

    def _fetch_lesson_from_firebase(self, lesson: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch full lesson details from Firebase /assistir endpoint."""
        lesson_id = lesson.get("id", "")
        if not lesson_id:
            return lesson

        url = LESSON_URL.format(lesson_id=lesson_id)
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {self._jwt}"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            # Merge Firebase data into lesson, converting to standard fields
            video_uuid = data.get("video", "")
            lesson["video_uuid"] = video_uuid
            lesson["video_url"] = f"{STREAM_BASE}/{video_uuid}/playlist.m3u8" if video_uuid else ""
            lesson["description"] = data.get("descricao", "")
            lesson["files"] = data.get("files", [])
            lesson["title"] = data.get("nome", lesson.get("title", ""))
            logger.debug("CaktoMembers: fetched lesson %s from Firebase", lesson_id)
        except Exception as exc:
            logger.error("CaktoMembers: failed to fetch lesson %s: %s", lesson_id, exc)

        return lesson

    # ------------------------------------------------------------------
    # Attachment download
    # ------------------------------------------------------------------

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        try:
            url = attachment.url
            if not url:
                logger.error("CaktoMembers: attachment has no URL: %s", attachment.filename)
                return False

            headers = {
                "User-Agent": self._settings.user_agent,
                "Referer": "https://members.cakto.com.br/",
            }
            if self._jwt:
                headers["Authorization"] = f"Bearer {self._jwt}"

            response = requests.get(url, stream=True, headers=headers, timeout=120)
            response.raise_for_status()

            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True

        except Exception as exc:
            logger.error("CaktoMembers: failed to download attachment %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Cakto (Variante Members)", CaktoMembersPlatform)
