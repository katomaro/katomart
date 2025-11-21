from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

LOGIN_URL = "https://admin-api.kiwify.com.br/v1/handleAuth/login"
COURSES_URL = "https://admin-api.kiwify.com.br/v1/viewer/schools/courses"
SCHOOL_COURSES_URL = "https://admin-api.kiwify.com.br/v1/viewer/schools/{school_id}/courses"
COURSE_DETAILS_URLS = [
    "https://admin-api.kiwify.com/v1/viewer/clubs/{course_id}/content?caipirinha=true",
    "https://admin-api.kiwify.com.br/v1/viewer/courses/{course_id}",
]
LESSON_DETAILS_URL = "https://admin-api.kiwify.com/v1/viewer/courses/{course_id}/lesson/{lesson_id}"


class KiwifyPlatform(BasePlatform):
    """Implements the Kiwify platform using the shared platform interface."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 5.00) ativos podem informar usuário/senha. O sistema irá trocar essas credenciais automaticamente pelo token da etapa acima, além de usar alguns algoritmos melhores e ter funcionalidades extras na aplicação, e obter suporte prioritário.

Para usuários gratuitos: Como obter o token da Kiwify?:
1) Acesse https://admin.kiwify.com.br em seu navegador e faça login normalmente.
2) Abra o DevTools (F12) e vá para a aba Rede (Network).
3) Recarregue a página e procure por requisições para "admin-api.kiwify.com".
4) Abra uma requisição autenticada e copie o valor do cabeçalho Authorization (Bearer ...).
5) Cole apenas o token acima no campo de token
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        try:
            response = requests.post(
                LOGIN_URL,
                json={"email": username, "password": password, "returnSecureToken": True},
                headers={"User-Agent": self._settings.user_agent},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            token = data.get("idToken")
            if not token:
                raise ValueError("Resposta de autenticação não retornou token.")
            return token
        except Exception as exc:  # pragma: no cover - network dependent
            raise ConnectionError("Falha ao autenticar na Kiwify. Verifique as credenciais.") from exc

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://admin.kiwify.com.br",
                "Referer": "https://admin.kiwify.com.br/",
            }
        )

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        top_level = self._list_courses(False, "")
        aggregated: Dict[str, Dict[str, Any]] = {}

        for course in top_level:
            if course.get("is_school"):
                nested_courses = self._list_courses(True, str(course["id"]))
                school_name = course.get("name", "Escola")
                for nested in nested_courses:
                    nested_id = str(nested.get("id", ""))
                    nested_name = nested.get("name", "Curso")
                    nested["name"] = f"{school_name} - {nested_name}"
                    aggregated[nested_id] = nested
            else:
                course_id = str(course.get("id", ""))
                aggregated[course_id] = course

        return sorted(aggregated.values(), key=lambda c: c.get("name", ""))

    def _list_courses(self, school_listing: bool, school_id: str) -> List[Dict[str, Any]]:
        courses: List[Dict[str, Any]] = []
        page_counter = 1

        while True:
            if school_listing:
                url = f"{SCHOOL_COURSES_URL.format(school_id=school_id)}?page={page_counter}&archived=false"
            else:
                url = f"{COURSES_URL}?page={page_counter}&archived=false"

            response = self._session.get(url)
            response.raise_for_status()
            data = response.json()

            if school_listing:
                items = data.get("my_courses", [])
                for course in items:
                    courses.append(
                        {
                            "id": course.get("id"),
                            "name": course.get("name", "Curso"),
                            "seller_name": course.get("producer", {}).get("name", "Produtor"),
                            "slug": str(course.get("id", "")),
                        }
                    )
            else:
                items = data.get("courses", [])
                for course in items:
                    course_info = course.get("course_info") or {}
                    school_info = course.get("school_info") or {}
                    producer_name = course.get("producer", {}).get("name", "Produtor")
                    if course.get("course_in_school"):
                        courses.append(
                            {
                                "id": school_info.get("id"),
                                "name": school_info.get("name", "Escola"),
                                "seller_name": producer_name,
                                "slug": str(school_info.get("id", "")),
                                "is_school": True,
                            }
                        )
                    else:
                        courses.append(
                            {
                                "id": course_info.get("id"),
                                "name": course_info.get("name", "Curso"),
                                "seller_name": producer_name,
                                "slug": str(course_info.get("id", "")),
                                "is_school": False,
                            }
                        )

            total_courses = data.get("count", 0)
            page_size = data.get("page_size", 10)
            if page_counter * page_size >= total_courses:
                break
            page_counter += 1

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                logging.warning("Curso sem ID encontrado, ignorando.")
                continue

            details = self._get_course_details(str(course_id))
            if not details:
                logging.warning("Nenhum detalhe encontrado para o curso %s", course_id)
                continue

            modules_data = self._extract_modules(details)
            processed_modules = self._process_modules(modules_data)

            course_entry = course.copy()
            course_entry["title"] = course.get("name", f"Curso {course_id}")
            course_entry["modules"] = processed_modules
            all_content[str(course_id)] = course_entry

        return all_content

    def _get_course_details(self, course_id: str) -> Dict[str, Any]:
        for url_template in COURSE_DETAILS_URLS:
            url = url_template.format(course_id=course_id)
            try:
                response = self._session.get(url)
                response.raise_for_status()
                data = response.json()

                if isinstance(data, dict):
                    if "course" in data:
                        return data["course"]
                    if "data" in data:
                        return data["data"]
                    if "content" in data:
                        return data["content"]
                    if "modules" in data:
                        return data
            except Exception as exc:  # pragma: no cover - network dependent
                logging.debug("Kiwify course detail fetch failed for %s: %s", url, exc)
                continue

        logging.error("Falha ao obter detalhes do curso %s em todas as APIs conhecidas.", course_id)
        return {}

    def _extract_modules(self, course_details: Dict[str, Any]) -> Any:
        if "modules" in course_details:
            return course_details["modules"]
        if "all_modules" in course_details:
            return course_details["all_modules"]
        return []

    def _process_modules(self, modules_data: Any) -> List[Dict[str, Any]]:
        modules: List[Dict[str, Any]] = []
        if isinstance(modules_data, dict):
            iterable = modules_data.values()
        else:
            iterable = modules_data or []

        for module_index, module in enumerate(iterable, start=1):
            lessons_raw = module.get("lessons", {}) if isinstance(module, dict) else {}
            if isinstance(lessons_raw, dict):
                lessons_iterable = lessons_raw.values()
            else:
                lessons_iterable = lessons_raw or []

            lessons: List[Dict[str, Any]] = []
            for lesson_index, lesson in enumerate(lessons_iterable, start=1):
                lesson_id = lesson.get("id") if isinstance(lesson, dict) else str(lesson)
                lesson_title = self._extract_lesson_title(lesson) or f"Aula {lesson_index}"
                lesson_order = lesson.get("order", lesson_index) if isinstance(lesson, dict) else lesson_index
                lessons.append(
                    {
                        "id": lesson_id,
                        "title": lesson_title,
                        "order": lesson_order,
                        "locked": False,
                    }
                )

            module_id = module.get("id") if isinstance(module, dict) else str(module_index)
            module_title = module.get("name") if isinstance(module, dict) else f"Módulo {module_index}"
            module_order = module.get("order", module_index) if isinstance(module, dict) else module_index

            modules.append(
                {
                    "id": module_id,
                    "title": module_title or f"Módulo {module_index}",
                    "order": module_order,
                    "lessons": lessons,
                    "locked": False,
                }
            )

        return modules

    def _extract_lesson_title(self, lesson: Any) -> str:
        if not isinstance(lesson, dict):
            return str(lesson)

        for key in ("title", "name", "ref"):
            value = lesson.get(key)
            if value:
                return str(value)

        lesson_id = lesson.get("id")
        return str(lesson_id) if lesson_id else ""

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        lesson_id = lesson.get("id")
        if not lesson_id:
            raise ValueError("ID da aula não encontrado.")

        url = LESSON_DETAILS_URL.format(course_id=course_id, lesson_id=lesson_id)
        response = self._session.get(url)
        response.raise_for_status()
        lesson_json = response.json().get("lesson", {})

        content = LessonContent()

        description = lesson_json.get("content")
        if description:
            content.description = Description(text=description, description_type="html")

        video_info = lesson_json.get("video") or {}
        video_url = video_info.get("stream_link") or video_info.get("download_link")
        if video_url:
            content.videos.append(
                Video(
                    video_id=str(video_info.get("id", lesson_id)),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=video_info.get("name", "Aula"),
                    size=video_info.get("size", 0),
                    duration=video_info.get("duration", 0),
                )
            )

        for file_index, file_info in enumerate(lesson_json.get("files", []), start=1):
            filename = file_info.get("name") or f"file_{file_index}"
            attachment_id = str(file_info.get("id", file_index))
            extension = filename.split(".")[-1] if "." in filename else ""
            content.attachments.append(
                Attachment(
                    attachment_id=attachment_id,
                    url=file_info.get("url", ""),
                    filename=filename,
                    order=file_info.get("order", file_index),
                    extension=extension,
                    size=file_info.get("size", 0),
                )
            )

        return content

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        if not attachment.url:
            logging.error("Anexo sem URL disponível: %s", attachment.filename)
            return False

        try:
            response = self._session.get(attachment.url, stream=True)
            response.raise_for_status()
            with open(download_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    file_handle.write(chunk)
            return True
        except Exception as exc:  # pragma: no cover - network dependent
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Kiwify", KiwifyPlatform)
