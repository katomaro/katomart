from __future__ import annotations

import logging
import json
import re
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urljoin, urlparse

import requests
from playwright.async_api import Page

from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher
from src.app.models import LessonContent, Description, AuxiliaryURL, Video, Attachment
from src.config.settings_manager import SettingsManager
from src.app.api_service import ApiService

logger = logging.getLogger(__name__)

API_BASE = "https://learner-api.alpaclass.com"

class AlpaclassTokenFetcher(PlaywrightTokenFetcher):
    """
    Automates AlpaClass login (custom URL) to capture the bearer token.
    The login URL is dynamic based on user input.
    """

    def __init__(self):
        self._login_url: str = ""
        self._target_endpoints: List[str] = [
            f"{API_BASE}/learner/students/profile",
            f"{API_BASE}/learner/categories",
        ]

    def set_login_url(self, url: str) -> None:
        """Sets the login URL provided by the user."""
        self._login_url = url

    @property
    def login_url(self) -> str:
        if not self._login_url:
            raise ValueError("Login URL not set. Please provide the platform URL in credentials.")
        return self._login_url

    @property
    def target_endpoints(self) -> Sequence[str]:
        return self._target_endpoints

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        await page.wait_for_selector("input[type='email'], input[name='email'], input#email-input", state="visible")
        await page.fill("input[type='email'], input[name='email'], input#email-input", username)
        
        await page.wait_for_selector("input[type='password'], input[name='password'], input#password-input", state="visible")
        await page.fill("input[type='password'], input[name='password'], input#password-input", password)

    async def submit_login(self, page: Page) -> None:
        login_button = page.locator("button[type='submit'], button#submit-button, button:has-text('Entrar'), button:has-text('Acessar')")
        if await login_button.count() > 0:
            await login_button.first.click()
        else:
            await page.press("input[type='password']", "Enter")


class AlpaclassPlatform(BasePlatform):
    """
    Implements the AlpaClass platform scraping logic.
    Supports custom URLs (White Label).
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = AlpaclassTokenFetcher()
        self.origin_url: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="url",
                label="URL da Página de Login",
                placeholder="Ex: https://aluno.draanevaz.com.br/s/login",
                required=True,
            )
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Para AlpaClass, voce precisa informar a URL da area de membros (Ex: https://aluno.seucurso.com.br) 
junto com seu e-mail e senha. Ou então, o TOKEN Authorization.
Para obter o token:
1) Abra o seu navegador e vá para a página de Login
2) Abra as Ferramentas de Desenvolvedor (F12) → aba Rede (também pode ser chamada de Requisições ou Network).
3) Faça o login normalmente sem fechar essa aba e aguarde aparecer a lista de produtos da conta.
4) Use a lupa para procurar a URL "https://learner-api.alpaclass.com/learner/students/profile".
5) Clique nessa requisição que tenha o indicativo GET e vá para a aba Headers (Cabeçalhos), em requisição lá em baixo.
6) Copie o valor do cabeçalho 'Authorization' — ele se parece com 'Bearer <token>'. Cole apenas a parte do token aqui.
"""

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        """Authenticates on the AlpaClass platform."""
        self.credentials = credentials

        def _fetch_token_provider(username: str, password: str, creds: Dict[str, Any]) -> str:
            url = creds.get("url", "").strip()
            if not url:
                raise ValueError("URL da plataforma é obrigatória.")

            parsed = urlparse(url)
            self.origin_url = f"{parsed.scheme}://{parsed.netloc}"

            self._token_fetcher.set_login_url(url)
            use_browser = creds.get("browser_emulation", False)
            return self._token_fetcher.fetch_token(username, password, headless=not use_browser)

        token = self.resolve_access_token(credentials, _fetch_token_provider)

        if not self.origin_url:
            raw_url = credentials.get("url", "").strip()
            if raw_url:
                parsed = urlparse(raw_url)
                self.origin_url = f"{parsed.scheme}://{parsed.netloc}"
            if not self.origin_url:
                pass

        self._configure_session(token)

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "subdomain": "aluno",
        }
        if self.origin_url:
            headers["Origin"] = self.origin_url
            headers["Referer"] = self.origin_url + "/"

        self._session.headers.update(headers)

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Fetches available courses by iterating over categories."""
        if not self._session:
            raise ConnectionError("Session not authenticated.")

        try:
            resp_cats = self._session.get(f"{API_BASE}/learner/categories")
            resp_cats.raise_for_status()
            data = resp_cats.json()
            if isinstance(data, dict):
                 categories = data.get("categories", [])
            elif isinstance(data, list):
                 categories = data
            else:
                 categories = []
        except Exception as e:
            logger.error(f"Error fetching categories: {e}")
            return []

        all_courses = []
        seen_slugs = set()

        for cat in categories:
            cat_id = cat.get("id")
            if not cat_id:
                continue

            url = f"{API_BASE}/learner/categories/{cat_id}/courses?showUnavailableCourses=true"
            try:
                resp_courses = self._session.get(url)
                resp_courses.raise_for_status()
                courses_data = resp_courses.json()
                
                for c in courses_data:
                    slug = c.get("slug")
                    if slug and slug not in seen_slugs:
                        seen_slugs.add(slug)
                        all_courses.append({
                            "id": slug,
                            "name": c.get("name"),
                            "seller_name": c.get("author", {}).get("name") if c.get("author") else "AlpaClass",
                            "description": c.get("summary"),
                            "slug": slug,
                            "original_json": c
                        })
            except Exception as e:
                logger.warning(f"Error fetching courses for category {cat_id}: {e}")
                continue

        return all_courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fetches modules and lessons for selected courses."""
        results = {}
        for course in courses:
            slug = course.get("slug")
            if not slug: 
                continue
            
            try:
                resp = self._session.get(f"{API_BASE}/learner/courses/{slug}")
                resp.raise_for_status()
                data = resp.json()

                modules_out = []
                for mod in data.get("modules", []):
                    lessons_out = []
                    for less in mod.get("lessons", []):
                        lessons_out.append({
                            "id": less.get("slug"),
                            "name": less.get("title") or less.get("name"),
                            "title": less.get("title") or less.get("name"),
                            "slug": less.get("slug"),
                            "type": less.get("type", "video"),
                        })

                    if lessons_out:
                        modules_out.append({
                            "id": mod.get("slug"),
                            "name": mod.get("name"),
                            "title": mod.get("name"),
                            "lessons": lessons_out
                        })
                results[slug] = {
                    "id": slug,
                    "name": data.get("name"),
                    "title": data.get("name"),
                    "modules": modules_out
                }

            except Exception as e:
                logger.error(f"Error fetching content for course {slug}: {e}")

        return results

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        """Fetches detailed info for a lesson (videos, files)."""
        lesson_slug = lesson.get("slug")
        if not lesson_slug:
             raise ValueError("Lesson slug missing")
        url_details = f"{API_BASE}/learner/lessons/{lesson_slug}"
        resp_details = self._session.get(url_details)
        resp_details.raise_for_status()
        details_data = resp_details.json()

        content = LessonContent(
            description=Description(
                text=details_data.get("htmlContent", "") or "",
                description_type="html"
            )
        )

        lesson_content = details_data.get("content", {})
        if lesson_content and isinstance(lesson_content, dict):
            content_type = lesson_content.get("type", "")

            if content_type == "pandavideo":
                video_url = lesson_content.get("data")
                if video_url:
                    content.videos.append(Video(
                        video_id=lesson_slug,
                        url=video_url,
                        title=details_data.get("title") or details_data.get("name") or "Aula",
                        order=1,
                        size=0,
                        duration=0
                    ))
            elif content_type:
                logger.warning(f"Unsupported content type '{content_type}' for lesson {lesson_slug}. Skipping video extraction.")

        url_files = f"{API_BASE}/learner/lessons/{lesson_slug}/files"
        try:
            resp_files = self._session.get(url_files)
            if resp_files.status_code == 200:
                files_data = resp_files.json()
                if isinstance(files_data, list):
                    for f in files_data:
                        # Example: /lessons/download-file?token=...
                        file_url = f.get("url")
                        if file_url and file_url.startswith("/"):
                             if file_url.startswith("/lessons/") and not file_url.startswith("/learner/"):
                                 file_url = f"{API_BASE}/learner{file_url}"
                             else:
                                 file_url = f"{API_BASE}{file_url}"

                        file_name = f.get("name") or "arquivo"
                        file_id = f.get("uuid") or str(f.get("id", ""))
                        
                        if file_url:
                            content.attachments.append(Attachment(
                                attachment_id=file_id,
                                filename=file_name,
                                url=file_url,
                                order=0,
                                extension=file_name.split(".")[-1] if "." in file_name else "",
                                size=0
                            ))
        except Exception as e:
            logger.warning(f"Error fetching files for lesson {lesson_slug}: {e}")

        return content

    def download_attachment(self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        """Downloads a file."""
        if not self._session:
            return False

        try:
            with self._session.get(attachment.url, stream=True) as r:
                r.raise_for_status()
                with open(download_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"Download failed for {attachment.filename}: {e}")
            return False

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        """
        Marks a lesson as watched or unwatched on the platform.
        The API endpoint is a toggle, so we must check status first.
        """
        if not self._session:
            logger.error("Session not authenticated.")
            return

        lesson_slug = lesson.get("id") or lesson.get("slug")
        if not lesson_slug:
            logger.error(f"Could not find lesson slug for lesson: {lesson}")
            return

        url_details = f"{API_BASE}/learner/lessons/{lesson_slug}"
        try:
            resp = self._session.get(url_details)
            resp.raise_for_status()
            data = resp.json()
            is_completed = data.get("progress", {}).get("completed", False)
            
            if is_completed == watched:
                logger.debug(f"Lesson {lesson_slug} is already {'watched' if watched else 'unwatched'}, skipping toggle.")
                return

            url_watch = f"{API_BASE}/learner/lessons/{lesson_slug}/watch"
            resp_watch = self._session.post(url_watch)
            resp_watch.raise_for_status()

            logger.info(f"Lesson {lesson_slug} toggled to {'watched' if watched else 'unwatched'}")
        except Exception as e:
            logger.error(f"Error marking lesson {lesson_slug}: {e}")


PlatformFactory.register_platform('AlpaClass Domínio Customizado', AlpaclassPlatform)
