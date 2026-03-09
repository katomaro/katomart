from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)


class CademiPlatform(BasePlatform):
    """Implements the Cademi (cademi.com.br) members area platform.

    Cademi is a white-label LMS at {school}.cademi.com.br.
    All content is server-side rendered HTML (no JSON API for content).
    Auth is cookie-based via app_v4_session.
    Videos are typically hosted on PandaVideo.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._site_url: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="site_url",
                label="URL do Site Cademi",
                field_type=AuthFieldType.TEXT,
                placeholder="https://seusite.cademi.com.br",
                required=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """Para autenticacao manual (Token):
1) Acesse sua area de membros (ex: https://seusite.cademi.com.br) e faca login.
2) Abra o DevTools (F12) > aba Application > Cookies.
3) Copie o valor do cookie "app_v4_session".
4) Cole no campo de token.

Assinantes ativos podem informar usuario/senha para login automatico.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        site_url = (credentials.get("site_url") or "").strip().rstrip("/")
        if not site_url:
            raise ValueError("A URL do site Cademi e obrigatoria.")
        if not site_url.startswith("http"):
            site_url = f"https://{site_url}"
        self._site_url = site_url

        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        session = requests.Session()
        session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        login_url = f"{self._site_url}/auth/login"
        resp = session.get(login_url, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        csrf_input = soup.find("input", {"name": "_token"})
        if not csrf_input:
            raise ConnectionError("Nao foi possivel encontrar o token CSRF na pagina de login.")
        csrf_token = csrf_input.get("value", "")

        resp = session.post(
            login_url,
            files={
                "_token": (None, csrf_token),
                "Acesso[email]": (None, username),
                "Acesso[senha]": (None, password),
            },
            timeout=30,
            allow_redirects=True,
        )

        if "/auth/login" in resp.url:
            raise ConnectionError("Falha ao autenticar na Cademi. Verifique suas credenciais.")

        session_cookie = session.cookies.get("app_v4_session")
        if not session_cookie:
            raise ConnectionError("Login realizado mas cookie de sessao nao encontrado.")

        self._login_session = session
        return session_cookie

    def _configure_session(self, token: str) -> None:
        if hasattr(self, "_login_session") and self._login_session:
            self._session = self._login_session
            self._login_session = None
        else:
            self._session = requests.Session()
            self._session.cookies.set("app_v4_session", token)

        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        resp = self._session.get(f"{self._site_url}/area/vitrine", timeout=30, allow_redirects=True)
        if "/auth/login" in resp.url:
            raise ConnectionError(
                "Falha ao autenticar na Cademi. Verifique o cookie app_v4_session."
            )
        logger.info("Cademi: authenticated successfully")

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        resp = self._session.get(f"{self._site_url}/area/vitrine", timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        courses: Dict[str, Dict[str, Any]] = {}

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if "/area/produto/item/" in href:
                continue

            match = re.search(r"/area/produto/(\d+)", href)
            if not match:
                continue

            product_id = match.group(1)
            if product_id in courses:
                continue

            title = self._extract_title_near(link) or f"Curso {product_id}"
            courses[product_id] = {
                "id": product_id,
                "name": title,
                "slug": product_id,
                "seller_name": "",
            }

        if not courses:
            courses = self._extract_courses_from_classes(soup)

        logger.debug("Cademi: found %d courses", len(courses))
        return sorted(courses.values(), key=lambda c: c.get("name", ""))

    def _extract_title_near(self, element: Any) -> str:
        img = element.find("img")
        if img and img.get("alt"):
            alt = img.get("alt", "").strip()
            if alt and len(alt) > 3:
                return alt

        for parent_cls in ("card", "produto", "course", "item", "col"):
            parent = element.find_parent(class_=re.compile(parent_cls, re.I))
            if parent:
                for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    title_el = parent.find(tag)
                    if title_el:
                        text = title_el.get_text(strip=True)
                        if text:
                            return text

        text = element.get_text(strip=True)
        if text and len(text) > 3 and not text.isdigit():
            return text
        return ""

    def _extract_courses_from_classes(self, soup: BeautifulSoup) -> Dict[str, Dict[str, Any]]:
        courses: Dict[str, Dict[str, Any]] = {}
        for el in soup.find_all(class_=re.compile(r"produto-id-\d+")):
            for cls in el.get("class", []):
                match = re.match(r"produto-id-(\d+)", cls)
                if match:
                    product_id = match.group(1)
                    if product_id not in courses:
                        title_el = el.find(["h1", "h2", "h3", "h4", "h5", "h6"])
                        title = title_el.get_text(strip=True) if title_el else f"Curso {product_id}"
                        courses[product_id] = {
                            "id": product_id,
                            "name": title,
                            "slug": product_id,
                            "seller_name": "",
                        }
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            logger.debug("Cademi: fetching content for course %s", course_id)

            try:
                resp = self._session.get(
                    f"{self._site_url}/area/produto/{course_id}",
                    timeout=30,
                    allow_redirects=True,
                )
                resp.raise_for_status()
                html = resp.text

                modules = self._parse_sidebar(html)

                if not modules:
                    soup = BeautifulSoup(html, "html.parser")
                    first_item = soup.find("a", href=re.compile(r"/area/produto/item/\d+"))
                    if first_item:
                        item_href = first_item.get("href", "")
                        if not item_href.startswith("http"):
                            item_href = f"{self._site_url}{item_href}"
                        item_resp = self._session.get(item_href, timeout=30)
                        item_resp.raise_for_status()
                        modules = self._parse_sidebar(item_resp.text)
                        html = item_resp.text

                if not modules:
                    modules = self._parse_product_page(html)

            except Exception as exc:
                logger.error("Cademi: failed to fetch course %s: %s", course_id, exc)
                continue

            course_entry = course.copy()
            course_entry["title"] = self._extract_page_title(html) or course.get("name", "Curso")
            course_entry["modules"] = modules
            all_content[str(course_id)] = course_entry

            time.sleep(0.3)

        return all_content

    def _parse_sidebar(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        sections_container = soup.find("div", id="sections") or soup.find("div", class_="sections")
        if not sections_container:
            return []

        modules: List[Dict[str, Any]] = []

        for group in sections_container.find_all("div", class_="section-group"):
            if "progresso-total" in (group.get("class") or []):
                continue

            mod_order = len(modules) + 1
            title_div = group.find("div", class_=re.compile("section-group-titulo"))
            section_id = str(mod_order)
            module_title = f"Modulo {mod_order}"

            if title_div:
                section_id = title_div.get("data-secao-id", str(mod_order))
                titulo_el = title_div.find("div", class_="item-titulo")
                if titulo_el:
                    raw_text = " ".join(titulo_el.get_text(separator=" ", strip=True).split())
                    module_title = re.sub(r"\s*\d+\s*aulas?\s*$", "", raw_text).strip() or raw_text

            items_container = group.find("div", class_="section-items")
            lessons: List[Dict[str, Any]] = []

            if items_container:
                for les_idx, link in enumerate(
                    items_container.find_all("a", href=re.compile(r"/area/produto/item/\d+")), start=1
                ):
                    href = link.get("href", "")
                    item_match = re.search(r"/area/produto/item/(\d+)", href)
                    if not item_match:
                        continue

                    item_id = item_match.group(1)
                    titulo_el = link.find("div", class_="item-titulo")
                    lesson_title = titulo_el.get_text(strip=True) if titulo_el else f"Aula {les_idx}"

                    lessons.append({
                        "id": item_id,
                        "title": lesson_title,
                        "order": les_idx,
                        "locked": False,
                    })

            modules.append({
                "id": section_id,
                "title": module_title,
                "order": mod_order,
                "lessons": lessons,
                "locked": False,
            })

        return modules

    def _parse_product_page(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        lessons: List[Dict[str, Any]] = []
        seen: set = set()

        for link in soup.find_all("a", href=re.compile(r"/area/produto/item/\d+")):
            href = link.get("href", "")
            match = re.search(r"/area/produto/item/(\d+)", href)
            if not match:
                continue

            item_id = match.group(1)
            if item_id in seen:
                continue
            seen.add(item_id)

            title = link.get_text(strip=True) or f"Aula {len(lessons) + 1}"
            lessons.append({
                "id": item_id,
                "title": title,
                "order": len(lessons) + 1,
                "locked": False,
            })

        if not lessons:
            return []

        return [{
            "id": "1",
            "title": "Conteudo",
            "order": 1,
            "lessons": lessons,
            "locked": False,
        }]

    def _extract_page_title(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.find("title")
        if title_el:
            text = title_el.get_text(strip=True)
            for suffix in (" - Cademi", " | Cademi"):
                if text.endswith(suffix):
                    text = text[: -len(suffix)]
            return text.strip()
        return ""

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        item_id = lesson.get("id")
        resp = self._session.get(
            f"{self._site_url}/area/produto/item/{item_id}",
            timeout=30,
        )
        resp.raise_for_status()
        html = resp.text

        content = LessonContent()
        soup = BeautifulSoup(html, "html.parser")

        article = soup.find("article")
        if article:
            desc_parts = []
            for p in article.find_all("div", class_="article-paragraph"):
                text = p.get_text(strip=True)
                if text:
                    desc_parts.append(text)
            if desc_parts:
                content.description = Description(
                    text="\n\n".join(desc_parts),
                    description_type="text",
                )

        video_div = soup.find("div", class_=re.compile(r"video-pandavideo"))
        if video_div:
            iframe = video_div.find("iframe")
            if iframe and iframe.get("src"):
                video_url = iframe.get("src")
                panda_id = video_div.get("data-id", "")
                content.videos.append(
                    Video(
                        video_id=panda_id or str(item_id),
                        url=video_url,
                        order=lesson.get("order", 1),
                        title=lesson.get("title", "Aula"),
                        size=0,
                        duration=0,
                        extra_props={"referer": self._site_url + "/"},
                    )
                )

        if not content.videos:
            for iframe in soup.find_all("iframe", src=True):
                src = iframe.get("src", "")
                if any(p in src for p in ("youtube", "youtu.be", "vimeo", "pandavideo")):
                    vid_id = self._extract_video_id(src)
                    content.videos.append(
                        Video(
                            video_id=vid_id or str(item_id),
                            url=src,
                            order=lesson.get("order", 1),
                            title=lesson.get("title", "Aula"),
                            size=0,
                            duration=0,
                            extra_props={"referer": self._site_url + "/"},
                        )
                    )
                    break

        for idx, attach_link in enumerate(
            soup.find_all("a", class_="article-attach", href=True), start=1
        ):
            url = attach_link.get("href", "")
            if not url:
                continue

            title_div = attach_link.find(class_="attach-title")
            filename = title_div.get_text(strip=True) if title_div else f"Anexo {idx}"
            extension = filename.rsplit(".", 1)[-1] if "." in filename else ""

            content.attachments.append(
                Attachment(
                    attachment_id=str(idx),
                    url=url,
                    filename=filename,
                    order=idx,
                    extension=extension,
                    size=0,
                )
            )

        return content

    @staticmethod
    def _extract_video_id(url: str) -> str:
        if "pandavideo" in url:
            match = re.search(r"[?&]v=([a-f0-9-]+)", url)
            if match:
                return match.group(1)
        if "youtube" in url or "youtu.be" in url:
            match = re.search(r"(?:v=|youtu\.be/|embed/)([a-zA-Z0-9_-]+)", url)
            if match:
                return match.group(1)
        if "vimeo" in url:
            match = re.search(r"vimeo\.com/(?:video/)?(\d+)", url)
            if match:
                return match.group(1)
        return ""

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        try:
            url = attachment.url
            if not url:
                return False

            response = self._session.get(url, stream=True, timeout=120)
            response.raise_for_status()

            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True

        except Exception as exc:
            logger.error("Cademi: failed to download attachment %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Cademi", CademiPlatform)
