from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import requests
from playwright.async_api import Page

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent, Video, Description
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

# Base URLs for LDI API
LDI_API_BASE = "https://api.estrategia.com"
CONCURSOS_ORIGIN = "https://concursos.estrategia.com"
LOGIN_URL = "https://perfil.estrategia.com/login"
PLAYWRIGHT_LOGIN_URL = LOGIN_URL
LOGIN_COMPLETE_URL = "https://concursos.estrategia.com/objetivos/"

# API Endpoints
SEARCH_GOALS_URL = f"{LDI_API_BASE}/bff/goals/shelves"
GOAL_LDI_CONTENTS_URL = f"{LDI_API_BASE}/bff/goals/{{goal_id}}/contents/ldi"
COURSE_BY_SLUG_URL = f"{LDI_API_BASE}/v3/mci/courses/slug/{{slug}}"
ITEM_CONTENT_URL = f"{LDI_API_BASE}/v3/mci/items/{{item_id}}"
CAST_TRACK_URL = f"{LDI_API_BASE}/v2/tracks/{{track_id}}"
BLOCK_NAVIGATION_URL = f"{LDI_API_BASE}/v3/mci/blocks/{{block_id}}/navigation"


class EstrategiaLdiTokenFetcher(PlaywrightTokenFetcher):
    """Automates Estrategia LDI login with a real browser to capture the bearer token."""

    network_idle_timeout_ms: int = 600_000  # 10 minutes for manual login with captcha

    @property
    def login_url(self) -> str:
        return PLAYWRIGHT_LOGIN_URL

    @property
    def target_endpoints(self) -> list[str]:
        return [
            f"{LDI_API_BASE}/bff/",
            f"{LDI_API_BASE}/v3/",
            f"{LDI_API_BASE}/v2/",
        ]

    async def dismiss_cookie_banner(self, page: Page) -> None:
        try:
            cookie_btn = page.locator('button:has-text("Aceitar")')
            if await cookie_btn.count() > 0:
                await cookie_btn.first.click()
        except Exception:
            pass

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        pass

    async def submit_login(self, page: Page) -> None:
        pass

    async def fetch_token_async(
        self,
        username: str,
        password: str,
        *,
        headless: bool = True,
        user_agent: Optional[str] = None,
        wait_for_user_confirmation: Optional[Callable[[], None]] = None,
    ) -> str:
        """Custom fetch for Estrategia LDI - waits for manual login then captures token.

        Note: LDI pages open in a NEW TAB, so we monitor all pages in the context.
        """
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            args = ["--disable-blink-features=AutomationControlled"]
            browser = await playwright.chromium.launch(headless=headless, args=args)
            ua_to_use = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            context = await browser.new_context(user_agent=ua_to_use)
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()

            captured_token: Optional[str] = None
            login_complete = False

            def on_request(request):
                nonlocal captured_token
                if captured_token:
                    return
                url = request.url
                if any(url.startswith(ep) for ep in self.target_endpoints):
                    auth = request.headers.get("authorization", "")
                    if auth.lower().startswith("bearer "):
                        captured_token = auth[7:].strip()
                        logger.info("Token captured from request to %s", url)

            # Monitor ALL pages in the context (LDI opens in a new tab)
            context.on("request", on_request)

            def on_new_page(new_page):
                nonlocal login_complete
                logger.info("Nova aba detectada: %s", new_page.url)
                # If new tab is LDI page, login is complete
                if LOGIN_COMPLETE_URL in new_page.url or "concursos.estrategia.com" in new_page.url:
                    login_complete = True

            context.on("page", on_new_page)

            try:
                await page.goto(self.login_url, wait_until="domcontentloaded")
                await self.dismiss_cookie_banner(page)

                logger.info(
                    "Aguardando login manual. Complete o login, abra a pagina de LDI "
                    "e clique OK quando estiver pronto..."
                )

                # Wait for login to complete (token captured or new tab with LDI)
                for _ in range(600):  # 10 minutes max
                    # Check all pages in context for the target URL
                    all_pages = context.pages
                    for p in all_pages:
                        if p.url.startswith(LOGIN_COMPLETE_URL) or "concursos.estrategia.com/objetivos" in p.url:
                            login_complete = True
                            break

                    if captured_token:
                        login_complete = True
                        logger.info("Token capturado com sucesso!")
                        break

                    if login_complete:
                        logger.info("Pagina de LDI detectada. Aguardando token...")
                        # Give a bit more time for API calls
                        await asyncio.sleep(2)
                        if captured_token:
                            break

                    await asyncio.sleep(1)

                # Wait for user confirmation (OK click)
                if wait_for_user_confirmation:
                    logger.info("Clique OK na aplicacao para continuar...")
                    try:
                        await asyncio.to_thread(wait_for_user_confirmation)
                    except Exception:
                        pass

                # After user clicks OK, give a moment for any pending requests
                if not captured_token:
                    logger.info("Aguardando requisicoes pendentes...")
                    await asyncio.sleep(3)

                if not captured_token:
                    raise ValueError(
                        "Nao foi possivel capturar o token. Certifique-se de fazer login "
                        "e abrir a pagina de LDI (nova aba) antes de clicar OK."
                    )

                return captured_token

            finally:
                await browser.close()


class EstrategiaLdiPlatform(BasePlatform):
    """Implements the Estrategia LDI (Livro Didatico Interativo) platform."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._access_token: Optional[str] = None
        self._current_goal_id: Optional[str] = None
        self._token_fetcher = EstrategiaLdiTokenFetcher()

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="secure_sid",
                label="Cookie __Secure-SID",
                field_type=AuthFieldType.PASSWORD,
                placeholder="Cole o valor do cookie '__Secure-SID' (JWT token)",
                required=False,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como obter as credenciais do Estratégia Concursos:

1. Acesse https://www.estrategiaconcursos.com.br e faça login normalmente. Depois, va para a pagina de LDI: https://concursos.estrategia.com/objetivos/
2. Abra o DevTools (F12) e vá para a aba Network (Rede).
3. Recarregue a página e procure por requisições para "api.estrategiaconcursos.com.br".
4. Clique em uma requisição e veja os Headers.
5. Copie o valor do header "Authorization" (sem o "Bearer ").
6. Navegue até a aba Aplicação (Application).
7. No menu lateral, clique em Cookies > www.estrategiaconcursos.com.br.
8. Encontre o cookie chamado "__Secure-SID" e copie seu valor.
9. Cole o valor do cookie no campo "Cookie __Secure-SID".

**Assinantes:**
Marque a opção "Emular Navegador" e faça login manualmente no navegador, após isso, clique em OK no pop-up.

**IMPORTANTE:** Este modulo requer que voce faca uma BUSCA para localizar
conteudos LDI. Os cursos nao sao carregados automaticamente.
""".strip()

    @classmethod
    def requires_search(cls) -> bool:
        """LDI platform requires search to find content."""
        return True

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        secure_sid = (credentials.get("secure_sid") or "").strip()
        token = (credentials.get("token") or "").strip()
        use_browser_emulation = bool(credentials.get("browser_emulation"))

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": CONCURSOS_ORIGIN,
            "Referer": f"{CONCURSOS_ORIGIN}/",
            "x-vertical": "concursos",
            "x-requester-id": "front-student",
        })

        # Method 1: Direct token provided
        if token:
            self._access_token = token
            self._configure_api_session()
            self._validate_session()
            logger.info("Sessao autenticada no Estrategia LDI via token.")
            return

        # Method 2: __Secure-SID cookie (JWT token)
        if secure_sid:
            self._access_token = secure_sid
            self._configure_api_session()
            self._validate_session()
            logger.info("Sessao autenticada no Estrategia LDI via cookie __Secure-SID.")
            return

        # Method 3: Browser emulation for login with captcha (requires membership)
        if self._settings.has_full_permissions and use_browser_emulation:
            obtained_token = self._exchange_credentials_for_token(
                credentials.get("username", ""),
                credentials.get("password", ""),
                credentials,
            )
            self._access_token = obtained_token
            self._configure_api_session()
            self._validate_session()
            logger.info("Sessao autenticada no Estrategia LDI via emulacao de navegador.")
            return

        raise ValueError(
            "Informe um Token de Acesso, cookie __Secure-SID, ou utilize emulacao "
            "de navegador (assinantes) para autenticacao."
        )

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        """Exchanges credentials for an access token using browser emulation."""
        confirmation_event = credentials.get("manual_auth_confirmation")

        try:
            return self._token_fetcher.fetch_token(
                username,
                password,
                headless=False,
                user_agent=self._settings.user_agent,
                wait_for_user_confirmation=(
                    confirmation_event.wait if confirmation_event else None
                ),
            )
        except Exception as exc:
            raise ConnectionError(
                "Falha ao obter o token do Estrategia LDI via emulacao de navegador. "
                "Realize o login/captcha manualmente no navegador e clique OK na aplicacao."
            ) from exc

    def _validate_session(self) -> None:
        """Validates the session by making a test API call."""
        if not self._session:
            raise ConnectionError("Sessao nao inicializada.")

        try:
            # Test with a simple search to validate the token
            response = self._session.get(
                SEARCH_GOALS_URL,
                params={"page": 1, "per_page": 1, "name": "test"},
            )
            response.raise_for_status()
            logger.debug("Sessao LDI validada com sucesso.")
        except Exception as exc:
            logger.warning("Falha ao validar sessao LDI: %s", exc)

    def _configure_api_session(self) -> None:
        """Configures session headers for API requests."""
        if not self._session or not self._access_token:
            raise ConnectionError("Sessao ou token nao configurados.")

        self._session.headers.update({
            "Authorization": f"Bearer {self._access_token}",
        })

        # Also set cookie for some endpoints that might need it
        self._session.cookies.set(
            "__Secure-SID",
            self._access_token,
            domain=".estrategia.com"
        )

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """
        For LDI, returns an empty list since search is required.
        Users must use search_courses to find LDI content.
        """
        logger.info("LDI requer busca para localizar conteudos. Use a funcao de pesquisa.")
        return []

    def search_courses(self, query: str) -> List[Dict[str, Any]]:
        """Searches for LDI courses by name using the goals/shelves endpoint."""
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        if not query or not query.strip():
            logger.warning("LDI requer uma consulta de pesquisa.")
            return []

        encoded_query = quote(query.strip())
        url = f"{SEARCH_GOALS_URL}?page=1&per_page=20&name={encoded_query}"

        try:
            response = self._session.get(url)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.error("Falha ao buscar conteudos LDI: %s", exc)
            return []

        courses: List[Dict[str, Any]] = []
        result_data = data.get("data", {})

        # Process shelves (grouped by institution/category)
        shelves = result_data.get("shelves", {})
        for shelf_name, goals in shelves.items():
            for goal in goals:
                goal_id = goal.get("id")
                goal_name = goal.get("name", "")

                if not goal_id:
                    continue

                # Fetch LDI courses for this goal
                ldi_courses = self._fetch_ldi_courses_for_goal(goal_id)

                for ldi_course in ldi_courses:
                    courses.append({
                        "id": ldi_course.get("id"),
                        "name": ldi_course.get("title", ""),
                        "slug": ldi_course.get("slug", ""),
                        "seller_name": f"{shelf_name} - {goal_name}",
                        "goal_id": goal_id,
                        "is_favorite": ldi_course.get("is_favorite", False),
                        "is_completed": ldi_course.get("is_completed", False),
                        "course_types": ldi_course.get("course_types", []),
                    })

        # Also check highlights section
        highlights = result_data.get("highlights", {})
        highlight_goals = highlights.get("goals", [])
        for goal in highlight_goals:
            goal_id = goal.get("id")
            goal_name = goal.get("name", "")

            if not goal_id:
                continue

            ldi_courses = self._fetch_ldi_courses_for_goal(goal_id)
            for ldi_course in ldi_courses:
                # Avoid duplicates
                if any(c["id"] == ldi_course.get("id") for c in courses):
                    continue

                courses.append({
                    "id": ldi_course.get("id"),
                    "name": ldi_course.get("title", ""),
                    "slug": ldi_course.get("slug", ""),
                    "seller_name": f"Destaque - {goal_name}",
                    "goal_id": goal_id,
                    "is_favorite": ldi_course.get("is_favorite", False),
                    "is_completed": ldi_course.get("is_completed", False),
                    "course_types": ldi_course.get("course_types", []),
                })

        logger.info("Encontrados %d cursos LDI para '%s'.", len(courses), query)
        return courses

    def _fetch_ldi_courses_for_goal(self, goal_id: str) -> List[Dict[str, Any]]:
        """Fetches LDI courses for a specific goal."""
        if not self._session:
            return []

        url = GOAL_LDI_CONTENTS_URL.format(goal_id=goal_id)
        params = {"page": 1, "per_page": 50}

        try:
            response = self._session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("data", {}).get("contents", [])
        except Exception as exc:
            logger.warning("Falha ao obter LDI para goal %s: %s", goal_id, exc)
            return []

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fetches chapters and items for selected LDI courses."""
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            course_slug = course.get("slug")

            if not course_id or not course_slug:
                logger.warning("Curso sem ID ou slug encontrado, ignorando.")
                continue

            # Fetch course structure by slug
            url = COURSE_BY_SLUG_URL.format(slug=course_slug)

            try:
                response = self._session.get(url, headers={"cache-control": "no-cache"})
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.error("Falha ao obter detalhes do curso LDI %s: %s", course_slug, exc)
                continue

            course_data = data.get("data", {})
            chapters = course_data.get("chapters", [])

            # Each chapter is a module, each item within is a lesson (page)
            modules: List[Dict[str, Any]] = []

            for chapter_idx, chapter in enumerate(chapters, start=1):
                chapter_id = chapter.get("chapter_id") or chapter.get("id")
                chapter_name = chapter.get("name", f"Capitulo {chapter_idx}")
                items = chapter.get("items", [])

                lessons: List[Dict[str, Any]] = []
                for item_idx, item in enumerate(items, start=1):
                    # IMPORTANT: Use 'item_id' for API calls, not 'id'
                    # 'id' is the reference ID, 'item_id' is the actual content ID
                    actual_item_id = item.get("item_id") or item.get("id")
                    item_name = item.get("name") or item.get("title", f"Pagina {item_idx}")

                    if not actual_item_id:
                        continue

                    lessons.append({
                        "id": str(actual_item_id),
                        "title": item_name,
                        "order": item.get("position", item_idx),
                        "locked": False,
                        "chapter_id": chapter_id,
                        "path": item.get("path", ""),
                    })

                modules.append({
                    "id": str(chapter_id) if chapter_id else f"chapter_{chapter_idx}",
                    "title": chapter_name,
                    "order": chapter.get("order_index", chapter_idx - 1) + 1,
                    "lessons": lessons,
                    "locked": not chapter.get("free", True),
                })

            course_entry = {
                "id": str(course_id),
                "name": course_data.get("name", course.get("name", "Curso LDI")),
                "slug": course_slug,
                "title": course_data.get("name", course.get("name", "Curso LDI")),
                "modules": modules,
                "goal_id": course.get("goal_id"),
            }

            content[str(course_id)] = course_entry

        return content

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        """Fetches detailed content for an LDI item (page)."""
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        content = LessonContent()
        item_id = lesson.get("id")
        lesson_title = lesson.get("title", "Pagina")

        if not item_id:
            logger.warning("Item sem ID: %s", lesson_title)
            return content

        # Fetch item content with all sub_blocks (with pagination)
        url = ITEM_CONTENT_URL.format(item_id=item_id)
        all_sub_blocks: List[Dict[str, Any]] = []
        page = 1

        while True:
            params = {
                "page": page,
                "order": "asc",
                "per_page": 30,
                "view_mode": "complete",
                "should_load_metadata": "true",
                "video_only": "false",
                "text_only": "false",
                "question_only": "false",
                "cast_only": "false",
                "attachment_only": "false",
            }

            try:
                response = self._session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.error("Falha ao obter detalhes do item %s (pagina %d): %s", item_id, page, exc)
                break

            item_data = data.get("data", {})
            sub_blocks = item_data.get("sub_blocks", [])
            all_sub_blocks.extend(sub_blocks)

            # Check if there are more pages
            meta = data.get("meta", {})
            total_pages = meta.get("last_page", 1)
            if page >= total_pages:
                break
            page += 1

        sub_blocks = all_sub_blocks

        # Process sub_blocks
        text_content: List[str] = []
        video_order = 1
        attachment_order = 1

        for block in sub_blocks:
            block_type = block.get("type", "")

            if block_type == "tiptap":
                # Rich text block - extract text for description
                extracted_text = self._extract_text_from_tiptap(block)
                if extracted_text:
                    text_content.append(extracted_text)

            elif block_type == "cast":
                # Video/audio block using tracks API
                block_data = block.get("data") or block.get("simple_data") or {}
                track_type = block_data.get("type", "")
                track_value = block_data.get("value", "")

                if track_type == "track" and track_value:
                    # Fetch track details
                    track_info = self._fetch_track_info(track_value)
                    if track_info:
                        video_url = track_info.get("video_url")
                        audio_url = track_info.get("audio_url")
                        track_title = track_info.get("title", f"Video {video_order}")

                        if video_url:
                            content.videos.append(
                                Video(
                                    video_id=str(track_value),
                                    url=video_url,
                                    order=video_order,
                                    title=track_title,
                                    size=0,
                                    duration=track_info.get("duration", 0),
                                    extra_props={
                                        "track_id": track_value,
                                        "thumbnail": track_info.get("thumbnail"),
                                    }
                                )
                            )
                            video_order += 1

                        if audio_url:
                            content.attachments.append(
                                Attachment(
                                    attachment_id=f"audio_{track_value}",
                                    url=audio_url,
                                    filename=f"{attachment_order}. Audio - {track_title}.mp3",
                                    order=attachment_order,
                                    extension="mp3",
                                    size=0
                                )
                            )
                            attachment_order += 1

            elif block_type == "videoMyDocuments":
                # Video from My Documents - has direct URL in resolved.data
                block_data = block.get("data") or block.get("simple_data") or {}
                resolved = block_data.get("resolved")

                # If resolved data is missing, fetch via navigation endpoint
                if not resolved:
                    block_id = block.get("id")
                    if block_id:
                        resolved = self._resolve_block_data(block_id) or {}

                video_url = resolved.get("data", "") if resolved else ""
                video_name = resolved.get("name", f"Video {video_order}") if resolved else f"Video {video_order}"
                video_id = resolved.get("id", block.get("id", "")) if resolved else block.get("id", "")
                file_size = resolved.get("file_size", 0) if resolved else 0

                # Try to get higher resolution if available
                resolutions = resolved.get("resolutions", {}) if resolved else {}
                if resolutions:
                    # Prefer highest resolution
                    for res in ["1080p", "720p", "480p", "360p"]:
                        if res in resolutions:
                            video_url = resolutions[res]
                            break

                if video_url:
                    content.videos.append(
                        Video(
                            video_id=str(video_id),
                            url=video_url,
                            order=video_order,
                            title=video_name,
                            size=file_size,
                            duration=0,
                            extra_props={
                                "poster": block_data.get("posterImage"),
                            }
                        )
                    )
                    video_order += 1

            elif block_type == "pdfMyDocuments":
                # PDF from My Documents - has direct URL in resolved.data
                block_data = block.get("data") or block.get("simple_data") or {}
                resolved = block_data.get("resolved")

                # If resolved data is missing, fetch via navigation endpoint
                if not resolved:
                    block_id = block.get("id")
                    if block_id:
                        resolved = self._resolve_block_data(block_id) or {}

                pdf_url = resolved.get("data", "") if resolved else ""
                pdf_name = block_data.get("title") or (resolved.get("name", f"Slide {attachment_order}") if resolved else f"Slide {attachment_order}")
                pdf_id = resolved.get("id", block.get("id", "")) if resolved else block.get("id", "")
                file_size = resolved.get("file_size", 0) if resolved else 0

                if pdf_url:
                    content.attachments.append(
                        Attachment(
                            attachment_id=str(pdf_id),
                            url=pdf_url,
                            filename=f"{attachment_order}. {pdf_name}.pdf",
                            order=attachment_order,
                            extension="pdf",
                            size=file_size
                        )
                    )
                    attachment_order += 1

            elif block_type == "attachment":
                # Generic attachment block
                block_data = block.get("data") or {}
                att_url = block_data.get("url", "")
                att_name = block_data.get("name", f"Anexo {attachment_order}")

                if att_url:
                    ext = Path(att_url).suffix.lstrip(".") or "pdf"
                    content.attachments.append(
                        Attachment(
                            attachment_id=f"att_{block.get('id', attachment_order)}",
                            url=att_url,
                            filename=f"{attachment_order}. {att_name}.{ext}",
                            order=attachment_order,
                            extension=ext,
                            size=0
                        )
                    )
                    attachment_order += 1

            elif block_type == "question":
                # Question block - extract text content
                extracted_text = self._extract_question_text(block)
                if extracted_text:
                    text_content.append(extracted_text)

        # Create description from extracted text
        if text_content:
            full_text = f"# {lesson_title}\n\n" + "\n\n".join(text_content)
            content.description = Description(
                text=full_text,
                description_type="markdown",
            )

        return content

    def _extract_text_from_tiptap(self, block: Dict[str, Any]) -> str:
        """Extracts plain text from a tiptap sub_block."""
        sub_block_content = block.get("sub_block_content", "")

        if not sub_block_content or sub_block_content == "{}":
            return ""

        try:
            content_json = json.loads(sub_block_content)
            return self._extract_text_recursive(content_json)
        except json.JSONDecodeError:
            return ""

    def _extract_text_recursive(self, node: Any) -> str:
        """Recursively extracts text from tiptap JSON structure."""
        if not isinstance(node, dict):
            return ""

        text_parts: List[str] = []

        # Direct text content
        if node.get("type") == "text" and node.get("text"):
            text_parts.append(node["text"])

        # Process content array
        content = node.get("content", [])
        if isinstance(content, list):
            for child in content:
                extracted = self._extract_text_recursive(child)
                if extracted:
                    text_parts.append(extracted)

        # Handle specific node types
        node_type = node.get("type", "")
        if node_type in ("paragraph", "heading"):
            result = " ".join(text_parts)
            return result + "\n" if result else ""
        elif node_type == "table":
            return "\n".join(text_parts) + "\n"
        elif node_type == "bulletList" or node_type == "orderedList":
            return "\n".join(f"  - {part}" for part in text_parts if part.strip()) + "\n"
        elif node_type == "listItem":
            return " ".join(text_parts)

        return " ".join(text_parts)

    def _extract_question_text(self, block: Dict[str, Any]) -> str:
        """Extracts text from a question sub_block."""
        block_data = block.get("data") or block.get("simple_data") or {}
        resolved = block_data.get("resolved", {})

        parts: List[str] = []

        # Question statement
        statement = resolved.get("statement", "")
        if statement:
            parts.append(f"**Questão:** {statement}")

        # Alternatives
        alternatives = resolved.get("alternatives", [])
        for alt in alternatives:
            letter = alt.get("letter", "")
            text = alt.get("text", "")
            if letter and text:
                parts.append(f"  {letter}) {text}")

        # Answer/explanation
        answer = resolved.get("answer", "")
        if answer:
            parts.append(f"**Resposta:** {answer}")

        explanation = resolved.get("explanation", "")
        if explanation:
            parts.append(f"**Explicação:** {explanation}")

        return "\n".join(parts) if parts else ""

    def _resolve_block_data(self, block_id: str) -> Optional[Dict[str, Any]]:
        """Fetches resolved data for a block via navigation endpoint."""
        if not self._session or not block_id:
            return None

        url = BLOCK_NAVIGATION_URL.format(block_id=block_id)

        try:
            response = self._session.get(url)
            response.raise_for_status()
            data = response.json()

            block_data = data.get("data", {}).get("data", {})
            return block_data.get("resolved")
        except Exception as exc:
            logger.warning("Falha ao resolver bloco %s: %s", block_id, exc)
            return None

    def _fetch_track_info(self, track_id: str) -> Optional[Dict[str, Any]]:
        """Fetches video/audio track information."""
        if not self._session:
            return None

        url = CAST_TRACK_URL.format(track_id=track_id)

        try:
            response = self._session.get(url)
            response.raise_for_status()
            data = response.json()

            track_data = data.get("data", {})

            # Extract video URL - prefer higher quality
            video_files = track_data.get("video_files", [])
            video_url = None
            for vf in sorted(video_files, key=lambda x: x.get("height", 0), reverse=True):
                if vf.get("link"):
                    video_url = vf.get("link")
                    break

            # Extract audio URL
            audio_url = track_data.get("audio_url") or track_data.get("audio")

            return {
                "video_url": video_url,
                "audio_url": audio_url,
                "title": track_data.get("name", track_data.get("title", "")),
                "duration": track_data.get("duration", 0),
                "thumbnail": track_data.get("thumbnail"),
            }
        except Exception as exc:
            logger.warning("Falha ao obter informacoes do track %s: %s", track_id, exc)
            return None

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        """Downloads an attachment from the platform."""
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        if not attachment.url:
            logger.error("Anexo sem URL disponivel: %s", attachment.filename)
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

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        """Marks a lesson as watched on the platform."""
        # LDI tracking would go here if needed
        logger.debug("Marcar item LDI %s como visualizado: %s", lesson.get("id"), watched)


PlatformFactory.register_platform("Estrategia Concursos VARIANTE LDI", EstrategiaLdiPlatform)
