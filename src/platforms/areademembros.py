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

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._platform_url: str = ""
        self._base_url: str = ""

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
        logging.info("Sessão autenticada na Área de Membros.")

    def _verify_session(self) -> None:
        """Verifies that the session is authenticated."""
        vitrine_url = f"{self._base_url}/area/vitrine"
        response = self._session.get(vitrine_url, allow_redirects=False)

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            if "login" in location.lower() or "auth" in location.lower():
                raise ValueError("Token inválido ou expirado. Faça login novamente e copie os cookies.")

        response.raise_for_status()

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """
        Fetches courses from the vitrine page.
        Each grupo-vitrine represents a course containing multiple modules.
        """
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        vitrine_url = f"{self._base_url}/area/vitrine"
        response = self._session.get(vitrine_url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        courses: List[Dict[str, Any]] = []

        # Each grupo-vitrine is a COURSE
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
            module_urls = []
            for box in accessible_modules:
                link = box.select_one("a[href*='/area/produto/']")
                if link and link.get("href"):
                    href = link.get("href", "").split("?")[0]
                    module_urls.append(urljoin(self._base_url, href))

            courses.append({
                "id": vitrine_id,
                "title": course_title,
                "name": course_title,
                "slug": vitrine_id,
                "url": vitrine_url,  # We'll use module_urls in fetch_course_content
                "module_urls": module_urls,
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
        The sidebar has modules (div.section-group-titulo with data-target)
        and lessons (a.layer-link) inside each module's collapse container.
        """
        try:
            response = self._session.get(page_url)
            response.raise_for_status()
        except Exception as exc:
            logging.warning("Failed to fetch course structure from %s: %s", page_url, exc)
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        modules: List[Dict[str, Any]] = []

        # Find all module headers (section-group-titulo with data-target)
        module_headers = soup.select("a.section-group-titulo[data-target], div.section-group-titulo[data-target]")

        for module_index, header in enumerate(module_headers, start=1):
            data_target = header.get("data-target", "")
            if not data_target:
                continue

            # Extract module ID from data-target (format: #s597777)
            module_id = data_target.lstrip("#s")

            # Get module title from span.item-titulo inside the header
            module_title = None
            title_elem = header.select_one("span.item-titulo")
            if title_elem:
                module_title = title_elem.get_text(strip=True)

            if not module_title:
                # Fallback: get all text and strip trailing numbers (e.g., "7 aulas")
                full_text = header.get_text(strip=True)
                if full_text:
                    # Remove trailing lesson counts like "7 aulas", "8 aulas", etc.
                    module_title = re.sub(r'\d+\s*aulas?$', '', full_text).strip()

            if not module_title:
                module_title = f"Módulo {module_index}"

            # Find the collapse container for this module
            collapse_container = soup.select_one(f"div{data_target}")
            lessons: List[Dict[str, Any]] = []

            if collapse_container:
                # Find all lesson links inside this module (they use layer-link class)
                lesson_links = collapse_container.select("a.layer-link[href*='/area/produto/item/']")

                for lesson_index, link in enumerate(lesson_links, start=1):
                    href = link.get("href", "")
                    if not href:
                        continue

                    match = re.search(r"/area/produto/item/(\d+)", href)
                    if not match:
                        continue

                    lesson_id = match.group(1)
                    full_url = urljoin(self._base_url, href.split("?")[0])

                    # Get lesson title from item-titulo span
                    title_elem = link.select_one("span.item-titulo")
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

            modules.append({
                "id": module_id,
                "title": module_title,
                "order": module_index,
                "lessons": lessons,
                "locked": False,
            })

        return modules

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

        # Extract description
        description_div = soup.select_one("div.descricao, div.conteudo-descricao, div.aba-descricao")
        if description_div:
            description_text = description_div.get_text(separator="\n", strip=True)
            if description_text:
                content.description = Description(text=description_text, description_type="text")

        # Extract video from video container iframe
        iframe = soup.select_one("div.video-container iframe, div.video iframe, iframe.video-iframe")
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

        # Extract attachments
        attachments_container = soup.select_one("div.anexos, div.aba-anexos, div.lista-anexos")
        if attachments_container:
            for index, link in enumerate(attachments_container.select("a[href]"), start=1):
                href = link.get("href")
                if not href or href.startswith("#") or href.startswith("javascript:"):
                    continue

                filename = link.get_text(strip=True) or f"anexo_{index}"
                file_url = urljoin(lesson_url, href)
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
