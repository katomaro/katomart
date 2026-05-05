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

BASE_URL = "https://www.matematicaprapassar.com.br"


class MatematicaPraPassarPlatform(BasePlatform):
    """Implements the Matematica Pra Passar (matematicaprapassar.com.br) platform.

    CakePHP-based EAD platform with cookie-based auth (CAKEPHP session).
    Content is server-side rendered HTML.
    Videos are hosted on PandaVideo.

    URL hierarchy:
      /sala-virtual/meus-cursos  (course listing)
      /virtual_rooms/mycourses_plan/{b64}  (plan/mentoria course)
      /virtual_rooms/courseplandisciplines/{id}/{slug}  (discipline/module)
      /virtual_rooms/disciplineplanvideos/{id}/{id}/{slug}  (lesson listing)
      /virtual_rooms/disciplineplanvideoplayer/{id}/{id}/{id}  (video player)
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

        token_input = soup.find("input", {"name": "data[_Token][key]"})
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
            title_el = card.find(class_="course-title")
            title = title_el.get_text(strip=True) if title_el else ""

            content_url = None
            course_id = None

            for link in card.find_all("a", href=True):
                href = link.get("href", "")

                m = re.search(
                    r"/sala-virtual/meu-curso/disciplinas/([^/]+)/([^/]+)/([^/\"'\s]+)", href
                )
                if m:
                    content_url = self._abs(href)
                    course_id = m.group(2)
                    break

                m = re.search(r"/virtual_rooms/mycourses_plan/([^/\"'\s]+)", href)
                if m:
                    content_url = self._abs(href)
                    course_id = m.group(1)
                    break

            if not content_url or not course_id:
                continue
            if course_id in courses:
                continue

            if not title:
                img = card.find("img", title=True)
                title = img.get("title", "").strip() if img else ""
            if not title:
                title = f"Curso {course_id}"

            courses[course_id] = {
                "id": course_id,
                "name": title,
                "slug": course_id,
                "seller_name": "",
                "extra": {"content_url": content_url},
            }

        if not courses:
            courses = self._fallback_extract_courses(soup)

        logger.debug("MatematicaPraPassar: found %d courses", len(courses))
        return sorted(courses.values(), key=lambda c: c.get("name", ""))

    def _fallback_extract_courses(self, soup: BeautifulSoup) -> Dict[str, Dict[str, Any]]:
        courses: Dict[str, Dict[str, Any]] = {}

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")

            m = re.search(
                r"/sala-virtual/meu-curso/disciplinas/([^/]+)/([^/]+)/([^/\"'\s]+)", href
            )
            if m:
                cid = m.group(2)
                if cid not in courses:
                    title = link.get_text(strip=True) or f"Curso {cid}"
                    courses[cid] = {
                        "id": cid,
                        "name": title,
                        "slug": cid,
                        "seller_name": "",
                        "extra": {"content_url": self._abs(href)},
                    }
                continue

            m = re.search(r"/virtual_rooms/mycourses_plan/([^/\"'\s]+)", href)
            if m:
                cid = m.group(1)
                if cid not in courses:
                    title = link.get_text(strip=True) or f"Curso {cid}"
                    courses[cid] = {
                        "id": cid,
                        "name": title,
                        "slug": cid,
                        "seller_name": "",
                        "extra": {"content_url": self._abs(href)},
                    }

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            content_url = course.get("extra", {}).get("content_url")
            if not course_id or not content_url:
                continue

            logger.debug("MatematicaPraPassar: fetching content for course %s", course_id)

            try:
                resp = self._session.get(content_url, timeout=30, allow_redirects=True)
                resp.raise_for_status()
                html = resp.text

                modules = self._resolve_page_modules(html)

            except Exception as exc:
                logger.error("MatematicaPraPassar: failed to fetch course %s: %s", course_id, exc)
                continue

            course_entry = course.copy()
            course_entry["title"] = self._extract_page_title(html) or course.get("name", "Curso")
            course_entry["modules"] = modules
            all_content[str(course_id)] = course_entry

            time.sleep(0.3)

        return all_content

    def _resolve_page_modules(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")

        # 1) Direct lesson links (disciplineplanvideoplayer) — e.g. player sidebar
        modules = self._parse_videoplayer_links_as_modules(soup)
        if modules:
            return modules

        # 2) disciplineplanvideos links — each becomes a module
        modules = self._fetch_video_list_modules(soup)
        if modules:
            return modules

        # 3) courseplandisciplines links — drill one level deeper
        modules = self._fetch_discipline_modules(soup)
        if modules:
            return modules

        return []

    def _parse_videoplayer_links_as_modules(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        side_list = soup.find("ul", class_="videos-side-list")
        search_root = side_list if side_list else soup

        lessons = self._extract_videoplayer_lessons(search_root)
        if not lessons:
            return []

        module_map: Dict[str, List[Dict[str, Any]]] = {}
        for lesson in lessons:
            mid = lesson.get("extra", {}).get("module_id", "1")
            module_map.setdefault(mid, []).append(lesson)

        modules: List[Dict[str, Any]] = []
        for idx, (mid, mod_lessons) in enumerate(module_map.items(), 1):
            modules.append({
                "id": mid,
                "title": f"Modulo {idx}",
                "order": idx,
                "lessons": mod_lessons,
                "locked": False,
            })
        return modules

    def _fetch_video_list_modules(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        modules: List[Dict[str, Any]] = []
        seen: set = set()

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            m = re.search(
                r"/virtual_rooms/disciplineplanvideos/(\d+)/(\d+)/([^\"'\s]+)", href
            )
            if not m:
                continue
            key = f"{m.group(1)}/{m.group(2)}"
            if key in seen:
                continue
            seen.add(key)

            url = self._abs(href)
            title = link.get_text(strip=True) or f"Modulo {len(modules) + 1}"

            try:
                resp = self._session.get(url, timeout=30, allow_redirects=True)
                resp.raise_for_status()
                sub_soup = BeautifulSoup(resp.text, "html.parser")
                lessons = self._extract_videoplayer_lessons(sub_soup)
                if lessons:
                    modules.append({
                        "id": m.group(2),
                        "title": title,
                        "order": len(modules) + 1,
                        "lessons": lessons,
                        "locked": False,
                    })
            except Exception as exc:
                logger.warning("MatematicaPraPassar: error fetching video list %s: %s", url, exc)

            time.sleep(0.3)

        return modules

    def _fetch_discipline_modules(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        modules: List[Dict[str, Any]] = []
        seen: set = set()

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            m = re.search(
                r"/virtual_rooms/courseplandisciplines/(\d+)/([^\"'\s]+)", href
            )
            if not m:
                continue
            disc_id = m.group(1)
            if disc_id in seen:
                continue
            seen.add(disc_id)

            url = self._abs(href)
            title = link.get_text(strip=True) or f"Modulo {len(modules) + 1}"

            try:
                resp = self._session.get(url, timeout=30, allow_redirects=True)
                resp.raise_for_status()
                sub_soup = BeautifulSoup(resp.text, "html.parser")

                sub_modules = self._fetch_video_list_modules(sub_soup)
                if sub_modules:
                    for sub_mod in sub_modules:
                        sub_mod["title"] = f"{title} - {sub_mod['title']}"
                        sub_mod["order"] = len(modules) + 1
                        modules.append(sub_mod)
                else:
                    lessons = self._extract_videoplayer_lessons(sub_soup)
                    if lessons:
                        modules.append({
                            "id": disc_id,
                            "title": title,
                            "order": len(modules) + 1,
                            "lessons": lessons,
                            "locked": False,
                        })
            except Exception as exc:
                logger.warning("MatematicaPraPassar: error fetching discipline %s: %s", url, exc)

            time.sleep(0.3)

        return modules

    def _extract_videoplayer_lessons(self, root: Any) -> List[Dict[str, Any]]:
        side_list = root.find("ul", class_="videos-side-list")
        search_root = side_list if side_list else root

        lessons: List[Dict[str, Any]] = []
        seen: set = set()

        for link in search_root.find_all("a", href=True):
            href = link.get("href", "")
            m = re.search(
                r"/virtual_rooms/disciplineplanvideoplayer/(\d+)/(\d+)/(\d+)", href
            )
            if not m:
                continue

            lesson_id = m.group(3)
            if lesson_id in seen:
                continue
            seen.add(lesson_id)

            title = link.get_text(strip=True)
            if not title or len(title) < 2:
                title = f"Aula {len(lessons) + 1}"

            lessons.append({
                "id": lesson_id,
                "title": title,
                "order": len(lessons) + 1,
                "locked": False,
                "extra": {
                    "course_id": m.group(1),
                    "module_id": m.group(2),
                },
            })

        return lessons

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

        lesson_id = lesson.get("id")
        extra = lesson.get("extra", {})
        real_course_id = extra.get("course_id", course_id)
        real_module_id = extra.get("module_id", module_id)

        url = f"{BASE_URL}/virtual_rooms/disciplineplanvideoplayer/{real_course_id}/{real_module_id}/{lesson_id}"
        resp = self._session.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        content = LessonContent()
        soup = BeautifulSoup(html, "html.parser")

        desc_el = soup.find("div", class_=re.compile(r"descricao|description|conteudo-aula|lesson-content"))
        if desc_el:
            text = desc_el.get_text(strip=True)
            if text:
                content.description = Description(text=text, description_type="text")

        self._extract_pandavideo(soup, content, lesson, url)

        if not content.videos:
            self._extract_generic_iframes(soup, content, lesson, url)

        self._extract_attachments(soup, content)

        return content

    def _extract_pandavideo(
        self, soup: BeautifulSoup, content: LessonContent, lesson: Dict[str, Any], page_url: str
    ) -> None:
        for iframe in soup.find_all("iframe", src=True):
            src = iframe.get("src", "")
            if "pandavideo" in src:
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
                return

        video_div = soup.find("div", class_=re.compile(r"video-panda|pandavideo"))
        if video_div:
            iframe = video_div.find("iframe")
            if iframe and iframe.get("src"):
                src = iframe.get("src", "")
                panda_match = re.search(r"[?&]v=([a-f0-9-]+)", src)
                video_id = panda_match.group(1) if panda_match else video_div.get("data-id", str(lesson.get("id", "")))
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

    def _extract_generic_iframes(
        self, soup: BeautifulSoup, content: LessonContent, lesson: Dict[str, Any], page_url: str
    ) -> None:
        for iframe in soup.find_all("iframe", src=True):
            src = iframe.get("src", "")
            if any(p in src for p in ("youtube", "youtu.be", "vimeo", "pandavideo", "player")):
                vid_id = self._extract_video_id(src)
                content.videos.append(
                    Video(
                        video_id=vid_id or str(lesson.get("id", "")),
                        url=src,
                        order=lesson.get("order", 1),
                        title=lesson.get("title", "Aula"),
                        size=0,
                        duration=0,
                        extra_props={"referer": page_url},
                    )
                )
                break

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

    def _extract_attachments(self, soup: BeautifulSoup, content: LessonContent) -> None:
        for idx, link in enumerate(
            soup.find_all("a", href=re.compile(r"\.(pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar)", re.I)),
            start=1,
        ):
            url = link.get("href", "")
            if not url:
                continue

            if not url.startswith("http"):
                url = f"{BASE_URL}{url}"

            filename = link.get_text(strip=True)
            if not filename or len(filename) < 2:
                filename = url.rsplit("/", 1)[-1]

            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

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

        for idx_extra, link in enumerate(
            soup.find_all("a", class_=re.compile(r"attach|download|material|anexo"), href=True),
            start=len(content.attachments) + 1,
        ):
            url = link.get("href", "")
            if not url or any(a.url == url for a in content.attachments):
                continue

            if not url.startswith("http"):
                url = f"{BASE_URL}{url}"

            filename = link.get_text(strip=True) or f"Anexo {idx_extra}"
            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

            content.attachments.append(
                Attachment(
                    attachment_id=str(idx_extra),
                    url=url,
                    filename=filename,
                    order=idx_extra,
                    extension=extension,
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

            response = self._session.get(url, stream=True, timeout=120)
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


PlatformFactory.register_platform("Matematica Pra Passar", MatematicaPraPassarPlatform)
