from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import (
    AuthField,
    AuthFieldType,
    BasePlatform,
    PlatformFactory,
)

INTEGRATION_SLUG = "alura"
INTEGRATION_VERSION = "1.1.0"
INTEGRATION_EXPERIMENTAL = False

logger = logging.getLogger(__name__)

# Alura (cursos.alura.com.br). Authentication is cookie-based: a successful
# form login sets `SESSION` + `caelum.login.token`; both flow back through the
# requests cookie jar and authenticate every subsequent API call.
BASE_URL = "https://cursos.alura.com.br"
COOKIE_DOMAIN = ".alura.com.br"
SIGNIN_URL = f"{BASE_URL}/signin"
LOGIN_FORM_URL = f"{BASE_URL}/loginForm"
PROFILE_URL = f"{BASE_URL}/api/v1/profile"

# The login form posts the post-login return URL as a bracket-wrapped base64
# blob ("[<base64 of https://cursos.alura.com.br/>]"); reused verbatim.
URL_AFTER_LOGIN = "[aHR0cHM6Ly9jdXJzb3MuYWx1cmEuY29tLmJyLw]"

# /classpage/api endpoints (slug -> id resolution, sections tree, task detail).
COURSE_BY_SLUG_URL = f"{BASE_URL}/classpage/api/v1/course/{{slug}}"
COURSE_SECTIONS_URL = f"{BASE_URL}/classpage/api/v2/course/{{course_id}}/sections/progress"
TASK_DETAIL_URL = f"{BASE_URL}/classpage/api/v1/course/{{course_id}}/task/{{task_id}}"
TASK_VIDEO_URL = f"{BASE_URL}/classpage/api/v1/course/{{course_id}}/task/{{task_id}}/video"

# Carreira (career path) endpoints. A career groups every course the user must
# take into ordered `steps`; each step lists `contents` whose `code` is a course
# slug. This is the bulk-discovery path: one career URL enumerates all of its
# courses in a single session, so the user no longer has to mark each course as
# "continuar assistindo" (which triggered a fresh login and got accounts blocked
# for simultaneous sessions).
CAREER_BY_SLUG_URL = f"{BASE_URL}/api/v1/next/career/{{slug}}"
CAREER_STEP_URL = f"{BASE_URL}/api/v1/next/career/{{slug}}/step/{{step_id}}"

# A career reference taken from the browser address bar, e.g.
#   /career/path/engenharia-de-ia
#   /career/engenharia-de-ia
#   /dashboard/career/especialista-em-ia-v977868
# The dashboard form carries a "-v<id>" suffix that the API slug omits.
CAREER_PATH_RE = re.compile(r"/(?:dashboard/)?career(?:/path)?/([a-zA-Z0-9-]+)")
CAREER_VERSION_SUFFIX_RE = re.compile(r"-v\d+$")

# Best-to-worst preference for the direct MP4 variants returned by the video
# endpoint (fullhd / hd / sd).
VIDEO_QUALITY_RANK = {"fullhd": 3, "hd": 2, "sd": 1}

# Task kinds that carry a streamable video; everything else is text/quiz and
# only yields the markdown description.
VIDEO_KINDS = {"VIDEO"}


class AluraPlatform(BasePlatform):
    """Implements the Alura (cursos.alura.com.br) platform.

    Alura is a subscription catalogue with thousands of courses and no
    list-everything endpoint, so course discovery is *search based*: the user
    pastes a URL or slug into the search box. Two kinds are accepted:

    - A single **course** URL/slug -> resolved to a numeric id via
      `/classpage/api/v1/course/<slug>` (one course).
    - A **Carreira** (career path) URL/slug -> expanded into *every* course it
      contains via `/api/v1/next/career/<slug>` (steps) +
      `/api/v1/next/career/<slug>/step/<id>` (contents). This queues the whole
      career in a single authenticated session, so the user no longer has to
      replay each course in the browser to surface it on the dashboard.

    Content layout: a course has `sections` (modules), each with `tasks`
    (lessons). A task's `kind` is one of VIDEO / TEXT_CONTENT / HQ_EXPLANATION /
    SINGLE_CHOICE. For every task we save the markdown description; VIDEO tasks
    additionally expose direct MP4 variants (video2.alura.com.br, token in URL)
    plus WebVTT subtitles, which are surfaced as attachments.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)

    @classmethod
    def token_field(cls) -> AuthField:
        return AuthField(
            name="token",
            label="Cookies de sessão",
            field_type=AuthFieldType.PASSWORD,
            placeholder="Cole o cabeçalho Cookie inteiro (ou o valor do cookie SESSION)",
            required=False,
        )

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 9.90) ativos podem informar e-mail e senha — o login é feito
automaticamente via API.

Na busca, cole a URL de um curso OU de uma Carreira inteira:
• Curso:    https://cursos.alura.com.br/course/<slug>
• Carreira: https://cursos.alura.com.br/career/path/<slug>
            (ou o link do painel: .../dashboard/career/<slug>-v<id>)
Ao colar uma Carreira, todos os cursos dela são enfileirados de uma só vez,
na mesma sessão — sem precisar reabrir o navegador a cada curso.

Para usuários gratuitos, como obter os cookies de sessão:
1) Acesse https://cursos.alura.com.br e faça login normalmente.
2) Abra o DevTools (F12) → aba Rede (Network) e atualize a página.
3) Clique em qualquer requisição para cursos.alura.com.br, vá em Cabeçalhos
   (Headers) e localize o cabeçalho de requisição "Cookie".
4) Copie o valor inteiro e cole no campo acima (os cookies essenciais são
   SESSION e caelum.login.token).
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        cookie = (credentials.get("token") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
            }
        )
        self._session = session

        if username and password and self._settings.has_full_permissions:
            self._login_with_credentials(username, password)
        elif cookie:
            self._apply_cookie_string(cookie)
        else:
            raise ValueError(
                "Informe os cookies de sessão ou utilize e-mail e senha (assinante)."
            )

        self._validate_session()
        logger.info("Sessão autenticada na Alura.")

    def _apply_cookie_string(self, cookie: str) -> None:
        """Loads a pasted Cookie header (or a bare SESSION value) into the jar."""
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        if "=" in cookie:
            for part in cookie.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name, value = part.split("=", 1)
                name = name.strip()
                value = value.strip()
                if name:
                    self._session.cookies.set(name, value, domain=COOKIE_DOMAIN)
        else:
            # A lone value is treated as the active SESSION cookie.
            self._session.cookies.set("SESSION", cookie, domain=COOKIE_DOMAIN)

    def _login_with_credentials(self, username: str, password: str) -> None:
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        # Prime the cookie jar (some deployments set anti-CSRF cookies here).
        try:
            self._session.get(LOGIN_FORM_URL, timeout=30)
        except Exception:  # noqa: BLE001 - priming is best-effort
            pass

        data = {
            "urlAfterLogin": URL_AFTER_LOGIN,
            "username": username,
            "password": password,
            "uriOnError": "",
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE_URL,
            "Referer": LOGIN_FORM_URL,
        }
        response = self._session.post(
            SIGNIN_URL, data=data, headers=headers, allow_redirects=True, timeout=30
        )
        response.raise_for_status()

        logged_in = any(
            cookie.name in ("caelum.login.token", "SESSION")
            for cookie in self._session.cookies
        )
        if "/loginForm" in response.url or not logged_in:
            if "recaptcha" in response.text.lower():
                raise ConnectionError(
                    "Login bloqueado pelo reCAPTCHA. Use os cookies de sessão."
                )
            raise ConnectionError("Falha no login da Alura. Verifique e-mail e senha.")

    def _validate_session(self) -> None:
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")
        response = self._session.get(PROFILE_URL, allow_redirects=False, timeout=30)
        if response.status_code != 200 or "json" not in response.headers.get(
            "content-type", ""
        ):
            raise ConnectionError(
                "Cookies da Alura inválidos ou expirados. Faça login novamente."
            )

    @classmethod
    def requires_search(cls) -> bool:
        # Alura has no "list all my courses" endpoint; the user pastes a course
        # URL or slug instead.
        return True

    def fetch_courses(self) -> List[Dict[str, Any]]:
        # Without a search query there is nothing to enumerate. Surface the
        # in-progress courses from the dashboard as a convenience.
        return self._fetch_dashboard_courses()

    def search_courses(self, query: str) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        # A Carreira URL expands into every course it contains, in one session.
        if self._looks_like_career(query):
            career_slug = self._extract_career_slug(query)
            if career_slug:
                return self._resolve_career(career_slug)
            return []

        slug = self._extract_slug(query)
        if not slug:
            return []

        course = self._resolve_course(slug)
        return [course] if course else []

    def _resolve_course(self, slug: str) -> Optional[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        response = self._session.get(COURSE_BY_SLUG_URL.format(slug=slug), timeout=30)
        if response.status_code == 404:
            logger.warning("Alura: curso '%s' não encontrado.", slug)
            return None
        response.raise_for_status()
        data = response.json()

        course = data.get("course") or {}
        course_id = course.get("id")
        if not course_id:
            logger.warning("Alura: resposta sem id para o curso '%s'.", slug)
            return None

        has_access = (data.get("hasAccess") or {}).get("canAccess", True)
        if not has_access:
            logger.warning("Alura: sem acesso ao curso '%s'.", slug)
            return None

        return {
            "id": course_id,
            "name": course.get("name") or slug,
            "slug": course.get("code") or slug,
            "seller_name": "Alura",
        }
    
    # CARREIRA

    @staticmethod
    def _looks_like_career(query: str) -> bool:
        q = (query or "").lower()
        return "career" in q or "carreira" in q

    @staticmethod
    def _extract_career_slug(query: str) -> str:
        query = (query or "").strip()
        if not query:
            return ""
        match = CAREER_PATH_RE.search(query)
        if match:
            return match.group(1)
        # Bare slug (no path): use the trailing segment as-is.
        if "/" not in query:
            return query
        return ""

    @staticmethod
    def _career_slug_candidates(slug: str) -> List[str]:
        """The API slug usually omits the dashboard's "-v<id>" suffix, but try
        the slug verbatim first in case it is already canonical."""
        candidates = [slug]
        stripped = CAREER_VERSION_SUFFIX_RE.sub("", slug)
        if stripped and stripped != slug:
            candidates.append(stripped)
        return candidates

    def _resolve_career(self, slug: str) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        data, api_slug = self._get_career(slug)
        if not data:
            logger.warning("Alura: carreira '%s' não encontrada.", slug)
            return []

        title = data.get("title") or slug
        if not data.get("hasUserAccess", True):
            logger.warning("Alura: sem acesso à carreira '%s'.", title)

        steps = data.get("steps") or []
        courses: List[Dict[str, Any]] = []
        seen_codes: set[str] = set()

        for step in steps:
            step_id = step.get("id")
            if not step_id:
                continue
            for content in self._fetch_career_step_contents(api_slug, step_id):
                content_type = (content.get("contentType") or "").upper()
                code = content.get("code")
                if not code or code in seen_codes:
                    continue
                if content_type != "COURSE":
                    logger.debug(
                        "Alura: ignorando conteúdo '%s' (%s) da carreira.",
                        code,
                        content_type or "?",
                    )
                    continue
                seen_codes.add(code)
                course = self._resolve_course(code)
                if course:
                    course["career"] = title
                    courses.append(course)

        logger.info(
            "Alura: carreira '%s' expandida em %d curso(s).", title, len(courses)
        )
        return courses

    def _get_career(self, slug: str) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Returns (career data, slug that resolved) or (None, None)."""
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        for candidate in self._career_slug_candidates(slug):
            response = self._session.get(
                CAREER_BY_SLUG_URL.format(slug=candidate), timeout=30
            )
            if response.status_code == 404:
                continue
            response.raise_for_status()
            data = (response.json() or {}).get("data") or {}
            if data.get("id"):
                return data, candidate
        return None, None

    def _fetch_career_step_contents(
        self, slug: str, step_id: Any
    ) -> List[Dict[str, Any]]:
        try:
            response = self._session.get(
                CAREER_STEP_URL.format(slug=slug, step_id=step_id), timeout=30
            )
            response.raise_for_status()
            data = (response.json() or {}).get("data") or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Alura: falha ao obter etapa %s da carreira '%s': %s",
                step_id,
                slug,
                exc,
            )
            return []
        return data.get("contents") or []

    def _fetch_dashboard_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        try:
            response = self._session.get(
                f"{BASE_URL}/dashboard",
                headers={"Accept": "text/html,application/xhtml+xml,*/*"},
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alura: falha ao ler o dashboard: %s", exc)
            return []

        courses: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for slug in re.findall(r"/course/([a-z0-9-]+)", response.text):
            if slug in seen:
                continue
            seen.add(slug)
            course = self._resolve_course(slug)
            if course:
                courses.append(course)

        logger.info("Alura: %d curso(s) em andamento no dashboard.", len(courses))
        return courses

    @staticmethod
    def _extract_slug(query: str) -> str:
        query = (query or "").strip()
        if not query:
            return ""
        if "/" in query:
            match = re.search(r"/course/([a-zA-Z0-9-]+)", query)
            if match:
                return match.group(1)
            # Bare path or trailing segment fallback.
            return query.rstrip("/").rsplit("/", 1)[-1]
        return query

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}
        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            modules = self._fetch_modules(course_id)
            course_entry = dict(course)
            course_entry["title"] = course.get("name", "Curso")
            course_entry["modules"] = modules
            content[str(course_id)] = course_entry

        return content

    def _fetch_modules(self, course_id: Any) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        try:
            response = self._session.get(
                COURSE_SECTIONS_URL.format(course_id=course_id), timeout=60
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("Alura: falha ao obter seções do curso %s: %s", course_id, exc)
            return []

        modules: List[Dict[str, Any]] = []
        for section in data.get("sections") or []:
            tasks = section.get("tasks") or []
            lessons: List[Dict[str, Any]] = []
            for task in tasks:
                task_id = task.get("id")
                if not task_id:
                    continue
                lessons.append(
                    {
                        "id": task_id,
                        "title": task.get("title") or f"Aula {task.get('position', '')}".strip(),
                        "order": task.get("position", len(lessons) + 1),
                        "locked": False,
                        "kind": task.get("kind") or "",
                    }
                )

            if not lessons:
                continue

            modules.append(
                {
                    "id": section.get("sectionId"),
                    "title": section.get("sectionName") or f"Seção {section.get('number', '')}".strip(),
                    "order": section.get("number", len(modules) + 1),
                    "locked": False,
                    "lessons": lessons,
                }
            )

        return modules

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content = LessonContent()
        task_id = lesson.get("id")
        kind = (lesson.get("kind") or "").upper()
        title = lesson.get("title", "Aula")
        order = lesson.get("order", 1)

        detail = self._fetch_task_detail(course_id, task_id)
        if detail:
            description = self._build_description(detail)
            if description:
                content.description = description

        if kind in VIDEO_KINDS:
            self._append_video(content, course_id, task_id, title, order)

        return content

    def _fetch_task_detail(self, course_id: Any, task_id: Any) -> Dict[str, Any]:
        try:
            response = self._session.get(
                TASK_DETAIL_URL.format(course_id=course_id, task_id=task_id), timeout=30
            )
            response.raise_for_status()
            return response.json() or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alura: falha ao obter detalhes da tarefa %s: %s", task_id, exc)
            return {}

    @staticmethod
    def _build_description(detail: Dict[str, Any]) -> Optional[Description]:
        block = detail.get("content") or {}
        text = block.get("text")
        if text and text.strip():
            return Description(text=text, description_type="markdown")
        html = block.get("highlightedText")
        if html and html.strip():
            return Description(text=html, description_type="html")
        return None

    def _append_video(
        self, content: LessonContent, course_id: Any, task_id: Any, title: str, order: int
    ) -> None:
        try:
            response = self._session.get(
                TASK_VIDEO_URL.format(course_id=course_id, task_id=task_id), timeout=30
            )
            response.raise_for_status()
            variants = response.json() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Alura: falha ao obter vídeo da tarefa %s: %s", task_id, exc)
            return

        if not isinstance(variants, list) or not variants:
            return

        best = max(
            variants,
            key=lambda v: VIDEO_QUALITY_RANK.get((v.get("quality") or "").lower(), 0),
        )
        mp4_url = best.get("mp4")
        if not mp4_url:
            return

        content.videos.append(
            Video(
                video_id=self._video_id_from_url(mp4_url) or str(task_id),
                url=mp4_url,
                order=order,
                title=title,
                size=0,
                duration=0,
                extra_props={"referer": f"{BASE_URL}/"},
            )
        )

        # Subtitles ride along as .vtt attachments (deduped by URL).
        seen_subs: set[str] = set()
        sub_order = 1
        for sub in best.get("subtitles") or []:
            sub_url = sub.get("url")
            if not sub_url or sub_url in seen_subs:
                continue
            seen_subs.add(sub_url)
            code = sub.get("code") or "sub"
            content.attachments.append(
                Attachment(
                    attachment_id=f"sub-{task_id}-{code}",
                    url=sub_url,
                    filename=f"{self._sanitize_filename(title)}.{code}.vtt",
                    order=sub_order,
                    extension="vtt",
                    size=0,
                )
            )
            sub_order += 1

    @staticmethod
    def _video_id_from_url(url: str) -> str:
        basename = Path(urlparse(url).path).name
        # e.g. "0sjUeCnQPXo-hd.mp4" -> "0sjUeCnQPXo"
        stem = basename.rsplit(".", 1)[0]
        return re.sub(r"-(fullhd|hd|sd)$", "", stem)

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not attachment.url:
            logger.error("Alura: anexo sem URL: %s", attachment.filename)
            return False

        # Subtitle/media assets live on video2.alura.com.br and authenticate via
        # the token baked into the URL; they only require the site Referer, not
        # the session cookies, so a clean request keeps things simple.
        try:
            response = requests.get(
                attachment.url,
                headers={
                    "User-Agent": self._settings.user_agent,
                    "Referer": f"{BASE_URL}/",
                },
                stream=True,
                allow_redirects=True,
                timeout=60,
            )
            response.raise_for_status()

            download_path.parent.mkdir(parents=True, exist_ok=True)
            with open(download_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Alura: falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        cleaned = re.sub(r"[\\/:*?\"<>|]+", "-", name or "").strip()
        return cleaned or "arquivo"


#PlatformFactory.register_platform("Alura", AluraPlatform, slug=INTEGRATION_SLUG, version=INTEGRATION_VERSION, experimental=INTEGRATION_EXPERIMENTAL)
PlatformFactory.register_platform("Alura", AluraPlatform)
