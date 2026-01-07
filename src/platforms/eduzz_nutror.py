from __future__ import annotations

import logging
import json
import time
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from playwright.async_api import Page

from src.app.api_service import ApiService
from src.app.models import LessonContent, Description, Video, Attachment
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

LOGIN_URL = "https://app.nutror.com"
API_BASE_URL = "https://learner-api.nutror.com"
SEARCH_URL = f"{API_BASE_URL}/learner/course/search"
MODULES_URL = f"{API_BASE_URL}/learner/course/{{course_hash}}/lessons/v2"
LESSON_DETAILS_URL = f"{API_BASE_URL}/learner/lessons/{{lesson_hash}}"

# token dessa plataforma dura 12 minutos.

class NutrorTokenFetcher(PlaywrightTokenFetcher):
    """Automatiza o login na Eduzz/Nutror para capturar o token."""

    def __init__(self):
        self.captured_cookies: List[Dict[str, Any]] = []

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
        email_sel = "#email-login"
        await page.wait_for_selector(email_sel)
        await page.fill(email_sel, username)
        
        password_sel = "#password-login"
        await page.wait_for_selector(password_sel)
        await page.fill(password_sel, password)

    async def submit_login(self, page: Page) -> None:
        submit_btn = page.locator("#signin")
        await submit_btn.first.click()

    async def _capture_authorization_header(self, page: Page) -> Tuple[Optional[str], Optional[str]]:
        """
        Override to capture token from cookies if header capture fails or as a fallback.
        Eduzz stores the token in 'newAuthToken' cookie.
        """
        start_time = time.time()
        while time.time() - start_time < (self.network_idle_timeout_ms / 1000):
            cookies = await page.context.cookies()
            for cookie in cookies:
                if cookie['name'] == 'newAuthToken':
                    self.captured_cookies = cookies
                    return f"Bearer {cookie['value']}", page.url

            await asyncio.sleep(0.5)
            
        return None, None

class NutrorPlatform(BasePlatform):
    """Implementação da plataforma Nutror (Eduzz)."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = NutrorTokenFetcher()
        self.cookies: List[Dict[str, Any]] = []

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
ATENÇÃO: EM CONFIGURAÇÕES VOCÊ DEVE ATIVAR REAUTENTICAÇÃO AUTOMÁTICA, A SESSÃO DESSA PLATAFORMA DUA 12 MINUTOS!
Para autenticação manual (Token Direto, não recomendado, use credenciais se possível):
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
            token = self._token_fetcher.fetch_token(
                username,
                password,
                headless=not use_browser_emulation,
                wait_for_user_confirmation=(confirmation_event.wait if confirmation_event else None),
            )
            self.cookies = self._token_fetcher.captured_cookies
            return token
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

    def refresh_auth(self) -> None:
        """
        Refreshes the authentication session using the refresh token flow.
        """
        if not self._session:
            return super().refresh_auth()

        if self._attempt_api_refresh():
            return

        try:
            logger.info("Refreshing Eduzz session via re-authentication...")
            self.authenticate(self.credentials)
        except Exception as e:
            logger.error(f"Failed to refresh Eduzz session: {e}")
            raise

    def _attempt_api_refresh(self) -> bool:
        if not self.cookies or not self._session:
            return False
            
        refresh_token = next((c['value'] for c in self.cookies if c['name'] == 'refreshToken'), None)
        if not refresh_token:
            return False
            
        try:
            url = "https://learner-api.nutror.com/oauth/refresh?startWhen=requestFailed"

            cookie_dict = {c['name']: c['value'] for c in self.cookies}

            headers = {
                "RefreshToken": refresh_token,
                "Authorization": self._session.headers.get("Authorization", ""),
                "User-Agent": self._session.headers.get("User-Agent", ""),
                "Origin": "https://app.nutror.com",
                "Referer": "https://app.nutror.com/",
                "Accept": "application/json, text/plain, */*",
            }

            if "FrontVersion" in self._session.headers:
                headers["FrontVersion"] = self._session.headers["FrontVersion"]

            response = requests.post(url, headers=headers, cookies=cookie_dict)
            response.raise_for_status()

            data = response.json()
            new_token = data.get("data", {}).get("token")

            if new_token:
                logger.info("Eduzz session refreshed successfully via API.")
                self._configure_session(new_token)

                for cookie in response.cookies:
                    found = False
                    for c in self.cookies:
                        if c['name'] == cookie.name:
                            c['value'] = cookie.value
                            found = True
                            break
                    if not found:
                        self.cookies.append({'name': cookie.name, 'value': cookie.value})

                updated_token_cookie = False
                for c in self.cookies:
                    if c['name'] == 'newAuthToken':
                        c['value'] = new_token
                        updated_token_cookie = True
                        break
                if not updated_token_cookie:
                    self.cookies.append({'name': 'newAuthToken', 'value': new_token})

                return True
                
        except Exception as e:
            logger.warning(f"Eduzz API refresh failed: {e}")
            
        return False

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
                    expire_at = item.get("expire_at")
                    title = item.get("title")

                    if expire_at:
                        try:
                            # Ex: "2022-01-27T11:08:29.000Z"
                            expire_dt = datetime.fromisoformat(expire_at.replace("Z", "+00:00"))
                            if expire_dt < datetime.now(expire_dt.tzinfo):
                                logger.info(f"Pulnado curso expirado: {title} (Expirou em {expire_at})")
                                continue
                        except Exception as e:
                            logger.warning(f"Erro ao verificar expiração do curso {title}: {e}")

                    course_id = item.get("hash") or item.get("course_hash") or item.get("id")
                    producer = item.get("author", {}).get("name") or item.get("producer", {}).get("name") or "Desconhecido"
                    
                    courses.append({
                        "id": str(course_id),
                        "name": title,
                        "seller_name": producer,
                        "slug": str(course_id)
                    })

                page_info = data.get("page", {})
                if isinstance(page_info, dict):
                    total_pages = page_info.get("total_pages", 1)
                else:
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
                modules_raw = []
                page = 1
                
                while True:
                    response = self._session.get(url, params={"page": page, "size": 10})
                    response.raise_for_status()
                    data = response.json()
                    
                    current_page_modules = data.get("data", [])
                    if not current_page_modules:
                        break
                        
                    modules_raw.extend(current_page_modules)
                    
                    total_pages = data.get("total_pages")
                    if total_pages is None and "page" in data:
                        total_pages = data["page"].get("total_pages", 1)
                    if total_pages is None: total_pages = 1

                    if page >= total_pages:
                        break
                    
                    page += 1
                    time.sleep(0.5)

                processed_modules = []

                for i, mod in enumerate(modules_raw, start=1):
                    module_title = mod.get("title") or f"Módulo {i}"
                    lessons_raw = mod.get("lessons", [])
                    
                    processed_lessons = []
                    for j, lesson in enumerate(lessons_raw, start=1):
                        expired_at = lesson.get("expired_at")
                        lesson_title = lesson.get("title")

                        if expired_at:
                            try:
                                expire_dt = datetime.fromisoformat(expired_at.replace("Z", "+00:00"))
                                if expire_dt < datetime.now(expire_dt.tzinfo):
                                    logger.info(f"Pulando aula expirada: {lesson_title}")
                                    continue
                            except Exception:
                                pass
                        
                        processed_lessons.append({
                            "id": lesson.get("hash") or lesson.get("lesson_hash") or lesson.get("id"),
                            "title": lesson_title,
                            "order": j,
                        })

                    if processed_lessons:
                        processed_modules.append({
                            "id": str(mod.get("id", "")),
                            "title": module_title,
                            "lessons": processed_lessons,
                            "order": i
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

        response = self._session.get(url, params={"forceManual": "true"})
        response.raise_for_status()
        data = response.json()
        lesson_data = data.get("data", {})

        desc_text = lesson_data.get("description") or lesson_data.get("content")
        if desc_text:
            content.description = Description(
                text=desc_text,
                description_type="html"
            )

        contents = lesson_data.get("contents", [])
        video_idx = 1
        for item in contents:
            type_id = item.get("type", {}).get("id")
            video_url = None
            
            # Type 9: SafeVideo / Bunkr
            if type_id == 9:
                video_url = item.get("embed")
            else:
                logger.debug(f"Nutror: Conteúdo não tratado encontrado (Type {type_id}): {item}")
            
            # Type 4: Text (usually ignored or handled as description, but sometimes in contents)
            
            if video_url:
                content.videos.append(Video(
                    video_id=str(item.get("id")),
                    url=video_url,
                    title=lesson_data.get("title") or f"Vídeo {video_idx}",
                    order=item.get("sequence", video_idx),
                    size=0,
                    duration=0
                ))
                video_idx += 1

        files = lesson_data.get("lesson_files", [])
        for idx, file_item in enumerate(files, 1):
            file_path = file_item.get("file_name")
            file_url = f"{API_BASE_URL}{file_path}" if file_path and file_path.startswith("/") else file_url
            
            content.attachments.append(Attachment(
                attachment_id=str(file_item.get("id_lesson_file") or file_item.get("id")),
                url=file_url,
                filename=file_item.get("title") or f"Anexo {idx}",
                extension=file_item.get("extension", ""),
                order=idx,
                size=0
            ))

        return content

    def download_attachment(self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")
        
        url = attachment.url
        if not url: return False

        with self._session.get(url, stream=True) as r:
            r.raise_for_status()
            with open(download_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return True

PlatformFactory.register_platform("Eduzz/Nutror", NutrorPlatform)
