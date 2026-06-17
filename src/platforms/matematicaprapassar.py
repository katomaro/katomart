from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory

INTEGRATION_SLUG = "matematicaprapassar"
INTEGRATION_VERSION = "1.0.0"
INTEGRATION_EXPERIMENTAL = False

logger = logging.getLogger(__name__)

BASE_URL = "https://www.matematicaprapassar.com.br"

# URL hierarchy of the "sala virtual" (CakePHP). The path segments after the
# action are base64-encoded ids, then URL-encoded (so "905" -> "OTA1",
# "1" -> "MQ%3D%3D"). We keep the raw (still-encoded) segments to rebuild child
# URLs verbatim, and decode them only for human-friendly ids.
#   course disciplines: /sala-virtual/meu-curso/disciplinas/{enroll}/{course}/{slug}
#   discipline lessons: /sala-virtual/meu-curso/disciplina/aulas/{enroll}/{course}/{module}/{slug}
#   lesson player:      /sala-virtual/meu-curso/disciplina/aula/{enroll}/{course}/{module}/{lesson}
DISCIPLINES_RE = re.compile(
    r"/sala-virtual/meu-curso/disciplinas/([^/?\"'\s]+)/([^/?\"'\s]+)/([^/?\"'\s]+)"
)
AULAS_RE = re.compile(
    r"/sala-virtual/meu-curso/disciplina/aulas/([^/?\"'\s]+)/([^/?\"'\s]+)/([^/?\"'\s]+)/([^/?\"'\s]+)"
)
AULA_RE = re.compile(
    r"/sala-virtual/meu-curso/disciplina/aula/([^/?\"'\s]+)/([^/?\"'\s]+)/([^/?\"'\s]+)/([^/?\"'\s]+)"
)


def _decode_seg(seg: str) -> str:
    """Decodes a URL-encoded + base64-encoded path segment to its plain id."""
    raw = unquote(seg)
    try:
        decoded = base64.b64decode(raw).decode("utf-8", "ignore").strip()
        if decoded:
            return decoded
    except Exception:
        pass
    return raw


class MatematicaPraPassarPlatform(BasePlatform):
    """Implements the Matematica Pra Passar (matematicaprapassar.com.br) platform.

    CakePHP-based EAD platform with cookie-based auth (CAKEPHP session).
    Content is server-side rendered HTML. Videos are hosted on PandaVideo.

    URL hierarchy (see module-level constants):
      /sala-virtual/meus-cursos                                course listing
      /sala-virtual/meu-curso/disciplinas/{e}/{c}/{slug}       disciplines (modules)
      /sala-virtual/meu-curso/disciplina/aulas/{e}/{c}/{m}/..  lessons of a discipline
      /sala-virtual/meu-curso/disciplina/aula/{e}/{c}/{m}/{l}  lesson player
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._login_session: Optional[requests.Session] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """Para autenticacao manual (Token):
1) Acesse https://www.matematicaprapassar.com.br/login e faca login.
2) Abra o DevTools (F12) > aba Application > Cookies.
3) Copie o valor do cookie "CAKEPHP".
4) Cole no campo de token.

Assinantes ativos podem informar usuario/senha para login automatico.""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        session = requests.Session()
        session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3",
        })

        resp = session.get(f"{BASE_URL}/login", timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # The login page renders several CakePHP forms, each with its own
        # data[_Token][key]. Scope the CSRF token to the actual login form.
        login_form = soup.find("form", action="/login")
        token_input = login_form.find("input", {"name": "data[_Token][key]"}) if login_form else None
        if not token_input:
            raise ConnectionError("Nao foi possivel encontrar o token CSRF na pagina de login.")
        csrf_token = token_input.get("value", "")

        resp = session.post(
            f"{BASE_URL}/login",
            data={
                "_method": "POST",
                "data[_Token][key]": csrf_token,
                "data[User][username]": username,
                "data[User][password]": password,
            },
            headers={
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/login",
                "Content-Type": "application/x-www-form-urlencoded",
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=30,
            allow_redirects=True,
        )

        if "/login" in resp.url and resp.status_code != 302:
            raise ConnectionError("Falha ao autenticar no Matematica Pra Passar. Verifique suas credenciais.")

        session_cookie = session.cookies.get("CAKEPHP")
        if not session_cookie:
            raise ConnectionError("Login realizado mas cookie de sessao CAKEPHP nao encontrado.")

        self._login_session = session
        return session_cookie

    def _configure_session(self, token: str) -> None:
        if self._login_session:
            self._session = self._login_session
            self._login_session = None
        else:
            self._session = requests.Session()
            self._session.cookies.set("CAKEPHP", token, domain="www.matematicaprapassar.com.br")

        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3",
            "Referer": f"{BASE_URL}/",
        })

        resp = self._session.get(f"{BASE_URL}/sala-virtual/meus-cursos", timeout=30, allow_redirects=True)
        if "/login" in resp.url:
            raise ConnectionError(
                "Falha ao autenticar no Matematica Pra Passar. Verifique o cookie CAKEPHP."
            )
        logger.info("MatematicaPraPassar: authenticated successfully")

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        resp = self._session.get(f"{BASE_URL}/sala-virtual/meus-cursos", timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        courses: Dict[str, Dict[str, Any]] = {}

        for card in soup.find_all("div", class_="course"):
            link = card.find("a", href=DISCIPLINES_RE)
            if not link:
                continue
            m = DISCIPLINES_RE.search(link.get("href", ""))
            if not m:
                continue

            enroll_seg, course_seg, slug_seg = m.group(1), m.group(2), m.group(3)
            course_id = _decode_seg(course_seg)
            if course_id in courses:
                continue

            title_el = card.find(class_="course-title")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                img = card.find("img", title=True)
                title = img.get("title", "").strip() if img else ""
            if not title:
                title = f"Curso {course_id}"

            courses[course_id] = self._build_course_entry(
                course_id, title, enroll_seg, course_seg, slug_seg
            )

        if not courses:
            courses = self._fallback_extract_courses(soup)

        logger.debug("MatematicaPraPassar: found %d courses", len(courses))
        return sorted(courses.values(), key=lambda c: c.get("name", ""))

    def _build_course_entry(
        self, course_id: str, title: str, enroll_seg: str, course_seg: str, slug_seg: str
    ) -> Dict[str, Any]:
        content_url = (
            f"{BASE_URL}/sala-virtual/meu-curso/disciplinas/"
            f"{enroll_seg}/{course_seg}/{slug_seg}"
        )
        return {
            "id": course_id,
            "name": title,
            "slug": _decode_seg(slug_seg) or slug_seg,
            "seller_name": "",
            "extra": {
                "content_url": content_url,
                "enroll_seg": enroll_seg,
                "course_seg": course_seg,
                "slug_seg": slug_seg,
            },
        }

    def _fallback_extract_courses(self, soup: BeautifulSoup) -> Dict[str, Dict[str, Any]]:
        courses: Dict[str, Dict[str, Any]] = {}
        for link in soup.find_all("a", href=DISCIPLINES_RE):
            m = DISCIPLINES_RE.search(link.get("href", ""))
            if not m:
                continue
            enroll_seg, course_seg, slug_seg = m.group(1), m.group(2), m.group(3)
            course_id = _decode_seg(course_seg)
            if course_id in courses:
                continue
            title = link.get_text(strip=True) or f"Curso {course_id}"
            courses[course_id] = self._build_course_entry(
                course_id, title, enroll_seg, course_seg, slug_seg
            )
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            extra = course.get("extra", {})
            content_url = extra.get("content_url")
            if not course_id or not content_url:
                continue

            logger.debug("MatematicaPraPassar: fetching content for course %s", course_id)

            try:
                resp = self._session.get(content_url, timeout=30, allow_redirects=True)
                resp.raise_for_status()
                html = resp.text
                modules = self._parse_disciplines(html)
            except Exception as exc:
                logger.error("MatematicaPraPassar: failed to fetch course %s: %s", course_id, exc)
                continue

            course_entry = course.copy()
            course_entry["title"] = course.get("name") or self._extract_page_title(html) or "Curso"
            course_entry["modules"] = modules
            all_content[str(course_id)] = course_entry

            time.sleep(0.3)

        return all_content

    def _parse_disciplines(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        modules: List[Dict[str, Any]] = []
        seen: set = set()

        for disc in soup.find_all("div", class_="discipline"):
            link = disc.find("a", href=AULAS_RE)
            if not link:
                continue
            m = AULAS_RE.search(link.get("href", ""))
            if not m:
                continue

            enroll_seg, course_seg, module_seg, slug_seg = m.groups()
            module_key = module_seg
            if module_key in seen:
                continue
            seen.add(module_key)

            img = disc.find("img", title=True)
            title = (img.get("title", "").strip() if img else "") or link.get_text(strip=True)
            if not title:
                title = _decode_seg(slug_seg) or f"Modulo {len(modules) + 1}"

            order = len(modules) + 1
            aulas_url = (
                f"{BASE_URL}/sala-virtual/meu-curso/disciplina/aulas/"
                f"{enroll_seg}/{course_seg}/{module_seg}/{slug_seg}"
            )

            try:
                lessons = self._parse_lessons(aulas_url, enroll_seg, course_seg, module_seg)
            except Exception as exc:
                logger.warning("MatematicaPraPassar: error fetching discipline %s: %s", aulas_url, exc)
                lessons = []

            modules.append({
                "id": _decode_seg(module_seg),
                "title": title,
                "order": order,
                "lessons": lessons,
                "locked": False,
            })
            time.sleep(0.3)

        return modules

    def _parse_lessons(
        self, aulas_url: str, enroll_seg: str, course_seg: str, module_seg: str
    ) -> List[Dict[str, Any]]:
        resp = self._session.get(aulas_url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        lessons: List[Dict[str, Any]] = []
        seen: set = set()

        for link in soup.find_all("a", href=AULA_RE):
            m = AULA_RE.search(link.get("href", ""))
            if not m:
                continue
            lesson_seg = m.group(4)
            if lesson_seg in seen:
                continue
            seen.add(lesson_seg)

            lesson_id = _decode_seg(lesson_seg)
            title = link.get_text(strip=True) or f"Aula {len(lessons) + 1}"

            row = link.find_parent("div", class_="row")
            attachments = self._extract_row_attachments(row) if row else []

            lessons.append({
                "id": lesson_id,
                "title": title,
                "order": len(lessons) + 1,
                "locked": False,
                "extra": {
                    "enroll_seg": enroll_seg,
                    "course_seg": course_seg,
                    "module_seg": module_seg,
                    "lesson_seg": lesson_seg,
                    "attachments": attachments,
                },
            })

        return lessons

    def _extract_row_attachments(self, row: Any) -> List[Dict[str, str]]:
        """Harvests the per-lesson download links present in the listing row.

        Lessons expose up to four files (pdf, pdf2, pdf3, pdf4) under
        /files/lesson/<type>/<lesson_id>/<filename>.
        """
        attachments: List[Dict[str, str]] = []
        seen_urls: set = set()

        for link in row.find_all("a", href=re.compile(r"/files/lesson/")):
            href = link.get("href", "")
            if not href:
                continue
            url = self._abs(href)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            basename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
            label = link.get_text(strip=True)
            filename = basename or (f"{label}.pdf" if label else "material.pdf")
            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf"

            attachments.append({
                "url": url,
                "filename": filename,
                "extension": extension,
                "label": label,
            })

        return attachments

    def _extract_page_title(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.find("title")
        if title_el:
            text = title_el.get_text(strip=True)
            for suffix in (" - Matemática Pra Passar", " - MPP", " | MPP"):
                if text.endswith(suffix):
                    text = text[: -len(suffix)]
            text = text.replace("Matemática Pra Passar", "").strip(" -|")
            return text.strip()
        return ""

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        extra = lesson.get("extra", {})
        enroll_seg = extra.get("enroll_seg")
        course_seg = extra.get("course_seg")
        module_seg = extra.get("module_seg")
        lesson_seg = extra.get("lesson_seg")

        content = LessonContent()

        if enroll_seg and course_seg and module_seg and lesson_seg:
            url = (
                f"{BASE_URL}/sala-virtual/meu-curso/disciplina/aula/"
                f"{enroll_seg}/{course_seg}/{module_seg}/{lesson_seg}"
            )
            resp = self._session.get(url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            self._extract_video(soup, content, lesson, url)
        else:
            logger.warning(
                "MatematicaPraPassar: lesson %s missing URL segments, skipping video",
                lesson.get("id"),
            )

        self._attach_listing_attachments(content, extra.get("attachments", []))

        return content

    def _extract_video(
        self, soup: BeautifulSoup, content: LessonContent, lesson: Dict[str, Any], page_url: str
    ) -> None:
        """Extracts the lesson video. Scoped to the player container so the
        promotional YouTube iframe in the page footer is never picked up."""
        container = soup.find("div", class_="video-container") or soup.find(
            "div", class_=re.compile(r"box-vimeo-player")
        )
        search_root = container if container else soup

        iframe = search_root.find("iframe", src=re.compile(r"pandavideo"))
        if not iframe and container:
            iframe = container.find("iframe", src=True)

        if not iframe or not iframe.get("src"):
            logger.warning("MatematicaPraPassar: no video found for lesson %s", lesson.get("id"))
            return

        src = iframe.get("src", "")
        panda_match = re.search(r"[?&]v=([a-f0-9-]+)", src)
        video_id = panda_match.group(1) if panda_match else str(lesson.get("id", ""))

        content.videos.append(
            Video(
                video_id=video_id,
                url=src,
                order=lesson.get("order", 1),
                title=lesson.get("title", "Aula"),
                size=0,
                duration=0,
                extra_props={"referer": page_url},
            )
        )

    def _attach_listing_attachments(
        self, content: LessonContent, raw_attachments: List[Dict[str, str]]
    ) -> None:
        for idx, att in enumerate(raw_attachments, start=1):
            url = att.get("url")
            if not url:
                continue
            content.attachments.append(
                Attachment(
                    attachment_id=str(idx),
                    url=url,
                    filename=att.get("filename") or f"Anexo {idx}",
                    order=idx,
                    extension=att.get("extension", ""),
                    size=0,
                )
            )

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        try:
            url = attachment.url
            if not url:
                return False

            response = self._session.get(
                url, stream=True, timeout=120, headers={"Referer": f"{BASE_URL}/"}
            )
            response.raise_for_status()

            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True

        except Exception as exc:
            logger.error(
                "MatematicaPraPassar: failed to download attachment %s: %s",
                attachment.filename,
                exc,
            )
            return False

    @staticmethod
    def _abs(href: str) -> str:
        return href if href.startswith("http") else f"{BASE_URL}{href}"


# PlatformFactory.register_platform("Matematica Pra Passar", MatematicaPraPassarPlatform, slug=INTEGRATION_SLUG, version=INTEGRATION_VERSION, experimental=INTEGRATION_EXPERIMENTAL)
PlatformFactory.register_platform("Matematica Pra Passar", MatematicaPraPassarPlatform)
