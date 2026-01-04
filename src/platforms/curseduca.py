from __future__ import annotations

import json
import logging
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory

LOGIN_DISCOVERY_URL = "https://application.curseduca.pro/platform-by-url"
LOGIN_AUTH_URL = "https://prof.curseduca.pro/login?redirectUrl="
LESSON_WATCH_URL = "https://clas.curseduca.pro/bff/aulas/{lesson_uuid}/watch"


def _extract_next_data(html_content: str) -> Optional[Dict[str, Any]]:
    """Extracts the Next.js payload with course data from the HTML."""
    script_pattern = r"self\.__next_f\.push\(\[(.*?)\]\)"
    matches = re.findall(script_pattern, html_content, re.DOTALL)

    for match in matches:
        parts = match.split(",", 1)
        if len(parts) < 2:
            continue
        _, payload = parts
        payload = payload.strip()

        try:
            candidate = json.loads(payload)
        except Exception:
            continue

        if isinstance(candidate, str) and candidate.startswith("b:"):
            try:
                decoded = json.loads(candidate[2:])
            except Exception:
                continue

            if isinstance(decoded, list) and len(decoded) >= 4 and isinstance(decoded[3], dict):
                return decoded[3]

        if isinstance(candidate, dict):
            return candidate

    return None


def _simplify_course_structure(course_data: Dict[str, Any]) -> Dict[str, Any]:
    """Reduces the course payload to modules and lessons only."""
    simplified = {"title": "", "slug": "", "modules": []}

    content = course_data.get("content", {})
    inner_content = content.get("content", {}) if isinstance(content, dict) else {}
    simplified["title"] = inner_content.get("title", "Curso")
    simplified["slug"] = inner_content.get("slug", "curso")

    structure = inner_content.get("structure", [])
    for module_index, item in enumerate(structure, start=1):
        if not isinstance(item, dict) or item.get("type") != "MODULE":
            continue
        module_data = item.get("data", {})
        module_title = module_data.get("title", f"Módulo {module_index}")
        module = {
            "id": module_data.get("uuid") or module_data.get("id") or f"module-{module_index}",
            "title": module_title,
            "order": module_index,
            "lessons": [],
            "locked": False,
        }

        for lesson_index, lesson_item in enumerate(module_data.get("structure", []), start=1):
            if not isinstance(lesson_item, dict) or lesson_item.get("type") != "LESSON":
                continue
            lesson_data = lesson_item.get("data", {})
            lesson_title = lesson_data.get("title", f"Aula {lesson_index}")
            module["lessons"].append(
                {
                    "id": lesson_data.get("id") or lesson_data.get("uuid") or f"lesson-{lesson_index}",
                    "uuid": lesson_data.get("uuid") or lesson_data.get("id"),
                    "title": lesson_title,
                    "order": lesson_data.get("order", lesson_index),
                    "type": lesson_data.get("type"),
                    "locked": lesson_data.get("status") == "LOCKED",
                }
            )

        simplified["modules"].append(module)

    return simplified


class CurseducaPlatform(BasePlatform):
    """Implements the Curseduca whitelabel platform using the shared interface."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._base_url: str = ""
        self._api_key: str = ""
        self._access_token: str = ""
        self._tenant_slug: str = ""
        self._tenant_uuid: str = ""
        self._tenant_id: str = ""
        self._current_login_id: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="base_url",
                label="URL base da plataforma",
                placeholder="https://portal.suaescola.com.br",
            )
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 5.00) ativos podem informar usuário/senha. O sistema irá trocar essas credenciais automaticamente pelo token da etapa acima, além de usar alguns algoritmos melhores e ter funcionalidades extras na aplicação, e obter suporte prioritário. Usuários sem assinatura devem colar diretamente o token de sessão.

Para plataformas whitelabel Curseduca:
1) Informe a URL base do portal (ex.: https://portal.geoone.com.br).
2) Navegue até uma aula e (Instruções em construção, pelo momento, login apenas por credencial para assinantes).
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        base_url = (credentials.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("Informe a URL base da plataforma Curseduca.")

        self._base_url = base_url
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self._settings.user_agent})

        headers = {
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Origin": base_url,
            "Referer": f"{base_url}/",
        }

        api_key_response = self._session.get(LOGIN_DISCOVERY_URL, headers=headers)
        api_key_response.raise_for_status()
        discovery_payload = api_key_response.json()
        logging.debug("Curseduca discovery payload: %s", discovery_payload)
        api_key = discovery_payload.get("key")
        if not api_key:
            raise ValueError("Não foi possível obter a chave da plataforma.")

        self._api_key = api_key

        token = (credentials.get("token") or "").strip()
        if token:
            self._access_token = token
            self._tenant_slug = (credentials.get("tenant_slug") or "").strip()
            self._tenant_uuid = (credentials.get("tenant_uuid") or "").strip()
            self._tenant_id = str(credentials.get("tenant_id") or "")
            self._current_login_id = (credentials.get("current_login_id") or "").strip()
            self._configure_cookies(base_url)
            logging.info("Sessão autenticada na Curseduca via token.")
            return

        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()
        if not self._settings.has_full_permissions:
            raise ValueError(
                "Login com usuário e senha está disponível apenas para assinantes. Forneça um token válido da plataforma."
            )
        if not username or not password:
            raise ValueError("Usuário e senha são obrigatórios para Curseduca.")

        login_page = self._session.get(f"{base_url}/login")
        login_page.raise_for_status()

        auth_headers = headers | {"api_key": api_key, "Content-Type": "application/json"}
        login_response = self._session.post(
            LOGIN_AUTH_URL,
            headers=auth_headers,
            json={"username": username, "password": password},
        )
        login_response.raise_for_status()
        auth_data = login_response.json()
        logging.debug("Curseduca login response: %s", auth_data)

        self._access_token = auth_data.get("accessToken", "")
        member = auth_data.get("member", {})
        tenant = member.get("tenant", {})
        self._tenant_slug = tenant.get("slug", "")
        self._tenant_uuid = tenant.get("uuid", "")
        self._tenant_id = str(tenant.get("id", ""))
        self._current_login_id = auth_data.get("currentLoginId", "")

        self._configure_cookies(base_url)
        logging.info("Sessão autenticada na Curseduca.")

    def _configure_cookies(self, base_url: str) -> None:
        domain = urlparse(base_url).netloc
        cookie_domain = f".{domain}" if not domain.startswith(".") else domain
        self._session.cookies.set("access_token", self._access_token, domain=cookie_domain)
        self._session.cookies.set("api_key", self._api_key, domain=cookie_domain)
        self._session.cookies.set("tenant_slug", self._tenant_slug, domain=cookie_domain)
        self._session.cookies.set("tenant_uuid", self._tenant_uuid, domain=cookie_domain)
        self._session.cookies.set("tenantId", self._tenant_id, domain=cookie_domain)
        self._session.cookies.set("current_login_id", self._current_login_id, domain=cookie_domain)
        self._session.cookies.set("platform_url", base_url, domain=cookie_domain)
        self._session.cookies.set("language", "pt_BR", domain=cookie_domain)
        self._session.cookies.set("language_tenant", "1", domain=cookie_domain)

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session or not self._base_url:
            raise ConnectionError("A sessão não está autenticada.")

        courses: List[Dict[str, Any]] = []
        page = 1

        while True:
            response = self._session.get(
                f"{self._base_url}/restrita", params={"redirect": "0", "limit": "100", "page": str(page)}
            )
            response.raise_for_status()

            logging.debug("Curseduca course list page %s content length: %s", page, len(response.text))

            soup = BeautifulSoup(response.text, "html.parser")
            page_courses: List[Dict[str, Any]] = []
            for card in soup.select("div.classified"):
                anchor = card.select_one("a.font-size-h4")
                if not anchor or not anchor.get("href"):
                    continue
                name = anchor.get_text(strip=True)
                href = anchor["href"]
                full_url = urljoin(self._base_url, href)
                slug = PurePosixPath(urlparse(full_url).path).name or full_url
                page_courses.append({"id": slug, "name": name, "slug": slug, "url": full_url})

            if not page_courses:
                break

            courses.extend(page_courses)
            page += 1

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        result: Dict[str, Any] = {}
        for course in courses:
            course_url = course.get("url")
            if not course_url:
                continue

            response = self._session.get(course_url)
            response.raise_for_status()
            logging.debug("Curseduca course page fetched: %s", course_url)
            course_data = _extract_next_data(response.text)
            if not course_data:
                logging.warning("Não foi possível extrair dados para o curso %s", course.get("name"))
                continue

            logging.debug("Curseduca course payload for %s: %s", course.get("name"), course_data)

            simplified = _simplify_course_structure(course_data)
            course_entry = course.copy()
            course_entry["title"] = simplified.get("title") or course.get("name", "Curso")
            course_entry["modules"] = simplified.get("modules", [])
            result[str(course_entry.get("id"))] = course_entry

        return result

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        lesson_uuid = lesson.get("uuid") or lesson.get("id")
        if not lesson_uuid:
            raise ValueError("Aula sem UUID/ID informada.")

        headers = {"Authorization": f"Bearer {self._access_token}", "api_key": self._api_key, "Origin": self._base_url}
        response = self._session.get(LESSON_WATCH_URL.format(lesson_uuid=lesson_uuid), headers=headers)
        response.raise_for_status()
        lesson_json = response.json()
        logging.debug("Curseduca lesson %s details: %s", lesson_uuid, lesson_json)

        content = LessonContent()
        if description_html := lesson_json.get("description"):
            content.description = Description(text=description_html, description_type="html")

        video_type = lesson.get("type") or lesson_json.get("type")
        video_id = lesson_json.get("videoId")
        if video_id:
            video_url = None
            if video_type == 7:
                video_url = f"https://player.vimeo.com/video/{video_id}"
            elif video_type == 4:
                video_url = f"https://www.youtube.com/watch?v={video_id}"
            else:
                video_url = str(video_id)

            content.videos.append(
                Video(
                    video_id=str(video_id),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": self._base_url}
                )
            )

        complementaries = lesson_json.get("complementaries") or []
        for file_index, complementary in enumerate(complementaries, start=1):
            file_name = complementary.get("title") or f"anexo_{file_index}"
            file_url = (complementary.get("file") or {}).get("url")
            if not file_url:
                continue

            download_url = (
                "https://clas.curseduca.pro/lessons-complementaries/download"
                f"?fileName={file_name}&fileUrl={file_url}&api_key={self._api_key}"
            )
            extension = file_name.split(".")[-1] if "." in file_name else ""
            content.attachments.append(
                Attachment(
                    attachment_id=str(complementary.get("id", file_index)),
                    url=download_url,
                    filename=file_name,
                    order=file_index,
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
            with open(download_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    file_handle.write(chunk)
            return True
        except Exception as exc:  # pragma: no cover - network dependent
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Curseduca", CurseducaPlatform)
