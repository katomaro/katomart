from __future__ import annotations

import json
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
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

INTEGRATION_SLUG = "tpdplay"
INTEGRATION_VERSION = "1.0.0"
# Marcada como experimental: integracao construida a partir de um unico HAR,
# ainda nao validada ponta-a-ponta contra o site ao vivo.
INTEGRATION_EXPERIMENTAL = True

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tpdplay.com"


def _js_unescape(s: str) -> str:
    """Decodes a JS single-quoted string body (as produced by Laravel/Livewire
    ``JSON.parse('...')`` blobs) back into a plain string suitable for
    ``json.loads``.

    Handles ``\\uXXXX``, ``\\n``, ``\\t`` and generic ``\\x`` -> ``x`` escapes.
    ``\\/`` is left untouched because ``json.loads`` accepts it.
    """
    out: List[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt == "u":
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "t":
                out.append("\t")
                i += 2
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


class TpdPlayPlatform(BasePlatform):
    """Implements the TPD Play (www.tpdplay.com) members area.

    TPD Play is a Laravel/Livewire/Filament LMS. All content is server-side
    rendered HTML (there is no JSON API for courses or lessons). Auth is
    cookie/session based (form login with a CSRF ``_token``). Videos are hosted
    on PandaVideo (player ``standard``) with an optional Vimeo 4K fallback
    (player ``advanced``, gated behind the Premium plan).
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._login_session: Optional[requests.Session] = None

    @classmethod
    def auth_instructions(cls) -> str:
        return """TPD Play (https://www.tpdplay.com)

Assinantes ativos podem informar e-mail e senha para login automatico.

Autenticacao manual (Token):
1) Acesse https://www.tpdplay.com e faca login.
2) Abra o DevTools (F12) > aba Network, selecione qualquer requisicao para
   www.tpdplay.com e copie o cabecalho "Cookie" inteiro.
3) Cole o conteudo no campo Token (ex: "laravel_session=...; XSRF-TOKEN=...").
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        """Performs the Laravel form login and keeps the authenticated session.

        Returns a sentinel ``"session"`` token: the live cookies live on
        ``self._login_session`` and are reused by ``_configure_session``.
        """
        session = requests.Session()
        session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        login_url = f"{BASE_URL}/login"
        resp = session.get(login_url, timeout=30)
        resp.raise_for_status()

        csrf_token = self._extract_csrf_token(resp.text)
        if not csrf_token:
            raise ConnectionError("Nao foi possivel encontrar o token CSRF na pagina de login do TPD Play.")

        resp = session.post(
            login_url,
            data={
                "_token": csrf_token,
                "email": username,
                "password": password,
            },
            headers={"Referer": login_url, "Origin": BASE_URL},
            timeout=30,
            allow_redirects=True,
        )

        # A successful login redirects to /app. If we are still on /login the
        # credentials were rejected (Laravel re-renders the login form).
        if resp.url.rstrip("/").endswith("/login"):
            raise ConnectionError("Falha ao autenticar no TPD Play. Verifique e-mail e senha.")

        self._login_session = session
        return "session"

    @staticmethod
    def _extract_csrf_token(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        field = soup.find("input", {"name": "_token"})
        if field and field.get("value"):
            return field.get("value", "").strip()
        meta = soup.find("meta", {"name": "csrf-token"})
        if meta and meta.get("content"):
            return meta.get("content", "").strip()
        return ""

    def _configure_session(self, token: str) -> None:
        if self._login_session is not None:
            self._session = self._login_session
            self._login_session = None
        else:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": self._settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            })
            self._apply_manual_token(token)

        # Probe an authenticated page to confirm the session is valid.
        resp = self._session.get(f"{BASE_URL}/app", timeout=30, allow_redirects=True)
        if resp.url.rstrip("/").endswith("/login"):
            raise ConnectionError(
                "Sessao TPD Play invalida. Refaca o login ou cole o cabecalho Cookie atualizado no campo Token."
            )

    def _apply_manual_token(self, token: str) -> None:
        """Applies a manually supplied cookie string to the session.

        Accepts a full ``Cookie:`` header (``name=value; name2=value2``) or a
        single ``name=value`` / ``name:value`` pair.
        """
        raw = (token or "").strip()
        if not raw:
            raise ValueError("Token TPD Play vazio. Cole o cabecalho Cookie da sessao logada.")

        pairs = re.findall(r"([^=;,\s]+)\s*[=:]\s*([^;]+)", raw)
        if not pairs:
            raise ValueError("Token TPD Play invalido. Use o formato 'nome=valor; nome2=valor2'.")
        for name, value in pairs:
            self._session.cookies.set(name.strip(), value.strip(), domain="www.tpdplay.com")

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Lists the two top-level groups the TPD Play app exposes.

        The members area splits content into ``AULAS`` (standalone single-video
        lessons, listed flat under ``/app/aulas``) and ``CURSOS`` (structured
        multi-module courses, listed under ``/app/cursos``). These are distinct
        sections in the site navigation, so we list them separately instead of
        scraping the mixed ``/app`` dashboard.

        The standalone ``AULAS`` are bundled into a single synthetic course
        named ``AULAS`` so they land together under one output folder, matching
        how the site presents them (a flat grid, no chapters).
        """
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        courses: List[Dict[str, Any]] = []

        aulas_course = self._fetch_standalone_aulas()
        if aulas_course:
            courses.append(aulas_course)

        courses.extend(self._fetch_structured_courses())

        # Fallback: if the dedicated listing pages yielded nothing (layout
        # change, permissions), scrape the mixed dashboard as before.
        if not courses:
            courses = self._fetch_courses_from_dashboard()

        return courses

    def _fetch_structured_courses(self) -> List[Dict[str, Any]]:
        """Lists the multi-module courses from ``/app/cursos`` (the CURSOS group)."""
        try:
            resp = self._session.get(f"{BASE_URL}/app/cursos", timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("TPD Play: falha ao listar /app/cursos: %s", exc)
            return []
        return self._parse_course_cards(resp.text)

    def _fetch_standalone_aulas(self) -> Optional[Dict[str, Any]]:
        """Collects the standalone lessons (``/app/aulas`` + ``/app/aulas/legado``)
        into a single synthetic ``AULAS`` course, or ``None`` if there are none."""
        aulas: List[Dict[str, Any]] = []
        seen: set = set()
        for path in ("/app/aulas", "/app/aulas/legado"):
            try:
                resp = self._session.get(f"{BASE_URL}{path}", timeout=30)
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("TPD Play: falha ao listar %s: %s", path, exc)
                continue
            for slug, title, url in self._parse_aula_cards(resp.text):
                if slug in seen:
                    continue
                seen.add(slug)
                aulas.append({
                    "id": slug,
                    "slug": slug,
                    "name": title,
                    "title": title,
                    "order": len(aulas) + 1,
                    "watch_url": url,
                })

        if not aulas:
            return None

        return {
            "id": "__aulas__",
            "slug": "__aulas__",
            "name": "AULAS",
            "title": "AULAS",
            "kind": "aulas",
            "_aulas": aulas,
        }

    def _fetch_courses_from_dashboard(self) -> List[Dict[str, Any]]:
        """Legacy fallback: scrape every course card off the ``/app`` dashboard."""
        try:
            resp = self._session.get(f"{BASE_URL}/app", timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("TPD Play: falha ao ler o dashboard /app: %s", exc)
            return []
        return self._parse_course_cards(resp.text)

    def _parse_course_cards(self, html: str) -> List[Dict[str, Any]]:
        """Extracts ``/app/curso/<slug>`` course cards from a listing page."""
        soup = BeautifulSoup(html, "html.parser")
        courses: Dict[str, Dict[str, Any]] = {}
        for link in soup.find_all("a", href=True):
            slug = self._course_slug_from_href(link["href"])
            if not slug or slug in courses:
                continue
            name = self._title_from_card(link) or slug
            courses[slug] = {"id": slug, "slug": slug, "name": name, "kind": "curso"}
        return list(courses.values())

    def _parse_aula_cards(self, html: str) -> List[tuple]:
        """Extracts standalone lesson cards from an ``/app/aulas`` listing.

        Each card links to a lesson watch page ending in ``/aula/<slug>``. The
        full href is preserved as the watch URL so we don't have to reconstruct
        a (course-scoped vs. standalone) path we can't observe offline.
        """
        soup = BeautifulSoup(html, "html.parser")
        out: List[tuple] = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            match = re.search(r"/aula/([^/?#]+)/?$", urlparse(href).path)
            if not match:
                continue
            slug = match.group(1)
            title = self._title_from_card(link) or slug
            url = href if href.startswith("http") else urljoin(f"{BASE_URL}/", href)
            out.append((slug, title, url))
        return out

    @staticmethod
    def _course_slug_from_href(href: str) -> str:
        """Returns the course slug for a ``/app/curso/<slug>`` link, ignoring
        deeper ``/aula/...`` lesson links."""
        path = urlparse(href).path
        match = re.match(r"^/app/curso/([^/]+)/?$", path)
        return match.group(1) if match else ""

    @staticmethod
    def _title_from_card(link: Any) -> str:
        img = link.find("img")
        if img and img.get("alt"):
            alt = img["alt"].strip()
            if alt:
                return alt
        text = link.get_text(" ", strip=True)
        return text if text else ""

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}
        for course in courses:
            # The synthetic AULAS bundle already carries its lessons; it has no
            # course page to fetch.
            if course.get("kind") == "aulas" or course.get("id") == "__aulas__":
                all_content[course["id"]] = self._build_aulas_content(course)
                continue

            slug = course.get("slug") or course.get("id")
            if not slug:
                continue
            try:
                resp = self._session.get(f"{BASE_URL}/app/curso/{slug}", timeout=30)
                resp.raise_for_status()
                modules = self._parse_modules(resp.text, slug)
                all_content[course["id"]] = {
                    "id": course["id"],
                    "slug": slug,
                    "name": course.get("name", slug),
                    "title": course.get("name", slug),
                    "modules": modules,
                }
            except Exception as exc:
                logger.error("TPD Play: erro ao buscar conteudo do curso %s: %s", slug, exc)

        return all_content

    def _build_aulas_content(self, course: Dict[str, Any]) -> Dict[str, Any]:
        """Wraps the standalone AULAS into a single flat module."""
        lessons = []
        for order, aula in enumerate(course.get("_aulas", []), start=1):
            lessons.append({
                "id": aula["slug"],
                "slug": aula["slug"],
                "name": aula.get("name", aula["slug"]),
                "title": aula.get("title", aula["slug"]),
                "order": order,
                "watch_url": aula.get("watch_url"),
            })
        module = {
            "id": "aulas",
            "name": "Aulas",
            "title": "Aulas",
            "order": 1,
            "lessons": lessons,
        }
        return {
            "id": course["id"],
            "slug": course.get("slug", "__aulas__"),
            "name": course.get("name", "AULAS"),
            "title": course.get("title", "AULAS"),
            "modules": [module],
        }

    def _parse_modules(self, html: str, course_slug: str) -> List[Dict[str, Any]]:
        """Builds the module/lesson tree from a course page.

        The course page renders each module as a Filament tab: a ``<button>``
        whose ``x-on:click`` sets ``<course>ActiveTab = N`` (the module name is
        the button text) and a sibling ``<div panelKey="N">`` panel holding that
        module's lesson cards. We read the module names from the tabs and the
        lessons from the matching panels, preserving the site's grouping and
        ordering. Synthesizing modules from the "Capitulo X.Y" numbers (the old
        behaviour) was wrong: module names were lost, and courses that restart
        chapter numbering per module had their lessons merged together.
        """
        soup = BeautifulSoup(html, "html.parser")

        tab_names = self._extract_tab_names(soup)
        panels = soup.find_all("div", attrs={"panelkey": True})

        if panels:
            modules: List[Dict[str, Any]] = []
            for panel in panels:
                try:
                    key = int(panel.get("panelkey"))
                except (TypeError, ValueError):
                    key = len(modules)
                lessons = self._lessons_from_container(panel)
                if not lessons:
                    continue
                name = tab_names.get(key) or f"Modulo {key + 1}"
                modules.append({
                    "id": f"{course_slug}-m{key}",
                    "name": name,
                    "title": name,
                    "order": key + 1,
                    "lessons": lessons,
                })
            if modules:
                return modules

        # Fallback: no tabs/panels (single ungrouped grid) -> one flat module.
        lessons = self._lessons_from_container(soup)
        if not lessons:
            return []
        return [{
            "id": f"{course_slug}-m0",
            "name": "Aulas",
            "title": "Aulas",
            "order": 1,
            "lessons": lessons,
        }]

    @staticmethod
    def _extract_tab_names(soup: BeautifulSoup) -> Dict[int, str]:
        """Maps tab index -> module name from the ``...ActiveTab = N`` buttons."""
        names: Dict[int, str] = {}
        for btn in soup.find_all("button"):
            click = btn.get("x-on:click", "") or btn.get("@click", "")
            match = re.search(r"ActiveTab\s*=\s*(\d+)", click)
            if not match:
                continue
            text = btn.get_text(" ", strip=True)
            if text:
                names[int(match.group(1))] = text
        return names

    def _lessons_from_container(self, container: Any) -> List[Dict[str, Any]]:
        """Collects lesson cards (``.../aula/<slug>`` links) within a container.

        Dedupes by slug in first-seen order, upgrading the title/chapter label
        whenever a richer occurrence shows up (the same lesson can appear both
        as a bare prev/next nav arrow and as a grid card).
        """
        records: Dict[str, Dict[str, str]] = {}
        order_slugs: List[str] = []
        for link in container.find_all("a", href=True):
            href = link["href"]
            if "/aula/" not in href:
                continue
            slug = href.split("/aula/")[-1].split("?")[0].split("#")[0].strip("/")
            if not slug:
                continue
            chapter_label, title = self._lesson_label_and_title(link)
            record = records.get(slug)
            if record is None:
                records[slug] = {"title": title, "chapter_label": chapter_label}
                order_slugs.append(slug)
            else:
                if not record["title"] and title:
                    record["title"] = title
                if not record["chapter_label"] and chapter_label:
                    record["chapter_label"] = chapter_label

        lessons: List[Dict[str, Any]] = []
        for order, slug in enumerate(order_slugs, start=1):
            record = records[slug]
            title = self._compose_lesson_title(record["chapter_label"], record["title"], slug)
            lessons.append({
                "id": slug,
                "slug": slug,
                "name": title,
                "title": title,
                "order": order,
            })
        return lessons

    @staticmethod
    def _compose_lesson_title(chapter_label: str, title: str, slug: str) -> str:
        """Renders the lesson name as "Capitulo X.Y: Title" when both exist."""
        title = (title or "").strip()
        label = (chapter_label or "").strip()
        if label and title:
            return f"{label}: {title}"
        return title or label or slug

    @staticmethod
    def _lesson_label_and_title(link: Any) -> tuple:
        chapter_label = ""
        title = ""
        for p in link.find_all("p"):
            text = p.get_text(" ", strip=True)
            if not text:
                continue
            if re.match(r"^Cap[ií]tulo\b", text, re.I):
                chapter_label = text
            elif not title:
                title = text
        if not title:
            img = link.find("img")
            if img and img.get("alt"):
                title = img["alt"].strip()
        return chapter_label, title

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        lesson_slug = lesson.get("slug") or lesson.get("id")
        if not lesson_slug:
            raise ValueError("Slug da aula nao encontrado.")

        # Standalone AULAS carry their own watch URL (the listing href may be
        # course-scoped or standalone); structured lessons reconstruct it.
        url = lesson.get("watch_url") or f"{BASE_URL}/app/curso/{course_slug}/aula/{lesson_slug}"
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        html = resp.text

        content = LessonContent()

        players = self._extract_players(html)
        lesson_id = self._extract_progress_id(html) or lesson_slug
        title = lesson.get("title") or self._extract_lesson_title(html) or lesson_slug
        extra_props = {"referer": f"{BASE_URL}/"}

        # Prefer the PandaVideo "standard" player (reliable HLS download); fall
        # back to the Vimeo 4K "advanced" player when standard is absent.
        chosen = None
        standard = players.get("standard") or {}
        advanced = players.get("advanced") or {}
        if standard.get("url") and standard.get("enabled", True):
            chosen = standard["url"]
        elif advanced.get("url") and advanced.get("enabled", True):
            chosen = advanced["url"]

        if chosen:
            content.videos.append(Video(
                video_id=str(lesson_id),
                url=chosen,
                order=lesson.get("order", 1),
                title=title,
                size=0,
                duration=0,
                extra_props=extra_props,
            ))
        else:
            logger.warning("TPD Play: nenhum player encontrado para a aula %s", lesson_slug)

        return content

    def _extract_players(self, html: str) -> Dict[str, Dict[str, Any]]:
        match = re.search(r"players:\s*JSON\.parse\('(.*?)'\)", html, re.S)
        if not match:
            return {}
        try:
            return json.loads(_js_unescape(match.group(1)))
        except (ValueError, json.JSONDecodeError) as exc:
            logger.warning("TPD Play: falha ao decodificar players JSON: %s", exc)
            return {}

    @staticmethod
    def _extract_progress_id(html: str) -> str:
        # The endpoint lives inside an escaped JSON blob, so the slash before the
        # id may be written as "\/" (one or more backslashes then a slash).
        match = re.search(r"lesson-progress\\*/(\d+)", html)
        return match.group(1) if match else ""

    @staticmethod
    def _extract_lesson_title(html: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
        if not match:
            return ""
        title = match.group(1).strip()
        title = re.sub(r"\s*-\s*TPDPlay\s*$", "", title, flags=re.I)
        return title.strip()

    def download_attachment(self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")
        try:
            url = attachment.url
            if not url.startswith("http"):
                url = urljoin(f"{BASE_URL}/", url)
            resp = self._session.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            with open(download_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)
            return True
        except Exception as exc:
            logger.error("TPD Play: erro ao baixar anexo '%s': %s", attachment.filename, exc)
            return False


# PlatformFactory.register_platform("TPD Play", TpdPlayPlatform, slug=INTEGRATION_SLUG, version=INTEGRATION_VERSION, experimental=INTEGRATION_EXPERIMENTAL)
PlatformFactory.register_platform("TPD Play", TpdPlayPlatform)
