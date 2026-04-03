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

    # Fallback: no MODULE refs found — look for course content in React component refs.
    # Some Curseduca pages have LESSons directly in the structure (no MODULE wrapper).
    for key, value in refs.items():
        if not isinstance(value, list) or len(value) < 4:
            continue
        if value[0] != "$" or not isinstance(value[3], dict):
            continue
        props = value[3]
        content = props.get("content")
        if not isinstance(content, dict):
            continue
        inner = content.get("content")
        if isinstance(inner, dict) and "structure" in inner:
            resolved = resolve_refs(inner)
            return {"content": {"content": resolved}}

    return None


def _collect_lessons(structure: Any) -> List[Dict[str, Any]]:
    """Recursively collect LESSON items from a structure, flattening nested MODULEs."""
    lessons: List[Dict[str, Any]] = []
    if not isinstance(structure, list):
        return lessons
    for item in structure:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "LESSON":
            lesson_data = item.get("data", {})
            if isinstance(lesson_data, str):
                continue  # Unresolved reference
            lesson_order = item.get("order") or lesson_data.get("order") or len(lessons) + 1
            metadata_me = lesson_data.get("metadata", {}).get("me", {})
            is_blocked = metadata_me.get("isBlocked", False) or metadata_me.get("isBlockedByAvailabilityDate", False)
            lessons.append(
                {
                    "id": str(lesson_data.get("id") or lesson_data.get("uuid") or f"lesson-{len(lessons)+1}"),
                    "uuid": lesson_data.get("uuid") or str(lesson_data.get("id")),
                    "title": lesson_data.get("title", f"Aula {len(lessons)+1}"),
                    "order": lesson_order,
                    "type": lesson_data.get("type"),
                    "locked": lesson_data.get("status") == "LOCKED" or is_blocked,
                }
            )
        elif item.get("type") == "MODULE":
            sub_data = item.get("data", {})
            if isinstance(sub_data, dict):
                lessons.extend(_collect_lessons(sub_data.get("structure", [])))
    return lessons


def _find_child_module_ids(modules: List[Dict[str, Any]]) -> set:
    """Return IDs of modules that are nested inside another module's structure."""
    child_ids: set = set()
    for item in modules:
        if not isinstance(item, dict) or item.get("type") != "MODULE":
            continue
        data = item.get("data", {})
        if not isinstance(data, dict):
            continue
        _collect_child_ids(data.get("structure", []), child_ids)
    return child_ids


def _collect_child_ids(structure: Any, child_ids: set) -> None:
    if not isinstance(structure, list):
        return
    for item in structure:
        if isinstance(item, dict) and item.get("type") == "MODULE":
            sub_data = item.get("data", {})
            if isinstance(sub_data, dict) and sub_data.get("id"):
                child_ids.add(sub_data["id"])
            _collect_child_ids(sub_data.get("structure", []) if isinstance(sub_data, dict) else [], child_ids)


def _simplify_course_structure(course_data: Dict[str, Any]) -> Dict[str, Any]:
    """Reduces the course payload to modules and lessons only."""
    simplified: Dict[str, Any] = {"title": "", "slug": "", "modules": []}

    # Handle new RSC format with direct modules list
    rsc_modules = course_data.get("modules", [])
    if rsc_modules:
        child_ids = _find_child_module_ids(rsc_modules)

        module_order = 0
        for item in rsc_modules:
            if not isinstance(item, dict) or item.get("type") != "MODULE":
                continue

            module_data = item.get("data", {})
            if isinstance(module_data, str):
                continue  # Unresolved reference

            # Skip child modules — their lessons are collected by the parent
            if module_data.get("id") in child_ids:
                continue

            module_order += 1
            lessons = _collect_lessons(module_data.get("structure", []))
            # Re-number lessons sequentially
            for i, lesson in enumerate(lessons, start=1):
                lesson["order"] = i

            simplified["modules"].append(
                {
                    "id": str(module_data.get("id") or module_data.get("uuid") or f"module-{module_order}"),
                    "title": module_data.get("title", f"Módulo {module_order}"),
                    "order": module_order,
                    "lessons": lessons,
                    "locked": False,
                }
            )
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
        lessons = _collect_lessons(module_data.get("structure", []))
        for i, lesson in enumerate(lessons, start=1):
            lesson["order"] = i

        simplified["modules"].append(
            {
                "id": str(module_data.get("uuid") or module_data.get("id") or f"module-{module_index}"),
                "title": module_data.get("title", f"Módulo {module_index}"),
                "order": module_index,
                "lessons": lessons,
                "locked": False,
            }
        )

    # Handle courses with LESSons directly in structure (no MODULE wrapper)
    if not simplified["modules"] and structure:
        top_lessons = _collect_lessons(structure)
        if top_lessons:
            for i, lesson in enumerate(top_lessons, start=1):
                lesson["order"] = i
            simplified["modules"].append(
                {
                    "id": str(inner_content.get("uuid") or inner_content.get("id") or "module-1"),
                    "title": inner_content.get("title", "Módulo 1"),
                    "order": 1,
                    "lessons": top_lessons,
                    "locked": False,
                }
            )

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
        self._platform_tenant_id: str = ""
        self._platform_tenant_slug: str = ""
        self._platform_tenant_uuid: str = ""
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
        raw_url = (credentials.get("base_url") or "").strip().rstrip("/")
        if not raw_url:
            raise ValueError("Informe a URL base da plataforma Curseduca.")

        parsed = urlparse(raw_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("URL inválida. Informe no formato https://portal.suaescola.com.br")
        base_url = f"{parsed.scheme}://{parsed.netloc}"

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

        # Fetch login page — also updates base_url to HTTPS if redirected
        platform_tenant_uuid = None
        try:
            login_page = self._session.get(f"{base_url}/login", timeout=30)
            login_page.raise_for_status()
            # Update base_url if the site redirected (e.g. http → https)
            final_parsed = urlparse(login_page.url)
            final_base = f"{final_parsed.scheme}://{final_parsed.netloc}"
            if final_base != base_url:
                logging.info("Curseduca: base_url updated from %s to %s after redirect", base_url, final_base)
                base_url = final_base
                self._base_url = base_url
                headers["Origin"] = base_url
                headers["Referer"] = f"{base_url}/"
            # Try to extract platform tenant UUID from HTML data-tenant attribute
            match = re.search(r'data-tenant="([^"]+)"', login_page.text)
            if match:
                platform_tenant_uuid = match.group(1)
                logging.info("Curseduca platform tenant UUID from HTML: %s", platform_tenant_uuid)
        except Exception as exc:
            logging.debug("Curseduca: could not fetch login page: %s", exc)

        token = (credentials.get("token") or "").strip()
        if token:
            self._access_token = token
            self._tenant_slug = (credentials.get("tenant_slug") or "").strip()
            self._tenant_uuid = (credentials.get("tenant_uuid") or "").strip()
            self._tenant_id = str(credentials.get("tenant_id") or "")
            self._current_login_id = (credentials.get("current_login_id") or "").strip()

            self._resolve_token_context(headers)

            if platform_tenant_uuid:
                self._resolve_platform_tenant(platform_tenant_uuid)

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
        self._current_login_id = str(auth_data.get("currentLoginId", ""))
        self._auth_id = str(auth_data.get("authenticationId", ""))
        self._member_data = {
            "id": member.get("id"),
            "name": member.get("name", ""),
            "email": member.get("email", ""),
            "image": member.get("image"),
            "isAdmin": member.get("isAdmin", False),
        }

        # If the login response didn't include tenant info, resolve from API
        if not self._tenant_uuid:
            logging.info("Curseduca: tenant info missing from login response, resolving from API...")
            self._resolve_token_context(headers)

        if platform_tenant_uuid:
            self._resolve_platform_tenant(platform_tenant_uuid)

        self._configure_cookies(base_url)
        logging.info("Sessão autenticada na Curseduca.")

    def _resolve_platform_tenant(self, platform_tenant_uuid: str) -> None:
        """Resolve the platform's tenant numeric ID from its UUID via /by/tenants."""
        try:
            auth_headers = {
                "Authorization": f"Bearer {self._access_token}",
                "api_key": self._api_key,
                "Origin": self._base_url,
                "Referer": f"{self._base_url}/",
            }
            resp = self._session.get(
                "https://application.curseduca.pro/by/tenants",
                headers=auth_headers,
                params={"id": "", "slug": "", "uuid": platform_tenant_uuid},
                timeout=30,
            )
            resp.raise_for_status()
            tenant_data = resp.json()
            self._platform_tenant_id = str(tenant_data.get("id", ""))
            self._platform_tenant_uuid = platform_tenant_uuid
            self._platform_tenant_slug = tenant_data.get("slug", "")
            logging.info(
                "Curseduca platform tenant resolved: id=%s, slug=%s, uuid=%s",
                self._platform_tenant_id, self._platform_tenant_slug, self._platform_tenant_uuid,
            )
        except Exception as exc:
            logging.warning("Curseduca: failed to resolve platform tenant: %s", exc)

    def _resolve_token_context(self, base_headers: Dict[str, str]) -> None:
        """Populate tenant and member data from API when authenticating via token."""
        auth_headers = base_headers | {
            "Authorization": f"Bearer {self._access_token}",
            "api_key": self._api_key,
        }

        # Fetch user profile for the 'user' cookie
        if not self._member_data:
            try:
                me_resp = self._session.get(
                    "https://prof.curseduca.pro/me",
                    headers=auth_headers, params={"skipCache": "true"}, timeout=30,
                )
                me_resp.raise_for_status()
                me = me_resp.json()
                self._member_data = {
                    "id": me.get("id"),
                    "name": me.get("name", ""),
                    "email": me.get("email", ""),
                    "image": me.get("image"),
                    "isAdmin": False,
                }
                self._auth_id = str(me.get("id", ""))
                logging.debug("Curseduca /me profile resolved: id=%s", me.get("id"))
            except Exception as exc:
                logging.warning("Curseduca: falha ao buscar perfil /me: %s", exc)

        # Resolve tenant info from groups if not already provided
        if self._tenant_uuid:
            return

        try:
            groups_resp = self._session.get(
                "https://prof.curseduca.pro/me/groups",
                headers=auth_headers, timeout=30,
            )
            groups_resp.raise_for_status()
            groups_data = groups_resp.json()

            tenant_uuid = ""
            tenant_id = ""
            for group in groups_data.get("groups", []):
                for tenant in group.get("tenants", []):
                    if tenant.get("uuid"):
                        tenant_uuid = tenant["uuid"]
                        tenant_id = str(tenant.get("id", ""))
                        break
                if tenant_uuid:
                    break

            if not tenant_uuid:
                logging.warning("Curseduca: nenhum tenant encontrado nos grupos do usuário")
                return

            self._tenant_uuid = tenant_uuid
            self._tenant_id = tenant_id

            # Resolve tenant slug via /by/tenants
            tenants_resp = self._session.get(
                "https://application.curseduca.pro/by/tenants",
                headers=auth_headers,
                params={"id": "", "slug": "", "uuid": tenant_uuid},
                timeout=30,
            )
            tenants_resp.raise_for_status()
            tenant_data = tenants_resp.json()
            self._tenant_slug = tenant_data.get("slug", "")
            logging.info(
                "Curseduca tenant resolved: slug=%s, uuid=%s, id=%s",
                self._tenant_slug, self._tenant_uuid, self._tenant_id,
            )
        except Exception as exc:
            logging.warning("Curseduca: falha ao resolver tenant: %s", exc)

    def _configure_cookies(self, base_url: str) -> None:
        domain = urlparse(base_url).netloc
        # Set cookies with explicit domain and path to ensure they're sent correctly
        cookie_params = {"domain": domain, "path": "/"}
        # Prefer platform tenant over member tenant for cookies (they may differ)
        effective_tenant_id = self._platform_tenant_id or self._tenant_id
        effective_tenant_slug = self._platform_tenant_slug or self._tenant_slug
        effective_tenant_uuid = self._platform_tenant_uuid or self._tenant_uuid
        self._session.cookies.set("access_token", self._access_token, **cookie_params)
        self._session.cookies.set("api_key", self._api_key, **cookie_params)
        self._session.cookies.set("tenant_slug", effective_tenant_slug, **cookie_params)
        self._session.cookies.set("tenant_uuid", effective_tenant_uuid, **cookie_params)
        self._session.cookies.set("tenantId", effective_tenant_id, **cookie_params)
        self._session.cookies.set("current_login_id", self._current_login_id, **cookie_params)
        self._session.cookies.set("platform_url", base_url, **cookie_params)
        self._session.cookies.set("language", "pt_BR", **cookie_params)
        self._session.cookies.set("language_tenant", effective_tenant_id or "1", **cookie_params)

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
            "x-platform": "web",
        }
        if self._platform_tenant_id or self._tenant_id:
            headers["x-tenant-id"] = self._platform_tenant_id or self._tenant_id

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
            "x-platform": "web",
        }
        if self._platform_tenant_id or self._tenant_id:
            headers["x-tenant-id"] = self._platform_tenant_id or self._tenant_id

        result: Dict[str, Any] = {}
        for course in courses:
            course_url = course.get("url")
            if not course_url:
                continue

            logging.debug("Curseduca cookies before request: %s", {c.name: c.value for c in self._session.cookies})
            response = self._session.get(course_url, headers=headers)
            logging.info("Curseduca course page response: status=%s, url=%s, final_url=%s",
                        response.status_code, course_url, response.url)
            if response.url != course_url:
                logging.warning("Curseduca course page was redirected - cookies may not be working")
            response.raise_for_status()

            # Extract platform tenant from course page if not yet resolved
            if not self._platform_tenant_id:
                tenant_match = re.search(r'data-tenant="([^"]+)"', response.text)
                if tenant_match:
                    pt_uuid = tenant_match.group(1)
                    # Only resolve if it differs from the member tenant
                    if pt_uuid != self._tenant_uuid:
                        logging.info("Curseduca platform tenant UUID from course page: %s", pt_uuid)
                        self._resolve_platform_tenant(pt_uuid)

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

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "api_key": self._api_key,
            "Content-Type": "application/json",
            "Origin": self._base_url,
            "Referer": f"{self._base_url}/",
            "x-platform": "web",
        }
        if self._platform_tenant_id or self._tenant_id:
            headers["x-tenant-id"] = self._platform_tenant_id or self._tenant_id
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
            elif video_type == 11:
                # PandaVideo — videoId is already the HLS playlist URL
                video_url = video_id
            elif video_type == 20:
                # Curseduca native player — resolve HLS URL from player API
                player_resp = self._session.get(
                    f"https://player.curseduca.pro/videos/{video_id}",
                    params={"tenant": self._platform_tenant_uuid or self._tenant_uuid, "api_key": self._api_key},
                )
                player_resp.raise_for_status()
                player_data = player_resp.json()
                video_url = (
                    player_data.get("watch", {}).get("hls")
                    or player_data.get("watch", {}).get("embed")
                    or f"https://player.curseduca.com/embed/{video_id}?api_key={self._api_key}"
                )
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
            "x-platform": "web",
        }
        if self._platform_tenant_id or self._tenant_id:
            headers["x-tenant-id"] = self._platform_tenant_id or self._tenant_id

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
