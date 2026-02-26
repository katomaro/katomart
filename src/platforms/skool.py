from __future__ import annotations

import json
import logging
import re
import time
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

BASE_URL = "https://www.skool.com"
API_URL = "https://api2.skool.com"
LOGIN_URL = f"{API_URL}/auth/login"
GROUPS_URL = f"{API_URL}/self/groups"
FILE_DOWNLOAD_URL = f"{API_URL}/files/{{file_id}}/download-url"


class SkoolTokenFetcher(PlaywrightTokenFetcher):
    """Automates Skool login with a real browser to capture session cookies."""

    def __init__(self):
        self.captured_cookies: List[Dict[str, Any]] = []

    @property
    def login_url(self) -> str:
        return BASE_URL

    @property
    def target_endpoints(self) -> list[str]:
        return [
            f"{API_URL}/self/groups",
            f"{API_URL}/auth/login",
        ]

    async def dismiss_cookie_banner(self, page: Page) -> None:
        pass

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        # Click login button to open modal
        try:
            login_btn = page.locator("text=Log in")
            if await login_btn.count():
                await login_btn.first.click()
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        email_sel = "input[type='email'], input[name='email']"
        password_sel = "input[type='password'], input[name='password']"

        await page.wait_for_selector(email_sel, timeout=15000)
        await page.fill(email_sel, username)

        await page.wait_for_selector(password_sel, timeout=15000)
        await page.fill(password_sel, password)

    async def submit_login(self, page: Page) -> None:
        for selector in (
            "button[type='submit']",
            "button:has-text('Log in')",
            "button:has-text('Sign in')",
        ):
            try:
                await page.click(selector, timeout=3000)
                return
            except Exception:
                continue
        await page.press("body", "Enter")


class SkoolPlatform(BasePlatform):
    """Implements the Skool platform using the shared platform interface."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = SkoolTokenFetcher()
        self._build_id: Optional[str] = None
        self._cookies: List[Dict[str, Any]] = []

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como obter o token do Skool:
1) Acesse https://www.skool.com em seu navegador e faca login normalmente.
2) Abra o DevTools (F12) e va para a aba Application > Cookies.
3) Copie o valor do cookie "skooltok" e cole no campo de token acima.

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
                    if cookie.get("name") == "skooltok":
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

            response = session.post(
                LOGIN_URL,
                json={"email": username, "password": password},
                timeout=30,
            )
            response.raise_for_status()

            # Extract skooltok from cookies
            skooltok = session.cookies.get("skooltok")
            if not skooltok:
                for cookie in response.cookies:
                    if cookie.name == "skooltok":
                        skooltok = cookie.value
                        break

            if not skooltok:
                raise ValueError("Cookie de sessao nao encontrado na resposta.")

            # Store all cookies
            self._cookies = [{"name": c.name, "value": c.value} for c in session.cookies]
            return skooltok

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
        self._session.cookies.set("skooltok", token, domain=".skool.com", path="/")

        # Set other cookies if available
        for cookie in self._cookies:
            if cookie["name"] != "skooltok":
                self._session.cookies.set(cookie["name"], cookie["value"], domain=".skool.com", path="/")

        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        })

    def _fetch_build_id(self) -> None:
        """Fetch Next.js build ID from the main page."""
        try:
            response = self._session.get(BASE_URL)
            response.raise_for_status()

            # Look for buildId in the HTML
            match = re.search(r'"buildId"\s*:\s*"([^"]+)"', response.text)
            if match:
                self._build_id = match.group(1)
                logger.debug("Skool build ID: %s", self._build_id)
            else:
                # Try alternative pattern
                match = re.search(r'/_next/data/([^/]+)/', response.text)
                if match:
                    self._build_id = match.group(1)
                    logger.debug("Skool build ID (alt): %s", self._build_id)
        except Exception as e:
            logger.warning("Could not fetch Skool build ID: %s", e)

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Fetch all groups (communities) the user belongs to."""
        if not self._session:
            raise ConnectionError("A sessao nao foi autenticada.")

        courses: List[Dict[str, Any]] = []

        try:
            response = self._session.get(GROUPS_URL, params={"limit": 100, "prefs": "false"})
            response.raise_for_status()
            data = response.json()
            logger.debug("Skool groups: %s", data)

            groups = data.get("groups", [])
            for group in groups:
                group_id = group.get("id")
                group_name = group.get("name")
                metadata = group.get("metadata", {})

                courses.append({
                    "id": group_id,
                    "name": metadata.get("title") or metadata.get("name") or group_name,
                    "slug": group_name,
                    "seller_name": metadata.get("ownerName", ""),
                    "description": metadata.get("description", ""),
                    "_group": group,
                })

        except Exception as e:
            logger.error("Falha ao listar grupos do Skool: %s", e)
            raise

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

        if not self._build_id:
            logger.error("Build ID not available")
            return None

        url = f"{BASE_URL}/_next/data/{self._build_id}/{group_slug}/classroom.json"
        params = {"group": group_slug}

        try:
            response = self._session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("pageProps", {})
        except Exception as e:
            logger.error("Falha ao buscar classroom do grupo %s: %s", group_slug, e)
            return None

    def _fetch_course_details(self, group_slug: str, course_slug: str, course_id: str) -> Optional[Dict[str, Any]]:
        """Fetch course details with modules."""
        if not self._build_id:
            return None

        url = f"{BASE_URL}/_next/data/{self._build_id}/{group_slug}/classroom/{course_slug}.json"
        params = {"group": group_slug, "course": course_slug}

        try:
            response = self._session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            page_props = data.get("pageProps", {})

            # Handle redirect
            if page_props.get("__N_REDIRECT"):
                redirect_path = page_props["__N_REDIRECT"]
                # Extract md parameter
                md_match = re.search(r'md=([^&]+)', redirect_path)
                if md_match:
                    md = md_match.group(1)
                    params["md"] = md
                    response = self._session.get(url, params=params)
                    response.raise_for_status()
                    data = response.json()
                    page_props = data.get("pageProps", {})

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
