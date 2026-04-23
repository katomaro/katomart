from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        self._version: int = 4

    _SESSION_COOKIE_PATTERN = re.compile(r"^app_v([1-6])_session$")

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
3) Identifique o cookie de sessao (ex: app_v1_session ate app_v6_session).
   A versao da Cademi e inferida pelo numero no nome do cookie (v4, v6, etc.).
4) Cole no campo de token no formato: cookie_name:value
   Exemplo v4: app_v4_session:SEU_VALOR_AQUI
   Exemplo v6: app_v6_session:SEU_VALOR_AQUI

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

    def _parse_session_token(self, token: str) -> Tuple[str, str]:
        """Parses token input in cookie_name:value format for Cademi session cookies."""
        raw = (token or "").strip()
        if not raw:
            raise ValueError("Token de sessao invalido. Informe no formato cookie_name:value.")

        # Accept common copy/paste variants:
        # - app_v4_session:VALUE
        # - app_v4_session=VALUE
        # - Cookie: foo=1; app_v4_session=VALUE; bar=2
        cookie_header_matches = re.findall(r"(app_v[1-6]_session)\s*=\s*([^;\s]+)", raw)
        if cookie_header_matches:
            cookie_name, cookie_value = cookie_header_matches[-1]
            return cookie_name, cookie_value

        match = re.match(r"^(app_v[1-6]_session)\s*[:=]\s*(.+)$", raw)
        if match:
            cookie_name = match.group(1).strip()
            cookie_value = match.group(2).strip()
            if cookie_value:
                return cookie_name, cookie_value

        raise ValueError(
            "Token Cademi invalido. Use o formato cookie_name:value, por exemplo "
            "app_v4_session:SEU_VALOR."
        )

    def _extract_cademi_session_cookie(self, session: requests.Session) -> Optional[Tuple[str, str]]:
        for cookie in session.cookies:
            if self._SESSION_COOKIE_PATTERN.match(cookie.name) and cookie.value:
                return cookie.name, cookie.value
        return None

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

        session_cookie = self._extract_cademi_session_cookie(session)
        if not session_cookie:
            raise ConnectionError(
                "Login realizado mas cookie de sessao Cademi (app_v1_session ate app_v6_session) nao encontrado."
            )

        cookie_name, cookie_value = session_cookie

        self._login_session = session
        return f"{cookie_name}:{cookie_value}"

    def _configure_session(self, token: str) -> None:
        cookie_name, cookie_value = self._parse_session_token(token)

        version_match = self._SESSION_COOKIE_PATTERN.match(cookie_name)
        self._version = int(version_match.group(1)) if version_match else 4

        if hasattr(self, "_login_session") and self._login_session:
            self._session = self._login_session
            self._login_session = None
            live_cookie = self._extract_cademi_session_cookie(self._session)
            if live_cookie:
                live_match = self._SESSION_COOKIE_PATTERN.match(live_cookie[0])
                if live_match:
                    self._version = int(live_match.group(1))
        else:
            self._session = requests.Session()
            self._session.cookies.set(cookie_name, cookie_value)

        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        probe_path = "/area/vitrine/home" if self._version >= 6 else "/area/vitrine"
        resp = self._session.get(f"{self._site_url}{probe_path}", timeout=30, allow_redirects=True)
        if "/auth/login" in resp.url:
            raise ConnectionError(
                "Falha ao autenticar na Cademi. Verifique o token no formato cookie_name:value "
                "(ex: app_v4_session:SEU_VALOR ou app_v6_session:SEU_VALOR)."
            )
        logger.info("Cademi v%d: authenticated successfully", self._version)

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")
        if self._version >= 6:
            return self._fetch_courses_v6()
        return self._fetch_courses_v4()

    def _fetch_courses_v4(self) -> List[Dict[str, Any]]:
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

        logger.debug("Cademi v4: found %d courses", len(courses))
        return sorted(courses.values(), key=lambda c: c.get("name", ""))

    def _fetch_courses_v6(self) -> List[Dict[str, Any]]:
        max_vitrines = 50
        to_visit: List[str] = ["/area/vitrine/home"]
        visited_vitrines: set = set()
        discovered: Dict[str, str] = {}

        while to_visit and len(visited_vitrines) < max_vitrines:
            path = to_visit.pop(0)
            if path in visited_vitrines:
                continue
            visited_vitrines.add(path)

            try:
                resp = self._session.get(f"{self._site_url}{path}", timeout=30, allow_redirects=True)
                if not resp.ok:
                    logger.warning("Cademi v6: vitrine %s returned %s", path, resp.status_code)
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
            except Exception as exc:
                logger.warning("Cademi v6: failed to traverse %s: %s", path, exc)
                continue

            for link in soup.find_all("a", href=re.compile(r"/area/conteudo/produto/\d+")):
                match = re.search(r"/area/conteudo/produto/(\d+)", link.get("href", ""))
                if not match:
                    continue
                pid = match.group(1)
                if pid in discovered:
                    continue
                img = link.find("img")
                name = (img.get("alt", "").strip() if img else "") or ""
                discovered[pid] = name

            for link in soup.find_all("a", href=re.compile(r"/area/vitrine/\d+")):
                href = link.get("href", "")
                normalized = href.split("?", 1)[0].split("#", 1)[0]
                if not re.match(r"^/area/vitrine/\d+$", normalized):
                    continue
                if normalized in visited_vitrines or normalized in to_visit:
                    continue
                to_visit.append(normalized)

            time.sleep(0.2)

        courses: List[Dict[str, Any]] = []
        for pid, name in discovered.items():
            if not name:
                name = self._resolve_course_name_v6(pid) or f"Curso {pid}"
                time.sleep(0.3)
            courses.append({
                "id": pid,
                "name": name,
                "slug": pid,
                "seller_name": "",
            })

        logger.debug(
            "Cademi v6: found %d courses across %d vitrines", len(courses), len(visited_vitrines)
        )
        return sorted(courses, key=lambda c: c.get("name", ""))

    def _resolve_course_name_v6(self, product_id: str) -> str:
        try:
            resp = self._session.get(
                f"{self._site_url}/area/conteudo/produto/{product_id}",
                timeout=30,
                allow_redirects=True,
            )
            if not resp.ok:
                return ""
            name = self._extract_course_title_v6(resp.text, product_id)
            if name:
                return name
            soup = BeautifulSoup(resp.text, "html.parser")
            title_el = soup.find("title")
            if title_el:
                text = title_el.get_text(strip=True)
                if text:
                    return text
        except Exception as exc:
            logger.warning("Cademi v6: could not resolve name for course %s: %s", product_id, exc)
        return ""

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
        if self._version >= 6:
            return self._fetch_course_content_v6(courses)
        return self._fetch_course_content_v4(courses)

    def _fetch_course_content_v4(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            logger.debug("Cademi v4: fetching content for course %s", course_id)

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
                logger.error("Cademi v4: failed to fetch course %s: %s", course_id, exc)
                continue

            course_entry = course.copy()
            course_entry["title"] = self._extract_page_title(html) or course.get("name", "Curso")
            course_entry["modules"] = modules
            all_content[str(course_id)] = course_entry

            time.sleep(0.3)

        return all_content

    def _fetch_course_content_v6(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            logger.debug("Cademi v6: fetching content for course %s", course_id)

            modules_by_id: Dict[str, Dict[str, Any]] = {}
            lesson_html = ""

            try:
                modulo_ids = self._discover_modules_from_listagem(course_id)

                if modulo_ids:
                    for mid in modulo_ids:
                        if mid in modules_by_id:
                            continue
                        try:
                            mod_resp = self._session.get(
                                f"{self._site_url}/area/conteudo/modulo/{mid}",
                                timeout=30,
                                allow_redirects=True,
                            )
                            mod_resp.raise_for_status()
                        except Exception as exc:
                            logger.warning("Cademi v6: modulo %s failed: %s", mid, exc)
                            continue

                        html = mod_resp.text
                        if not lesson_html and "/area/conteudo/aula/" in mod_resp.url:
                            lesson_html = html
                        for m in self._parse_sidebar_v6(html):
                            if m["id"] not in modules_by_id:
                                modules_by_id[m["id"]] = m
                        time.sleep(0.2)
                else:
                    entry_resp = self._session.get(
                        f"{self._site_url}/area/conteudo/produto/{course_id}",
                        timeout=30,
                        allow_redirects=True,
                    )
                    entry_resp.raise_for_status()
                    final_url = entry_resp.url
                    entry_html = entry_resp.text

                    if "/area/conteudo/aula/" in final_url:
                        for m in self._parse_sidebar_v6(entry_html):
                            if m["id"] not in modules_by_id:
                                modules_by_id[m["id"]] = m
                        lesson_html = entry_html
                    else:
                        entry_soup = BeautifulSoup(entry_html, "html.parser")
                        hop = (
                            entry_soup.find("a", href=re.compile(r"/area/conteudo/modulo/\d+"))
                            or entry_soup.find("a", href=re.compile(r"/area/conteudo/aula/\d+"))
                        )
                        if hop:
                            hop_href = hop.get("href", "")
                            if not hop_href.startswith("http"):
                                hop_href = f"{self._site_url}{hop_href}"
                            lesson_resp = self._session.get(hop_href, timeout=30, allow_redirects=True)
                            lesson_resp.raise_for_status()
                            lesson_html = lesson_resp.text
                            for m in self._parse_sidebar_v6(lesson_html):
                                if m["id"] not in modules_by_id:
                                    modules_by_id[m["id"]] = m
                        else:
                            lesson_html = entry_html

            except Exception as exc:
                logger.error("Cademi v6: failed to fetch course %s: %s", course_id, exc)
                continue

            modules = list(modules_by_id.values())
            for idx, m in enumerate(modules, start=1):
                m["order"] = idx

            course_entry = course.copy()
            course_entry["title"] = (
                self._extract_course_title_v6(lesson_html, course_id)
                or self._extract_page_title(lesson_html)
                or course.get("name", "Curso")
            )
            course_entry["modules"] = modules
            all_content[str(course_id)] = course_entry

            time.sleep(0.3)

        return all_content

    def _discover_modules_from_listagem(self, course_id: Any) -> List[str]:
        try:
            resp = self._session.get(
                f"{self._site_url}/area/conteudo/listagem/{course_id}",
                timeout=30,
                allow_redirects=True,
            )
        except Exception as exc:
            logger.debug("Cademi v6: listagem %s unreachable: %s", course_id, exc)
            return []

        if not resp.ok or "/area/conteudo/listagem/" not in resp.url:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        ordered: List[str] = []
        seen: set = set()
        for a in soup.find_all("a", href=re.compile(r"/area/conteudo/modulo/\d+")):
            match = re.search(r"/area/conteudo/modulo/(\d+)", a.get("href", ""))
            if not match:
                continue
            mid = match.group(1)
            if mid in seen:
                continue
            seen.add(mid)
            ordered.append(mid)
        return ordered

    def _extract_course_title_v6(self, html: str, course_id: Any) -> str:
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        breadcrumb = soup.find(class_="breadcrumb")
        if breadcrumb:
            target = breadcrumb.find(
                "a",
                href=re.compile(rf"/area/conteudo/produto/{re.escape(str(course_id))}$"),
            )
            if target:
                text = target.get_text(strip=True)
                if text:
                    return text
            links = breadcrumb.find_all("a")
            if links:
                last = links[-1].get_text(strip=True)
                if last and last.lower() != "inicio":
                    return last
        return ""

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

    def _parse_sidebar_v6(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        sections_container = soup.find("div", id="sections") or soup.find("div", class_="sections")
        if not sections_container:
            return []

        modules: List[Dict[str, Any]] = []

        for group in sections_container.find_all("div", class_="section-group"):
            if "progresso-total" in (group.get("class") or []):
                continue

            items_container = group.find("div", class_="section-items")
            if items_container:
                inner_items = items_container.find_all("div", class_="section-item")
                if inner_items and all(
                    "section-next" in (item.get("class") or []) for item in inner_items
                ):
                    continue

            mod_order = len(modules) + 1
            title_div = group.find("div", class_=re.compile("section-group-titulo"))
            section_id = group.get("data-acesso-secao-id") or str(mod_order)
            module_title = ""

            if title_div:
                data_target = title_div.get("data-target", "") or ""
                id_match = re.match(r"^#s(\d+)$", data_target)
                if id_match:
                    section_id = id_match.group(1)
                else:
                    section_id = title_div.get("data-secao-id", section_id)
                titulo_el = title_div.find("div", class_="item-titulo")
                if titulo_el:
                    raw_text = " ".join(titulo_el.get_text(separator=" ", strip=True).split())
                    module_title = re.sub(r"\s*\d+\s*aulas?\s*$", "", raw_text).strip() or raw_text

            if not module_title:
                is_single = bool(items_container) and "single" in (items_container.get("class") or [])
                module_title = "Conteudo" if is_single else f"Modulo {mod_order}"
            lessons: List[Dict[str, Any]] = []

            if items_container:
                for les_idx, link in enumerate(
                    items_container.find_all("a", href=re.compile(r"/area/conteudo/aula/\d+")), start=1
                ):
                    inner = link.find("div", class_="section-item")
                    if inner and "section-next" in (inner.get("class") or []):
                        continue

                    href = link.get("href", "")
                    item_match = re.search(r"/area/conteudo/aula/(\d+)", href)
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
        if self._version >= 6:
            lesson_url = f"{self._site_url}/area/conteudo/aula/{item_id}"
        else:
            lesson_url = f"{self._site_url}/area/produto/item/{item_id}"

        resp = self._session.get(lesson_url, timeout=30)
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

        video_div = soup.find("div", class_=re.compile(r"video-(pandavideo|vturb)"))
        if video_div:
            iframe = video_div.find("iframe")
            if iframe and iframe.get("src"):
                video_url = iframe.get("src")
                data_id = video_div.get("data-id", "")
                classes = " ".join(video_div.get("class") or [])
                if "video-vturb" in classes or "converteai" in video_url:
                    resolved = self._resolve_vturb_video(video_url)
                    if resolved:
                        hls_url, vturb_video_id = resolved
                        content.videos.append(
                            Video(
                                video_id=vturb_video_id or data_id or str(item_id),
                                url=hls_url,
                                order=lesson.get("order", 1),
                                title=lesson.get("title", "Aula"),
                                size=0,
                                duration=0,
                                extra_props={"referer": self._site_url + "/"},
                            )
                        )
                    else:
                        logger.warning(
                            "Cademi: vturb video %s could not be resolved to HLS", video_url
                        )
                else:
                    content.videos.append(
                        Video(
                            video_id=data_id or str(item_id),
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
                if any(p in src for p in ("youtube", "youtu.be", "vimeo", "pandavideo", "converteai")):
                    if "converteai" in src:
                        resolved = self._resolve_vturb_video(src)
                        if resolved:
                            hls_url, vturb_video_id = resolved
                            content.videos.append(
                                Video(
                                    video_id=vturb_video_id or str(item_id),
                                    url=hls_url,
                                    order=lesson.get("order", 1),
                                    title=lesson.get("title", "Aula"),
                                    size=0,
                                    duration=0,
                                    extra_props={"referer": self._site_url + "/"},
                                )
                            )
                            break
                        logger.warning("Cademi: converteai iframe %s could not be resolved", src)
                        continue
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
        if "converteai" in url:
            match = re.search(r"/players/([a-f0-9]+)", url)
            if match:
                return match.group(1)
        return ""

    def _resolve_vturb_video(self, embed_url: str) -> Optional[Tuple[str, str]]:
        """Fetches the vturb/ConverteAI embed page and builds the HLS master URL.

        Why: yt-dlp does not have a native vturb extractor, but the embed HTML
        exposes the organization id, video id, and CDN hostname, which together
        form a stable HLS master URL that yt-dlp can download.
        """
        try:
            resp = self._session.get(embed_url, timeout=30)
            resp.raise_for_status()
            body = resp.text
        except Exception as exc:
            logger.warning("Cademi: vturb embed fetch failed (%s): %s", embed_url, exc)
            return None

        oid_match = re.search(r'\boid\s*:\s*["\']([0-9a-f\-]+)', body)
        vid_match = re.search(r'video\s*:\s*\{[^}]*?\bid\s*:\s*["\']([0-9a-f]+)', body)
        cdn_match = re.search(r'\bcdn\s*:\s*["\']([^"\']+)', body)
        if not (oid_match and vid_match):
            return None

        oid = oid_match.group(1)
        video_id = vid_match.group(1)
        cdn = cdn_match.group(1) if cdn_match else "cdn.converteai.net"
        return f"https://{cdn}/{oid}/{video_id}/main.m3u8", video_id

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
