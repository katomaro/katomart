from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin
from typing import Any, Dict, List, Optional

import requests
from playwright.async_api import Page

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

LOGIN_URL = "https://admin-api.kiwify.com.br/v1/handleAuth/login"
KIWIFY_LOGIN_PAGE = "https://admin.kiwify.com.br/login"
PLAYWRIGHT_KIWIFY_LOGIN_URL= "https://dashboard.kiwify.com.br/login?redirect=%2F"
COURSES_URL = "https://admin-api.kiwify.com.br/v1/viewer/schools/courses"
SCHOOL_COURSES_URL = "https://admin-api.kiwify.com.br/v1/viewer/schools/{school_id}/courses"
COURSE_DETAILS_URLS = [
    "https://admin-api.kiwify.com/v1/viewer/clubs/{course_id}/content?caipirinha=true",
    "https://admin-api.kiwify.com.br/v1/viewer/courses/{course_id}",
]
LESSON_DETAILS_URL = "https://admin-api.kiwify.com/v1/viewer/courses/{course_id}/lesson/{lesson_id}"
FILES_URL = "https://admin-api.kiwify.com.br/v1/viewer/courses/{course_id}/files/{file_id}"


class KiwifyTokenFetcher(PlaywrightTokenFetcher):
    """Automates Kiwify login with a real browser to capture the bearer token."""

    @property
    def login_url(self) -> str:
        return PLAYWRIGHT_KIWIFY_LOGIN_URL

    @property
    def target_endpoints(self) -> list[str]:
        return [
            "https://admin-api.kiwify.com.br/v1/viewer/",
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
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

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

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": self._settings.user_agent,
                'User-Agent': 'Mozilla/5.0 ...',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-site',
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
            details = self._get_course_details(str(course_id))
            logger.debug("Kiwify course %s details payload: %s", course_id, details)
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

            processed_modules = self._process_modules(modules_data, all_lessons)

            course_entry = course.copy()
            course_entry["title"] = course.get("name", f"Curso {course_id}")
            course_entry["modules"] = processed_modules
            all_content[str(course_id)] = course_entry

        return all_content


    def _get_course_details(self, course_id: str) -> Dict[str, Any]:
        for url_template in COURSE_DETAILS_URLS:
            url = url_template.format(course_id=course_id)
            try:
                logger.debug("Kiwify: chamando detalhes do curso em %s", url)
                response = self._session.get(url)
                response.raise_for_status()
                data = response.json()
                logger.debug("Kiwify: resposta bruta de %s (tipo=%s): %r", url, type(data), data)

                if isinstance(data, dict):
                    if "course" in data:
                        logger.debug("Kiwify: usando data['course'] para curso %s", course_id)
                        return data["course"]
                    if "data" in data:
                        logger.debug("Kiwify: usando data['data'] para curso %s", course_id)
                        return data["data"]
                    if "content" in data:
                        logger.debug("Kiwify: usando data['content'] para curso %s", course_id)
                        return data["content"]
                    if "modules" in data:
                        logger.debug("Kiwify: usando data completo (já tem 'modules') para curso %s", course_id)
                        return data
            except Exception as exc:  # pragma: no cover - network dependent
                logger.debug("Kiwify course detail fetch failed for %s: %s", url, exc)
                continue

        logger.error("Falha ao obter detalhes do curso %s em todas as APIs conhecidas.", course_id)
        return {}


    def _extract_modules(self, course_details: Dict[str, Any]) -> tuple[Any, Dict[str, Any]]:
        # course_details aqui é o data["data"] da resposta
        all_lessons = course_details.get("all_lessons", {})  # <- mapa id -> aula
        if "modules" in course_details:
            modules = course_details["modules"]
        elif "all_modules" in course_details:
            modules = course_details["all_modules"]
        else:
            modules = []

        return modules, all_lessons


    def _process_modules(self, modules_data: Any, all_lessons: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
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

                lessons.append(
                    {
                        "id": lesson_id,
                        "title": lesson_title,
                        "order": lesson_order,
                        "locked": False,
                    }
                )

            module_id = module.get("id") if isinstance(module, dict) else str(module_index)
            module_title_raw = module.get("name") if isinstance(module, dict) else None
            if not module_title_raw or module_title_raw == ".":
                module_title = f"Módulo {module_index}"
            else:
                module_title = module_title_raw
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

        url = LESSON_DETAILS_URL.format(course_id=course_id, lesson_id=lesson_id)
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
                    extra_props={"referer": f"https://dashboard.kiwify.com.br/course/{course_id}/{lesson_id}"}
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
                api_url = FILES_URL.format(course_id=course_id, file_id=attachment.attachment_id)
                response = self._session.get(api_url, params={"forceDownload": "true"})
                response.raise_for_status()
                download_url = response.json().get("url", "")

            if not download_url:
                logging.error("Anexo sem URL disponível: %s", attachment.filename)
                return False

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
