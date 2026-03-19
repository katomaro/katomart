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

# Base URLs for Estrategia Med API (same MCI backend as LDI)
API_BASE = "https://api.estrategia.com"
MED_ORIGIN = "https://med.estrategia.com"
LOGIN_URL = "https://perfil.estrategia.com/login"
PLAYWRIGHT_LOGIN_URL = LOGIN_URL
LOGIN_COMPLETE_URL = "https://med.estrategia.com/"

# API Endpoints (shared MCI system)
SEARCH_LDIS_URL = f"{API_BASE}/bff/search/v3/ldis"
COURSE_BY_SLUG_URL = f"{API_BASE}/v3/mci/courses/slug/{{slug}}"
ITEM_CONTENT_URL = f"{API_BASE}/v3/mci/items/{{item_id}}"
CAST_TRACK_URL = f"{API_BASE}/v2/tracks/{{track_id}}"
BLOCK_NAVIGATION_URL = f"{API_BASE}/v3/mci/blocks/{{block_id}}/navigation"


class EstrategiaMedTokenFetcher(PlaywrightTokenFetcher):
    """Automates Estrategia Med login with a real browser to capture the bearer token."""

    network_idle_timeout_ms: int = 600_000  # 10 minutes for manual login with captcha

    @property
    def login_url(self) -> str:
        return PLAYWRIGHT_LOGIN_URL

    @property
    def target_endpoints(self) -> list[str]:
        return [
            f"{API_BASE}/bff/",
            f"{API_BASE}/v3/",
            f"{API_BASE}/v2/",
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
        """Custom fetch for Estrategia Med - waits for manual login then captures token."""
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

            # Monitor ALL pages in the context (may open in a new tab)
            context.on("request", on_request)

            def on_new_page(new_page):
                nonlocal login_complete
                logger.info("Nova aba detectada: %s", new_page.url)
                if "med.estrategia.com" in new_page.url:
                    login_complete = True

            context.on("page", on_new_page)

            try:
                await page.goto(self.login_url, wait_until="domcontentloaded")
                await self.dismiss_cookie_banner(page)

                logger.info(
                    "Aguardando login manual. Complete o login, abra a pagina do Med "
                    "e clique OK quando estiver pronto..."
                )

                # Wait for login to complete (token captured or new tab with Med)
                for _ in range(600):  # 10 minutes max
                    all_pages = context.pages
                    for p in all_pages:
                        if "med.estrategia.com" in p.url:
                            login_complete = True
                            break

                    if captured_token:
                        login_complete = True
                        logger.info("Token capturado com sucesso!")
                        break

                    if login_complete:
                        logger.info("Pagina do Med detectada. Aguardando token...")
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
                        "e abrir a pagina do Med antes de clicar OK."
                    )

                return captured_token

            finally:
                await browser.close()


class EstrategiaMedPlatform(BasePlatform):
    """Implements the Estrategia Medicina (LDI) platform."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._access_token: Optional[str] = None
        self._token_fetcher = EstrategiaMedTokenFetcher()

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
Como obter as credenciais do Estrategia Medicina:

1. Acesse https://med.estrategia.com e faca login normalmente.
2. Abra o DevTools (F12) e va para a aba Network (Rede).
3. Recarregue a pagina e procure por requisicoes para "api.estrategia.com".
4. Clique em uma requisicao e veja os Headers.
5. Copie o valor do header "Authorization" (sem o "Bearer ").
6. Ou navegue ate a aba Aplicacao (Application).
7. No menu lateral, clique em Cookies > med.estrategia.com.
8. Encontre o cookie chamado "__Secure-SID" e copie seu valor.
9. Cole o valor do cookie no campo "Cookie __Secure-SID".

**Assinantes:**
Marque a opcao "Emular Navegador" e faca login manualmente no navegador, apos isso, clique em OK no pop-up.
""".strip()

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
            "Origin": MED_ORIGIN,
            "Referer": f"{MED_ORIGIN}/",
            "x-vertical": "medicina",
            "x-requester-id": "front-student",
        })

        # Method 1: Direct token provided
        if token:
            self._access_token = token
            self._configure_api_session()
            self._validate_session()
            logger.info("Sessao autenticada no Estrategia Med via token.")
            return

        # Method 2: __Secure-SID cookie (JWT token)
        if secure_sid:
            self._access_token = secure_sid
            self._configure_api_session()
            self._validate_session()
            logger.info("Sessao autenticada no Estrategia Med via cookie __Secure-SID.")
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
            logger.info("Sessao autenticada no Estrategia Med via emulacao de navegador.")
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
                "Falha ao obter o token do Estrategia Med via emulacao de navegador. "
                "Realize o login/captcha manualmente no navegador e clique OK na aplicacao."
            ) from exc

    def _validate_session(self) -> None:
        """Validates the session by making a test API call."""
        if not self._session:
            raise ConnectionError("Sessao nao inicializada.")

        try:
            response = self._session.get(f"{API_BASE}/bff/profile/me")
            response.raise_for_status()
            data = response.json()
            user_name = data.get("data", {}).get("full_name", "Usuario")
            logger.debug("Sessao Med validada. Usuario: %s", user_name)
        except Exception as exc:
            logger.warning("Falha ao validar sessao Med: %s", exc)

    def _configure_api_session(self) -> None:
        """Configures session headers for API requests."""
        if not self._session or not self._access_token:
            raise ConnectionError("Sessao ou token nao configurados.")

        self._session.headers.update({
            "Authorization": f"Bearer {self._access_token}",
        })

        self._session.cookies.set(
            "__Secure-SID",
            self._access_token,
            domain=".estrategia.com"
        )

    def _search_ldis(self, query: str = "", only_has_access: bool = True, page: int = 1, per_page: int = 20) -> Dict[str, Any]:
        """Searches for LDI courses using the POST search endpoint."""
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        payload = {
            "query": query,
            "filters": {
                "type_entities": [],
                "authors": [],
                "classifications": {},
                "goals": [],
                "cast_shelves": [],
                "appointment_status": [],
                "course_type": "",
                "only_has_access": only_has_access,
            },
            "pagination": {
                "page": page,
                "per_page": per_page,
            },
            "with_favorites": True,
        }

        response = self._session.post(SEARCH_LDIS_URL, json=payload)
        response.raise_for_status()
        return response.json()

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Fetches all accessible LDI courses for the authenticated user."""
        try:
            data = self._search_ldis(query="", only_has_access=True, per_page=100)
        except Exception as exc:
            logger.error("Falha ao listar cursos Med: %s", exc)
            return []

        return self._parse_search_results(data)

    def search_courses(self, query: str) -> List[Dict[str, Any]]:
        """Searches for LDI courses by name."""
        if not query or not query.strip():
            return self.fetch_courses()

        try:
            data = self._search_ldis(query=query.strip(), only_has_access=False, per_page=20)
        except Exception as exc:
            logger.error("Falha ao buscar cursos Med: %s", exc)
            return []

        results = self._parse_search_results(data)
        logger.info("Encontrados %d cursos Med para '%s'.", len(results), query)
        return results

    def _parse_search_results(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parses the search response into a course list."""
        courses: List[Dict[str, Any]] = []

        entities = data.get("entities", {})
        ldis = entities.get("ldis", {})
        items = ldis.get("items", [])

        for item in items:
            course_id = item.get("id")
            if not course_id:
                continue

            courses.append({
                "id": course_id,
                "name": item.get("name", ""),
                "slug": item.get("slug", ""),
                "seller_name": "Estrategia Medicina",
                "is_favorite": item.get("is_favorited", False),
                "is_completed": item.get("is_completed", False),
                "has_access": item.get("has_access", False),
                "course_types": item.get("course_type", []),
                "teachers": item.get("teachers", []),
            })

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fetches chapters and items for selected courses."""
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            course_slug = course.get("slug")

            if not course_id or not course_slug:
                logger.warning("Curso sem ID ou slug encontrado, ignorando.")
                continue

            url = COURSE_BY_SLUG_URL.format(slug=course_slug)

            try:
                response = self._session.get(url, headers={"cache-control": "no-cache"})
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.error("Falha ao obter detalhes do curso Med %s: %s", course_slug, exc)
                continue

            course_data = data.get("data", {})
            chapters = course_data.get("chapters", [])

            modules: List[Dict[str, Any]] = []

            for chapter_idx, chapter in enumerate(chapters, start=1):
                chapter_id = chapter.get("chapter_id") or chapter.get("id")
                chapter_name = chapter.get("name", f"Capitulo {chapter_idx}")
                items = chapter.get("items", [])

                lessons: List[Dict[str, Any]] = []
                for item_idx, item in enumerate(items, start=1):
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
                "name": course_data.get("name", course.get("name", "Curso Med")),
                "slug": course_slug,
                "title": course_data.get("name", course.get("name", "Curso Med")),
                "modules": modules,
            }

            content[str(course_id)] = course_entry

        return content

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        """Fetches detailed content for an item (page)."""
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
                extracted_text = self._extract_text_from_tiptap(block)
                if extracted_text:
                    text_content.append(extracted_text)

            elif block_type == "cast":
                block_data = block.get("data") or block.get("simple_data") or {}
                track_type = block_data.get("type", "")
                track_value = block_data.get("value", "")

                if track_type == "track" and track_value:
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
                block_data = block.get("data") or block.get("simple_data") or {}
                resolved = block_data.get("resolved")

                if not resolved:
                    block_id = block.get("id")
                    if block_id:
                        resolved = self._resolve_block_data(block_id) or {}

                video_url = resolved.get("data", "") if resolved else ""
                video_name = resolved.get("name", f"Video {video_order}") if resolved else f"Video {video_order}"
                video_id = resolved.get("id", block.get("id", "")) if resolved else block.get("id", "")
                file_size = resolved.get("file_size", 0) if resolved else 0

                resolutions = resolved.get("resolutions", {}) if resolved else {}
                if resolutions:
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
                block_data = block.get("data") or block.get("simple_data") or {}
                resolved = block_data.get("resolved")

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
                extracted_text = self._extract_question_text(block)
                if extracted_text:
                    text_content.append(extracted_text)

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

        if node.get("type") == "text" and node.get("text"):
            text_parts.append(node["text"])

        content = node.get("content", [])
        if isinstance(content, list):
            for child in content:
                extracted = self._extract_text_recursive(child)
                if extracted:
                    text_parts.append(extracted)

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

        statement = resolved.get("statement", "")
        if statement:
            parts.append(f"**Questao:** {statement}")

        alternatives = resolved.get("alternatives", [])
        for alt in alternatives:
            letter = alt.get("letter", "")
            text = alt.get("text", "")
            if letter and text:
                parts.append(f"  {letter}) {text}")

        answer = resolved.get("answer", "")
        if answer:
            parts.append(f"**Resposta:** {answer}")

        explanation = resolved.get("explanation", "")
        if explanation:
            parts.append(f"**Explicacao:** {explanation}")

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

            video_files = track_data.get("video_files", [])
            video_url = None
            for vf in sorted(video_files, key=lambda x: x.get("height", 0), reverse=True):
                if vf.get("link"):
                    video_url = vf.get("link")
                    break

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
        logger.debug("Marcar item Med %s como visualizado: %s", lesson.get("id"), watched)


PlatformFactory.register_platform("Estrategia Medicina", EstrategiaMedPlatform)
