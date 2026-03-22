from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)

WISTIA_JSON_URL = "https://fast.wistia.com/embed/medias/{hashed_id}.json"


class KajabiPlatform(BasePlatform):
    """Implements the Kajabi platform (HTML-scraping based, Wistia video)."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._base_url: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="site_url",
                label="URL do site Kajabi",
                field_type=AuthFieldType.TEXT,
                placeholder="Ex: https://conteudo.cursospm3.com.br",
                required=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como obter o token do Kajabi:
1) Abra o site Kajabi do seu curso e faca login.
2) No DevTools (F12) abra a aba Application > Cookies.
3) Copie o valor do cookie "_kjb_session".
4) Cole no campo Token acima.
5) Informe tambem a URL base do site (ex: https://conteudo.cursospm3.com.br).
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._base_url = (credentials.get("site_url") or "").strip().rstrip("/")
        if not self._base_url:
            raise ValueError("A URL base do site Kajabi e obrigatoria.")

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{self._base_url}/",
        })
        self._session.cookies.set("_kjb_session", token, domain=self._base_url.split("//")[-1].split("/")[0])

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        """Logs in via the Kajabi HTML form and returns the _kjb_session cookie."""
        base_url = (credentials.get("site_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("A URL base do site Kajabi e obrigatoria.")

        session = requests.Session()
        session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        # Fetch login page to get CSRF token
        login_page = session.get(f"{base_url}/login", timeout=30)
        login_page.raise_for_status()
        soup = BeautifulSoup(login_page.text, "html.parser")

        authenticity_token = ""
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta:
            authenticity_token = meta.get("content", "")
        if not authenticity_token:
            hidden = soup.find("input", attrs={"name": "authenticity_token"})
            if hidden:
                authenticity_token = hidden.get("value", "")

        if not authenticity_token:
            raise ConnectionError("Nao foi possivel obter o CSRF token da pagina de login do Kajabi.")

        # Submit login form
        login_resp = session.post(
            f"{base_url}/login",
            data={
                "utf8": "\u2713",
                "authenticity_token": authenticity_token,
                "member[email]": username,
                "member[password]": password,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{base_url}/login",
            },
            timeout=30,
            allow_redirects=True,
        )

        # Check for login failure
        if "auth__message" in login_resp.text and ("invalida" in login_resp.text.lower() or "invalid" in login_resp.text.lower()):
            raise ConnectionError("Falha no login: email ou senha invalidos.")

        kjb_session = session.cookies.get("_kjb_session")
        if not kjb_session:
            raise ConnectionError("Login aparentemente bem-sucedido, mas o cookie _kjb_session nao foi encontrado.")

        logger.info("Kajabi: login bem-sucedido, _kjb_session obtido")
        return kjb_session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        resp = self._session.get(f"{self._base_url}/library", timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        courses: List[Dict[str, Any]] = []
        for product in soup.select(".product"):
            link = product.select_one("a[href*='/products/']")
            if not link:
                continue

            href = link.get("href", "")
            slug = href.rstrip("/").split("/products/")[-1].split("/")[0] if "/products/" in href else ""
            if not slug:
                continue

            title_el = product.select_one(".product__title")
            title = title_el.get_text(strip=True) if title_el else slug

            courses.append({
                "id": slug,
                "name": title,
                "slug": slug,
                "seller_name": "Kajabi",
            })

        logger.info("Kajabi: encontrados %d cursos em /library", len(courses))
        return sorted(courses, key=lambda c: c.get("name", ""))

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            slug = course.get("slug") or course.get("id")
            if not slug:
                continue

            # Fetch modules from course page
            try:
                resp = self._session.get(f"{self._base_url}/products/{slug}", timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                logger.error("Kajabi: falha ao buscar curso %s: %s", slug, exc)
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            modules = self._extract_modules(soup, slug)

            course_entry = course.copy()
            course_entry["title"] = course.get("name", slug)
            course_entry["modules"] = modules
            all_content[slug] = course_entry

        return all_content

    def _extract_modules(self, soup: BeautifulSoup, course_slug: str) -> List[Dict[str, Any]]:
        modules: List[Dict[str, Any]] = []

        for idx, item in enumerate(soup.select(".syllabus__item"), start=1):
            link = item.select_one("a[href*='/categories/']")
            if not link:
                continue

            href = link.get("href", "")
            link_id = link.get("id", "")

            # Extract category ID from id="category-{id}" or from the URL
            category_id = ""
            if link_id.startswith("category-"):
                category_id = link_id.replace("category-", "")
            if not category_id:
                match = re.search(r"/categories/(\d+)", href)
                if match:
                    category_id = match.group(1)
            if not category_id:
                continue

            title_el = item.select_one(".syllabus__title")
            title = title_el.get_text(strip=True) if title_el else f"Modulo {idx}"

            # Fetch lessons for this module
            lessons = self._fetch_module_lessons(course_slug, category_id)

            modules.append({
                "id": category_id,
                "title": title,
                "order": idx,
                "lessons": lessons,
                "locked": False,
            })

        logger.info("Kajabi: curso %s tem %d modulos", course_slug, len(modules))
        return modules

    def _fetch_module_lessons(self, course_slug: str, category_id: str) -> List[Dict[str, Any]]:
        lessons: List[Dict[str, Any]] = []

        try:
            resp = self._session.get(
                f"{self._base_url}/products/{course_slug}/categories/{category_id}",
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Kajabi: falha ao buscar modulo %s/%s: %s", course_slug, category_id, exc)
            return lessons

        soup = BeautifulSoup(resp.text, "html.parser")

        seen_post_ids: set = set()
        order = 0
        for item in soup.select(".syllabus__item"):
            item_id = item.get("id", "")

            # Skip sub-category header items (they duplicate the first post)
            if item_id.startswith("category-"):
                continue

            post_id = ""
            if item_id.startswith("post-"):
                post_id = item_id.replace("post-", "")

            link = item.select_one("a[href*='/posts/']")
            if not post_id and link:
                match = re.search(r"/posts/(\d+)", link.get("href", ""))
                if match:
                    post_id = match.group(1)

            if not post_id or post_id in seen_post_ids:
                continue
            seen_post_ids.add(post_id)

            # Extract the real sub-category ID from the post's href
            # (may differ from the module's category_id)
            real_category_id = category_id
            if link:
                cat_match = re.search(r"/categories/(\d+)/posts/", link.get("href", ""))
                if cat_match:
                    real_category_id = cat_match.group(1)

            order += 1
            title_el = item.select_one(".syllabus__title")
            title = title_el.get_text(strip=True) if title_el else f"Aula {order}"

            lessons.append({
                "id": post_id,
                "title": title,
                "order": order,
                "locked": False,
                "category_id": real_category_id,
            })

        return lessons

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        content = LessonContent()
        post_id = str(lesson.get("id"))
        # Use the lesson's real category_id (sub-category) when available,
        # falling back to the module-level category_id.
        effective_category = str(lesson.get("category_id") or module_id)

        try:
            resp = self._session.get(
                f"{self._base_url}/products/{course_slug}/categories/{effective_category}/posts/{post_id}",
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Kajabi: falha ao buscar aula %s: %s", post_id, exc)
            return content

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # Extract Wistia video IDs
        wistia_ids = set(re.findall(r"wistia_async_([a-z0-9]+)", html))
        for vid_idx, wistia_id in enumerate(wistia_ids, start=1):
            video = self._resolve_wistia_video(wistia_id, vid_idx, lesson.get("title", ""))
            if video:
                content.videos.append(video)

        # Extract downloads
        for dl_idx, dl_link in enumerate(soup.select(".downloads__download, .downloads a[href*='/courses/downloads/']"), start=1):
            href = dl_link.get("href", "")
            if not href or "/courses/downloads/" not in href:
                continue

            # Make absolute URL
            if href.startswith("/"):
                href = f"{self._base_url}{href}"

            label_el = dl_link.select_one(".media-body")
            label = label_el.get_text(strip=True) if label_el else f"download_{dl_idx}"

            # Detect extension from icon or URL slug
            extension = ""
            icon = dl_link.select_one("img.downloads__icon")
            if icon:
                icon_src = icon.get("src", "")
                if "acrobat" in icon_src or "pdf" in icon_src:
                    extension = "pdf"
                elif "word" in icon_src or "doc" in icon_src:
                    extension = "docx"
                elif "excel" in icon_src or "xls" in icon_src:
                    extension = "xlsx"
                elif "powerpoint" in icon_src or "ppt" in icon_src:
                    extension = "pptx"
            if not extension:
                url_slug = href.rstrip("/").split("/")[-1]
                ext_match = re.search(r"-(\w{2,4})$", url_slug)
                if ext_match:
                    extension = ext_match.group(1)

            filename = f"{label}.{extension}" if extension else label

            content.attachments.append(
                Attachment(
                    attachment_id=href.split("/downloads/")[-1] if "/downloads/" in href else str(dl_idx),
                    url=href,
                    filename=filename,
                    order=dl_idx,
                    extension=extension,
                    size=0,
                )
            )

        return content

    def _resolve_wistia_video(self, wistia_id: str, order: int, lesson_title: str) -> Optional[Video]:
        """Fetches Wistia video metadata and returns a Video with the embed URL for yt-dlp."""
        try:
            resp = requests.get(
                WISTIA_JSON_URL.format(hashed_id=wistia_id),
                headers={"Referer": f"{self._base_url}/"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Kajabi: falha ao resolver Wistia %s: %s", wistia_id, exc)
            # Fallback: return a yt-dlp compatible URL anyway
            return Video(
                video_id=wistia_id,
                url=f"https://fast.wistia.com/embed/medias/{wistia_id}",
                order=order,
                title=lesson_title or wistia_id,
                size=0,
                duration=0,
                extra_props={"referer": f"{self._base_url}/"},
            )

        media = data.get("media", {})
        duration = int(media.get("duration", 0))

        # Find best MP4 asset for size info
        best_size = 0
        for asset in media.get("assets", []):
            if asset.get("type") in ("hd_mp4_video", "original") and asset.get("size", 0) > best_size:
                best_size = asset.get("size", 0)

        # Use the yt-dlp compatible embed URL (yt-dlp has native Wistia support)
        return Video(
            video_id=wistia_id,
            url=f"https://fast.wistia.com/embed/medias/{wistia_id}",
            order=order,
            title=lesson_title or media.get("name", wistia_id),
            size=best_size,
            duration=duration,
            extra_props={"referer": f"{self._base_url}/"},
        )

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        if not attachment.url:
            return False

        try:
            # Kajabi download URLs redirect to pre-signed S3 URLs
            resp = self._session.get(attachment.url, stream=True, timeout=120, allow_redirects=True)
            resp.raise_for_status()

            with open(download_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            return True
        except Exception as exc:
            logger.error("Kajabi: falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Kajabi", KajabiPlatform)
