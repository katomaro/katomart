from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)

BASE_URL = "https://www.grancursosonline.com.br"
LOGIN_PAGE_URL = f"{BASE_URL}/identificacao"
LOGIN_URL = f"{BASE_URL}/identificacao/login"
SEARCH_COURSES_URL = f"{BASE_URL}/aluno/api/assinatura/buscar-cursos"
LIST_DISCIPLINES_URL = f"{BASE_URL}/aluno/curso/listar-conteudo-aula/codigo/{{course_id}}/tipo/{{content_type}}"
LIST_CONTENT_URL = f"{BASE_URL}/aluno/curso/listar-conteudo-aula/codigo/{{course_id}}/tipo/{{content_type}}/disciplina/{{discipline_id}}"
LIST_LESSONS_URL = f"{BASE_URL}/aluno/curso/listar-conteudo-aula/codigo/{{course_id}}/tipo/{{content_type}}/disciplina/{{discipline_id}}/conteudo/{{content_id}}"
VIDEO_INFO_URL = f"{BASE_URL}/aluno/sala-de-aula/video/co/{{course_id}}/a/{{video_id}}/c/{{contract_id}}"
MATERIALS_URL = f"{BASE_URL}/aluno/sala-de-aula/get-materiais/co/{{course_id}}/a/{{video_id}}/t/video"
AUDIO_URL = f"{BASE_URL}/aluno/espaco/download-audio/codigo/{{course_id}}/c/{{video_id}}"

# CDN base URLs for materials
ASSETS_CDN_URL = "https://assets.infra.grancursosonline.com.br"
VIDEO_CDN_URL = "https://videoaulas.infra.grancursosonline.com.br"


class GranCursosPlatform(BasePlatform):
    """Implements the Gran Cursos Online platform."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._contract_id: Optional[str] = None
        self._csrf_token: Optional[str] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="cookie",
                label="Cookie de Sessão (grancursosonline)",
                field_type=AuthFieldType.PASSWORD,
                placeholder="Cole o valor do cookie 'grancursosonline'",
                required=False,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 9.90) ativos podem informar usuário/senha. O sistema irá fazer login automaticamente.

Para usuários Gratuitos: Como obter o cookie de sessão:
1) Acesse https://www.grancursosonline.com.br e faça login normalmente.
2) Abra o DevTools (F12) e navegue até a aba Aplicação (Application).
3) No menu lateral, clique em Cookies > www.grancursosonline.com.br.
4) Encontre o cookie chamado "grancursosonline" e copie seu valor.
5) Cole o valor do cookie no campo acima.

Observação: O login com usuário/senha pode não funcionar se houver Cloudflare challenge.
Nesse caso, use o método do cookie.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        cookie = (credentials.get("cookie") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        session = requests.Session()
        session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        if cookie:
            session.cookies.set("grancursosonline", cookie, domain=".grancursosonline.com.br")
            self._session = session
            self._validate_session()
            logger.info("Sessão autenticada no Gran Cursos via cookie.")
        elif username and password:
            self._session = session
            self._login_with_credentials(username, password)
            logger.info("Sessão autenticada no Gran Cursos via login.")
        else:
            raise ValueError("Informe um cookie de sessão ou usuário/senha para autenticar.")

    def _get_csrf_token(self) -> str:
        """Fetches CSRF token from login page."""
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        response = self._session.get(LOGIN_PAGE_URL)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        csrf_input = soup.find("input", {"name": "csrf-token"})
        if csrf_input and csrf_input.get("value"):
            return csrf_input["value"]

        match = re.search(r'csrf-token["\']?\s*:\s*["\']([^"\']+)["\']', response.text)
        if match:
            return match.group(1)

        raise ValueError("Não foi possível obter o token CSRF.")

    def _login_with_credentials(self, username: str, password: str) -> None:
        """Performs login with username and password."""
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        csrf_token = self._get_csrf_token()

        login_data = {
            "email": username,
            "senha": password,
            "csrf-token": csrf_token,
            "action-url-retorno": "undefined",
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": LOGIN_PAGE_URL,
        }

        response = self._session.post(LOGIN_URL, data=login_data, headers=headers)
        response.raise_for_status()

        data = response.json()
        if data.get("status") != "Sucesso":
            raise ValueError(f"Falha no login: {data.get('mensagem', 'Erro desconhecido')}")

        self._validate_session()

    def _validate_session(self) -> None:
        """Validates the session by fetching user data."""
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        response = self._session.get(f"{BASE_URL}/aluno/aluno/buscar-dados")
        response.raise_for_status()

        data = response.json()
        if not data.get("id"):
            raise ConnectionError("Sessão inválida. Verifique suas credenciais.")

        logger.debug("Usuário autenticado: %s %s", data.get("nome"), data.get("sobrenome"))

    def _get_contract_id(self) -> str:
        """Gets the contract ID for the current user."""
        if self._contract_id:
            return self._contract_id

        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        response = self._session.get(f"{SEARCH_COURSES_URL}?q=&page=1&limit=1")
        response.raise_for_status()

        data = response.json()
        contract_id = data.get("contrato")
        if not contract_id:
            raise ValueError("Não foi possível obter o ID do contrato.")

        self._contract_id = contract_id
        return contract_id

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """
        Gran Cursos has thousands of courses available.
        Use the search box to find specific courses.
        """
        logger.info("Gran Cursos: Utilize a caixa de pesquisa para encontrar cursos específicos.")
        return []

    def search_courses(self, query: str) -> List[Dict[str, Any]]:
        """Searches for courses matching the query using the API."""
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        if not query or not query.strip():
            logger.warning("Gran Cursos: Informe um termo de busca para encontrar cursos.")
            return []

        logger.info("Buscando cursos por '%s' no Gran Cursos...", query)

        courses: List[Dict[str, Any]] = []
        seen_ids = set()
        page = 1
        limit = 50

        while page <= 10:  # Limit to 10 pages max
            response = self._session.get(
                SEARCH_COURSES_URL,
                params={"q": query, "page": page, "limit": limit}
            )
            response.raise_for_status()
            data = response.json()

            new_items = 0
            for course in data.get("cursos", []):
                course_id = course.get("id_curso_online")
                if not course_id or course_id in seen_ids:
                    continue

                seen_ids.add(course_id)
                new_items += 1

                courses.append({
                    "id": course_id,
                    "name": course.get("nome_curso_online", "Curso"),
                    "slug": course.get("st_slug", str(course_id)),
                    "seller_name": course.get("st_instituicao", "Gran Cursos"),
                    "module_id": course.get("modulo_id"),
                    "turma": course.get("turma"),
                    "nr_video_aula": course.get("nr_video_aula", 0),
                })

            if new_items == 0:
                break

            pagination = data.get("pagination", {})
            if not pagination.get("has_next_page"):
                break
            page += 1

        logger.info("Encontrados %d cursos para '%s'.", len(courses), query)
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}
        contract_id = self._get_contract_id()

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                logger.warning("Curso sem ID encontrado, ignorando.")
                continue

            course_id_encoded = quote(course_id, safe="")
            disciplines = self._fetch_disciplines(course_id_encoded, "video")
            modules: List[Dict[str, Any]] = []

            module_order = 0  # Global module counter for the course

            for discipline in disciplines:
                discipline_id = discipline.get("id")
                discipline_name = discipline.get("nome", "Disciplina")

                if not discipline_id:
                    continue

                content_categories = self._fetch_content_categories(course_id_encoded, discipline_id, "video")

                for cat_idx, category in enumerate(content_categories):
                    content_id = category.get("id")
                    content_desc = category.get("desc", "")

                    if not content_id:
                        continue

                    lessons = self._fetch_lessons(course_id_encoded, discipline_id, content_id, "video")

                    if not lessons:
                        continue

                    module_order += 1

                    # Build module title
                    if len(content_categories) > 1 and content_desc:
                        content_desc_short = content_desc[:80].strip()
                        module_title = f"{module_order:02d}. {discipline_name} - {content_desc_short}"
                    else:
                        module_title = f"{module_order:02d}. {discipline_name}"

                    module_entry = {
                        "id": f"{discipline_id}_{content_id}",
                        "title": module_title,
                        "order": module_order,
                        "lessons": lessons,
                        "locked": False,
                        "discipline_id": discipline_id,
                        "content_id": content_id,
                    }
                    modules.append(module_entry)

            course_entry = {
                "id": course_id,
                "name": course.get("name", "Curso"),
                "slug": course.get("slug", str(course_id)),
                "title": course.get("name", "Curso"),
                "modules": modules,
                "contract_id": contract_id,
            }

            content[str(course_id)] = course_entry

        return content

    def _fetch_disciplines(self, course_id: str, content_type: str) -> List[Dict[str, Any]]:
        """Fetches the list of disciplines (modules) for a course."""
        url = LIST_DISCIPLINES_URL.format(course_id=course_id, content_type=content_type)
        response = self._session.get(url, params={"audiobooks": "true"})
        response.raise_for_status()
        return response.json()

    def _fetch_content_categories(self, course_id: str, discipline_id: str, content_type: str) -> List[Dict[str, Any]]:
        """Fetches the content categories for a discipline."""
        url = LIST_CONTENT_URL.format(course_id=course_id, discipline_id=discipline_id, content_type=content_type)
        response = self._session.get(url, params={"audiobooks": "true"})
        response.raise_for_status()
        return response.json()

    def _fetch_lessons(self, course_id: str, discipline_id: str, content_id: str, content_type: str) -> List[Dict[str, Any]]:
        """Fetches the lessons for a content category."""
        url = LIST_LESSONS_URL.format(
            course_id=course_id,
            discipline_id=discipline_id,
            content_id=content_id,
            content_type=content_type
        )
        response = self._session.get(url, params={"audiobooks": "true"})
        response.raise_for_status()
        data = response.json()

        lessons = []
        for lesson_order, lesson in enumerate(data, start=1):
            duration_str = lesson.get("st_tempo_duracao", "00:00:00")
            duration = self._parse_duration(duration_str)

            lessons.append({
                "id": lesson.get("id_video"),
                "title": lesson.get("st_titulo_novo", f"Aula {lesson_order}"),
                "order": lesson_order,
                "locked": False,
                "professor": lesson.get("professor"),
                "duration": duration,
                "duration_str": duration_str,
                "codigo": lesson.get("codigo"),
                "materia": lesson.get("Materia"),
                "st_nome_arquivo": lesson.get("st_nome_arquivo"),
                "fk_apostila": lesson.get("fk_apostila"),
                "fk_material_resumo": lesson.get("fk_material_resumo"),
            })

        return lessons

    def _parse_duration(self, duration_str: str) -> int:
        """Parses duration string (HH:MM:SS) to seconds."""
        try:
            parts = duration_str.split(":")
            if len(parts) == 3:
                h, m, s = map(int, parts)
                return h * 3600 + m * 60 + s
            elif len(parts) == 2:
                m, s = map(int, parts)
                return m * 60 + s
        except (ValueError, AttributeError):
            pass
        return 0

    def _build_signed_url(self, video_url: str, file_path: str) -> Optional[str]:
        """Builds a signed URL for a file using the video URL's signature."""
        if not video_url or not file_path:
            return None

        try:
            # Extract base URL and query params from video URL
            if "?" in video_url:
                base_part, query_part = video_url.rsplit("?", 1)
            else:
                return None

            # Get the CDN base (e.g., https://videoaulas.infra.grancursosonline.com.br)
            # The file_path already contains the full path after the CDN base
            cdn_base = "/".join(base_part.split("/")[:3])

            return f"{cdn_base}/{file_path}?{query_part}"
        except Exception as exc:
            logger.debug("Falha ao construir URL assinada: %s", exc)
            return None

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content = LessonContent()
        video_id = lesson.get("id")
        lesson_title = lesson.get("title", "Aula")

        if not video_id:
            logger.warning("Aula sem ID de vídeo: %s", lesson_title)
            return content

        contract_id = self._get_contract_id()
        course_id_encoded = quote(course_id, safe="")
        video_id_encoded = quote(video_id, safe="")
        contract_id_encoded = quote(contract_id, safe="")

        # Fetch video info
        video_url = VIDEO_INFO_URL.format(
            course_id=course_id_encoded,
            video_id=video_id_encoded,
            contract_id=contract_id_encoded
        )

        try:
            response = self._session.get(video_url)
            response.raise_for_status()
            video_data = response.json()
        except Exception as exc:
            logger.error("Falha ao obter informações do vídeo %s: %s", video_id, exc)
            return content

        player = video_data.get("player", {})
        sources = player.get("sources", [])
        logger.debug("Vídeo %s: %d source(s) encontrada(s)", video_id, len(sources))

        stream_url = ""
        if sources:
            primary_source = sources[0]
            stream_url = primary_source.get("file", "")

            if stream_url:
                logger.debug("Vídeo %s URL: %s...", video_id, stream_url[:80] if len(stream_url) > 80 else stream_url)
                content.videos.append(
                    Video(
                        video_id=str(video_id),
                        url=stream_url,
                        order=1,
                        title=video_data.get("titulo") or lesson_title,
                        size=0,
                        duration=lesson.get("duration", 0),
                        extra_props={
                            "sources": sources,
                            "codigo": video_data.get("codigo"),
                            "thumbnail": player.get("thumbnail"),
                        }
                    )
                )
            else:
                logger.warning("Vídeo %s não possui URL de stream válida", video_id)

        # Fetch materials (slide, degravacao, legenda, audio, etc.)
        materials_url = MATERIALS_URL.format(
            course_id=course_id_encoded,
            video_id=video_id_encoded
        )

        try:
            response = self._session.get(materials_url)
            response.raise_for_status()
            materials_data = response.json()
            arquivos = materials_data.get("arquivos", {})
        except Exception as exc:
            logger.debug("Falha ao obter materiais da aula %s: %s", video_id, exc)
            arquivos = {}

        attachment_order = 0

        # Slide PDF - relative path, use main site URL with session cookies
        slide_path = arquivos.get("slide")
        if slide_path:
            attachment_order += 1
            content.attachments.append(
                Attachment(
                    attachment_id=f"slide_{video_id}",
                    url=f"{BASE_URL}/{slide_path}",
                    filename=f"Slide - {lesson_title}.pdf",
                    order=attachment_order,
                    extension="pdf",
                    size=0
                )
            )

        # Degravação (Transcription) PDF - relative path, use main site URL with session cookies
        degravacao_path = arquivos.get("degravacao")
        if degravacao_path:
            attachment_order += 1
            content.attachments.append(
                Attachment(
                    attachment_id=f"degravacao_{video_id}",
                    url=f"{BASE_URL}/{degravacao_path}",
                    filename=f"Degravação - {lesson_title}.pdf",
                    order=attachment_order,
                    extension="pdf",
                    size=0
                )
            )

        # Legenda (Subtitle) SRT - CDN path, needs signed URL from video
        legenda_path = arquivos.get("legenda")
        if legenda_path and stream_url:
            legenda_full_url = self._build_signed_url(stream_url, legenda_path)
            if legenda_full_url:
                attachment_order += 1
                content.attachments.append(
                    Attachment(
                        attachment_id=f"legenda_{video_id}",
                        url=legenda_full_url,
                        filename=f"Legenda - {lesson_title}.srt",
                        order=attachment_order,
                        extension="srt",
                        size=0
                    )
                )

        # Audio (MP3) - uses special download endpoint
        attachment_order += 1
        audio_download_url = AUDIO_URL.format(
            course_id=course_id_encoded,
            video_id=video_id_encoded
        )
        content.attachments.append(
            Attachment(
                attachment_id=f"audio_{video_id}",
                url=audio_download_url,
                filename=f"Áudio - {lesson_title}.mp3",
                order=attachment_order,
                extension="mp3",
                size=0
            )
        )

        # Additional materials with full URLs (resumo, flashcards, mindmap, etc.)
        url_materials = [
            ("resumo", "Resumo", "md"),
            ("transcricao", "Transcrição", "json"),
            ("flashcards", "Flashcards", "json"),
            ("mindmap", "Mapa Mental", "md"),
            ("questions", "Questões", "json"),
        ]

        for key, name, ext in url_materials:
            material_url = arquivos.get(key)
            if material_url and material_url.startswith("http"):
                attachment_order += 1
                content.attachments.append(
                    Attachment(
                        attachment_id=f"{key}_{video_id}",
                        url=material_url,
                        filename=f"{name} - {lesson_title}.{ext}",
                        order=attachment_order,
                        extension=ext,
                        size=0
                    )
                )

        return content

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        if not attachment.url:
            logger.error("Anexo sem URL disponível: %s", attachment.filename)
            return False

        try:
            response = self._session.get(attachment.url, stream=True)
            response.raise_for_status()
            with open(download_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    file_handle.write(chunk)
            return True
        except Exception as exc:
            logger.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Gran Cursos Online", GranCursosPlatform)
