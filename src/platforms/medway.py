from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
from pathlib import Path

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory

COURSE_LIST_URL = "https://cms.medway.com.br/api/v2/student_group/"
SUBJECT_LIST_URL = "https://cms.medway.com.br/api/v2/lesson-subject/"
MODULES_URL = "https://cms.medway.com.br/api/v2/lesson-subject/{subject_id}/modules/"
MODULE_CONTENT_URL = "https://cms.medway.com.br/api/v2/lesson-module/{module_id}/"
DOCUMENT_DETAILS_URL = "https://cms.medway.com.br/api/v2/lesson-document/{document_id}/"


class MedwayPlatform(BasePlatform):
    """Implements the Medway platform using the shared downloader interface."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 9.90) ativos podem informar usuário/senha. O sistema irá trocar essas credenciais automaticamente pelo token da etapa acima, além de usar alguns algoritmos melhores e ter funcionalidades extras na aplicação, e obter suporte prioritário.

Para usuários Gratuitos: Como obter o token de acesso da Medway?:
1) Acesse https://app.medway.com.br e faça login normalmente.
2) Abra o DevTools (F12) e navegue até a aba Rede (Network).
3) Recarregue a página e encontre uma requisição chamada "student_group".
4) Nos cabeçalhos da requisição, copie o valor do Authorization (apenas o token, sem o prefixo Bearer).
5) Cole o token no campo de autenticação acima.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = (credentials.get("token") or "").strip()
        if not token:
            raise ValueError("Informe um token válido para autenticar na Medway.")

        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": self._settings.user_agent,
            }
        )
        self._session = session
        logging.info("Sessão autenticada na Medway.")

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        courses: List[Dict[str, Any]] = []
        next_url: Optional[str] = COURSE_LIST_URL

        while next_url:
            response = self._session.get(next_url)
            response.raise_for_status()
            data = response.json()
            logging.debug("Medway course page payload from %s: %s", next_url, data)
            results = data.get("results", [])

            for course in results:
                course_id = course.get("id")
                name = course.get("name", "Curso")
                courses.append(
                    {
                        "id": course_id,
                        "name": name,
                        "slug": str(course_id),
                        "seller_name": "Medway",
                    }
                )

            next_url = data.get("next")

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                logging.warning("Curso sem ID encontrado, ignorando.")
                continue

            subjects = self._fetch_subjects(str(course_id))
            modules: List[Dict[str, Any]] = []

            for subject_index, subject in enumerate(subjects, start=1):
                subject_id = subject.get("id")
                if not subject_id:
                    logging.warning("Assunto sem ID encontrado no curso %s", course_id)
                    continue
                subject_name = subject.get("name", f"Módulo {subject_index}")
                subject_order = subject.get("order", subject_index)

                course_modules = self._fetch_course_modules(str(subject_id))
                for module in course_modules:
                    if not module.get("id"):
                        logging.warning("Módulo sem ID encontrado em %s", subject_name)
                        continue
                    module_entry = self._build_module_entry(subject_name, subject_order, module)
                    modules.append(module_entry)

            course_entry = {
                "id": course_id,
                "name": course.get("name", "Curso"),
                "slug": str(course_id),
                "title": course.get("name", "Curso"),
                "modules": modules,
            }

            content[str(course_id)] = course_entry

        return content

    def _fetch_subjects(self, course_id: str) -> List[Dict[str, Any]]:
        subjects: List[Dict[str, Any]] = []
        next_url: Optional[str] = f"{SUBJECT_LIST_URL}?ordering=order&studentgroup={course_id}"

        while next_url:
            response = self._session.get(next_url)
            response.raise_for_status()
            data = response.json()
            logging.debug("Medway subjects payload from %s: %s", next_url, data)
            subjects.extend(data.get("results", []))
            next_url = data.get("next")

        return subjects

    def _fetch_course_modules(self, subject_id: str) -> List[Dict[str, Any]]:
        response = self._session.get(MODULES_URL.format(subject_id=subject_id))
        response.raise_for_status()
        payload = response.json()
        logging.debug("Medway modules for subject %s: %s", subject_id, payload)
        return payload

    def _fetch_module_contents(self, module_id: str) -> Dict[str, Any]:
        response = self._session.get(MODULE_CONTENT_URL.format(module_id=module_id))
        response.raise_for_status()
        payload = response.json()
        logging.debug("Medway module %s contents: %s", module_id, payload)
        return payload

    def _build_module_entry(self, subject_name: str, subject_order: int, module: Dict[str, Any]) -> Dict[str, Any]:
        module_id = module.get("id")
        module_name = module.get("name", "Módulo")
        module_order = module.get("order") or module.get("lesson_order") or 0
        module_title = f"{subject_order}. {subject_name} - {module_order}. {module_name}" if module_order else f"{subject_order}. {subject_name} - {module_name}"

        module_contents = self._fetch_module_contents(str(module_id))
        lessons: List[Dict[str, Any]] = []

        for item_index, item in enumerate(module_contents.get("module_items", []), start=1):
            item_type = item.get("type", "")
            lessons.append(
                {
                    "id": item.get("id") or f"{module_id}-{item_index}",
                    "title": item.get("name", f"Aula {item_index}"),
                    "order": item.get("order", item_index),
                    "locked": False,
                    "item_type": item_type,
                    "video_url": item.get("url_lesson"),
                    "document_id": item.get("object_id"),
                }
            )

        return {
            "id": module_id,
            "title": module_title,
            "order": module_order or len(lessons),
            "lessons": lessons,
            "locked": False,
        }

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        lesson_type = (lesson.get("item_type") or "").lower()
        content = LessonContent()

        if "vídeoaula" in lesson_type or "video" in lesson_type:
            video_url = lesson.get("video_url", "")
            if "vimeo.com" in video_url and "player.vimeo.com" not in video_url:
                video_id = video_url.rstrip("/").split("/")[-1]
                video_url = f"https://player.vimeo.com/video/{video_id}?autoplay=1&app_id=122963"

            content.videos.append(
                Video(
                    video_id=str(lesson.get("id")),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": "https://app.medway.com.br/"}
                )
            )
        elif "documento" in lesson_type:
            document_id = lesson.get("document_id")
            if not document_id:
                logging.warning("Documento sem ID encontrado na aula %s", lesson.get("title"))
                return content

            response = self._session.get(DOCUMENT_DETAILS_URL.format(document_id=document_id))
            response.raise_for_status()
            doc_data = response.json()
            logging.debug("Medway document %s metadata: %s", document_id, doc_data)

            file_url = doc_data.get("document", "")
            file_name = doc_data.get("name", "Documento")
            extension = file_url.split(".")[-1] if "." in file_url else ""
            filename_with_ext = f"{file_name}.{extension}" if extension else file_name

            content.attachments.append(
                Attachment(
                    attachment_id=str(doc_data.get("id", document_id)),
                    url=file_url,
                    filename=filename_with_ext,
                    order=lesson.get("order", 1),
                    extension=extension,
                    size=doc_data.get("size", 0),
                )
            )
        else:
            logging.warning("Tipo de conteúdo não suportado na aula %s: %s", lesson.get("title"), lesson_type)

        return content

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

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


PlatformFactory.register_platform("Medway", MedwayPlatform)
