from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory

LOGIN_DISCOVERY_URL = "https://application.curseduca.pro/platform-by-url"
LOGIN_AUTH_URL = "https://prof.curseduca.pro/login?redirectUrl="
COURSES_ACCESS_URL = "https://prof.curseduca.pro/me/access"
LESSON_WATCH_URL = "https://clas.curseduca.pro/bff/aulas/{lesson_uuid}/watch"


def _extract_next_data(html_content: str) -> Optional[Dict[str, Any]]:
    """Extracts the Next.js RSC payload with course data from the HTML."""
    # Find all __next_f.push calls and extract the RSC data lines
    script_pattern = r"self\.__next_f\.push\(\[1,\"(.*?)\"\]\)"
    matches = re.findall(script_pattern, html_content, re.DOTALL)

    # Concatenate all RSC data and parse key:value pairs
    rsc_data = "".join(matches)
    # Unescape the JSON string escapes
    rsc_data = rsc_data.replace("\\n", "\n").replace("\\\"", '"').replace("\\\\", "\\")

    # Parse RSC format: each line is like "key:{json}" or "key:[json]"
    refs: Dict[str, Any] = {}
    for line in rsc_data.split("\n"):
        line = line.strip()
        if not line or ":" not in line:
            continue
        # Split on first colon to get key and value
        colon_idx = line.find(":")
        if colon_idx < 1:
            continue
        key = line[:colon_idx]
        value_str = line[colon_idx + 1:]
        try:
            refs[key] = json.loads(value_str)
        except json.JSONDecodeError:
            continue

    def resolve_refs(obj: Any, depth: int = 0) -> Any:
        """Recursively resolve $XX references."""
        if depth > 50:
            return obj
        if isinstance(obj, str) and obj.startswith("$"):
            ref_key = obj[1:]
            if ref_key in refs:
                return resolve_refs(refs[ref_key], depth + 1)
            return obj
        if isinstance(obj, dict):
            return {k: resolve_refs(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [resolve_refs(item, depth + 1) for item in obj]
        return obj

    # Find the content/course structure - look for MODULE type objects
    modules = []
    for key, value in refs.items():
        if isinstance(value, dict) and value.get("type") == "MODULE":
            resolved = resolve_refs(value)
            modules.append(resolved)

    if modules:
        return {"modules": modules, "refs": refs}

    return None


def _simplify_course_structure(course_data: Dict[str, Any]) -> Dict[str, Any]:
    """Reduces the course payload to modules and lessons only."""
    simplified = {"title": "", "slug": "", "modules": []}

    # Handle new RSC format with direct modules list
    rsc_modules = course_data.get("modules", [])
    if rsc_modules:
        for module_index, item in enumerate(rsc_modules, start=1):
            if not isinstance(item, dict) or item.get("type") != "MODULE":
                continue

            module_data = item.get("data", {})
            if isinstance(module_data, str):
                continue  # Unresolved reference

            module_title = module_data.get("title", f"Módulo {module_index}")
            module = {
                "id": str(module_data.get("id") or module_data.get("uuid") or f"module-{module_index}"),
                "title": module_title,
                "order": module_index,
                "lessons": [],
                "locked": False,
            }

            structure = module_data.get("structure", [])
            if isinstance(structure, list):
                for lesson_index, lesson_item in enumerate(structure, start=1):
                    if not isinstance(lesson_item, dict) or lesson_item.get("type") != "LESSON":
                        continue

                    lesson_data = lesson_item.get("data", {})
                    if isinstance(lesson_data, str):
                        continue  # Unresolved reference

                    lesson_title = lesson_data.get("title", f"Aula {lesson_index}")
                    lesson_order = lesson_item.get("order") or lesson_data.get("order") or lesson_index
                    module["lessons"].append(
                        {
                            "id": str(lesson_data.get("id") or lesson_data.get("uuid") or f"lesson-{lesson_index}"),
                            "uuid": lesson_data.get("uuid") or str(lesson_data.get("id")),
                            "title": lesson_title,
                            "order": lesson_order,
                            "type": lesson_data.get("type"),
                            "locked": lesson_data.get("status") == "LOCKED",
                        }
                    )

            simplified["modules"].append(module)
        return simplified

    # Fallback: handle old format with nested content structure
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
            "id": str(module_data.get("uuid") or module_data.get("id") or f"module-{module_index}"),
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
                    "id": str(lesson_data.get("id") or lesson_data.get("uuid") or f"lesson-{lesson_index}"),
                    "uuid": lesson_data.get("uuid") or str(lesson_data.get("id")),
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
        self._auth_id: str = ""
        self._member_data: Dict[str, Any] = {}

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
Assinantes (R$ 9.90) ativos podem informar usuário/senha. O sistema irá trocar essas credenciais automaticamente pelo token da etapa acima, além de usar alguns algoritmos melhores e ter funcionalidades extras na aplicação, e obter suporte prioritário. Usuários sem assinatura devem colar diretamente o token de sessão.

Para plataformas whitelabel Curseduca:
1) Informe a URL base do portal (ex.: https://portal.geoone.com.br).
2) Navegue até uma aula e (Instruções em construção, pelo momento, login apenas por credencial para assinantes).
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
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
        self._auth_id = str(auth_data.get("authenticationId", ""))
        self._member_data = member

        self._configure_cookies(base_url)
        logging.info("Sessão autenticada na Curseduca.")

    def _configure_cookies(self, base_url: str) -> None:
        domain = urlparse(base_url).netloc
        # Set cookies with explicit domain and path to ensure they're sent correctly
        cookie_params = {"domain": domain, "path": "/"}
        self._session.cookies.set("access_token", self._access_token, **cookie_params)
        self._session.cookies.set("api_key", self._api_key, **cookie_params)
        self._session.cookies.set("tenant_slug", self._tenant_slug, **cookie_params)
        self._session.cookies.set("tenant_uuid", self._tenant_uuid, **cookie_params)
        self._session.cookies.set("tenantId", self._tenant_id, **cookie_params)
        self._session.cookies.set("current_login_id", self._current_login_id, **cookie_params)
        self._session.cookies.set("platform_url", base_url, **cookie_params)
        self._session.cookies.set("language", "pt_BR", **cookie_params)
        self._session.cookies.set("language_tenant", self._tenant_id or "1", **cookie_params)

        # Build and set the user cookie (required for page authentication)
        if self._member_data:
            user_cookie = {
                "id_prof_profile": self._member_data.get("id"),
                "nm_name": self._member_data.get("name", ""),
                "id_prof_authentication": int(self._auth_id) if self._auth_id else None,
                "im_image": self._member_data.get("image"),
                "nm_email": self._member_data.get("email", ""),
                "tenant_uuid": self._tenant_uuid,
                "is_admin": self._member_data.get("isAdmin", False),
            }
            self._session.cookies.set("user", json.dumps(user_cookie), **cookie_params)

        logging.info("Curseduca cookies configured for domain %s: %s", domain, list(self._session.cookies.keys()))

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session or not self._base_url:
            raise ConnectionError("A sessão não está autenticada.")

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "api_key": self._api_key,
            "Content-Type": "application/json",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/",
        }

        logging.info("Curseduca fetching courses from API: %s", COURSES_ACCESS_URL)
        response = self._session.get(COURSES_ACCESS_URL, headers=headers, params={"slug": ""})
        logging.info("Curseduca API response status: %s", response.status_code)
        response.raise_for_status()

        try:
            data = response.json()
        except Exception as e:
            logging.error("Curseduca failed to parse JSON response: %s", e)
            logging.error("Response text (first 500 chars): %s", response.text[:500])
            raise

        logging.info("Curseduca courses API response size: %s bytes", len(response.text))

        courses: List[Dict[str, Any]] = []
        access_list = data.get("access", [])
        logging.info("Curseduca found %s courses in API response", len(access_list))

        for item in access_list:
            course_id = item.get("id")
            title = item.get("title", "")
            slug = item.get("slug", "")
            if not course_id or not slug:
                continue

            course_url = f"{self._base_url}/m/lessons/{slug}"
            courses.append({
                "id": str(course_id),
                "name": title,
                "slug": slug,
                "url": course_url,
            })

        logging.info("Curseduca returning %s courses to UI", len(courses))
        if courses:
            logging.info("First course: %s", courses[0])
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        # Headers needed for authenticated page requests
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "api_key": self._api_key,
        }

        result: Dict[str, Any] = {}
        for course in courses:
            course_url = course.get("url")
            if not course_url:
                continue

            logging.debug("Curseduca cookies before request: %s", dict(self._session.cookies))
            response = self._session.get(course_url, headers=headers)
            logging.info("Curseduca course page response: status=%s, url=%s, final_url=%s",
                        response.status_code, course_url, response.url)
            if response.url != course_url:
                logging.warning("Curseduca course page was redirected - cookies may not be working")
            response.raise_for_status()
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
            if video_type == 7:
                video_url = f"https://player.vimeo.com/video/{video_id}"
            elif video_type == 4:
                video_url = f"https://www.youtube.com/watch?v={video_id}"
            else:
                # Type 22 and others: ScaleUp/SmartPlayer (Curseduca native)
                video_url = f"https://player.scaleup.com.br/embed/{video_id}"

            content.videos.append(
                Video(
                    video_id=str(video_id),
                    url=video_url,
                    order=lesson.get("order", 1),
                    title=lesson.get("title", "Aula"),
                    size=0,
                    duration=0,
                    extra_props={"referer": f"{self._base_url}/"}
                )
            )

        # Handle type 2 lessons (PDFs/Slides/Materials) where content is in filePath
        file_path = lesson_json.get("filePath") or ""
        if not video_id and file_path.startswith("https://media.curseduca.pro/pdf"):
            lesson_title = lesson.get("title") or lesson_json.get("title") or "documento"
            # Clean up the title for use as filename
            file_name = f"{lesson_title}.pdf"
            content.attachments.append(
                Attachment(
                    attachment_id=str(lesson_json.get("id", "pdf")),
                    url=file_path,
                    filename=file_name,
                    order=1,
                    extension="pdf",
                    size=0,
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
                f"?fileName={quote(file_name, safe='')}&fileUrl={quote(file_url, safe='')}&api_key={self._api_key}"
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

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "api_key": self._api_key,
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/",
        }

        try:
            response = self._session.get(attachment.url, headers=headers, stream=True, timeout=60)
            response.raise_for_status()
            with open(download_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    file_handle.write(chunk)
            return True
        except Exception as exc:  # pragma: no cover - network dependent
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Curseduca", CurseducaPlatform)
