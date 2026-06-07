from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Any, Dict, List, Optional

import requests
from playwright.async_api import Page

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory, sanitize_token
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

# Kiwify has two API hosts in production: the legacy ".com.br" used by older
# courses (version "v2", listing via /courses/{id}/sections) and ".com", used by
# the newer premium members area / clubs (version "v3", listing via
# /clubs/{clubId}/content). The course listing reports the version per course.
API_BASE = "https://admin-api.kiwify.com.br"
API_BASE_V3 = "https://admin-api.kiwify.com"

LOGIN_URL = f"{API_BASE}/v1/handleAuth/login"
KIWIFY_LOGIN_PAGE = "https://admin.kiwify.com.br/login"
PLAYWRIGHT_KIWIFY_LOGIN_URL= "https://dashboard.kiwify.com/login?redirect=%2F"
COURSES_URL = f"{API_BASE}/v1/viewer/schools/courses"
SCHOOL_COURSES_URL = f"{API_BASE}/v1/viewer/schools/{{school_id}}/courses"


def _course_detail_urls(course_id: str, version: Optional[str] = None) -> List[str]:
    """Returns the candidate course-detail URLs to try, ordered by likelihood
    for the given course ``version``. v3 (clubs) endpoints live on ``.com``; the
    legacy ``.com.br`` endpoints come first for everything else. Every candidate
    is included as a fallback so an unknown/missing version still resolves."""
    legacy = [
        f"{API_BASE}/v1/viewer/courses/{course_id}/sections",
        f"{API_BASE}/v1/viewer/courses/{course_id}",
        f"{API_BASE}/v1/viewer/clubs/{course_id}/content?caipirinha=true",
    ]
    clubs_v3 = [
        f"{API_BASE_V3}/v1/viewer/clubs/{course_id}/content?caipirinha=true",
        f"{API_BASE_V3}/v1/viewer/courses/{course_id}/sections",
        f"{API_BASE_V3}/v1/viewer/courses/{course_id}",
    ]
    if str(version) == "v3":
        return clubs_v3 + legacy
    return legacy + clubs_v3


def _lesson_details_url(api_host: str, course_id: str, lesson_id: str) -> str:
    return f"{api_host}/v1/viewer/courses/{course_id}/lesson/{lesson_id}"


def _files_url(api_host: str, course_id: str, file_id: str) -> str:
    return f"{api_host}/v1/viewer/courses/{course_id}/files/{file_id}"


class KiwifyTokenFetcher(PlaywrightTokenFetcher):
    """Automates Kiwify login with a real browser to capture the bearer token."""

    def __init__(self) -> None:
        # Since 2026-06 Kiwify gates the API behind a per-device 2FA token sent
        # as the ``kiwi-device-token`` header on every authenticated request.
        # It is harvested from the first captured viewer request (see
        # ``_on_request_captured``) and read back by the platform.
        self.device_token: Optional[str] = None

    @property
    def login_url(self) -> str:
        return PLAYWRIGHT_KIWIFY_LOGIN_URL

    def _on_request_captured(self, request) -> None:  # pragma: no cover - UI dependent
        device_token = request.headers.get("kiwi-device-token")
        if device_token:
            self.device_token = device_token

    @property
    def target_endpoints(self) -> list[str]:
        return [
            f"{API_BASE}/v1/viewer/",
            f"{API_BASE_V3}/v1/viewer/",
            COURSES_URL,
        ]

    async def dismiss_cookie_banner(self, page: Page) -> None:  # pragma: no cover - UI dependent
        cookies_button = page.get_by_role(
            "button", name=re.compile("aceitar|accept|ok", re.IGNORECASE)
        )
        try:
            if await cookies_button.count():
                await cookies_button.first.click()
        except Exception:
            return

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        email_selector = "input[type='email'], input[name='email'], input[name='username']"
        password_selector = "input[type='password'], input[name='password']"

        await page.wait_for_selector(email_selector)
        await page.fill(email_selector, username)

        await page.wait_for_selector(password_selector)
        await page.fill(password_selector, password)

    async def submit_login(self, page: Page) -> None:
        for selector in (
            "button[type='submit']",
            "button:has-text('Entrar')",
            "button:has-text('Login')",
        ):
            try:
                await page.click(selector, timeout=2000)
                return
            except Exception:
                continue
        await page.press("body", "Enter")


class KiwifyPlatform(BasePlatform):
    """Implements the Kiwify platform using the shared platform interface."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = KiwifyTokenFetcher()
        # Maps the course id reported by the listing (the clubId for v3) to the
        # API host whose endpoint actually served its content. Used so attachment
        # downloads hit the right host. Populated during fetch_course_content.
        self._course_api_host: Dict[str, str] = {}
        # Maps an attachment id to (api_host, real_course_id) so the FILES_URL
        # fallback works for v3, where the lesson's course differs from the club.
        self._attachment_meta: Dict[str, tuple[str, str]] = {}

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="kiwi_device_token",
                label="Device Token (kiwi-device-token)",
                field_type=AuthFieldType.TEXT,
                placeholder="Opcional: cole o valor do cabeçalho kiwi-device-token",
                required=False,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 9.90) ativos podem informar usuário/senha. O sistema irá trocar essas credenciais automaticamente pelo token da etapa acima (use a emulação de navegador, pois a Kiwify agora exige 2FA por código + reCAPTCHA no login), além de usar alguns algoritmos melhores e ter funcionalidades extras na aplicação, e obter suporte prioritário.

Para usuários gratuitos: Como obter o token da Kiwify?:
1) Acesse https://dashboard.kiwify.com em seu navegador e faça login normalmente.
2) Abra o DevTools (F12) e vá para a aba Rede (Network).
3) Recarregue a página e procure por requisições para "admin-api.kiwify.com".
4) Abra uma requisição autenticada e copie o valor do cabeçalho Authorization (Bearer ...) no campo de token.
5) Na MESMA requisição, copie o valor do cabeçalho "kiwi-device-token" e cole no campo Device Token (a Kiwify passou a exigir esse cabeçalho em todas as chamadas autenticadas).
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        device_token = self._resolve_device_token(credentials)
        self._configure_session(token, device_token)

    def _resolve_device_token(self, credentials: Dict[str, Any]) -> Optional[str]:
        """Prefer the token captured during browser login; fall back to a
        manually-supplied one. Returns None when unavailable (the session is
        still configured, just without the header)."""
        captured = getattr(self._token_fetcher, "device_token", None)
        if captured:
            return captured
        manual = sanitize_token((credentials.get("kiwi_device_token") or "").strip())
        return manual or None

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        use_browser_emulation = bool(credentials.get("browser_emulation"))
        confirmation_event = credentials.get("manual_auth_confirmation")

        if use_browser_emulation:
            try:
                return self._token_fetcher.fetch_token(
                    username,
                    password,
                    headless=False,
                    wait_for_user_confirmation=(confirmation_event.wait if confirmation_event else None),
                )
            except Exception as exc:
                raise ConnectionError(
                    "Falha ao obter o token da Kiwify via emulação de navegador. Revise as credenciais ou a interação de 2FA/Captcha."
                ) from exc

        try:
            response = requests.post(
                LOGIN_URL,
                json={"email": username, "password": password, "returnSecureToken": True},
                headers={"User-Agent": self._settings.user_agent},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            logger.debug("Kiwify authentication payload: %s", data)
            token = data.get("idToken")
            if not token:
                raise ValueError("Resposta de autenticação não retornou token.")
            return token
        except Exception as exc:  # pragma: no cover - network dependent
            raise ConnectionError("Falha ao autenticar na Kiwify. Verifique as credenciais.") from exc

    def _configure_session(self, token: str, device_token: Optional[str] = None) -> None:
        self._session = requests.Session()
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "Origin": "https://dashboard.kiwify.com",
            "Referer": "https://dashboard.kiwify.com/",
        }
        if device_token:
            # Required since 2026-06 on every authenticated admin-api request.
            headers["kiwi-device-token"] = device_token
        else:
            logger.warning(
                "Kiwify: sem kiwi-device-token; a API pode rejeitar as requisições. "
                "Use a emulação de navegador ou informe o Device Token manualmente."
            )
        self._session.headers.update(headers)

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
            logger.debug("Kiwify courses page %s payload (%s): %s", page_counter, url, data)

            if school_listing:
                items = data.get("my_courses", [])
                if not items:
                    break
                for course in items:
                    courses.append(
                        {
                            "id": course.get("id"),
                            "name": course.get("name", "Curso"),
                            "seller_name": course.get("producer", {}).get("name", "Produtor"),
                            "slug": str(course.get("id", "")),
                            "version": course.get("version"),
                        }
                    )
            else:
                items = data.get("courses", [])
                if not items:
                    break
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
                                "version": course_info.get("version"),
                            }
                        )

            total_courses = data.get("count", 0)
            page_size = data.get("page_size", 10)
            if page_size == 0 or page_counter * page_size >= total_courses:
                break
            page_counter += 1
            time.sleep(1)

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                logger.warning("Curso sem ID encontrado, ignorando: %r", course)
                continue

            logger.debug("Kiwify: buscando detalhes do curso %s", course_id)
            details, api_host = self._get_course_details(str(course_id), course.get("version"))
            self._course_api_host[str(course_id)] = api_host
            logger.debug("Kiwify course %s details payload (host=%s): %s", course_id, api_host, details)
            logger.debug("Kiwify: detalhes crus do curso %s (tipo=%s): %r", course_id, type(details), details)

            if not details:
                logger.warning("Nenhum detalhe encontrado para o curso %s", course_id)
                continue

            modules_data, all_lessons = self._extract_modules(details)
            logger.debug(
                "Kiwify: modules_data para curso %s (tipo=%s): %r",
                course_id,
                type(modules_data),
                modules_data,
            )

            processed_modules = self._process_modules(modules_data, all_lessons, api_host=api_host)

            course_entry = course.copy()
            course_entry["title"] = course.get("name", f"Curso {course_id}")
            course_entry["modules"] = processed_modules
            all_content[str(course_id)] = course_entry

        return all_content


    def _get_course_details(self, course_id: str, version: Optional[str] = None) -> tuple[Dict[str, Any], str]:
        """Returns ``(details, api_host)`` where ``api_host`` is the scheme+host
        of the endpoint that served the content (so lesson/attachment requests
        target the same host)."""
        for url in _course_detail_urls(course_id, version):
            api_host = self._api_host_of(url)
            try:
                logger.debug("Kiwify: chamando detalhes do curso em %s", url)
                response = self._session.get(url)
                response.raise_for_status()
                data = response.json()
                logger.debug("Kiwify: resposta bruta de %s (tipo=%s): %r", url, type(data), data)

                if isinstance(data, dict):
                    if "course" in data:
                        logger.debug("Kiwify: usando data['course'] para curso %s", course_id)
                        return data["course"], api_host
                    if "data" in data:
                        logger.debug("Kiwify: usando data['data'] para curso %s", course_id)
                        return data["data"], api_host
                    if "content" in data:
                        logger.debug("Kiwify: usando data['content'] para curso %s", course_id)
                        return data["content"], api_host
                    if "modules" in data:
                        logger.debug("Kiwify: usando data completo (já tem 'modules') para curso %s", course_id)
                        return data, api_host
            except Exception as exc:  # pragma: no cover - network dependent
                logger.debug("Kiwify course detail fetch failed for %s: %s", url, exc)
                continue

        logger.error("Falha ao obter detalhes do curso %s em todas as APIs conhecidas.", course_id)
        return {}, (API_BASE_V3 if str(version) == "v3" else API_BASE)

    @staticmethod
    def _api_host_of(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"


    def _extract_modules(self, course_details: Dict[str, Any]) -> tuple[Any, Dict[str, Any]]:
        # course_details aqui é o data["course"] (ou data["data"], etc.)
        all_lessons = course_details.get("all_lessons", {})  # <- mapa id -> aula (formato antigo)

        # v3 (clubs/content): {all_courses, all_modules, all_lessons} as id->obj
        # maps. A club may bundle several sub-courses; preserve their order via
        # all_courses[*].modules and all_modules[*].lessons (no explicit order
        # field exists). Each sub-course's id is what lesson/attachment requests
        # need, so carry it on the module as ``_course_id``.
        all_courses = course_details.get("all_courses")
        all_modules_map = course_details.get("all_modules")
        if isinstance(all_courses, dict) and all_courses and isinstance(all_modules_map, dict):
            flattened: List[Dict[str, Any]] = []
            multiple = len(all_courses) > 1
            for course in all_courses.values():
                if not isinstance(course, dict):
                    continue
                sub_course_id = course.get("id")
                sub_course_name = course.get("name") or ""
                for module_id in course.get("modules", []) or []:
                    module = all_modules_map.get(str(module_id))
                    if not isinstance(module, dict):
                        continue
                    module_copy = dict(module)
                    module_copy["_course_id"] = sub_course_id
                    if multiple and sub_course_name and sub_course_name != ".":
                        module_copy["_section_name"] = sub_course_name
                    flattened.append(module_copy)
            return flattened, all_lessons

        sections = course_details.get("sections")
        if isinstance(sections, list) and sections:
            flattened: List[Dict[str, Any]] = []
            for section in sections:
                if not isinstance(section, dict):
                    continue
                section_name = section.get("name") or ""
                section_order = section.get("order", 0)
                for module in section.get("modules", []) or []:
                    if not isinstance(module, dict):
                        continue
                    module_copy = dict(module)
                    if section_name and section_name != ".":
                        module_copy["_section_name"] = section_name
                        module_copy["_section_order"] = section_order
                    flattened.append(module_copy)
            return flattened, all_lessons

        if "modules" in course_details:
            modules = course_details["modules"]
        elif "all_modules" in course_details:
            modules = course_details["all_modules"]
        else:
            modules = []

        return modules, all_lessons


    def _process_modules(
        self,
        modules_data: Any,
        all_lessons: Dict[str, Any] | None = None,
        api_host: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        modules: List[Dict[str, Any]] = []

        logger.debug(
            "Kiwify: _process_modules recebeu modules_data (tipo=%s): %r",
            type(modules_data),
            modules_data,
        )

        if isinstance(modules_data, dict):
            iterable = modules_data.values()
        else:
            iterable = modules_data or []

        for module_index, module in enumerate(iterable, start=1):
            logger.debug(
                "Kiwify: processando módulo #%d (tipo=%s): %r",
                module_index,
                type(module),
                module,
            )

            lessons_raw = module.get("lessons", {}) if isinstance(module, dict) else {}
            logger.debug(
                "Kiwify: lessons_raw do módulo #%d (tipo=%s): %r",
                module_index,
                type(lessons_raw),
                lessons_raw,
            )

            if isinstance(lessons_raw, dict):
                lessons_iterable = lessons_raw.values()
            else:
                lessons_iterable = lessons_raw or []

            lessons: List[Dict[str, Any]] = []
            for lesson_index, lesson in enumerate(lessons_iterable, start=1):
                # se veio dict, usa direto; se veio id (str), tenta resolver em all_lessons
                if isinstance(lesson, dict):
                    lesson_obj = lesson
                else:
                    lesson_id_str = str(lesson)
                    lesson_obj = (all_lessons or {}).get(lesson_id_str, {"id": lesson_id_str})

                lesson_id = lesson_obj.get("id")
                lesson_title = self._extract_lesson_title(lesson_obj) or f"Aula {lesson_index}"
                lesson_order = lesson_obj.get("order", lesson_index)
                # The course that owns the lesson (sub-course for v3 clubs);
                # falls back to the module's course. Used to build the lesson
                # detail URL, since the listing id is the club, not the course.
                lesson_course_id = lesson_obj.get("course_id")
                if isinstance(module, dict):
                    lesson_course_id = lesson_course_id or module.get("_course_id") or module.get("course_id")

                lesson_entry = {
                    "id": lesson_id,
                    "title": lesson_title,
                    "order": lesson_order,
                    "locked": False,
                }
                if lesson_course_id:
                    lesson_entry["_course_id"] = lesson_course_id
                if api_host:
                    lesson_entry["_api_host"] = api_host
                lessons.append(lesson_entry)

            module_id = module.get("id") if isinstance(module, dict) else str(module_index)
            module_title_raw = module.get("name") if isinstance(module, dict) else None
            if not module_title_raw or module_title_raw == ".":
                module_title = f"Módulo {module_index}"
            else:
                module_title = module_title_raw
            section_name = module.get("_section_name") if isinstance(module, dict) else None
            if section_name:
                module_title = f"{section_name} - {module_title}"
            module_order = module.get("order", module_index) if isinstance(module, dict) else module_index

            logger.debug(
                "Kiwify: módulo #%d -> id=%r, title=%r, order=%r, total_aulas=%d",
                module_index,
                module_id,
                module_title,
                module_order,
                len(lessons),
            )

            modules.append(
                {
                    "id": module_id,
                    "title": module_title or f"Módulo {module_index}",
                    "order": module_order,
                    "lessons": lessons,
                    "locked": False,
                }
            )

        logger.debug("Kiwify: módulos processados: %r", modules)
        return modules


    def _extract_lesson_title(self, lesson: Any) -> str:
        logger.debug(
            "Kiwify: _extract_lesson_title recebeu (tipo=%s): %r",
            type(lesson),
            lesson,
        )

        if not isinstance(lesson, dict):
            result = str(lesson)
            logger.debug("Kiwify: lesson não é dict, usando str(lesson) como título: %r", result)
            return result

        for key in ("title", "name", "ref"):
            value = lesson.get(key)
            logger.debug("Kiwify: tentando campo '%s' em lesson -> %r", key, value)
            if value:
                result = str(value)
                logger.debug("Kiwify: usando '%s' como título da aula: %r", key, result)
                return result

        lesson_id = lesson.get("id")
        if lesson_id:
            result = str(lesson_id)
            logger.debug("Kiwify: caindo para 'id' como título da aula: %r", result)
            return result

        logger.debug("Kiwify: nenhum título encontrado, retornando string vazia.")
        return ""


    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        lesson_id = lesson.get("id")
        if not lesson_id:
            raise ValueError("ID da aula não encontrado.")

        # v3 lessons carry their real (sub-)course id and API host; fall back to
        # the listing course id / legacy host for v2.
        api_host = lesson.get("_api_host") or self._course_api_host.get(str(course_id)) or API_BASE
        real_course_id = lesson.get("_course_id") or course_id

        url = _lesson_details_url(api_host, real_course_id, lesson_id)
        response = self._session.get(url)
        response.raise_for_status()
        lesson_json = response.json().get("lesson", {})
        logger.debug("Kiwify lesson %s payload: %s", lesson_id, lesson_json)

        content = LessonContent()

        description = lesson_json.get("content")
        if description:
            content.description = Description(text=description, description_type="html")

        video_info = lesson_json.get("video") or {}
        stream_link = video_info.get("stream_link") or video_info.get("stream_link_full_url")
        download_link = video_info.get("download_link") or video_info.get("download_link_full_url")

        if isinstance(stream_link, str) and stream_link.startswith("/"):
            stream_link = f"https://d3pjuhbfoxhm7c.cloudfront.net{stream_link}"
        if isinstance(download_link, str) and download_link.startswith("/"):
            download_link = f"https://d3pjuhbfoxhm7c.cloudfront.net{download_link}"

        video_url = stream_link or download_link
        
        youtube_url = lesson_json.get("youtube_video")
        if youtube_url and not video_url:
            video_url = youtube_url
        if video_url:
            video_url = self._select_stream_by_quality(video_url, download_link)
            content.videos.append(
                Video(
                    video_id=str(video_info.get("id", lesson_id)),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=video_info.get("name", "Aula"),
                    size=video_info.get("size", 0),
                    duration=video_info.get("duration", 0),
                    extra_props={"referer": f"https://dashboard.kiwify.com/course/{course_id}/{lesson_id}"}
                )
            )

        for file_index, file_info in enumerate(lesson_json.get("files", []), start=1):
            filename = file_info.get("name") or f"file_{file_index}"
            attachment_id = str(file_info.get("id", file_index))
            extension = filename.split(".")[-1] if "." in filename else ""
            # Remember which host/course resolves this attachment so the
            # FILES_URL fallback in download_attachment targets the right API.
            self._attachment_meta[attachment_id] = (api_host, str(real_course_id))
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

    def _select_stream_by_quality(self, stream_url: str, download_url: str | None = None) -> str:
        """Resolve master playlists to a specific quality based on settings."""

        if not self._session or not stream_url or not stream_url.endswith(".m3u8"):
            return stream_url

        try:
            response = self._session.get(stream_url)
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - network dependent
            logger.debug("Kiwify: falha ao obter playlist master %s: %s", stream_url, exc)
            return stream_url

        lines = response.text.splitlines()
        variants: List[tuple[int | None, str]] = []

        for idx, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue

            height: int | None = None
            match = re.search(r"RESOLUTION=\d+x(\d+)", line)
            if match:
                try:
                    height = int(match.group(1))
                except ValueError:
                    height = None

            if idx + 1 < len(lines):
                uri = lines[idx + 1].strip()
                if uri and not uri.startswith("#"):
                    variants.append((height, urljoin(stream_url, uri)))

        if not variants:
            logger.debug("Kiwify: playlist master sem variações, usando URL original.")
            return download_url or stream_url

        quality_preference = self._settings.video_quality

        def extract_height(entry: tuple[int | None, str]) -> int:
            variant_height, variant_url = entry
            if variant_height:
                return variant_height
            derived = re.search(r"(\d{3,4})p", variant_url)
            return int(derived.group(1)) if derived else 0

        sorted_variants = sorted(variants, key=extract_height, reverse=True)

        if quality_preference == "Mais baixa":
            chosen = sorted_variants[-1]
        elif quality_preference == "Mais alta":
            chosen = sorted_variants[0]
        else:
            try:
                target_height = int(str(quality_preference).replace("p", ""))
            except (TypeError, ValueError):
                logger.warning(
                    "Kiwify: configuração de qualidade inválida '%s', usando a mais alta.",
                    quality_preference,
                )
                return sorted_variants[0][1]

            chosen = sorted_variants[-1]
            for variant in sorted_variants:
                if extract_height(variant) <= target_height:
                    chosen = variant
                    break

        logger.debug(
            "Kiwify: selecionada variante %sp para URL %s",
            extract_height(chosen),
            chosen[1],
        )
        return chosen[1]

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("The session has not been authenticated.")

        try:
            download_url = attachment.url

            if not download_url and attachment.attachment_id:
                logging.debug("Resolvendo URL do anexo %s via API.", attachment.filename)
                api_host, real_course_id = self._attachment_meta.get(
                    attachment.attachment_id,
                    (self._course_api_host.get(str(course_id), API_BASE), course_id),
                )
                api_url = _files_url(api_host, real_course_id, attachment.attachment_id)
                response = self._session.get(api_url, params={"forceDownload": "true"})
                response.raise_for_status()
                download_url = response.json().get("url", "")

            if not download_url:
                logging.error("Anexo sem URL disponível: %s", attachment.filename)
                return False

            if "storage.googleapis.com" in download_url:
                headers = {
                    "User-Agent": self._settings.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-User": "?1",
                }
                response = requests.get(download_url, stream=True, headers=headers)
            else:
                response = self._session.get(download_url, stream=True)

            response.raise_for_status()
            with open(download_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    file_handle.write(chunk)
            return True
        except Exception as exc:  # pragma: no cover - network dependent
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Kiwify", KiwifyPlatform)
