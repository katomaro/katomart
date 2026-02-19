from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from playwright.async_api import Page

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

BASE_URL = "https://aluno.cakto.com.br"
LOGIN_URL = f"{BASE_URL}/api/auth/sign-in/email"
LOGIN_PAGE_URL = f"{BASE_URL}/auth/login"
COURSES_URL = f"{BASE_URL}/api/user/courses"
COURSE_DETAILS_URL = f"{BASE_URL}/api/courses/{{course_id}}"
LESSON_FILES_URL = f"{BASE_URL}/api/lessons/{{lesson_id}}/files"
LESSON_FILE_DOWNLOAD_URL = f"{BASE_URL}/api/lessons/{{lesson_id}}/files/{{file_id}}/download"
LESSON_COMPLETION_URL = f"{BASE_URL}/api/lessons/{{lesson_id}}/completion"

SESSION_COOKIE_NAME = "__Secure-better-auth.session_token"


class CaktoTokenFetcher(PlaywrightTokenFetcher):
    """Automates Cakto login with a real browser to capture session cookies."""

    @property
    def login_url(self) -> str:
        return LOGIN_PAGE_URL

    @property
    def target_endpoints(self) -> list[str]:
        return [
            f"{BASE_URL}/api/user/courses",
            f"{BASE_URL}/api/auth/get-session",
        ]

    async def dismiss_cookie_banner(self, page: Page) -> None:
        cookies_button = page.get_by_role(
            "button", name=re.compile("aceitar|accept|ok|entendi", re.IGNORECASE)
        )
        try:
            if await cookies_button.count():
                await cookies_button.first.click()
        except Exception:
            return

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        email_selector = "input[type='email'], input[name='email']"
        password_selector = "input[type='password'], input[name='password']"

        await page.wait_for_selector(email_selector, timeout=15000)
        await page.fill(email_selector, username)

        await page.wait_for_selector(password_selector, timeout=15000)
        await page.fill(password_selector, password)

    async def submit_login(self, page: Page) -> None:
        for selector in (
            "button[type='submit']",
            "button:has-text('Entrar')",
            "button:has-text('Login')",
            "button:has-text('Acessar')",
        ):
            try:
                await page.click(selector, timeout=3000)
                return
            except Exception:
                continue
        await page.press("body", "Enter")


class CaktoPlatform(BasePlatform):
    """Implements the Cakto platform using the shared platform interface."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = CaktoTokenFetcher()
        self._session_token: Optional[str] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como obter o token da Cakto:
1) Acesse https://aluno.cakto.com.br em seu navegador e faça login normalmente.
2) Abra o DevTools (F12) e vá para a aba Application > Cookies.
3) Procure pelo cookie "__Secure-better-auth.session_token".
4) Copie o valor completo do cookie e cole no campo de token acima.

Assinantes ativos podem informar usuário/senha para login automático.
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
                result = self._token_fetcher.fetch_token(
                    username,
                    password,
                    headless=False,
                    wait_for_user_confirmation=(confirmation_event.wait if confirmation_event else None),
                )
                if isinstance(result, dict) and "cookies" in result:
                    for cookie in result["cookies"]:
                        if cookie.get("name") == SESSION_COOKIE_NAME:
                            return cookie.get("value", "")
                return result if isinstance(result, str) else ""
            except Exception as exc:
                raise ConnectionError(
                    "Falha ao obter o token da Cakto via emulacao de navegador. "
                    "Revise as credenciais ou passe pelo Cloudflare manualmente."
                ) from exc

        try:
            session = requests.Session()
            session.headers.update({
                "User-Agent": self._settings.user_agent,
                "Content-Type": "application/json",
                "Origin": BASE_URL,
                "Referer": LOGIN_PAGE_URL,
            })

            response = session.post(
                LOGIN_URL,
                json={"email": username, "password": password, "callbackURL": "/app"},
                timeout=30,
            )
            response.raise_for_status()

            session_token = session.cookies.get(SESSION_COOKIE_NAME)
            if not session_token:
                for cookie in response.cookies:
                    if cookie.name == SESSION_COOKIE_NAME:
                        session_token = cookie.value
                        break

            if not session_token:
                raise ValueError(
                    "Resposta de autenticacao nao retornou cookie de sessao. "
                    "Pode ser necessario usar emulacao de navegador devido ao Cloudflare."
                )
            return session_token
        except requests.exceptions.HTTPError as exc:
            raise ConnectionError(
                "Falha ao autenticar na Cakto. Verifique as credenciais ou use emulacao de navegador."
            ) from exc
        except Exception as exc:
            raise ConnectionError(
                "Falha ao autenticar na Cakto. O Cloudflare pode estar bloqueando. "
                "Tente usar emulacao de navegador."
            ) from exc

    def _configure_session(self, token: str) -> None:
        self._session_token = token
        self._session = requests.Session()
        self._session.cookies.set(SESSION_COOKIE_NAME, token, domain="aluno.cakto.com.br", path="/")
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/app/courses",
        })

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessao nao foi autenticada.")

        courses: List[Dict[str, Any]] = []
        page = 1

        while True:
            response = self._session.get(COURSES_URL, params={"page": page, "limit": 50})
            response.raise_for_status()
            data = response.json()
            logger.debug("Cakto courses page %s payload: %s", page, data)

            items = data.get("data", [])
            if not items:
                break

            for course in items:
                courses.append({
                    "id": course.get("courseId") or course.get("id"),
                    "name": course.get("title") or course.get("name", "Curso"),
                    "slug": course.get("courseId") or course.get("id"),
                    "seller_name": course.get("organizationSlug", ""),
                    "organization_slug": course.get("organizationSlug", ""),
                    "image": course.get("image", ""),
                })

            pagination = data.get("pagination", {})
            if not pagination.get("hasNextPage", False):
                break
            page += 1

        return sorted(courses, key=lambda c: c.get("name", ""))

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessao nao foi autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            org_slug = course.get("organization_slug", "")

            if not course_id:
                logger.warning("Curso sem ID encontrado, ignorando: %r", course)
                continue

            logger.debug("Cakto: buscando detalhes do curso %s", course_id)

            url = COURSE_DETAILS_URL.format(course_id=course_id)
            params = {"organizationSlug": org_slug} if org_slug else {}

            try:
                response = self._session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                logger.debug("Cakto course %s details payload: %s", course_id, data)
            except Exception as exc:
                logger.error("Falha ao obter detalhes do curso %s: %s", course_id, exc)
                continue

            course_data = data.get("course", data)
            modules_data = course_data.get("modules", [])

            processed_modules = []
            for module_index, module in enumerate(modules_data, start=1):
                lessons_data = module.get("lessons", [])
                processed_lessons = []

                for lesson_index, lesson in enumerate(lessons_data, start=1):
                    processed_lessons.append({
                        "id": lesson.get("id"),
                        "title": lesson.get("name", f"Aula {lesson_index}"),
                        "order": lesson.get("position", lesson_index),
                        "locked": not lesson.get("isAccessible", True),
                        "video_url": lesson.get("videoUrl", ""),
                        "description": lesson.get("description", ""),
                        "thumbnail": lesson.get("thumbnail", ""),
                        "duration": lesson.get("duration", ""),
                        "files": lesson.get("files", []),
                        "module_id": module.get("id"),
                    })

                processed_modules.append({
                    "id": module.get("id"),
                    "title": module.get("name", f"Modulo {module_index}"),
                    "order": module.get("position", module_index),
                    "lessons": processed_lessons,
                    "locked": False,
                })

            course_entry = course.copy()
            course_entry["title"] = course_data.get("name", course.get("name", f"Curso {course_id}"))
            course_entry["modules"] = processed_modules
            all_content[str(course_id)] = course_entry

        return all_content

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessao nao foi autenticada.")

        content = LessonContent()

        description_text = lesson.get("description", "")
        if description_text:
            content.description = Description(text=description_text, description_type="html")

        video_url = lesson.get("video_url", "")
        if video_url:
            video_id = self._extract_bunny_video_id(video_url)
            content.videos.append(
                Video(
                    video_id=video_id or lesson.get("id", "video"),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=self._parse_duration(lesson.get("duration", "")),
                    extra_props={
                        "referer": f"{BASE_URL}/app/{course_slug}/course/{course_id}",
                        "thumbnail": lesson.get("thumbnail", ""),
                    },
                )
            )

        lesson_id = lesson.get("id", "")
        files = lesson.get("files", [])
        for file_index, file_info in enumerate(files, start=1):
            filename = file_info.get("name", f"file_{file_index}")
            file_id = str(file_info.get("id", file_index))
            extension = filename.rsplit(".", 1)[-1] if "." in filename else ""

            # Store lesson_id:file_id in attachment_id for download
            composite_id = f"{lesson_id}:{file_id}"

            content.attachments.append(
                Attachment(
                    attachment_id=composite_id,
                    url=file_info.get("url", ""),
                    filename=filename,
                    order=file_index,
                    extension=extension,
                    size=file_info.get("size", 0),
                )
            )

        return content

    def _extract_bunny_video_id(self, video_url: str) -> str:
        """Extract video ID from Bunny Stream iframe URL."""
        match = re.search(r"mediadelivery\.net/embed/\d+/([a-f0-9-]+)", video_url)
        return match.group(1) if match else ""

    def _parse_duration(self, duration_str: str) -> int:
        """Parse duration string like '5 min' to seconds."""
        if not duration_str:
            return 0
        match = re.search(r"(\d+)", duration_str)
        if match:
            minutes = int(match.group(1))
            return minutes * 60
        return 0

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("A sessao nao foi autenticada.")

        try:
            # Parse composite attachment_id (lesson_id:file_id)
            if ":" in attachment.attachment_id:
                lesson_id, file_id = attachment.attachment_id.split(":", 1)
            else:
                lesson_id = module_id
                file_id = attachment.attachment_id

            # Try direct URL first if it contains signed S3 URL
            if attachment.url and "cakto-members-files" in attachment.url:
                download_url = attachment.url
            else:
                # Get signed URL via API
                api_url = LESSON_FILE_DOWNLOAD_URL.format(lesson_id=lesson_id, file_id=file_id)
                response = self._session.get(api_url, allow_redirects=False)

                if response.status_code == 302:
                    download_url = response.headers.get("Location", "")
                else:
                    response.raise_for_status()
                    download_url = attachment.url

            if not download_url:
                logger.error("Anexo sem URL disponivel: %s", attachment.filename)
                return False

            headers = {
                "User-Agent": self._settings.user_agent,
                "Referer": BASE_URL,
            }
            response = requests.get(download_url, stream=True, headers=headers, timeout=120)
            response.raise_for_status()

            with open(download_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    file_handle.write(chunk)
            return True

        except Exception as exc:
            logger.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool = True) -> None:
        """Mark a lesson as completed."""
        if not self._session:
            return

        lesson_id = lesson.get("id")
        if not lesson_id:
            return

        try:
            url = LESSON_COMPLETION_URL.format(lesson_id=lesson_id)
            if watched:
                self._session.post(url, json={"isCompleted": True})
            logger.debug("Cakto: aula %s marcada como assistida", lesson_id)
        except Exception as exc:
            logger.debug("Cakto: falha ao marcar aula %s como assistida: %s", lesson_id, exc)


PlatformFactory.register_platform("Cakto", CaktoPlatform)
