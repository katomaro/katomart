from __future__ import annotations

import json
import re
from typing import Any, Dict, List
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, AuxiliaryURL, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory

COURSES_PAGE_URL = "https://www.datascienceacademy.com.br/start"
COURSE_CONTENT_URL = "https://www.datascienceacademy.com.br/api/course/{slug}?contents=true&path=player"
COURSE_CONTENT_FALLBACK_URL = "https://www.datascienceacademy.com.br/api/course/{slug}?contents"
UNIT_PAGE_URL = "https://www.datascienceacademy.com.br/path-player?courseid={slug}&unit={unit_id}Unit"


class DataScienceAcademyPlatform(BasePlatform):
    """Implements the Data Science Academy platform using the shared interface."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 5.00) ativos podem informar usuário/senha. O sistema irá trocar essas credenciais automaticamente pelo token da etapa acima, além de usar alguns algoritmos melhores e ter funcionalidades extras na aplicação, e obter suporte prioritário.

Como obter o token da Data Science Academy?
1) Abra https://www.datascienceacademy.com.br/start e faça login.
2) No DevTools (F12) abra a aba Network e atualize a página.
3) Procure uma requisição para /api/course/<slug> e copie o valor do cabeçalho Authorization.
4) Cole apenas o token no campo acima ou informe usuário e senha se permitido pela sua licença.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.datascienceacademy.com.br",
                "Referer": "https://www.datascienceacademy.com.br/",
            }
        )

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        raise ConnectionError(
            "A troca automática de credenciais não é suportada para esta plataforma. Use um token válido."
        )

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        response = self._session.get(COURSES_PAGE_URL)
        response.raise_for_status()

        logging.debug("Data Science Academy courses page length: %s", len(response.text))

        soup = BeautifulSoup(response.text, "html.parser")
        course_cards = soup.select("a.lw-course-card--stretched-link")
        courses: Dict[str, Dict[str, Any]] = {}

        for card in course_cards:
            href = card.get("href") or ""
            slug = self._extract_slug_from_href(href)
            if not slug:
                continue

            name = self._extract_course_title(card) or slug.replace("-", " ").replace("_", " ").title()
            courses[slug] = {
                "id": slug,
                "name": name,
                "seller_name": "Data Science Academy",
                "slug": slug,
            }

        return sorted(courses.values(), key=lambda c: c.get("name", ""))

    def _extract_slug_from_href(self, href: str) -> str:
        if not href:
            return ""
        if "=" in href:
            return href.split("=")[-1]
        parsed = urlparse(href)
        if parsed.query:
            query = parse_qs(parsed.query)
            course_id = query.get("courseid") or query.get("course")
            if course_id:
                return course_id[0]
        return href.rstrip("/").split("/")[-1]

    def _extract_course_title(self, anchor: Any) -> str:
        parent = anchor.find_parent("div", class_=re.compile("lw-course-card"))
        if not parent:
            return ""
        title_el = parent.find(["h3", "h4", "div"], class_=re.compile("title|course-title")) or parent.find(["h3", "h4"])
        if title_el:
            return title_el.get_text(strip=True)
        return ""

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}

        for course in courses:
            slug = course.get("slug") or course.get("id")
            if not slug:
                continue

            course_json = self._get_course_json(slug)
            logging.debug("Data Science Academy course %s payload: %s", slug, course_json)
            modules = self._extract_sections(course_json)

            course_entry = course.copy()
            course_entry["title"] = course.get("name", "Curso")
            course_entry["modules"] = modules
            content[str(slug)] = course_entry

        return content

    def _get_course_json(self, slug: str) -> Dict[str, Any]:
        assert self._session
        primary = self._session.get(COURSE_CONTENT_URL.format(slug=slug))
        if primary.ok:
            payload = primary.json()
            logging.debug("Data Science Academy primary course response for %s: %s", slug, payload)
            return payload

        fallback = self._session.get(COURSE_CONTENT_FALLBACK_URL.format(slug=slug))
        fallback.raise_for_status()
        payload = fallback.json()
        logging.debug("Data Science Academy fallback course response for %s: %s", slug, payload)
        return payload

    def _extract_sections(self, course_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        sections = self._collect_sections(course_json)
        modules: List[Dict[str, Any]] = []

        for section_index, section in enumerate(sections, start=1):
            raw_title = section.get("titles") or section.get("title")
            section_title = self._resolve_title(raw_title, f"Módulo {section_index}")
            learning_path = section.get("learningPath") or []

            lessons: List[Dict[str, Any]] = []
            for lesson_index, item in enumerate(learning_path, start=1):
                lesson_id = str(item.get("id") or f"{section_index}-{lesson_index}")
                lesson_type = (item.get("type") or "").lower()
                lesson_title = self._resolve_title(item.get("titles"), f"Aula {lesson_index}")
                lessons.append(
                    {
                        "id": lesson_id,
                        "title": lesson_title,
                        "order": lesson_index,
                        "locked": False,
                        "item_type": lesson_type,
                    }
                )

            modules.append(
                {
                    "id": str(section.get("id") or section_index),
                    "title": section_title,
                    "order": section_index,
                    "lessons": lessons,
                    "locked": False,
                }
            )

        return modules

    def _collect_sections(self, node: Any) -> List[Dict[str, Any]]:
        collected: List[List[Dict[str, Any]]] = []

        def recursive(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() == "sections":
                        if isinstance(item, list):
                            collected.append(item)
                        elif isinstance(item, dict):
                            collected.append([v for v in item.values() if isinstance(v, dict)])
                    recursive(item)
            elif isinstance(value, list):
                for entry in value:
                    recursive(entry)

        recursive(node)

        flattened: List[Dict[str, Any]] = []
        for section_list in collected:
            for section in section_list:
                if isinstance(section, dict):
                    flattened.append(section)
        return flattened

    def _resolve_title(self, raw_title: Any, fallback: str) -> str:
        if isinstance(raw_title, dict):
            for key in ("pt-BR", "pt", "en", "title"):
                if raw_title.get(key):
                    return str(raw_title[key]).strip()
            first_value = next((str(v).strip() for v in raw_title.values() if v), "")
            if first_value:
                return first_value
        if isinstance(raw_title, list):
            for entry in raw_title:
                if entry:
                    return str(entry).strip()
        if isinstance(raw_title, str) and raw_title.strip():
            return raw_title.strip()
        return fallback

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        unit_id = str(lesson.get("id"))
        unit_type = (lesson.get("item_type") or "").lower()
        order = lesson.get("order", 1)

        html = self._fetch_unit_html(course_slug, unit_id)
        logging.debug("Data Science Academy unit %s html length: %s", unit_id, len(html))
        title = self._extract_title_from_html(html) or lesson.get("title", f"Aula {order}")

        content = LessonContent()

        if unit_type in {"ivideo", "video"}:
            video_url = self._extract_video_url(html)
            if video_url:
                content.videos.append(
                    Video(
                        video_id=unit_id,
                        url=video_url,
                        order=order,
                        title=title,
                        size=0,
                        duration=0,
                    )
                )
        elif unit_type == "youtube":
            yt_url = self._extract_youtube_url(html)
            if yt_url:
                content.videos.append(
                    Video(
                        video_id=unit_id,
                        url=yt_url,
                        order=order,
                        title=title,
                        size=0,
                        duration=0,
                    )
                )
        elif unit_type == "pdf":
            pdf_url = self._extract_pdf_url(html)
            if pdf_url:
                filename = pdf_url.split("/")[-1] or f"{unit_id}.pdf"
                extension = filename.split(".")[-1] if "." in filename else "pdf"
                content.attachments.append(
                    Attachment(
                        attachment_id=unit_id,
                        url=pdf_url,
                        filename=filename,
                        order=order,
                        extension=extension,
                        size=0,
                    )
                )
        elif unit_type == "url":
            external_url = self._extract_external_url(html)
            if external_url:
                content.auxiliary_urls.append(
                    AuxiliaryURL(
                        url_id=unit_id,
                        url=external_url,
                        order=order,
                        title=title,
                        description=external_url,
                    )
                )
        elif unit_type == "pbebook":
            page_url = self._extract_external_url(html)
            if page_url:
                content.auxiliary_urls.append(
                    AuxiliaryURL(
                        url_id=unit_id,
                        url=page_url,
                        order=order,
                        title=title,
                        description=page_url,
                    )
                )

        return content

    def _fetch_unit_html(self, slug: str, unit_id: str) -> str:
        assert self._session
        url = UNIT_PAGE_URL.format(slug=slug, unit_id=unit_id)
        response = self._session.get(url)
        response.raise_for_status()
        return response.text

    def _extract_title_from_html(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.find("title")
        if not title_el or not title_el.text:
            return ""
        title = title_el.text.replace(" - Data Science Academy", "").replace(" | Data Science Academy", "").strip()
        return title

    def _extract_video_url(self, html: str) -> str:
        match = re.search(r'"avc_url"\s*:\s*"([^"]+)"', html)
        if match:
            return match.group(1)

        soup = BeautifulSoup(html, "html.parser")
        script_tags = soup.find_all("script")
        for script in script_tags:
            if not script.string:
                continue
            if "playerConfig" not in script.string:
                continue
            json_match = re.search(r"window\\.playerConfig\\s*=\\s*({.*?})", script.string, flags=re.DOTALL)
            if not json_match:
                continue
            try:
                data = json.loads(json_match.group(1))
                cdns = data.get("request", {}).get("files", {}).get("hls", {}).get("cdns", {})
                akfire = cdns.get("akfire_interconnect_quic", {})
                avc_url = akfire.get("avc_url")
                if avc_url:
                    return avc_url
            except json.JSONDecodeError:
                continue
        return ""

    def _extract_youtube_url(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        iframe = soup.find("iframe", src=re.compile(r"youtube|youtu\.be", re.I))
        if iframe and iframe.get("src"):
            return self._normalize_url(iframe.get("src"))
        return ""

    def _extract_pdf_url(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        iframe = soup.find("iframe", id="playerFrame") or soup.find("iframe", attrs={"name": "playerFrame"})
        if iframe and iframe.get("src"):
            src = iframe.get("src")
            parsed = urlparse(src)
            file_param = parse_qs(parsed.query).get("file", [""])[0]
            if file_param:
                return self._normalize_url(file_param)
            return self._normalize_url(src)
        link = soup.find("a", href=re.compile(r"\.pdf($|\?)", re.I))
        if link and link.get("href"):
            return self._normalize_url(link.get("href"))
        file_match = re.search(r"file=([^&\"]+)", html)
        if file_match:
            return self._normalize_url(file_match.group(1))
        return ""

    def _extract_external_url(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        iframe = soup.find("iframe", id="iframePage")
        if iframe and iframe.get("src"):
            return self._normalize_url(iframe.get("src"))
        generic_iframe = soup.find("iframe")
        if generic_iframe and generic_iframe.get("src"):
            return self._normalize_url(generic_iframe.get("src"))
        return ""

    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return urljoin("https://www.datascienceacademy.com.br", url)
        return url

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        if not attachment.url:
            return False

        response = self._session.get(attachment.url, stream=True)
        response.raise_for_status()

        with open(download_path, "wb") as file_handle:
            for chunk in response.iter_content(chunk_size=8192):
                file_handle.write(chunk)

        return True


PlatformFactory.register_platform("Data Science Academy", DataScienceAcademyPlatform)
