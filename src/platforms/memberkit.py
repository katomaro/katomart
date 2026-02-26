from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)


class MemberKitPlatform(BasePlatform):
    """
    Implements the MemberKit whitelabel platform via HTML scraping.

    MemberKit has no public API. All data is extracted by parsing
    server-rendered HTML pages (Rails + Turbo/Stimulus).
    The user must provide their specific site URL since each MemberKit
    site runs on a different subdomain.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._site_url: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="site_url",
                label="URL do Site MemberKit",
                field_type=AuthFieldType.TEXT,
                placeholder="https://exemplo.memberkit.com.br",
                required=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
MemberKit e uma plataforma whitelabel. Voce precisa informar a URL do site.

Para autenticacao manual (Token):
1. Acesse o site MemberKit e faca login normalmente.
2. Abra o DevTools (F12) > aba Application > Cookies.
3. Copie o valor do cookie "_memberkit_session" e cole no campo de token.

Assinantes ativos podem informar usuario/senha para login automatico.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        site_url = (credentials.get("site_url") or "").strip().rstrip("/")
        if not site_url:
            raise ValueError("A URL do site MemberKit e obrigatoria.")

        if not site_url.startswith("http"):
            site_url = f"https://{site_url}"

        self._site_url = site_url

        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        """Authenticates via the MemberKit Rails login form."""
        session = requests.Session()
        session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        # Step 1: GET the login page to obtain the CSRF token
        login_page_url = self._site_url + "/"
        logger.debug("MemberKit: fetching login page at %s", login_page_url)

        response = session.get(login_page_url, timeout=30)
        response.raise_for_status()

        csrf_token = self._extract_csrf_token(response.text)
        if not csrf_token:
            raise ConnectionError(
                "Nao foi possivel extrair o token CSRF da pagina de login do MemberKit."
            )

        # Step 2: POST credentials
        login_url = self._site_url + "/users/sign_in"
        form_data = {
            "authenticity_token": csrf_token,
            "user[email]": username,
            "user[password]": password,
            "user[remember_me]": "true",
            "commit": "Login",
        }

        logger.debug("MemberKit: posting credentials to %s", login_url)
        login_response = session.post(
            login_url,
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self._site_url,
                "Referer": login_page_url,
            },
            timeout=30,
            allow_redirects=True,
        )

        # After successful login, we should be redirected to the home page
        if login_response.status_code not in (200, 302):
            raise ConnectionError(
                f"Falha ao autenticar no MemberKit. Status: {login_response.status_code}"
            )

        # Check if we're still on the login page (failed auth)
        if "/users/sign_in" in login_response.url or "new_user" in login_response.text[:5000]:
            raise ConnectionError(
                "Falha ao autenticar no MemberKit. Verifique email e senha."
            )

        # Extract session cookie
        memberkit_session = session.cookies.get("_memberkit_session")
        if not memberkit_session:
            raise ConnectionError(
                "Cookie de sessao nao encontrado apos login no MemberKit."
            )

        logger.info("MemberKit: login successful")
        return memberkit_session

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()

        parsed = urlparse(self._site_url)
        domain = parsed.hostname or ""

        self._session.cookies.set("_memberkit_session", token, domain=domain, path="/")
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": self._site_url + "/",
        })

    def _extract_csrf_token(self, html: str) -> Optional[str]:
        """Extracts the Rails CSRF token from meta tag or hidden input."""
        soup = BeautifulSoup(html, "html.parser")

        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta and meta.get("content"):
            return meta["content"]

        hidden = soup.find("input", attrs={"name": "authenticity_token"})
        if hidden and hidden.get("value"):
            return hidden["value"]

        return None

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    # ------------------------------------------------------------------
    # Courses
    # ------------------------------------------------------------------

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        response = self._session.get(self._site_url + "/", timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        courses: List[Dict[str, Any]] = []

        cards = soup.select("div.card[data-id]")
        for card in cards:
            course_id = card.get("data-id", "")
            if not course_id:
                continue

            link = card.select_one("a.text-base")
            if not link:
                link = card.select_one("a[href]")

            name = link.get_text(strip=True) if link else f"Curso {course_id}"
            href = link.get("href", "") if link else ""

            # Handle fully-qualified URLs: same-site → extract path, external → skip
            if href.startswith("http"):
                parsed_href = urlparse(href)
                parsed_site = urlparse(self._site_url)
                if parsed_href.hostname == parsed_site.hostname:
                    href = parsed_href.path
                else:
                    logger.debug("MemberKit: skipping external-link course %s (%s)", course_id, href)
                    continue

            # href is like /137888-formacao-escritorio-contabil-do-zero
            slug = href.lstrip("/") if href else f"{course_id}"

            courses.append({
                "id": course_id,
                "name": name,
                "slug": slug,
                "seller_name": "",
            })

        logger.debug("MemberKit: found %d courses", len(courses))
        return courses

    # ------------------------------------------------------------------
    # Course content (modules + lessons)
    # ------------------------------------------------------------------

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_slug = course.get("slug", "")
            course_id = str(course.get("id", ""))

            url = f"{self._site_url}/{course_slug}"
            logger.debug("MemberKit: fetching course content at %s", url)

            try:
                response = self._session.get(url, timeout=30)
                response.raise_for_status()
            except Exception as exc:
                logger.error("MemberKit: failed to fetch course %s: %s", course_id, exc)
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            modules = self._parse_modules(soup, course_slug)

            # Some single-lesson courses redirect to the lesson page (0 modules).
            # Detect this by checking if the final URL is a lesson path.
            if not modules and response.url and response.url.rstrip("/") != url.rstrip("/"):
                lesson_path = urlparse(response.url).path.lstrip("/")
                # Extract lesson id from the path (e.g. "222917-.../4521619-lesson-slug")
                parts = lesson_path.split("/")
                if len(parts) >= 2:
                    lesson_id_match = re.match(r"(\d+)", parts[-1])
                    lesson_id = lesson_id_match.group(1) if lesson_id_match else parts[-1]
                    title_tag = soup.select_one("h1") or soup.select_one(".content__title")
                    lesson_title = title_tag.get_text(strip=True) if title_tag else course.get("name", f"Aula {lesson_id}")
                    modules = [{
                        "id": f"single_{course_id}",
                        "title": course.get("name", f"Modulo {course_id}"),
                        "order": 1,
                        "lessons": [{
                            "id": lesson_id,
                            "title": lesson_title,
                            "order": 1,
                            "slug": lesson_path,
                            "duration_text": "",
                            "locked": False,
                        }],
                        "locked": False,
                    }]
                    logger.debug("MemberKit: course %s redirected to single lesson %s", course_id, lesson_id)

            course_entry = course.copy()
            course_entry["title"] = course.get("name", f"Curso {course_id}")
            course_entry["modules"] = modules
            all_content[course_id] = course_entry

            time.sleep(0.5)

        return all_content

    def _parse_modules(self, soup: BeautifulSoup, course_slug: str) -> List[Dict[str, Any]]:
        """Extracts modules (sections) and their lessons from the course page."""
        modules: List[Dict[str, Any]] = []

        sections = soup.select("div.section[id]")
        for module_order, section in enumerate(sections, start=1):
            section_id = section.get("id", "")

            # Module title is in <span class="ml-2.5">
            title_span = section.select_one('span[class*="ml-2"]')
            if not title_span:
                # Fallback: try h4 inside the section header
                h4 = section.select_one("h4")
                title_span = h4.select_one("span") if h4 else None

            module_title = title_span.get_text(strip=True) if title_span else f"Modulo {module_order}"

            # Lessons are <li data-id="..."> within the section
            lesson_items = section.select("li[data-id]")
            lessons: List[Dict[str, Any]] = []

            for lesson_order, li in enumerate(lesson_items, start=1):
                lesson_id = li.get("data-id", "")
                if not lesson_id:
                    continue

                title_link = li.select_one("a.lesson__title")
                lesson_title = title_link.get_text(strip=True) if title_link else f"Aula {lesson_order}"
                lesson_href = title_link.get("href", "") if title_link else ""

                # lesson_href is like /137888-slug/3208765-lesson-slug
                lesson_slug = lesson_href.lstrip("/") if lesson_href else ""

                # Duration from <small> tag
                small = li.select_one("small")
                duration_text = ""
                if small:
                    duration_text = small.get_text(strip=True).lstrip("- ").strip()

                lessons.append({
                    "id": lesson_id,
                    "title": lesson_title,
                    "order": lesson_order,
                    "slug": lesson_slug,
                    "duration_text": duration_text,
                    "locked": False,
                })

            modules.append({
                "id": section_id,
                "title": module_title,
                "order": module_order,
                "lessons": lessons,
                "locked": False,
            })

        logger.debug("MemberKit: parsed %d modules", len(modules))
        return modules

    # ------------------------------------------------------------------
    # Lesson details
    # ------------------------------------------------------------------

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        lesson_slug = lesson.get("slug", "")
        if not lesson_slug:
            lesson_slug = f"{course_slug}/{lesson.get('id', '')}"

        url = f"{self._site_url}/{lesson_slug}"
        logger.debug("MemberKit: fetching lesson at %s", url)

        response = self._session.get(url, timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        content = LessonContent()

        # Description (some whitelabel sites use div.prose instead of div.content__body)
        desc_div = soup.select_one("div.content__body") or soup.select_one("div.prose")
        if desc_div:
            desc_html = str(desc_div)
            if desc_html.strip():
                content.description = Description(text=desc_html, description_type="html")

        # Video
        video = self._extract_video(soup, lesson, course_slug)
        if video:
            content.videos.append(video)

        # Attachments
        materials_div = soup.select_one("div.content__materials")
        if materials_div:
            download_links = materials_div.select("a[href*='downloads/']")
            for idx, link in enumerate(download_links, start=1):
                href = link.get("href", "")
                if not href:
                    continue

                # Extract download ID from href
                dl_id_match = re.search(r"downloads/(\d+)", href)
                dl_id = dl_id_match.group(1) if dl_id_match else str(idx)

                # Filename from <p class="break-anywhere grow">
                filename_p = link.select_one("p.break-anywhere, p.grow")
                filename = filename_p.get_text(strip=True) if filename_p else f"Anexo {idx}"

                # Extension from <span class="badge">
                badge = link.select_one("span.badge")
                extension = badge.get_text(strip=True).lower() if badge else ""

                # Size from the last <p> with size text
                size_p = link.select_one("p.shrink-0")
                size_text = size_p.get_text(strip=True) if size_p else ""
                size_bytes = self._parse_size(size_text)

                content.attachments.append(
                    Attachment(
                        attachment_id=dl_id,
                        url=href,
                        filename=filename,
                        order=idx,
                        extension=extension,
                        size=size_bytes,
                    )
                )

        return content

    def _extract_video(
        self, soup: BeautifulSoup, lesson: Dict[str, Any], course_slug: str
    ) -> Optional[Video]:
        """Detects and extracts the video from the lesson page theater area."""
        theater = soup.select_one("div.bg-theater")
        if not theater:
            return None

        referer = f"{self._site_url}/{lesson.get('slug', course_slug)}"

        # YouTube: data-controller="youtube" data-youtube-uid-value="..."
        yt_div = theater.select_one("[data-controller='youtube']")
        if not yt_div:
            yt_div = theater.select_one("[data-youtube-uid-value]")
        if yt_div:
            yt_id = yt_div.get("data-youtube-uid-value", "")
            if yt_id:
                return Video(
                    video_id=yt_id,
                    url=f"https://www.youtube.com/watch?v={yt_id}",
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": referer},
                )

        # Vimeo: data-controller="vimeo" or iframe with vimeo
        vimeo_div = theater.select_one("[data-controller='vimeo']")
        if vimeo_div:
            vimeo_id = (
                vimeo_div.get("data-vimeo-uid-value")
                or vimeo_div.get("data-vimeo-id-value")
                or ""
            )
            if vimeo_id:
                return Video(
                    video_id=vimeo_id,
                    url=f"https://vimeo.com/{vimeo_id}",
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": referer},
                )

        # Vimeo iframe fallback
        vimeo_iframe = theater.select_one("iframe[src*='vimeo.com']")
        if vimeo_iframe:
            src = vimeo_iframe.get("src", "")
            vimeo_match = re.search(r"player\.vimeo\.com/video/(\d+)", src)
            if vimeo_match:
                vimeo_id = vimeo_match.group(1)
                return Video(
                    video_id=vimeo_id,
                    url=f"https://vimeo.com/{vimeo_id}",
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": referer},
                )

        # PandaVideo: data-controller contains "panda"
        panda_div = theater.select_one("[data-controller*='panda']")
        if panda_div:
            panda_url = (
                panda_div.get("data-panda-url-value")
                or panda_div.get("data-panda-embed-url-value")
                or panda_div.get("data-panda-player-url-value")
                or ""
            )
            if not panda_url:
                panda_iframe = theater.select_one("iframe[src*='pandavideo']")
                if panda_iframe:
                    panda_url = panda_iframe.get("src", "")

            if panda_url:
                panda_id = panda_div.get("data-panda-uid-value") or lesson.get("id", "panda")
                return Video(
                    video_id=panda_id,
                    url=panda_url,
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": referer},
                )

        # ScaleUp: iframe with player.scaleup.com.br
        scaleup_iframe = theater.select_one("iframe[src*='scaleup.com.br']")
        if scaleup_iframe:
            scaleup_url = scaleup_iframe.get("src", "")
            if scaleup_url:
                return Video(
                    video_id=lesson.get("id", "scaleup"),
                    url=scaleup_url,
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": referer},
                )

        # Generic iframe fallback
        iframe = theater.select_one("iframe[src]")
        if iframe:
            src = iframe.get("src", "")
            if src and "youtube" not in src:
                return Video(
                    video_id=lesson.get("id", "video"),
                    url=src,
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": referer},
                )

        logger.debug("MemberKit: no video found on lesson page")
        return None

    @staticmethod
    def _parse_size(size_text: str) -> int:
        """Parses a human-readable size string like '460 KB' into bytes."""
        if not size_text:
            return 0

        match = re.match(r"([\d.,]+)\s*(KB|MB|GB|B)", size_text.strip(), re.IGNORECASE)
        if not match:
            return 0

        value = float(match.group(1).replace(",", "."))
        unit = match.group(2).upper()

        multipliers = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}
        return int(value * multipliers.get(unit, 1))

    # ------------------------------------------------------------------
    # Attachment download
    # ------------------------------------------------------------------

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        try:
            url = attachment.url
            if not url:
                logger.error("MemberKit: attachment has no URL: %s", attachment.filename)
                return False

            # Ensure absolute URL
            if url.startswith("/"):
                url = self._site_url + url

            response = self._session.get(url, stream=True, timeout=120)
            response.raise_for_status()

            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            return True

        except Exception as exc:
            logger.error("MemberKit: failed to download attachment %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("MemberKit", MemberKitPlatform)
