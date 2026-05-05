from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, AuxiliaryURL, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)

API_BASE = "https://www.datascienceacademy.com.br"
PRODUCTS_URL = f"{API_BASE}/api/products_all"
USER_COURSES_URL = f"{API_BASE}/api/user/courses-progress?courses="
COURSE_CONTENT_URL = f"{API_BASE}/api/course/{{slug}}?contents"


class DataScienceAcademyPlatform(BasePlatform):
    """Implements the Data Science Academy (LearnWorlds) platform."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 9.90) ativos podem informar usuario/senha. O sistema ira trocar essas credenciais automaticamente pelo token da etapa acima, alem de usar alguns algoritmos melhores e ter funcionalidades extras na aplicacao, e obter suporte prioritario.

Como obter o token da Data Science Academy?
1) Abra https://www.datascienceacademy.com.br/start e faca login.
2) No DevTools (F12) abra a aba Network e atualize a pagina.
3) Procure uma requisicao para /api/course/<slug> ou /api/products_all e copie o valor do cabecalho "Token".
4) Cole apenas o token no campo acima ou informe usuario e senha se permitido pela sua licenca.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Token": token,
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Origin": API_BASE,
                "Referer": f"{API_BASE}/start",
            }
        )
        self._fetch_csrf_token()

    def _fetch_csrf_token(self) -> None:
        """Fetch the CSRF token from the /start page and add it to session headers.

        LearnWorlds requires this token on API calls that return user-specific
        data (e.g. courses-progress).  Without it the server returns empty
        enrollment info.  The GET also captures any session cookies the server
        sets, which may be required for authenticated API responses.
        """
        try:
            # Use Accept: text/html so the server returns the HTML page (not JSON)
            resp = self._session.get(
                f"{API_BASE}/start",
                headers={"Accept": "text/html,application/xhtml+xml,*/*"},
                timeout=30,
            )
            resp.raise_for_status()
            logger.info(
                "DSA: /start respondeu status=%d content-type=%s body=%d bytes cookies=%s",
                resp.status_code,
                resp.headers.get("content-type", "?"),
                len(resp.text),
                list(self._session.cookies.keys()),
            )
            match = re.search(
                r'<meta\s+name=["\']csrf-token["\']\s+content=["\'](.*?)["\']', resp.text
            )
            if match:
                csrf_token = match.group(1)
                self._session.headers["csrf-token"] = csrf_token
                logger.info("DSA: csrf-token obtido com sucesso (%d chars)", len(csrf_token))
            else:
                logger.warning("DSA: csrf-token nao encontrado na pagina /start (body preview: %.200s)", resp.text[:800])
        except Exception as exc:
            logger.warning("DSA: falha ao obter csrf-token de /start: %s", exc)

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        raise ConnectionError(
            "A troca automatica de credenciais nao e suportada para esta plataforma. Use um token valido."
        )

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        response = self._session.get(PRODUCTS_URL, timeout=30)
        response.raise_for_status()
        data = response.json()

        courses_map = data.get("courses", {})
        # allowed_ids = set(data.get("allowedCourseIds") or [])

        # # Fetch user enrollment data (premium/registered) from the dedicated endpoint.
        # # The products_all response does not populate me.premium for bundle-enrolled
        # # courses, so we must call user/courses-progress to get the real enrollment
        # # state.  The LearnWorlds frontend filters accessible courses using the same
        # # logic: me.premium || me.registered  (see getUserCoursesFiltered getter).
        # enrolled_ids: set = set()
        # try:
        #     progress_resp = self._session.get(
        #         USER_COURSES_URL, timeout=30
        #     )
        #     progress_resp.raise_for_status()
        #     progress_data = progress_resp.json()
        #     user_courses = progress_data.get("userCourses") or []
        #     logger.info("DSA: courses-progress retornou %d userCourses no total", len(user_courses))
        #     for uc in user_courses:
        #         uc_me = uc.get("me", {})
        #         cid = str(uc.get("courseId", ""))
        #         registered = uc_me.get("registered")
        #         premium = uc_me.get("premium")
        #         if premium or registered:
        #             enrolled_ids.add(cid)
        #             logger.info("DSA: courses-progress enrolled: id=%s registered=%s premium=%s", cid, registered, premium)
        #     logger.info("DSA: courses-progress total enrolled (premium||registered): %d", len(enrolled_ids))
        # except Exception as exc:
        #     logger.warning("DSA: failed to fetch user courses-progress: %s", exc)

        courses: List[Dict[str, Any]] = []

        logger.info("DSA: products_all retornou %d cursos no total", len(courses_map))
        # logger.info("DSA: allowedCourseIds contem %d ids: %s", len(allowed_ids), sorted(allowed_ids))
        # logger.info("DSA: courses-progress retornou %d enrolled ids: %s", len(enrolled_ids), sorted(enrolled_ids))

        for cid, course in courses_map.items():
            # me = course.get("me", {})
            course_id = str(course.get("id", cid))
            title = course.get("title", "Curso")
            # is_registered = me.get("registered")
            # is_premium = me.get("premium")
            # is_allowed = course_id in allowed_ids or cid in allowed_ids
            # is_free = course.get("status") == "free"
            # is_enrolled = course_id in enrolled_ids
            # status = course.get("status", "")

            # logger.info(
            #     "DSA: [%s] id=%s cid=%s | registered=%s premium=%s allowed=%s free=%s enrolled=%s status=%s",
            #     title, course_id, cid, is_registered, is_premium, is_allowed, is_free, is_enrolled, status,
            # )

            # if not is_registered and not is_allowed and not is_free and not is_enrolled:
            #     skipped.append(f"{title} (id={course_id})")
            #     continue

            courses.append(
                {
                    "id": course.get("id", cid),
                    "name": title,
                    "slug": course.get("titleId", cid),
                    "seller_name": "Data Science Academy",
                }
            )

        logger.info("DSA: retornando todos os %d cursos sem filtro", len(courses))
        return sorted(courses, key=lambda c: c.get("name", ""))

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        content: Dict[str, Any] = {}

        for course in courses:
            slug = course.get("slug") or course.get("id")
            if not slug:
                continue

            try:
                resp = self._session.get(COURSE_CONTENT_URL.format(slug=slug), timeout=60)
                resp.raise_for_status()
                course_json = resp.json()
            except Exception as exc:
                logger.error("DSA: failed to fetch course %s: %s", slug, exc)
                continue

            course_data = course_json.get("course", course_json)
            modules = self._extract_sections(course_data)

            course_entry = course.copy()
            course_entry["title"] = course_data.get("title", course.get("name", "Curso"))
            course_entry["modules"] = modules
            content[str(slug)] = course_entry

        return content

    def _extract_sections(self, course_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        sections = course_data.get("sections", {})
        videos = course_data.get("videos", {})
        objects = course_data.get("objects", {})

        if isinstance(sections, dict):
            sections_list = list(sections.values())
        elif isinstance(sections, list):
            sections_list = sections
        else:
            sections_list = []

        modules: List[Dict[str, Any]] = []

        for section_index, section in enumerate(sections_list, start=1):
            if not isinstance(section, dict):
                continue

            title = self._resolve_title(
                section.get("titles") or section.get("title"),
                f"Modulo {section_index}",
            )
            learning_path = section.get("learningPath") or []

            lessons: List[Dict[str, Any]] = []
            for lesson_index, item in enumerate(learning_path, start=1):
                unit_id = str(item.get("id") or f"{section_index}-{lesson_index}")
                unit_type = (item.get("type") or "").lower()

                lesson_title = ""
                if unit_type in ("ivideo", "video") and unit_id in videos:
                    lesson_title = videos[unit_id].get("title", "")
                elif unit_id in objects:
                    lesson_title = objects[unit_id].get("title", "")

                if not lesson_title or lesson_title == "Untitled":
                    lesson_title = self._resolve_title(
                        item.get("titles") or item.get("unitTitle"),
                        f"Aula {lesson_index}",
                    )

                lesson_data: Dict[str, Any] = {
                    "id": unit_id,
                    "title": lesson_title,
                    "order": lesson_index,
                    "locked": False,
                    "item_type": unit_type,
                }

                if unit_type in ("ivideo", "video") and unit_id in videos:
                    vid = videos[unit_id]
                    lesson_data["vimeoid"] = vid.get("vimeoid", "")
                    lesson_data["duration"] = vid.get("duration", 0)
                elif unit_id in objects:
                    lesson_data["object_data"] = objects[unit_id].get("data", {})

                lessons.append(lesson_data)

            modules.append(
                {
                    "id": str(section.get("titleId") or section.get("id") or section_index),
                    "title": title,
                    "order": section_index,
                    "lessons": lessons,
                    "locked": False,
                }
            )

        return modules

    def _resolve_title(self, raw_title: Any, fallback: str) -> str:
        if isinstance(raw_title, dict):
            for key in ("pt-BR", "pt", "en", "title"):
                if raw_title.get(key):
                    return str(raw_title[key]).strip()
            first_value = next((str(v).strip() for v in raw_title.values() if v), "")
            if first_value:
                return first_value
        if isinstance(raw_title, list):
            for entry in raw_title:
                if entry:
                    return str(entry).strip()
        if isinstance(raw_title, str) and raw_title.strip():
            return raw_title.strip()
        return fallback

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        content = LessonContent()
        unit_id = str(lesson.get("id"))
        unit_type = (lesson.get("item_type") or "").lower()
        order = lesson.get("order", 1)
        title = lesson.get("title", f"Aula {order}")

        if unit_type in ("ivideo", "video"):
            vimeoid = lesson.get("vimeoid", "")
            if vimeoid:
                content.videos.append(
                    Video(
                        video_id=vimeoid,
                        url=f"https://player.vimeo.com/video/{vimeoid}",
                        order=order,
                        title=title,
                        size=0,
                        duration=lesson.get("duration", 0) or 0,
                    )
                )

        elif unit_type == "youtube":
            obj_data = lesson.get("object_data", {})
            embed = obj_data.get("embed", "")
            yt_url = self._extract_url_from_embed(embed)
            if yt_url:
                content.videos.append(
                    Video(
                        video_id=unit_id,
                        url=yt_url,
                        order=order,
                        title=title,
                        size=0,
                        duration=0,
                    )
                )

        elif unit_type == "pdf":
            obj_data = lesson.get("object_data", {})
            pdf_url = obj_data.get("pdf_full", "")
            if pdf_url:
                filename = obj_data.get("pdf_name", f"{unit_id}.pdf")
                extension = filename.rsplit(".", 1)[-1] if "." in filename else "pdf"
                content.attachments.append(
                    Attachment(
                        attachment_id=unit_id,
                        url=pdf_url,
                        filename=filename,
                        order=order,
                        extension=extension,
                        size=0,
                    )
                )

        elif unit_type == "url":
            obj_data = lesson.get("object_data", {})
            link = obj_data.get("link", "")
            if link:
                content.auxiliary_urls.append(
                    AuxiliaryURL(
                        url_id=unit_id,
                        url=link,
                        order=order,
                        title=title,
                        description=link,
                    )
                )

        elif unit_type == "pbebook":
            obj_data = lesson.get("object_data", {})
            page_slug = obj_data.get("pageSlug", "")
            if page_slug:
                content.auxiliary_urls.append(
                    AuxiliaryURL(
                        url_id=unit_id,
                        url=f"{API_BASE}/ebook/{page_slug}",
                        order=order,
                        title=title,
                        description="eBook",
                    )
                )

        return content

    @staticmethod
    def _extract_url_from_embed(embed_html: str) -> str:
        if not embed_html:
            return ""
        match = re.search(r'src=["\']([^"\']+)["\']', embed_html)
        return match.group(1) if match else ""

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        if not attachment.url:
            return False

        response = self._session.get(attachment.url, stream=True, timeout=120)
        response.raise_for_status()

        with open(download_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return True


PlatformFactory.register_platform("Data Science Academy", DataScienceAcademyPlatform)
