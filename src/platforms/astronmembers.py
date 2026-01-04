from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory


class AstronmembersPlatform(BasePlatform):
    """Implements scraping for Astronmembers whitelabel portals."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._platform_url: str = ""
        self._base_url: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="platform_url",
                label="URL de login da plataforma",
                placeholder="https://exemplo.astronmembers.com/entrar",
            )
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Informe a URL completa de login da sua plataforma Astronmembers (normalmente termina em /entrar).
Sempre que possível, prefira colar o token de sessão no campo apropriado.
Assinantes podem informar email e senha para que o token seja obtido automaticamente.
O domínio costuma seguir o padrão *.astronmembers.com, mas instâncias customizadas também funcionam.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        platform_url = (credentials.get("platform_url") or "").strip()
        if not platform_url:
            raise ValueError("Informe a URL de login da plataforma Astronmembers.")
        if not platform_url.startswith(("http://", "https://")):
            raise ValueError("A URL deve começar com http:// ou https://.")

        self._platform_url = platform_url.rstrip("/")
        self._base_url = self._platform_url.rsplit("/", 1)[0]

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self._settings.user_agent,
                "Origin": self._base_url,
                "Referer": self._platform_url,
            }
        )

        token = (credentials.get("token") or "").strip()
        if token:
            session.headers["Cookie"] = token
            self._session = session
            logging.info("Sessão autenticada na Astronmembers via token.")
            return

        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()
        if not self._settings.has_full_permissions:
            raise ValueError(
                "Autenticação por usuário e senha está disponível apenas para assinantes. Forneça um token da plataforma."
            )
        if not username or not password:
            raise ValueError("Usuário e senha são obrigatórios para Astronmembers.")

        session.get(self._platform_url)

        parsed_url = urlparse(self._platform_url)
        login_url = f"{parsed_url.scheme}://{parsed_url.netloc}/entrar"

        login_data = {
            "return": (None, ""),
            "login": (None, username),
            "senha": (None, password),
        }

        response = session.post(login_url, files=login_data)
        response.raise_for_status()

        logging.debug("Astronmembers login response URL: %s", response.url)
        logging.debug("Astronmembers login response length: %s", len(response.text))

        self._session = session
        logging.info("Sessão autenticada na Astronmembers.")

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        dashboard_url = urljoin(self._base_url + "/", "dashboard")
        response = self._session.get(dashboard_url)
        response.raise_for_status()

        logging.debug("Astronmembers dashboard URL resolved to: %s", response.url)
        logging.debug("Astronmembers dashboard content length: %s", len(response.text))

        final_base_url = f"{urlparse(response.url).scheme}://{urlparse(response.url).netloc}"
        courses = self._parse_courses_from_html(response.text, final_base_url)
        logging.debug("Astronmembers parsed courses: %s", courses)
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        result: Dict[str, Any] = {}
        for course in courses:
            course_url = course.get("url")
            if not course_url:
                continue

            structure = self._get_course_details(course_url)
            if not structure:
                continue

            logging.debug("Astronmembers course structure for %s: %s", course_url, structure)

            course_entry = {
                "id": course.get("id") or course.get("slug") or course.get("title"),
                "name": course.get("title", "Curso"),
                "slug": course.get("slug", course.get("title", "curso")),
                "modules": [],
            }

            for module_index, module in enumerate(structure.get("modules", []), start=1):
                module_entry = {
                    "id": module.get("id") or f"module-{module_index}",
                    "title": module.get("module_title", f"Módulo {module_index}"),
                    "order": module_index,
                    "lessons": [],
                    "locked": False,
                }

                for lesson_index, lesson in enumerate(module.get("lessons", []), start=1):
                    module_entry["lessons"].append(
                        {
                            "id": lesson.get("id") or f"lesson-{lesson_index}",
                            "title": lesson.get("title", f"Aula {lesson_index}"),
                            "url": lesson.get("url"),
                            "order": lesson_index,
                            "locked": lesson.get("is_completed", False),
                        }
                    )

                course_entry["modules"].append(module_entry)

            result[str(course_entry["id"])] = course_entry

        return result

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        lesson_url = lesson.get("url")
        if not lesson_url:
            raise ValueError("URL da aula não informada.")

        content_data = self._get_lesson_content(lesson_url)
        if not content_data:
            raise ValueError("Não foi possível obter o conteúdo da aula.")

        content = LessonContent()
        if description := content_data.get("description"):
            content.description = Description(text=description, description_type="text")

        player_url = content_data.get("player_url") or ""
        video_url = self._resolve_video_url(player_url, lesson_url)
        if video_url:
            content.videos.append(
                Video(
                    video_id=lesson.get("id") or lesson.get("title", "aula"),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": lesson_url}
                )
            )

        attachments = content_data.get("attachments") or []
        for index, attachment in enumerate(attachments, start=1):
            filename = attachment.get("name") or f"anexo_{index}"
            file_url = attachment.get("url")
            if not file_url:
                continue
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
        except Exception as exc:  # pragma: no cover - network dependent
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False

    def _parse_courses_from_html(self, html_content: str, base_url: str) -> List[Dict[str, str]]:
        soup = BeautifulSoup(html_content, "html.parser")
        all_courses: List[Dict[str, str]] = []
        processed_urls = set()

        for carousel in soup.find_all("div", class_="box-slider-cursos"):
            for link_tag in carousel.select("div.swiper-slide a[href]"):
                relative_url = link_tag["href"]
                if not relative_url.startswith("curso/"):
                    continue

                full_url = urljoin(base_url, relative_url)
                if full_url in processed_urls:
                    continue

                try:
                    slug = relative_url.split("/")[1]
                except IndexError:
                    continue

                title = slug.replace("-", " ").title()
                all_courses.append({"title": title, "url": full_url, "slug": slug, "id": slug})
                processed_urls.add(full_url)

        return all_courses

    def _get_course_details(self, course_url: str) -> Dict[str, Any]:
        initial_response = self._session.get(course_url, allow_redirects=False)
        initial_response.raise_for_status()

        if initial_response.is_redirect:
            redirect_url = initial_response.headers.get("location")
            final_response = self._session.get(redirect_url)
            final_response.raise_for_status()
        else:
            final_response = initial_response

        base_url = f"{urlparse(final_response.url).scheme}://{urlparse(final_response.url).netloc}"
        return self._parse_course_structure(final_response.text, base_url)

    def _parse_course_structure(self, html_content: str, base_url: str) -> Dict[str, Any]:
        soup = BeautifulSoup(html_content, "html.parser")
        course_container = soup.select_one("div.modulos.videos")
        if not course_container:
            return {}

        modules: List[Dict[str, Any]] = []
        for module_index, module_dl in enumerate(course_container.find_all("dl"), start=1):
            module_title_tag = module_dl.find("dt").find("h3") if module_dl.find("dt") else None
            if not module_title_tag:
                continue

            module_title = module_title_tag.text.strip()
            lessons: List[Dict[str, Any]] = []

            for lesson_item in module_dl.select("dd li.aulabox"):
                link_tag = lesson_item.find_parent("a")
                title_tag = lesson_item.find("h6")
                if not (link_tag and title_tag):
                    continue

                lessons.append(
                    {
                        "id": lesson_item.get("data-aulaid"),
                        "title": title_tag.text.strip(),
                        "url": urljoin(base_url, link_tag["href"]),
                        "is_completed": "concluida" in lesson_item.get("class", []),
                    }
                )

            modules.append(
                {
                    "id": f"module-{module_index}",
                    "module_title": module_title,
                    "lessons": lessons,
                }
            )

        return {"modules": modules}

    def _get_lesson_content(self, lesson_url: str) -> Optional[Dict[str, Any]]:
        response = self._session.get(lesson_url)
        response.raise_for_status()
        logging.debug("Astronmembers lesson page length for %s: %s", lesson_url, len(response.text))
        soup = BeautifulSoup(response.text, "html.parser")

        player_iframe = soup.select_one("iframe.streaming-video-url")
        player_url = player_iframe["src"] if player_iframe else None

        description_container = soup.select_one("div.aba-descricao")
        description = None
        if description_container:
            not_found_div = description_container.select_one("div.content-notfound")
            if not not_found_div:
                description = description_container.get_text(separator="\n", strip=True)

        attachments: List[Dict[str, str]] = []
        attachments_container = soup.select_one("div.aba-anexos")
        if attachments_container:
            for link in attachments_container.select("div.lista-anexos a"):
                name_tag = link.select_one("p")
                name = name_tag.get_text(strip=True) if name_tag else "Anexo sem nome"
                relative_url = link.get("href")
                if not relative_url:
                    continue
                absolute_url = urljoin(lesson_url, relative_url)
                attachments.append({"name": name, "url": absolute_url})

        return {"player_url": player_url, "description": description, "attachments": attachments}

    def _resolve_video_url(self, player_url: str, lesson_url: str) -> Optional[str]:
        if not player_url:
            return None

        lowered = player_url.lower()
        if "pandavideo" in lowered:
            return self._convert_panda_video_url(player_url)
        if "play.hotmart.com" in lowered:
            return self._get_hotmart_video_url(player_url, lesson_url)
        if "youtube.com" in lowered or "vimeo.com" in lowered:
            return player_url
        return player_url

    def _convert_panda_video_url(self, url: str) -> Optional[str]:
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)
        video_id = query_params.get("v", [None])[0]
        if not video_id:
            return None
        new_netloc = parsed_url.netloc.replace("player", "b", 1)
        new_path = f"/{video_id}/playlist.m3u8"
        return urlunparse((parsed_url.scheme, new_netloc, new_path, "", "", ""))

    def _get_hotmart_video_url(self, player_url: str, lesson_url: str) -> Optional[str]:
        headers = {
            "User-Agent": self._settings.user_agent,
            "Referer": lesson_url,
        }
        response = self._session.get(player_url, headers=headers, timeout=20)
        response.raise_for_status()
        logging.debug("Astronmembers Hotmart player response length: %s", len(response.text))
        soup = BeautifulSoup(response.text, "html.parser")
        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if not next_data_script:
            return None

        try:
            data = next_data_script.string or ""
            payload = json.loads(data)
        except Exception:
            return None

        media_assets = payload.get("props", {}).get("pageProps", {}).get("applicationData", {}).get("mediaAssets", [])
        hls_asset = next((asset for asset in media_assets if "m3u8" in asset.get("url", "")), None)
        if hls_asset:
            return hls_asset.get("url")
        if media_assets:
            return media_assets[0].get("url")
        return None


PlatformFactory.register_platform("Astronmembers", AstronmembersPlatform)
