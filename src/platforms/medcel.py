from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
from pathlib import Path

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

API_BASE_URL = "https://service.medcel.com.br"

ORIGIN_URL = "https://areaaluno.medcel.com.br"

AUTH_URL = f"{API_BASE_URL}/m1/students/auth"
CONTRACTS_URL = f"{API_BASE_URL}/m1/contracts/getContractsByStudent"
PLAYLISTS_URL = f"{API_BASE_URL}/m9/subjectPlaylists/getSubjectPlaylists"
PLAYLIST_CONTENTS_URL = f"{API_BASE_URL}/m9/subjectPlaylists/getPlaylistContents"
VIDEO_URL = f"{API_BASE_URL}/m2/videos/getVideoToPlay"


class MedcelPlatform(BasePlatform):
    """Implements the Medcel platform (areaaluno.medcel.com.br)."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._student_id: Optional[str] = None
        self._session_token: Optional[str] = None
        self._custom_api_key: Optional[str] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="x_api_key",
                label="X-API-Key",
                field_type=AuthFieldType.TEXT,
                placeholder="Cole a X-API-Key aqui",
                required=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """1) Acesse https://areaaluno.medcel.com.br e faça login.
2) Abra o DevTools (F12) e vá para a aba Rede (Network).
3) Procure qualquer requisição para service.medcel.com.br
4) Nos cabeçalhos da requisição, copie o valor do Authorization (sem "Bearer ").
5) Cole o token no campo "Token de Acesso".
6) Nos cabeçalhos da requisição, copie o valor do "X-Api-Key".
7) Cole no campo "X-API-Key".
""".strip()

    def _get_default_headers(self) -> Dict[str, str]:
        """Returns the default headers for API requests."""
        return {
            "X-Api-Key": self._custom_api_key or "",
            "X-Amz-User-Agent": self._settings.user_agent,
            "User-Agent": self._settings.user_agent,
            "Referer": f"{ORIGIN_URL}/",
            "Origin": ORIGIN_URL,
            "Accept": "application/json, text/plain, */*",
        }

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = (credentials.get("token") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()
        x_api_key = (credentials.get("x_api_key") or "").strip()

        if not x_api_key:
            raise ValueError("Informe a X-API-Key para autenticar.")

        self._custom_api_key = x_api_key

        session = requests.Session()
        session.headers.update(self._get_default_headers())

        if token:
            session.headers["Authorization"] = f"Bearer {token}"
            self._session_token = token
            self._session = session
            self._fetch_student_id()
            logging.info("Sessão autenticada no Medcel via token.")
            return

        if not username or not password:
            raise ValueError("Informe um token válido ou credenciais (email/senha) para autenticar.")

        auth_payload = {"email": username, "password": password}
        response = session.post(AUTH_URL, json=auth_payload)
        response.raise_for_status()
        data = response.json()

        self._student_id = data.get("_id")
        self._session_token = data.get("sessionToken")

        if not self._session_token:
            raise ValueError("Falha ao obter token de sessão. Verifique suas credenciais.")

        session.headers["Authorization"] = f"Bearer {self._session_token}"
        self._session = session
        logging.info("Sessão autenticada no Medcel via credenciais.")

    def _fetch_student_id(self) -> None:
        """Fetches the student ID from the contracts endpoint when using token auth."""
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        response = self._session.get(f"{CONTRACTS_URL}?_id=me")
        if response.status_code == 200:
            data = response.json()
            self._student_id = data.get("_id")
        else:
            logging.warning("Não foi possível obter o ID do estudante automaticamente.")

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        if not self._student_id:
            self._fetch_student_id()

        response = self._session.get(f"{CONTRACTS_URL}?_id={self._student_id}")
        response.raise_for_status()
        data = response.json()

        courses: List[Dict[str, Any]] = []
        contracts = data.get("contracts", [])

        for contract in contracts:
            product = contract.get("product", {})
            product_id = product.get("_id")
            product_name = product.get("name", "Curso")

            if not product_id:
                logging.warning("Contrato sem produto encontrado, ignorando.")
                continue

            overall_status = contract.get("overallStatus", {})
            content_available = overall_status.get("content", {}).get("available", True)

            if not content_available:
                logging.info("Curso %s não está disponível para acesso.", product_name)
                continue

            courses.append({
                "id": product_id,
                "name": product_name,
                "slug": product_id,
                "seller_name": "Medcel",
                "contract_id": contract.get("_id"),
                "hierarchy": product.get("hierarchy", {}).get("_id"),
            })

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name", "Curso")

            if not course_id:
                logging.warning("Curso sem ID encontrado, ignorando.")
                continue

            playlists = self._fetch_playlists(course_id)
            modules: List[Dict[str, Any]] = []

            for playlist_index, playlist in enumerate(playlists, start=1):
                playlist_id = playlist.get("_id")
                playlist_name = playlist.get("name", f"Módulo {playlist_index}")
                playlist_order = playlist.get("order", playlist_index)

                if not playlist_id:
                    logging.warning("Playlist sem ID encontrada no curso %s", course_name)
                    continue

                playlist_contents = self._fetch_playlist_contents(playlist_id)
                lessons = self._build_lessons_from_contents(playlist_contents, course_id)

                modules.append({
                    "id": playlist_id,
                    "title": playlist_name.strip(),
                    "order": playlist_order,
                    "lessons": lessons,
                    "locked": False,
                })

            content[str(course_id)] = {
                "id": course_id,
                "name": course_name,
                "slug": course_id,
                "title": course_name,
                "modules": modules,
            }

        return content

    def _fetch_playlists(self, product_id: str) -> List[Dict[str, Any]]:
        """Fetches all playlists (modules) for a product."""
        playlists: List[Dict[str, Any]] = []
        page = 1
        limit = 100

        while True:
            params = {
                "studentId": self._student_id,
                "productId": product_id,
                "specialtyId": "",
                "sortMode": "incidence",
                "trial": "false",
                "page": str(page),
                "limit": str(limit),
            }

            response = self._session.get(PLAYLISTS_URL, params=params)
            response.raise_for_status()
            data = response.json()

            items = data.get("items", []) if isinstance(data, dict) else data
            if not items:
                break

            playlists.extend(items)

            pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
            total_pages = pagination.get("pageTotal", 1)

            if page >= total_pages or len(items) < limit:
                break
            page += 1

        return playlists

    def _fetch_playlist_contents(self, playlist_id: str) -> Dict[str, Any]:
        """Fetches the contents of a specific playlist."""
        params = {
            "playlistId": playlist_id,
            "studentId": self._student_id,
        }

        response = self._session.get(PLAYLIST_CONTENTS_URL, params=params)
        response.raise_for_status()
        return response.json()

    def _build_lessons_from_contents(self, playlist_data: Dict[str, Any], product_id: str) -> List[Dict[str, Any]]:
        """Builds lesson entries from playlist contents.

        Each playlistContent becomes one lesson. The videoClass within it
        is the main video, and all complementMaterial from all classes
        become attachments.
        """
        lessons: List[Dict[str, Any]] = []
        playlist_contents = playlist_data.get("playlistContents", [])

        for content_index, content in enumerate(playlist_contents, start=1):
            content_id = content.get("_id")
            content_name = content.get("name", f"Conteúdo {content_index}")
            classes = content.get("classes", [])

            if not classes:
                continue

            video_class = None
            all_materials: List[Dict[str, Any]] = []
            total_duration = 0

            for class_item in classes:
                class_type = class_item.get("subTypeLearningObject", {})
                type_id = class_type.get("id", "")

                materials = class_item.get("complementMaterial", [])
                all_materials.extend(materials)

                duration = class_item.get("duration") or 0
                if duration:
                    total_duration += duration

                if type_id == "videoClass" and not video_class:
                    video_class = class_item

            if not video_class:
                video_class = classes[0]

            class_id = video_class.get("id") or video_class.get("_id")
            if not class_id:
                logging.warning("Aula sem ID encontrada em %s", content_name)
                continue

            lessons.append({
                "id": class_id,
                "title": content_name.strip(),
                "order": content_index,
                "locked": False,
                "type_id": video_class.get("subTypeLearningObject", {}).get("id", ""),
                "type_name": video_class.get("subTypeLearningObject", {}).get("name", ""),
                "duration": total_duration,
                "product_id": product_id,
                "content_id": content_id,
                "complement_material": all_materials,
            })

        return lessons

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content = LessonContent()
        lesson_id = lesson.get("id")
        lesson_type = (lesson.get("type_id") or "").lower()
        product_id = lesson.get("product_id") or course_id
        complement_material = lesson.get("complement_material", [])

        seen_urls: set = set()
        for mat_index, material in enumerate(complement_material, start=1):
            mat_url = material.get("location", "")
            mat_title = material.get("title", f"Material {mat_index}")
            mat_key = material.get("key", "")

            if mat_url and mat_url not in seen_urls:
                seen_urls.add(mat_url)
                extension = mat_url.rsplit(".", 1)[-1] if "." in mat_url else "pdf"
                filename = f"{mat_title}.{extension}" if extension else mat_title

                content.attachments.append(
                    Attachment(
                        attachment_id=mat_key or f"{lesson_id}-mat-{mat_index}",
                        url=mat_url,
                        filename=filename,
                        order=mat_index,
                        extension=extension,
                        size=0,
                    )
                )

        if "video" in lesson_type or lesson_type == "videoclass":
            video_data = self._fetch_video_data(lesson_id, product_id)

            if video_data:
                video_uri = video_data.get("uri", "")
                signature = video_data.get("signature", "")
                video_url = f"{video_uri}{signature}" if signature else video_uri

                if video_url:
                    content.videos.append(
                        Video(
                            video_id=str(lesson_id),
                            url=video_url,
                            order=lesson.get("order", 1),
                            title=lesson.get("title", "Aula"),
                            size=0,
                            duration=video_data.get("duration", 0),
                            extra_props={
                                "referer": f"{ORIGIN_URL}/",
                            }
                        )
                    )

                description_text = video_data.get("description", "")
                if description_text:
                    content.description = Description(
                        text=description_text,
                        description_type="text"
                    )

        return content

    def _fetch_video_data(self, class_id: str, product_id: str) -> Optional[Dict[str, Any]]:
        """Fetches video playback data for a class."""
        params = {
            "id": class_id,
            "student": self._student_id,
            "product": product_id,
        }

        headers = dict(self._session.headers)
        headers["X-Host-Origin"] = ORIGIN_URL

        try:
            response = self._session.get(VIDEO_URL, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data.get("data", data)
        except Exception as exc:
            logging.error("Erro ao obter dados do vídeo %s: %s", class_id, exc)
            return None

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
        except Exception as exc:
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Medcel", MedcelPlatform)
