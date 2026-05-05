from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory


class AreaDeMembrosPlayground(BasePlatform):
    """
    Implements scraping for Área de Membros (areademembros.com) whitelabel portals.

    Structure:
    - Vitrine: contains "grupo-vitrine" divs, each representing a COURSE
    - Each grupo-vitrine contains multiple item-box, each representing a MODULE
    - Each module (secao) contains lessons (items)

    Videos are hosted on Scaleup/Smartplayer.
    """

    # Some whitelabels use the "classic" URL scheme, others use the "conteudo" scheme.
    _VITRINE_PATHS = ("/area/vitrine/home", "/area/vitrine")
    _PRODUCT_HREF_PATTERNS = ("/area/conteudo/produto/", "/area/produto/")
    _LESSON_HREF_PATTERNS = ("/area/conteudo/aula/", "/area/produto/item/")
    _LESSON_ID_REGEX = re.compile(r"/area/(?:conteudo/aula|produto/item)/(\d+)")
    _PRODUCT_ID_REGEX = re.compile(r"/area/(?:conteudo/produto|produto)/(\d+)")

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._platform_url: str = ""
        self._base_url: str = ""
        self._vitrine_url: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="platform_url",
                label="URL da plataforma",
                placeholder="https://membros.exemplo.com.br",
            )
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """Informe a URL base da sua plataforma Área de Membros sempre omitindo o /auth/login (ex: https://membros.exemplo.com.br)
Para obter o token de acesso:
1. Faça login na plataforma pelo navegador
2. Abra as ferramentas de desenvolvedor (F12)
3. Vá em Application > Cookies
4. Copie os valores de XSRF-TOKEN e app_v4_session
5. Cole no campo Token no formato: XSRF-TOKEN=valor; app_v4_session=valor
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        platform_url = (credentials.get("platform_url") or "").strip()
        if not platform_url:
            raise ValueError("Informe a URL da plataforma Área de Membros.")
        if not platform_url.startswith(("http://", "https://")):
            raise ValueError("A URL deve começar com http:// ou https://.")

        self._platform_url = platform_url.rstrip("/")
        parsed = urlparse(self._platform_url)
        self._base_url = f"{parsed.scheme}://{parsed.netloc}"

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self._settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": self._base_url,
                "Referer": self._platform_url,
            }
        )

        token = (credentials.get("token") or "").strip()
        if token:
            session.headers["Cookie"] = token
            self._session = session
            self._verify_session()
            logging.info("Sessão autenticada na Área de Membros via token.")
            return

        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        if not self._settings.has_full_permissions:
            raise ValueError(
                "Autenticação por usuário e senha está disponível apenas para assinantes. "
                "Forneça um token (cookies) da plataforma."
            )
        if not username or not password:
            raise ValueError("Usuário e senha são obrigatórios para Área de Membros.")

        login_url = f"{self._base_url}/auth/login"
        login_page = session.get(login_url)
        login_page.raise_for_status()

        soup = BeautifulSoup(login_page.text, "html.parser")
        token_input = soup.select_one("input[name='_token']")
        if not token_input:
            raise ValueError("Não foi possível obter o token CSRF da página de login.")
        csrf_token = token_input.get("value", "")

        login_data = {
            "_token": (None, csrf_token),
            "Acesso[email]": (None, username),
            "Acesso[senha]": (None, password),
        }
        response = session.post(login_url, files=login_data)
        response.raise_for_status()

        if "/area/vitrine" not in response.url and "vitrine" not in response.text.lower():
            raise ValueError("Falha na autenticação. Verifique suas credenciais.")

        self._session = session
        # Resolve the vitrine URL variant after a successful login.
        try:
            self._verify_session()
        except Exception:
            self._vitrine_url = response.url if "/area/vitrine" in response.url else f"{self._base_url}/area/vitrine"
        logging.info("Sessão autenticada na Área de Membros.")

    def _verify_session(self) -> None:
        """Verifies that the session is authenticated and detects the vitrine URL variant."""
        last_error: Optional[Exception] = None

        for path in self._VITRINE_PATHS:
            candidate = f"{self._base_url}{path}"
            try:
                response = self._session.get(candidate, allow_redirects=False)
            except Exception as exc:
                last_error = exc
                continue

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location", "")
                if "login" in location.lower() or "auth" in location.lower():
                    raise ValueError("Token inválido ou expirado. Faça login novamente e copie os cookies.")
                # Follow internal redirects (e.g. /area/vitrine -> /area/vitrine/home)
                if location:
                    self._vitrine_url = urljoin(candidate, location)
                    return

            if response.status_code == 200:
                self._vitrine_url = candidate
                return

            last_error = requests.HTTPError(f"HTTP {response.status_code} em {candidate}")

        if last_error:
            raise last_error
        raise ValueError("Não foi possível acessar a vitrine da Área de Membros.")

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """
        Fetches courses from the vitrine page.
        Each grupo-vitrine represents a course containing multiple modules.
        """
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        vitrine_url = self._vitrine_url or f"{self._base_url}{self._VITRINE_PATHS[0]}"
        response = self._session.get(vitrine_url)
        response.raise_for_status()
        # Update with the final URL after redirects.
        vitrine_url = response.url
        self._vitrine_url = vitrine_url

        soup = BeautifulSoup(response.text, "html.parser")
        courses: List[Dict[str, Any]] = []

        # Each grupo-vitrine is a COURSE (classic layout).
        vitrine_groups = soup.select("div[id^='grupo-vitrine-']")

        for course_index, group in enumerate(vitrine_groups, start=1):
            vitrine_id = group.get("id", "").replace("grupo-vitrine-", "")

            # Get course title from vitrine-title span
            title_elem = group.select_one("div.vitrine-title span")
            course_title = None
            if title_elem:
                # Get text content, excluding SVG elements
                for svg in title_elem.find_all("svg"):
                    svg.decompose()
                course_title = title_elem.get_text(strip=True)

            if not course_title:
                course_title = f"Curso {course_index}"

            # Count accessible modules (item-box without sem-acesso)
            accessible_modules = group.select("div.item-box-produto:not(.sem-acesso)")
            if not accessible_modules:
                continue  # Skip courses with no accessible content

            # Store module URLs for later fetching
            module_urls = self._collect_product_urls(accessible_modules)

            courses.append({
                "id": vitrine_id,
                "title": course_title,
                "name": course_title,
                "slug": vitrine_id,
                "url": vitrine_url,  # We'll use module_urls in fetch_course_content
                "module_urls": module_urls,
                "seller_name": "Área de Membros",
            })

        if courses:
            return courses

        # Fallback for whitelabels without grupo-vitrine grouping (e.g. /area/vitrine/home).
        # Treat each accessible product link as its own "course".
        return self._fetch_courses_flat(soup, vitrine_url)

    def _collect_product_urls(self, scope) -> List[str]:
        """Collects product URLs (/area/produto/{id} or /area/conteudo/produto/{id}) inside a scope."""
        urls: List[str] = []
        seen: set = set()
        selector = ", ".join(f"a[href*='{pattern}']" for pattern in self._PRODUCT_HREF_PATTERNS)
        for link in scope.select(selector) if hasattr(scope, "select") else self._select_in_each(scope, selector):
            href = (link.get("href") or "").split("?")[0]
            if not href:
                continue
            full_url = urljoin(self._base_url, href)
            if full_url in seen:
                continue
            seen.add(full_url)
            urls.append(full_url)
        return urls

    @staticmethod
    def _select_in_each(elements, selector: str):
        for element in elements:
            for match in element.select(selector):
                yield match

    def _fetch_courses_flat(self, soup: BeautifulSoup, vitrine_url: str) -> List[Dict[str, Any]]:
        """Builds course entries from a flat vitrine layout (no grupo-vitrine wrapper)."""
        courses: List[Dict[str, Any]] = []
        seen: set = set()
        selector = ", ".join(f"a[href*='{pattern}']" for pattern in self._PRODUCT_HREF_PATTERNS)

        for course_index, link in enumerate(soup.select(selector), start=1):
            href = (link.get("href") or "").split("?")[0]
            if not href:
                continue
            full_url = urljoin(self._base_url, href)
            if full_url in seen:
                continue
            seen.add(full_url)

            match = self._PRODUCT_ID_REGEX.search(href)
            if not match:
                continue
            product_id = match.group(1)

            # Try to find a title near the link (caption span, then anchor text).
            title = ""
            caption = link.select_one(".item-titulo, .titulo, h3, h2, .nome, .vitrine-title")
            if caption:
                title = caption.get_text(strip=True)
            if not title:
                title = link.get("title") or link.get_text(separator=" ", strip=True)
            title = re.sub(r"\s+", " ", title).strip()
            if not title:
                title = f"Curso {course_index}"

            courses.append({
                "id": product_id,
                "title": title,
                "name": title,
                "slug": product_id,
                "url": vitrine_url,
                "module_urls": [full_url],
                "seller_name": "Área de Membros",
            })

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Fetches course structure from the sidebar menu.
        The sidebar contains all modules and lessons for the course.
        """
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        result: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            course_title = course.get("title", "Curso")
            module_urls = course.get("module_urls", [])

            if not module_urls:
                continue

            # Access the first module URL to get the sidebar with full course structure
            first_url = module_urls[0]
            modules = self._extract_course_structure_from_sidebar(first_url)

            course_entry = {
                "id": course_id,
                "title": course_title,
                "name": course_title,
                "slug": course.get("slug", str(course_id)),
                "modules": modules,
            }

            result[str(course_id)] = course_entry

        return result

    def _extract_course_structure_from_sidebar(self, page_url: str) -> List[Dict[str, Any]]:
        """
        Extracts the full course structure from the sidebar menu.

        Two layouts are supported:
        - "Classic": each module has a section-group-titulo header with data-target="#sXXX"
          and lessons under /area/produto/item/{id}.
        - "Conteudo": each module is a div.section-group with data-acesso-secao-id and lessons
          under /area/conteudo/aula/{id}. Single-module courses may omit the header entirely.
        """
        try:
            response = self._session.get(page_url)
            response.raise_for_status()
        except Exception as exc:
            logging.warning("Failed to fetch course structure from %s: %s", page_url, exc)
            return []

        soup = BeautifulSoup(response.text, "html.parser")

        # Prefer the desktop sidebar (avoids duplicating from the mobile collapse).
        sidebar = soup.select_one("aside .sections#sections, aside .sections, .sections#sections, .sections")

        modules = self._parse_modules_classic(soup, sidebar)
        if modules:
            return modules

        return self._parse_modules_conteudo(soup, sidebar)

    def _parse_modules_classic(
        self, soup: BeautifulSoup, sidebar: Optional[Any]
    ) -> List[Dict[str, Any]]:
        """Parses the classic layout that uses section-group-titulo headers."""
        scope = sidebar if sidebar is not None else soup
        module_headers = scope.select(
            "a.section-group-titulo[data-target], div.section-group-titulo[data-target]"
        )

        modules: List[Dict[str, Any]] = []
        for module_index, header in enumerate(module_headers, start=1):
            data_target = header.get("data-target", "")
            if not data_target:
                continue

            module_id = data_target.lstrip("#s")

            module_title = None
            title_elem = header.select_one("span.item-titulo")
            if title_elem:
                module_title = title_elem.get_text(strip=True)

            if not module_title:
                full_text = header.get_text(strip=True)
                if full_text:
                    module_title = re.sub(r"\d+\s*aulas?$", "", full_text).strip()

            if not module_title:
                module_title = f"Módulo {module_index}"

            collapse_container = scope.select_one(f"div{data_target}") or soup.select_one(f"div{data_target}")
            lessons = self._extract_lessons(collapse_container) if collapse_container else []

            modules.append({
                "id": module_id,
                "title": module_title,
                "order": module_index,
                "lessons": lessons,
                "locked": False,
            })

        return modules

    def _parse_modules_conteudo(
        self, soup: BeautifulSoup, sidebar: Optional[Any]
    ) -> List[Dict[str, Any]]:
        """Parses the "conteudo" layout (section-group divs with data-acesso-secao-id)."""
        scope = sidebar if sidebar is not None else soup
        section_groups = scope.select("div.section-group[data-acesso-secao-id]")

        # Fallback to sections-items containers when section-group is missing.
        if not section_groups:
            section_items = scope.select("div.section-items[id^='s']")
            section_groups = []
            for items in section_items:
                section_id = items.get("id", "").lstrip("s")
                if section_id:
                    section_groups.append(items)

        course_title = self._extract_course_title(soup)
        modules: List[Dict[str, Any]] = []
        seen_ids: set = set()

        for module_index, group in enumerate(section_groups, start=1):
            module_id = group.get("data-acesso-secao-id") or ""
            if not module_id:
                items_container = group.select_one("div.section-items[id^='s']") or group
                container_id = items_container.get("id", "")
                module_id = container_id.lstrip("s")

            if not module_id or module_id in seen_ids:
                continue
            seen_ids.add(module_id)

            module_title = self._extract_module_title(group) or course_title or f"Módulo {module_index}"
            lessons = self._extract_lessons(group)

            modules.append({
                "id": module_id,
                "title": module_title,
                "order": module_index,
                "lessons": lessons,
                "locked": False,
            })

        return modules

    @staticmethod
    def _extract_module_title(group: Any) -> Optional[str]:
        """Tries several selectors to find a section/module title."""
        candidates = [
            "div.section-group-titulo span.item-titulo",
            "a.section-group-titulo span.item-titulo",
            "div.section-group-titulo",
            "a.section-group-titulo",
            "div.section-titulo",
            "span.section-titulo",
            "h3.section-titulo",
        ]
        for selector in candidates:
            elem = group.select_one(selector)
            if elem:
                text = elem.get_text(separator=" ", strip=True)
                text = re.sub(r"\d+\s*aulas?$", "", text).strip()
                if text:
                    return text
        return None

    @staticmethod
    def _extract_course_title(soup: BeautifulSoup) -> Optional[str]:
        """Extracts the course title from the breadcrumb."""
        breadcrumb_links = soup.select(
            "div.breadcrumb a[href*='/area/conteudo/produto/'], div.breadcrumb a[href*='/area/produto/']"
        )
        if breadcrumb_links:
            text = breadcrumb_links[-1].get_text(strip=True)
            if text:
                return text
        return None

    def _extract_lessons(self, container: Any) -> List[Dict[str, Any]]:
        """Extracts lessons inside a sidebar container, supporting both URL patterns."""
        if container is None:
            return []

        selector = ", ".join(
            f"a.layer-link[href*='{pattern}']" for pattern in self._LESSON_HREF_PATTERNS
        )
        lesson_links = container.select(selector)

        lessons: List[Dict[str, Any]] = []
        seen_ids: set = set()

        for lesson_index, link in enumerate(lesson_links, start=1):
            href = link.get("href", "")
            if not href:
                continue

            match = self._LESSON_ID_REGEX.search(href)
            if not match:
                continue

            lesson_id = match.group(1)
            if lesson_id in seen_ids:
                continue
            seen_ids.add(lesson_id)

            full_url = urljoin(self._base_url, href.split("?")[0])

            title_elem = link.select_one("span.item-titulo, div.item-titulo")
            if title_elem:
                title = title_elem.get_text(strip=True)
            else:
                title = link.get_text(strip=True)

            if not title:
                title = f"Aula {lesson_index}"

            lessons.append({
                "id": lesson_id,
                "title": title,
                "url": full_url,
                "order": lesson_index,
                "locked": False,
            })

        return lessons

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        lesson_url = lesson.get("url")
        if not lesson_url:
            raise ValueError("URL da aula não informada.")

        content = LessonContent()

        response = self._session.get(lesson_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Extract description (classic + conteudo layouts).
        description_div = soup.select_one(
            "article.aula-conteudo, div.descricao, div.conteudo-descricao, div.aba-descricao"
        )
        if description_div:
            # Avoid pulling attachment captions into the description text.
            description_clone = BeautifulSoup(str(description_div), "html.parser")
            for attach in description_clone.select("a.article-attach"):
                attach.decompose()
            description_text = description_clone.get_text(separator="\n", strip=True)
            if description_text:
                content.description = Description(text=description_text, description_type="text")

        # Extract video from video container iframe
        iframe = soup.select_one(
            "div.class-video-container iframe, div.video-container iframe, "
            "div.video iframe, iframe.video-iframe"
        )
        if iframe:
            embed_url = iframe.get("src") or iframe.get("data-src")
            if embed_url:
                if not embed_url.startswith("http"):
                    embed_url = urljoin(lesson_url, embed_url)

                content.videos.append(
                    Video(
                        video_id=lesson.get("id") or lesson.get("title", "aula"),
                        url=embed_url,
                        order=lesson.get("order", 1),
                        title=lesson.get("title", "Aula"),
                        size=0,
                        duration=0,
                        extra_props={"referer": lesson_url},
                    )
                )

        # Extract attachments — classic layout uses dedicated containers,
        # the conteudo layout inlines them as a.article-attach inside the article.
        attachment_links: List[Any] = []
        seen_hrefs: set = set()

        attachments_container = soup.select_one("div.anexos, div.aba-anexos, div.lista-anexos")
        if attachments_container:
            attachment_links.extend(attachments_container.select("a[href]"))

        attachment_links.extend(soup.select("a.article-attach[href]"))

        for index, link in enumerate(attachment_links, start=1):
            href = link.get("href")
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            file_url = urljoin(lesson_url, href)

            title_elem = link.select_one(".attach-title")
            if title_elem:
                filename = title_elem.get_text(strip=True)
            else:
                filename = link.get_text(separator=" ", strip=True)
            filename = re.sub(r"\s+", " ", filename).strip() or f"anexo_{index}"

            extension = Path(filename).suffix.lstrip(".")
            if not extension:
                extension = Path(urlparse(file_url).path).suffix.lstrip(".")

            content.attachments.append(
                Attachment(
                    attachment_id=str(index),
                    url=file_url,
                    filename=filename,
                    order=index,
                    extension=extension,
                    size=0,
                )
            )

        return content

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        try:
            response = self._session.get(attachment.url, stream=True)
            response.raise_for_status()
            with open(download_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    handle.write(chunk)
            return True
        except Exception as exc:
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Área de Membros (não é a única, nome tendencioso)", AreaDeMembrosPlayground)
