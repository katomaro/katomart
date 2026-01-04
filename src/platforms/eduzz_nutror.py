from __future__ import annotations

import logging
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import requests
from playwright.async_api import Page

from src.app.api_service import ApiService
from src.app.models import LessonContent, Description, Video, Attachment
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

LOGIN_URL = "https://accounts.eduzz.com/login?continue=https://app.nutror.com/"
API_BASE_URL = "https://learner-api.nutror.com"
SEARCH_URL = f"{API_BASE_URL}/learner/course/search"
MODULES_URL = f"{API_BASE_URL}/learner/course/{{course_hash}}/modules/v2"
LESSON_DETAILS_URL = f"{API_BASE_URL}/learner/lessons/{{lesson_hash}}/v2"

# token dessa plataforma dura 12 minutos.

class NutrorTokenFetcher(PlaywrightTokenFetcher):
    """Automatiza o login na Eduzz/Nutror para capturar o token."""

    @property
    def login_url(self) -> str:
        return LOGIN_URL

    @property
    def target_endpoints(self) -> list[str]:
        return [
            "https://learner-api.nutror.com/user",
            "https://learner-api.nutror.com/learner/course/search",
            "https://learner-api.nutror.com/oauth/eduzzaccount/validate"
        ]

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        email_sel = "input[type='email'], input[name='email']"
        await page.wait_for_selector(email_sel)
        await page.fill(email_sel, username)
        
        password_sel = "input[type='password'], input[name='password']"
        if not await page.is_visible(password_sel):
            next_btn = page.locator("button:has-text('Continuar'), button:has-text('Próximo')")
            if await next_btn.count() > 0 and await next_btn.first.is_visible():
                await next_btn.first.click()
                await page.wait_for_selector(password_sel)

        await page.fill(password_sel, password)

    async def submit_login(self, page: Page) -> None:
        submit_btn = page.locator("button[type='submit'], button:has-text('Entrar'), button:has-text('Acessar')")
        if await submit_btn.count() > 0:
            await submit_btn.first.click()
        else:
            await page.press("body", "Enter")

class NutrorPlatform(BasePlatform):
    """Implementação da plataforma Nutror (Eduzz)."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = NutrorTokenFetcher()

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Para autenticação manual (Token Direto):
1. Acesse https://app.nutror.com, faça login e aperte F12.
2. Vá na aba 'Rede' (Network) e filtre por 'search'.
3. Recarregue a página (F5).
4. Clique na requisição 'search?page=1...'.
5. Na aba 'Cabeçalhos' (Headers), procure por 'Authorization'.
6. Copie TODO o código que vem depois da palavra 'Bearer '.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        use_browser_emulation = bool(credentials.get("browser_emulation"))
        confirmation_event = credentials.get("manual_auth_confirmation")
        
        try:
            return self._token_fetcher.fetch_token(
                username,
                password,
                headless=not use_browser_emulation,
                wait_for_user_confirmation=(confirmation_event.wait if confirmation_event else None),
            )
        except Exception as exc:
            raise ConnectionError("Falha ao autenticar na Nutror via navegador.") from exc

    def _configure_session(self, token: str) -> None:
        token = token.strip()

        self._session = requests.Session()

        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
            "Origin": "https://app.nutror.com",
            "Referer": "https://app.nutror.com/",
            "Accept": "application/json, text/plain, */*",
            "FrontVersion": "1458"
        })

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        courses = []
        page = 1
        page_size = 50

        while True:
            params = {
                "page": page,
                "size": page_size,
                "status": "seeAll",
                "showShelf": "true"
            }
            
            try:
                response = self._session.get(SEARCH_URL, params=params)
                
                if response.status_code == 401:
                    raise ConnectionError("Token Inválido (401). Certifique-se de copiar o Token 'Authorization' (Bearer) da aba Rede, e não o Cookie.")
                
                response.raise_for_status()
                data = response.json()
                
                items = data.get("data", [])
                if not items:
                    break

                for item in items:
                    course_id = item.get("course_hash") or item.get("id")
                    title = item.get("title")
                    producer = item.get("producer", {}).get("name", "Desconhecido")
                    
                    courses.append({
                        "id": str(course_id),
                        "name": title,
                        "seller_name": producer,
                        "slug": str(course_id)
                    })

                total_pages = data.get("total_pages", 1)
                if page >= total_pages:
                    break
                
                page += 1
                time.sleep(0.5)

            except Exception as e:
                logger.error(f"Erro ao listar cursos Nutror na página {page}: {e}")
                if "401" in str(e): raise
                break

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        all_content = {}

        for course in courses:
            course_id = course["id"]
            url = MODULES_URL.format(course_hash=course_id)
            
            try:
                response = self._session.get(url)
                response.raise_for_status()
                data = response.json()
                
                modules_raw = data.get("data", [])
                processed_modules = []

                for mod in modules_raw:
                    module_title = mod.get("title", f"Módulo {mod.get('position')}")
                    lessons_raw = mod.get("lessons", [])
                    
                    processed_lessons = []
                    for lesson in lessons_raw:
                        processed_lessons.append({
                            "id": lesson.get("lesson_hash") or lesson.get("id"),
                            "title": lesson.get("title"),
                            "order": lesson.get("position", 0),
                        })

                    if processed_lessons:
                        processed_modules.append({
                            "id": str(mod.get("id", "")),
                            "title": module_title,
                            "lessons": processed_lessons,
                            "order": mod.get("position", 0)
                        })

                course_content = course.copy()
                course_content["modules"] = processed_modules
                course_content["title"] = course.get("name")
                all_content[str(course_id)] = course_content

            except Exception as e:
                logger.error(f"Erro ao buscar módulos do curso {course_id}: {e}")

        return all_content

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        lesson_hash = lesson.get("id")
        url = LESSON_DETAILS_URL.format(lesson_hash=lesson_hash)

        content = LessonContent()

        try:
            response = self._session.get(url)
            response.raise_for_status()
            data = response.json()
            lesson_data = data.get("data", {})

            if lesson_data.get("content"):
                content.description = Description(
                    text=lesson_data.get("content"),
                    description_type="html"
                )

            media_list = lesson_data.get("media", [])
            if isinstance(media_list, dict):
                media_list = [media_list]

            for idx, media in enumerate(media_list, 1):
                video_url = media.get("url") or media.get("hls")

                if not video_url and media.get("type") == "youtube":
                    video_url = media.get("video_id")

                if video_url:
                    content.videos.append(Video(
                        video_id=str(media.get("id", idx)),
                        url=video_url,
                        title=media.get("title", f"Vídeo {idx}"),
                        order=idx
                    ))

            files = lesson_data.get("files", [])
            for idx, file_item in enumerate(files, 1):
                content.attachments.append(Attachment(
                    attachment_id=str(file_item.get("id")),
                    url=file_item.get("url"),
                    filename=file_item.get("title") or file_item.get("fileName", f"Anexo {idx}"),
                    extension=file_item.get("extension", ""),
                    order=idx
                ))

        except Exception as e:
            logger.error(f"Erro ao detalhar aula {lesson_hash}: {e}")

        return content

    def download_attachment(self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")
        
        url = attachment.url
        if not url: return False

        try:
            with self._session.get(url, stream=True) as r:
                r.raise_for_status()
                with open(download_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"Falha no download do anexo Nutror: {e}")
            return False

PlatformFactory.register_platform("Eduzz/Nutror", NutrorPlatform)
