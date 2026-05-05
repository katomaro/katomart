from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

BASE_URL = "https://www.skool.com"
API_URL = "https://api2.skool.com"
LOGIN_URL = f"{API_URL}/auth/login"
GROUPS_URL = f"{API_URL}/self/groups"
FILE_DOWNLOAD_URL = f"{API_URL}/files/{{file_id}}/download-url"

# Skool migrated the session cookie name from `skooltok` (legacy) to `auth_token`.
# Both are still accepted at read time so older saved tokens keep working.
AUTH_COOKIE_NAME = "auth_token"
LEGACY_AUTH_COOKIE_NAME = "skooltok"
AUTH_COOKIE_CANDIDATES = (AUTH_COOKIE_NAME, LEGACY_AUTH_COOKIE_NAME)


class SkoolTokenFetcher(PlaywrightTokenFetcher):
    """Automates Skool login with a real browser to capture session cookies."""

    def __init__(self):
        self.captured_cookies: List[Dict[str, Any]] = []
        self.captured_auth_token: str = ""

    @property
    def login_url(self) -> str:
        return BASE_URL

    @property
    def target_endpoints(self) -> list[str]:
        return [
            f"{API_URL}/auth/login",
            f"{API_URL}/self/groups",
            f"{BASE_URL}/_next/data",
        ]

    async def dismiss_cookie_banner(self, page: Page) -> None:
        pass

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        # Navigate directly to login route so the modal is rendered immediately
        try:
            await page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded")
        except Exception:
            pass

        try:
            login_btn = page.locator("text=Log in")
            if await login_btn.count():
                await login_btn.first.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        email_sel = "input[type='email'], input[name='email'], input[id='email']"
        password_sel = "input[type='password'], input[name='password'], input[id='password']"

        await page.wait_for_selector(email_sel, timeout=15000)
        await page.fill(email_sel, username)

        await page.wait_for_selector(password_sel, timeout=15000)
        await page.fill(password_sel, password)

    async def submit_login(self, page: Page) -> None:
        for selector in (
            "button[type='submit']",
            "button:has-text('LOG IN')",
            "button:has-text('Log in')",
            "button:has-text('Sign in')",
        ):
            try:
                await page.click(selector, timeout=3000)
                return
            except Exception:
                continue
        await page.press("body", "Enter")

    async def fetch_token_async(
        self,
        username: str,
        password: str,
        *,
        headless: bool = True,
        user_agent: Optional[str] = None,
        wait_for_user_confirmation: Optional[Callable[[], None]] = None,
    ) -> str:
        """Skool uses cookie auth. Capture cookies (auth_token JWT) from the browser context."""

        manual_login = not (username and password)

        async with async_playwright() as playwright:
            ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            browser = await playwright.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(user_agent=ua)
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            page = await context.new_page()

            try:
                await page.goto(self.login_url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=self.network_idle_timeout_ms)
                except PlaywrightTimeoutError:
                    pass

                await self.dismiss_cookie_banner(page)

                if not manual_login:
                    await self.fill_credentials(page, username, password)
                    await self.submit_login(page)

                # Wait until auth_token cookie is set (login succeeded) or timeout
                auth_value = await self._wait_for_auth_cookie(context, timeout_ms=self.network_idle_timeout_ms)

                if wait_for_user_confirmation:
                    try:
                        await asyncio.to_thread(wait_for_user_confirmation)
                    except Exception:
                        pass
                    auth_value = await self._wait_for_auth_cookie(context, timeout_ms=5000) or auth_value

                cookies = await context.cookies()
                self.captured_cookies = [
                    {"name": c["name"], "value": c["value"], "domain": c.get("domain", ".skool.com")}
                    for c in cookies
                ]

                if not auth_value:
                    for c in self.captured_cookies:
                        if c["name"] in AUTH_COOKIE_CANDIDATES and c.get("value"):
                            auth_value = c["value"]
                            break

                if not auth_value:
                    raise ValueError("Não foi possível capturar o cookie de sessão do Skool durante o login.")

                self.captured_auth_token = auth_value
                return auth_value
            finally:
                await browser.close()

    async def _wait_for_auth_cookie(self, context, *, timeout_ms: int) -> str:
        """Poll the browser context for the session cookie (auth_token or legacy skooltok)."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            for c in await context.cookies():
                if c["name"] in AUTH_COOKIE_CANDIDATES and c.get("value"):
                    return c["value"]
            await asyncio.sleep(0.5)
        return ""


class SkoolPlatform(BasePlatform):
    """Implements the Skool platform using the shared platform interface."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = SkoolTokenFetcher()
        self._build_id: Optional[str] = None
        self._cookies: List[Dict[str, Any]] = []
        self._cached_groups: List[Dict[str, Any]] = []
        self._client_id: Optional[str] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como obter o token do Skool:
1) Acesse https://www.skool.com em seu navegador e faca login normalmente.
2) Abra o DevTools (F12) e va para a aba Application > Cookies.
3) Copie o valor do cookie "auth_token" (versao atual do Skool) e cole no campo de token acima.
   Tokens antigos no formato "skooltok" ainda sao aceitos.

Assinantes ativos podem informar usuario/senha para login automatico.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)
        self._fetch_build_id()

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
                self._cookies = self._token_fetcher.captured_cookies
                for cookie in self._cookies:
                    if cookie.get("name") in AUTH_COOKIE_CANDIDATES and cookie.get("value"):
                        return cookie.get("value", "")
                return result if isinstance(result, str) else ""
            except Exception as exc:
                raise ConnectionError(
                    "Falha ao obter o token do Skool via emulacao de navegador."
                ) from exc

        # Try direct API login
        try:
            session = requests.Session()
            session.headers.update({
                "User-Agent": self._settings.user_agent,
                "Content-Type": "application/json",
                "Origin": BASE_URL,
                "Referer": BASE_URL,
            })
            # Skool sends a generated client_id with every request; match that behavior.
            session.cookies.set("client_id", uuid.uuid4().hex, domain=".skool.com", path="/")

            response = session.post(
                LOGIN_URL,
                json={"email": username, "password": password},
                timeout=30,
            )
            response.raise_for_status()

            # Look for the session cookie in any of the supported names (current + legacy).
            session_token = ""
            for name in AUTH_COOKIE_CANDIDATES:
                value = session.cookies.get(name)
                if value:
                    session_token = value
                    break
            if not session_token:
                for cookie in response.cookies:
                    if cookie.name in AUTH_COOKIE_CANDIDATES and cookie.value:
                        session_token = cookie.value
                        break

            # Some endpoints respond with the JWT directly in the JSON payload.
            if not session_token:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                if isinstance(payload, dict):
                    session_token = (
                        payload.get("auth_token")
                        or payload.get("token")
                        or payload.get("skooltok")
                        or ""
                    )

            if not session_token:
                raise ValueError("Cookie de sessao nao encontrado na resposta.")

            self._cookies = [{"name": c.name, "value": c.value} for c in session.cookies]
            return session_token

        except requests.exceptions.HTTPError as exc:
            raise ConnectionError(
                "Falha ao autenticar no Skool. Verifique as credenciais."
            ) from exc
        except Exception as exc:
            raise ConnectionError(
                "Falha ao autenticar no Skool. Tente usar emulacao de navegador."
            ) from exc

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()
        # Set both cookie names so backends running either the new or the legacy
        # naming continue to work without requiring users to change their input.
        self._session.cookies.set(AUTH_COOKIE_NAME, token, domain=".skool.com", path="/")
        self._session.cookies.set(LEGACY_AUTH_COOKIE_NAME, token, domain=".skool.com", path="/")

        # Reuse client_id captured from the browser if present, otherwise generate one.
        for cookie in self._cookies:
            if cookie["name"] == "client_id" and cookie.get("value"):
                self._client_id = cookie["value"]
                break
        if not self._client_id:
            self._client_id = uuid.uuid4().hex
        self._session.cookies.set("client_id", self._client_id, domain=".skool.com", path="/")

        # Carry over any other cookies captured during browser-based login.
        for cookie in self._cookies:
            if cookie["name"] in AUTH_COOKIE_CANDIDATES or cookie["name"] == "client_id":
                continue
            self._session.cookies.set(
                cookie["name"], cookie["value"], domain=cookie.get("domain") or ".skool.com", path="/"
            )

        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        })

    def _fetch_build_id(self) -> None:
        """Fetch Next.js build ID from the main page and cache the embedded user groups."""
        try:
            response = self._session.get(BASE_URL)
            response.raise_for_status()

            match = re.search(r'"buildId"\s*:\s*"([^"]+)"', response.text)
            if match:
                self._build_id = match.group(1)
                logger.debug("Skool build ID: %s", self._build_id)
            else:
                match = re.search(r'/_next/data/([^/]+)/', response.text)
                if match:
                    self._build_id = match.group(1)
                    logger.debug("Skool build ID (alt): %s", self._build_id)

            # The home page also embeds the user's groups in __NEXT_DATA__.
            # Cache them so fetch_courses can avoid the api2 endpoint when it is
            # unavailable (e.g. when only the SSR cookie auth is valid).
            self._cached_groups = self._extract_groups_from_html(response.text)
            if self._cached_groups:
                logger.debug("Skool: cached %d groups from home page", len(self._cached_groups))
        except Exception as e:
            logger.warning("Could not fetch Skool build ID: %s", e)

    @staticmethod
    def _extract_groups_from_html(html: str) -> List[Dict[str, Any]]:
        match = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if not match:
            return []
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        page_props = (
            payload.get("props", {}).get("pageProps", {}) if isinstance(payload, dict) else {}
        )
        self_data = page_props.get("self") or {}
        groups = self_data.get("allGroups") or []
        return [g for g in groups if isinstance(g, dict)]

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Fetch all groups (communities) the user belongs to."""
        if not self._session:
            raise ConnectionError("A sessao nao foi autenticada.")

        groups: List[Dict[str, Any]] = []
        api_error: Optional[Exception] = None

        # Primary source: api2.skool.com/self/groups (legacy/current REST endpoint).
        try:
            response = self._session.get(GROUPS_URL, params={"limit": 100, "prefs": "false"})
            response.raise_for_status()
            data = response.json()
            logger.debug("Skool groups: %s", data)
            groups = data.get("groups", []) or []
        except Exception as e:
            api_error = e
            logger.warning("Falha ao listar grupos via /self/groups: %s", e)

        # Fallback: groups embedded in the home page __NEXT_DATA__ (always available
        # while the SSR session is valid).
        if not groups and self._cached_groups:
            logger.info("Skool: usando lista de grupos do __NEXT_DATA__ como fallback")
            groups = self._cached_groups

        if not groups:
            if api_error:
                raise api_error
            raise ConnectionError("Nao foi possivel obter a lista de grupos do Skool.")

        courses: List[Dict[str, Any]] = []
        for group in groups:
            group_id = group.get("id")
            group_name = group.get("name")
            metadata = group.get("metadata", {}) or {}

            display_name = (
                metadata.get("displayName")
                or metadata.get("title")
                or metadata.get("name")
                or group_name
            )

            courses.append({
                "id": group_id,
                "name": display_name,
                "slug": group_name,
                "seller_name": metadata.get("ownerName", ""),
                "description": metadata.get("description", ""),
                "_group": group,
            })

        return sorted(courses, key=lambda c: c.get("name", ""))

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fetch courses and modules for each group."""
        if not self._session:
            raise ConnectionError("A sessao nao foi autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            group_slug = course.get("slug")
            group_id = course.get("id")

            if not group_slug:
                logger.warning("Grupo sem slug encontrado, ignorando: %r", course)
                continue

            logger.debug("Skool: buscando cursos do grupo %s", group_slug)

            try:
                # Fetch classroom data
                classroom_data = self._fetch_classroom_data(group_slug)
                if not classroom_data:
                    continue

                all_courses = classroom_data.get("allCourses", [])
                processed_modules = []

                for course_idx, skool_course in enumerate(all_courses, start=1):
                    course_id = skool_course.get("id")
                    course_slug = skool_course.get("name")
                    course_metadata = skool_course.get("metadata", {})
                    course_title = course_metadata.get("title", f"Curso {course_idx}")

                    # Check access
                    if not course_metadata.get("hasAccess", 1):
                        logger.debug("Skool: curso %s sem acesso, ignorando", course_title)
                        continue

                    # Fetch course details with modules
                    course_details = self._fetch_course_details(group_slug, course_slug, course_id)
                    if not course_details:
                        continue

                    # Process children (sets/modules with lessons)
                    lessons = self._extract_lessons(course_details, group_slug, course_slug)

                    if lessons:
                        processed_modules.append({
                            "id": course_id,
                            "title": course_title,
                            "order": course_idx,
                            "lessons": lessons,
                            "locked": False,
                        })

                    time.sleep(0.5)  # Rate limiting

                course_entry = course.copy()
                course_entry["title"] = course.get("name")
                course_entry["modules"] = processed_modules
                all_content[str(group_id)] = course_entry

            except Exception as e:
                logger.error("Falha ao buscar conteudo do grupo %s: %s", group_slug, e)

        return all_content

    def _fetch_classroom_data(self, group_slug: str) -> Optional[Dict[str, Any]]:
        """Fetch classroom data (list of courses) for a group."""
        if not self._build_id:
            self._fetch_build_id()

        if self._build_id:
            url = f"{BASE_URL}/_next/data/{self._build_id}/{group_slug}/classroom.json"
            params = {"group": group_slug}
            try:
                response = self._session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                page_props = data.get("pageProps", {}) or {}
                if page_props.get("allCourses"):
                    return page_props
                logger.debug(
                    "Skool: _next/data/classroom.json sem allCourses, tentando fallback HTML"
                )
            except Exception as e:
                logger.warning(
                    "Falha ao buscar classroom do grupo %s via _next/data: %s", group_slug, e
                )

        # Fallback: parse __NEXT_DATA__ from the rendered classroom HTML page.
        # Build IDs rotate frequently and stale ones return 404; the HTML route
        # is stable as long as the session cookie is valid.
        try:
            html_response = self._session.get(f"{BASE_URL}/{group_slug}/classroom")
            html_response.raise_for_status()
            payload = self._extract_next_data(html_response.text)
            if not payload:
                return None
            new_build_id = payload.get("buildId")
            if new_build_id and new_build_id != self._build_id:
                logger.debug("Skool: atualizando build ID %s -> %s", self._build_id, new_build_id)
                self._build_id = new_build_id
            return payload.get("props", {}).get("pageProps", {}) or {}
        except Exception as e:
            logger.error("Falha ao buscar classroom do grupo %s: %s", group_slug, e)
            return None

    @staticmethod
    def _extract_next_data(html: str) -> Optional[Dict[str, Any]]:
        match = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

    def _fetch_course_details(self, group_slug: str, course_slug: str, course_id: str) -> Optional[Dict[str, Any]]:
        """Fetch course details with modules."""
        if self._build_id:
            url = f"{BASE_URL}/_next/data/{self._build_id}/{group_slug}/classroom/{course_slug}.json"
            params = {"group": group_slug, "course": course_slug}

            try:
                response = self._session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                page_props = data.get("pageProps", {})

                # Skool redirects to ?md=<selectedModule> on the JSON endpoint.
                if page_props.get("__N_REDIRECT"):
                    redirect_path = page_props["__N_REDIRECT"]
                    md_match = re.search(r'md=([^&]+)', redirect_path)
                    if md_match:
                        params["md"] = md_match.group(1)
                        response = self._session.get(url, params=params)
                        response.raise_for_status()
                        data = response.json()
                        page_props = data.get("pageProps", {})

                course = page_props.get("course", {})
                if course:
                    return course
            except Exception as e:
                logger.warning(
                    "Falha ao buscar detalhes do curso %s via _next/data: %s", course_slug, e
                )

        # Fallback: scrape the course HTML page to recover from rotated build IDs.
        try:
            html_response = self._session.get(
                f"{BASE_URL}/{group_slug}/classroom/{course_slug}"
            )
            html_response.raise_for_status()
            payload = self._extract_next_data(html_response.text)
            if not payload:
                return None
            new_build_id = payload.get("buildId")
            if new_build_id and new_build_id != self._build_id:
                self._build_id = new_build_id
            page_props = payload.get("props", {}).get("pageProps", {}) or {}
            return page_props.get("course", {})
        except Exception as e:
            logger.error("Falha ao buscar detalhes do curso %s: %s", course_slug, e)
            return None

    def _extract_lessons(self, course_data: Dict[str, Any], group_slug: str, course_slug: str) -> List[Dict[str, Any]]:
        """Extract lessons from course data hierarchy."""
        lessons = []
        lesson_order = 1

        def process_children(children: List[Dict[str, Any]], parent_title: str = "") -> None:
            nonlocal lesson_order

            for child in children:
                child_course = child.get("course", {})
                unit_type = child_course.get("unitType")
                metadata = child_course.get("metadata", {})

                if unit_type == "module":
                    # This is a lesson
                    lesson_id = child_course.get("id")
                    title = metadata.get("title", f"Aula {lesson_order}")
                    if parent_title:
                        title = f"{parent_title} - {title}"

                    lessons.append({
                        "id": lesson_id,
                        "title": title,
                        "order": lesson_order,
                        "locked": not metadata.get("hasAccess", True),
                        "video_url": metadata.get("videoLink", ""),
                        "video_id": metadata.get("videoId", ""),
                        "description": metadata.get("desc", ""),
                        "resources": metadata.get("resources", "[]"),
                        "video_len_ms": metadata.get("videoLenMs", 0),
                        "video_thumbnail": metadata.get("videoThumbnail", ""),
                        "group_slug": group_slug,
                        "course_slug": course_slug,
                        "_metadata": metadata,
                    })
                    lesson_order += 1

                elif unit_type == "set":
                    # This is a module/section, process its children
                    set_title = metadata.get("title", "")
                    nested_children = child.get("children", [])
                    process_children(nested_children, set_title)

        children = course_data.get("children", [])
        process_children(children)

        return lessons

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessao nao foi autenticada.")

        content = LessonContent()

        # Parse description
        desc_text = lesson.get("description", "")
        if desc_text:
            # Try to parse as JSON (Skool uses a custom format)
            parsed_desc = self._parse_description(desc_text)
            if parsed_desc:
                content.description = Description(text=parsed_desc, description_type="html")

        # Video
        video_url = lesson.get("video_url", "")
        video_id = lesson.get("video_id", "")

        if video_url:
            # External video (e.g., Vimeo)
            content.videos.append(
                Video(
                    video_id=video_id or lesson.get("id", "video"),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=lesson.get("video_len_ms", 0) // 1000,
                    extra_props={
                        "referer": f"{BASE_URL}/{lesson.get('group_slug', '')}/classroom/{lesson.get('course_slug', '')}",
                        "thumbnail": lesson.get("video_thumbnail", ""),
                    },
                )
            )
        elif video_id:
            # Internal video hosted on Skool
            internal_url = self._resolve_internal_video(video_id)
            if internal_url:
                content.videos.append(
                    Video(
                        video_id=video_id,
                        url=internal_url,
                        order=lesson.get("order", 1),
                        title=lesson.get("title", "Aula"),
                        size=0,
                        duration=lesson.get("video_len_ms", 0) // 1000,
                        extra_props={
                            "referer": f"{BASE_URL}/{lesson.get('group_slug', '')}/classroom/{lesson.get('course_slug', '')}",
                        },
                    )
                )

        # Resources/attachments
        resources_str = lesson.get("resources", "[]")
        try:
            resources = json.loads(resources_str) if resources_str else []
            for idx, resource in enumerate(resources, start=1):
                file_id = resource.get("fileId") or resource.get("id")
                filename = resource.get("name") or resource.get("title") or f"Resource {idx}"
                extension = filename.rsplit(".", 1)[-1] if "." in filename else ""

                if file_id:
                    content.attachments.append(
                        Attachment(
                            attachment_id=file_id,
                            url="",  # Will be resolved during download
                            filename=filename,
                            order=idx,
                            extension=extension,
                            size=resource.get("size", 0),
                        )
                    )
        except json.JSONDecodeError:
            pass

        return content

    def _parse_description(self, desc: str) -> str:
        """Parse Skool's JSON description format to HTML."""
        if not desc:
            return ""

        # Check if it's the [v2] format
        if desc.startswith("[v2]"):
            try:
                json_content = json.loads(desc[4:])
                return self._json_to_html(json_content)
            except json.JSONDecodeError:
                return desc[4:]

        return desc

    def _json_to_html(self, content: Any) -> str:
        """Convert Skool's JSON content format to HTML."""
        if isinstance(content, list):
            return "".join(self._json_to_html(item) for item in content)

        if not isinstance(content, dict):
            return str(content)

        node_type = content.get("type", "")

        if node_type == "paragraph":
            inner = self._json_to_html(content.get("content", []))
            return f"<p>{inner}</p>"

        elif node_type == "text":
            text = content.get("text", "")
            marks = content.get("marks", [])
            for mark in marks:
                mark_type = mark.get("type", "")
                if mark_type == "bold":
                    text = f"<strong>{text}</strong>"
                elif mark_type == "italic":
                    text = f"<em>{text}</em>"
                elif mark_type == "link":
                    href = mark.get("attrs", {}).get("href", "")
                    text = f'<a href="{href}">{text}</a>'
            return text

        elif node_type == "image":
            attrs = content.get("attrs", {})
            src = attrs.get("src", "")
            alt = attrs.get("alt", "")
            return f'<img src="{src}" alt="{alt}" />'

        elif node_type == "bulletList":
            items = self._json_to_html(content.get("content", []))
            return f"<ul>{items}</ul>"

        elif node_type == "listItem":
            inner = self._json_to_html(content.get("content", []))
            return f"<li>{inner}</li>"

        elif node_type == "heading":
            level = content.get("attrs", {}).get("level", 2)
            inner = self._json_to_html(content.get("content", []))
            return f"<h{level}>{inner}</h{level}>"

        return self._json_to_html(content.get("content", []))

    def _resolve_internal_video(self, video_id: str) -> Optional[str]:
        """Resolve internal Skool video ID to download URL."""
        try:
            url = FILE_DOWNLOAD_URL.format(file_id=video_id)
            response = self._session.post(url, params={"expire": 28800})
            response.raise_for_status()
            data = response.json()
            return data.get("url")
        except Exception as e:
            logger.error("Falha ao resolver video interno %s: %s", video_id, e)
            return None

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("A sessao nao foi autenticada.")

        try:
            file_id = attachment.attachment_id

            # Get signed download URL
            url = FILE_DOWNLOAD_URL.format(file_id=file_id)
            response = self._session.post(url, params={"expire": 28800})
            response.raise_for_status()
            data = response.json()
            download_url = data.get("url")

            if not download_url:
                logger.error("URL de download nao encontrada para %s", attachment.filename)
                return False

            # Download file
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


PlatformFactory.register_platform("Skool", SkoolPlatform)
