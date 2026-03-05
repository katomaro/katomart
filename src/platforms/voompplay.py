from __future__ import annotations

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
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

API_BASE = "https://api.voompplay.com.br"

TARGET_ENDPOINTS = [
    f"{API_BASE}/member/me",
    f"{API_BASE}/home",
    f"{API_BASE}/v2/app/home",
    f"{API_BASE}/course/",
    f"{API_BASE}/watch/",
]


class VoompPlayTokenFetcher(PlaywrightTokenFetcher):
    """Automates Voomp Play login via Playwright."""

    network_idle_timeout_ms: int = 300_000

    def __init__(self, site_url: str = ""):
        self._site_url = site_url

    @property
    def login_url(self) -> str:
        return self._site_url or "https://voompplay.com.br"

    @property
    def target_endpoints(self) -> list[str]:
        return TARGET_ENDPOINTS

    async def dismiss_cookie_banner(self, page: Page) -> None:
        for selector in (
            "button:has-text('Aceitar')",
            "button:has-text('OK')",
            "button:has-text('Entendi')",
        ):
            try:
                btn = page.locator(selector)
                if await btn.count():
                    await btn.first.click(timeout=2000)
                    return
            except Exception:
                continue

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        email_sel = "input[type='email'], input[name='email'], input[placeholder*='mail']"
        password_sel = "input[type='password'], input[name='password']"

        await page.wait_for_selector(email_sel, timeout=15000)
        await page.fill(email_sel, username)

        await page.wait_for_selector(password_sel, timeout=15000)
        await page.fill(password_sel, password)

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


class VoompPlayPlatform(BasePlatform):
    """
    Implements the Voomp Play (voompplay.com.br) members area platform.

    Uses a JSON REST API at api.voompplay.com.br.
    Auth is a site-specific token sent in the Authorization header (no Bearer prefix).
    Videos are typically hosted on PandaVideo.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._site_url: str = ""
        self._token_fetcher: Optional[VoompPlayTokenFetcher] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="site_url",
                label="URL do Site Voomp Play",
                field_type=AuthFieldType.TEXT,
                placeholder="https://seusite.voompplay.com.br",
                required=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Voomp Play - Como obter o token:

Para autenticacao manual (Token):
1) Acesse sua area de membros (ex: https://seusite.voompplay.com.br) e faca login.
2) Abra o DevTools (F12) > aba Network.
3) Procure uma requisicao para api.voompplay.com.br (ex: /member/me).
4) Copie o valor do header "Authorization" e cole no campo de token.

Assinantes ativos podem informar usuario/senha para login via navegador.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        site_url = (credentials.get("site_url") or "").strip().rstrip("/")
        if not site_url:
            raise ValueError("A URL do site Voomp Play e obrigatoria.")
        if not site_url.startswith("http"):
            site_url = f"https://{site_url}"
        self._site_url = site_url
        self._token_fetcher = VoompPlayTokenFetcher(site_url=site_url)

        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        confirmation_event = credentials.get("manual_auth_confirmation")

        try:
            token = self._token_fetcher.fetch_token(
                username,
                password,
                headless=False,
                wait_for_user_confirmation=(
                    confirmation_event.wait if confirmation_event else None
                ),
            )
            if not token:
                raise ValueError("Token vazio")
            return token
        except Exception as exc:
            raise ConnectionError(
                "Falha ao autenticar na Voomp Play via navegador. "
                "Tente novamente ou copie o token Authorization manualmente."
            ) from exc

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Authorization": token,
            "Origin": self._site_url,
            "Referer": self._site_url + "/",
        })

        try:
            resp = self._session.get(f"{API_BASE}/member/me", timeout=30)
            resp.raise_for_status()
            data = resp.json()
            member = data.get("current", {}).get("currentMember", {})
            site = data.get("current", {}).get("currentSite", {})
            logger.info(
                "VoompPlay: authenticated as '%s' on site '%s'",
                member.get("name", "?"),
                site.get("name", "?"),
            )
        except Exception as exc:
            raise ConnectionError(
                "Falha ao autenticar na Voomp Play. Verifique o token Authorization."
            ) from exc

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        courses: List[Dict[str, Any]] = []
        page = 1

        while True:
            resp = self._session.get(
                f"{API_BASE}/member/me/all-courses",
                params={"page": page},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            courses_by_site = data.get("courses", {})
            for site_name, site_courses in courses_by_site.items():
                for course in site_courses:
                    if not course.get("has_access", False):
                        continue

                    courses.append({
                        "id": course.get("id"),
                        "name": course.get("title", "Curso"),
                        "slug": str(course.get("id", "")),
                        "seller_name": site_name,
                        "lessons_count": course.get("lessons_count", 0),
                    })

            meta = data.get("meta", {})
            if page >= meta.get("last_page", 1):
                break
            page += 1

        logger.debug("VoompPlay: found %d accessible courses", len(courses))
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            logger.debug("VoompPlay: fetching modules for course %s", course_id)

            try:
                resp = self._session.get(
                    f"{API_BASE}/course/{course_id}/watch",
                    params={"data[]": ["course", "modules"]},
                    timeout=30,
                )
                resp.raise_for_status()
                watch_data = resp.json()
            except Exception as exc:
                logger.error("VoompPlay: failed to fetch course %s: %s", course_id, exc)
                continue

            modules_data = watch_data.get("modules", [])
            processed_modules = []

            for mod in modules_data:
                mod_id = mod.get("id")
                mod_title = mod.get("title", "Modulo")
                mod_order = mod.get("order", 0)

                if not mod.get("is_liberated", True):
                    processed_modules.append({
                        "id": mod_id,
                        "title": mod_title,
                        "order": mod_order + 1,
                        "lessons": [],
                        "locked": True,
                    })
                    continue

                logger.debug("VoompPlay: fetching lessons for module %s (%s)", mod_id, mod_title)

                try:
                    resp = self._session.get(
                        f"{API_BASE}/course/{course_id}/watch",
                        params={
                            "data[]": ["currentModuleLessons"],
                            "current_module_id": mod_id,
                        },
                        timeout=30,
                    )
                    resp.raise_for_status()
                    mod_data = resp.json()
                except Exception as exc:
                    logger.error("VoompPlay: failed to fetch module %s: %s", mod_id, exc)
                    processed_modules.append({
                        "id": mod_id,
                        "title": mod_title,
                        "order": mod_order + 1,
                        "lessons": [],
                        "locked": False,
                    })
                    continue

                lessons_data = mod_data.get("currentModuleLessons", [])
                processed_lessons = []

                for les_idx, les in enumerate(lessons_data, start=1):
                    processed_lessons.append({
                        "id": les.get("id"),
                        "title": les.get("title", f"Aula {les_idx}"),
                        "order": les.get("order", les_idx - 1) + 1,
                        "locked": not les.get("is_liberated", True),
                        "media_type": les.get("mediaType", ""),
                        "source": les.get("source", ""),
                        "content": les.get("content", ""),
                        "duration": les.get("duration", 0),
                        "attachments_data": les.get("attachments", []),
                        "module_id": mod_id,
                    })

                processed_modules.append({
                    "id": mod_id,
                    "title": mod_title,
                    "order": mod_order + 1,
                    "lessons": processed_lessons,
                    "locked": False,
                })

                time.sleep(0.3)

            course_entry = course.copy()
            course_entry["title"] = watch_data.get("course", {}).get("title", course.get("name", ""))
            course_entry["modules"] = processed_modules
            all_content[str(course_id)] = course_entry

        return all_content

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        content = LessonContent()

        description = lesson.get("content", "")
        if description:
            content.description = Description(text=description, description_type="text")

        source = lesson.get("source", "")
        media_type = lesson.get("media_type", "")
        if source:
            video_url = self._extract_video_url(source, media_type)
            video_id = self._extract_video_id(source, media_type)

            if video_url:
                content.videos.append(
                    Video(
                        video_id=video_id or str(lesson.get("id", "video")),
                        url=video_url,
                        order=lesson.get("order", 1),
                        title=lesson.get("title", "Aula"),
                        size=0,
                        duration=lesson.get("duration", 0) or 0,
                        extra_props={
                            "referer": self._site_url + "/",
                            "media_type": media_type,
                        },
                    )
                )

        attachments_data = lesson.get("attachments_data", [])
        for idx, att in enumerate(attachments_data, start=1):
            att_url = att.get("cdn_url") or att.get("path", "")
            filename = att.get("title", f"Anexo {idx}")
            mime = att.get("mime", "")
            extension = self._extension_from_mime(mime, filename)

            content.attachments.append(
                Attachment(
                    attachment_id=str(att.get("id", idx)),
                    url=att_url,
                    filename=f"{filename}.{extension}" if extension and "." not in filename else filename,
                    order=idx,
                    extension=extension,
                    size=att.get("size", 0) or 0,
                )
            )

        return content

    @staticmethod
    def _extract_video_url(source: str, media_type: str) -> str:
        """Extract the embed/player URL from an iframe source HTML."""
        if media_type == "iframe" or "<iframe" in source:
            match = re.search(r'src=["\']([^"\']+)["\']', source)
            if match:
                return match.group(1)
        if source.startswith("http"):
            return source
        return ""

    @staticmethod
    def _extract_video_id(source: str, media_type: str) -> str:
        """Extract video ID from source URL."""
        if "pandavideo" in source:
            match = re.search(r"[?&]v=([a-f0-9-]+)", source)
            if match:
                return match.group(1)
        if "youtube" in source or "youtu.be" in source:
            match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]+)", source)
            if match:
                return match.group(1)
        if "vimeo" in source:
            match = re.search(r"vimeo\.com/(?:video/)?(\d+)", source)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _extension_from_mime(mime: str, filename: str) -> str:
        """Derive file extension from MIME type or filename."""
        if "." in filename:
            return filename.rsplit(".", 1)[-1]
        mime_map = {
            "application/pdf": "pdf",
            "image/png": "png",
            "image/jpeg": "jpg",
            "application/zip": "zip",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        }
        return mime_map.get(mime, "")

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        try:
            url = attachment.url
            if not url:
                logger.error("VoompPlay: attachment has no URL: %s", attachment.filename)
                return False

            response = self._session.get(url, stream=True, timeout=120)
            response.raise_for_status()

            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True

        except Exception as exc:
            logger.error("VoompPlay: failed to download attachment %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Voomp Play", VoompPlayPlatform)
