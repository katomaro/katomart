from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from src.app.api_service import ApiService
from src.app.models import (
    Attachment,
    AuxiliaryURL,
    Description,
    LessonContent,
    Video,
)
from src.config.settings_manager import SettingsManager
from src.platforms.base import (
    AuthField,
    AuthFieldType,
    BasePlatform,
    PlatformFactory,
)

INTEGRATION_SLUG = "asimov_academy"
INTEGRATION_VERSION = "1.0.0"
INTEGRATION_EXPERIMENTAL = False

logger = logging.getLogger(__name__)

# Asimov Academy (hub.asimov.academy) is a WordPress-based subscription LMS.
# Content discovery is HTML-based: every catalog/curriculum page is server-rendered
# and authentication is cookie-based (a WordPress `wordpress_logged_in_<hash>`
# cookie). Content is laid out as Formação -> Trilha -> Curso/Projeto -> Atividade
# (lesson). Videos are Bunny.net Stream embeds (iframe.mediadelivery.net), handled
# downstream by `BunnyStreamDownloader`.
#
# There *is* a WordPress REST API under `/wp-json/wp/v1/*` (e.g. `activity/register`,
# `courses/all`, `formations/all`). The frontend drives it with axios, attaching the
# `X-WP-Nonce` read from the inline `wpApiSettings`/`hubVars` object localized into
# every logged-in page. We use `activity/register` to toggle lesson completion
# (`mark_lesson_watched`); enumeration stays on HTML scraping because the `*/all`
# endpoints return the public catalog, not the user's entitlements.
BASE_URL = "https://hub.asimov.academy"
COOKIE_DOMAIN = "hub.asimov.academy"
LOGIN_URL = f"{BASE_URL}/login/"
CATALOG_URL = f"{BASE_URL}/formacoes-e-trilhas/"

# The downloader re-fetches the Bunny embed itself; it only needs the parent page
# as Referer (Bunny validates the embed token against the referring origin).
BUNNY_REFERER = f"{BASE_URL}/"

# WordPress REST API used for lesson progress. `activity/register` is a *toggle*
# (it takes no desired-state argument — it flips the activity's completion), so
# `mark_lesson_watched` reads the current state and only POSTs on a mismatch. The
# nonce is localized inline as `wpApiSettings.nonce` (and `hubVars.nonce`) in every
# logged-in page.
ACTIVITY_REGISTER_URL = f"{BASE_URL}/wp-json/wp/v1/activity/register"
NONCE_RE = re.compile(r'wpApiSettings\s*=\s*\{[^}]*?"nonce"\s*:\s*"([0-9a-fA-F]+)"')

# A Bunny Stream embed inside a lesson page.
EMBED_SRC_RE = re.compile(r"iframe\.mediadelivery\.net/embed/\d+/[0-9a-fA-F-]{36}")
EMBED_GUID_RE = re.compile(r"/embed/\d+/([0-9a-fA-F-]{36})")

# A curso/projeto card links to its own page; the activities live in a reusable
# `lessons-<hash>` widget, grouped by uppercase section headers.
SECTION_HEADER_RE = re.compile(r"uppercase")
ACTIVITY_PATH_RE = re.compile(r"^/(?:curso|projeto)/atividade/[^/]+/?$")


class AsimovAcademyPlatform(BasePlatform):
    """Implements the Asimov Academy (hub.asimov.academy) platform.

    Discovery is *search based*: the user pastes a URL. Three shapes are accepted:

    - A **Formação** (`/formacao/<slug>/`) or **Trilha** (`/trilha/<slug>/`) ->
      expanded into *every* Curso/Projeto it contains, one katomart "course" per
      Curso/Projeto (mirrors how Alura expands a Carreira). The whole program is
      queued in a single authenticated session.
    - A single **Curso/Projeto** (`/curso/<slug>/`, `/projeto/<slug>/`) -> one
      course.
    - A single **Atividade** (`/curso/atividade/<slug>/`,
      `/projeto/atividade/<slug>/`) -> a one-lesson course.

    Content layout per course: modules come from the section headers of the
    `lessons-<hash>` curriculum widget; lessons are the activity rows. Each
    Atividade page carries a Bunny Stream embed (the streamable video) plus an
    optional "Baixar Material" link (usually a Google Drive folder/file, surfaced
    as an auxiliary URL because it is not a direct download).
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        # WP REST nonce, harvested lazily from any logged-in page's `wpApiSettings`.
        self._wp_nonce: Optional[str] = None

    @classmethod
    def token_field(cls) -> AuthField:
        return AuthField(
            name="token",
            label="Cookies de sessão",
            field_type=AuthFieldType.PASSWORD,
            placeholder="Cole o cabeçalho Cookie inteiro (ou o valor de wordpress_logged_in_*)",
            required=False,
        )

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes ativos podem informar e-mail e senha o login é feito automaticamente.

Na busca, cole a URL de uma Formação, Trilha, Curso ou Atividade:
• Formação: https://hub.asimov.academy/formacao/<slug>/
• Trilha:   https://hub.asimov.academy/trilha/<slug>/
• Curso:    https://hub.asimov.academy/curso/<slug>/
• Atividade:https://hub.asimov.academy/curso/atividade/<slug>/
Ao colar uma Formação ou Trilha, todos os cursos/projetos dela são enfileirados
de uma só vez, na mesma sessão.

Para obter os cookies de sessão (caso o login automático não funcione):
1) Acesse https://hub.asimov.academy e faça login normalmente.
2) Abra o DevTools (F12) → aba Rede (Network) e atualize a página.
3) Clique em qualquer requisição para hub.asimov.academy, vá em Cabeçalhos
   (Headers) e localize o cabeçalho de requisição "Cookie".
4) Copie o valor inteiro e cole no campo acima (o cookie essencial é
   wordpress_logged_in_<hash>).
""".strip()

    @classmethod
    def requires_search(cls) -> bool:
        # No "list all my courses" endpoint; the user pastes a Formação/Trilha/
        # Curso/Atividade URL.
        return True

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        cookie = (credentials.get("token") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self._settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
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
        logger.info("Sessão autenticada na Asimov Academy.")

    def _apply_cookie_string(self, cookie: str) -> None:
        """Loads a pasted Cookie header (or a bare logged-in cookie value) into the jar."""
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
            # A lone value is treated as the WordPress logged-in cookie.
            self._session.cookies.set(
                "wordpress_logged_in", cookie, domain=COOKIE_DOMAIN
            )

    def _login_with_credentials(self, username: str, password: str) -> None:
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        # Fetch the login form to harvest the WordPress/anti-CSRF hidden fields
        # (e.g. `_hub_nonce`, `_wp_http_referer`) that the POST must echo back.
        try:
            page = self._session.get(LOGIN_URL, timeout=30)
            page.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError(f"Falha ao abrir a página de login: {exc}") from exc

        soup = BeautifulSoup(page.text, "html.parser")
        form = self._find_login_form(soup)

        data: Dict[str, str] = {}
        action = LOGIN_URL
        if form is not None:
            action = urljoin(LOGIN_URL, form.get("action") or LOGIN_URL)
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    data[name] = inp.get("value", "")
            user_input = next(
                (
                    i
                    for i in form.find_all("input")
                    if (i.get("type") in ("text", "email") or i.get("name") == "login")
                ),
                None,
            )
            pass_input = form.find("input", {"type": "password"}) or form.find(
                "input", {"name": "password"}
            )
            data[(user_input.get("name") if user_input else "login") or "login"] = username
            data[(pass_input.get("name") if pass_input else "password") or "password"] = password
        else:
            data = {"login": username, "password": password, "_wp_http_referer": "/login/"}

        if "recaptcha" in page.text.lower() or "g-recaptcha" in page.text.lower():
            raise ConnectionError(
                "Login bloqueado pelo reCAPTCHA. Use os cookies de sessão."
            )

        response = self._session.post(
            action,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL,
                "Referer": LOGIN_URL,
            },
            allow_redirects=True,
            timeout=30,
        )
        response.raise_for_status()

        logged_in = any(
            c.name.startswith("wordpress_logged_in") for c in self._session.cookies
        )
        if not logged_in:
            raise ConnectionError(
                "Falha no login da Asimov Academy. Verifique e-mail e senha."
            )

    @staticmethod
    def _find_login_form(soup: BeautifulSoup) -> Optional[Tag]:
        for form in soup.find_all("form"):
            if form.find("input", {"type": "password"}) or form.find(
                "input", {"name": "password"}
            ):
                return form
        return None

    def _validate_session(self) -> None:
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")
        response = self._session.get(f"{BASE_URL}/", timeout=30)
        body = response.text or ""
        self._harvest_nonce(body)
        # Logged-in pages expose the account menu ("Sair"/"minha-conta") and never
        # render the login form (`_hub_nonce` / password input).
        authenticated = (
            response.status_code == 200
            and ("minha-conta" in body or "Sair" in body)
            and "_hub_nonce" not in body
        )
        if not authenticated:
            raise ConnectionError(
                "Sessão da Asimov Academy inválida ou expirada. Faça login novamente."
            )

    def fetch_courses(self) -> List[Dict[str, Any]]:
        # Nothing to enumerate without a query; the user pastes a URL in the search.
        return []

    def search_courses(self, query: str) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        url = self._normalize_query(query)
        if not url:
            return []

        path = urlparse(url).path.rstrip("/") + "/"

        if path.startswith("/formacao/") or path.startswith("/trilha/"):
            return self._expand_container(url)
        if "/atividade/" in path:
            return self._single_activity_course(url)
        if path.startswith("/curso/") or path.startswith("/projeto/"):
            return self._resolve_single_course(url)

        # Bare slug or unknown path: try the container endpoints, then the course.
        for builder in (
            lambda s: f"{BASE_URL}/formacao/{s}/",
            lambda s: f"{BASE_URL}/trilha/{s}/",
        ):
            courses = self._expand_container(builder(self._slug_of(url)))
            if courses:
                return courses
        return self._resolve_single_course(f"{BASE_URL}/curso/{self._slug_of(url)}/")

    @staticmethod
    def _normalize_query(query: str) -> str:
        query = (query or "").strip()
        if not query:
            return ""
        if query.startswith("http"):
            return query
        # Bare path or slug.
        if query.startswith("/"):
            return urljoin(BASE_URL, query)
        return f"{BASE_URL}/{query.strip('/')}/"

    @staticmethod
    def _slug_of(url: str) -> str:
        return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

    def _get_html(self, url: str) -> Optional[BeautifulSoup]:
        try:
            response = self._session.get(url, timeout=60)
            if response.status_code == 404:
                logger.warning("Asimov: página não encontrada: %s", url)
                return None
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.error("Asimov: falha ao abrir %s: %s", url, exc)
            return None
        self._harvest_nonce(response.text)
        return BeautifulSoup(response.text, "html.parser")

    def _harvest_nonce(self, html: str) -> None:
        """Caches the WP REST nonce from a logged-in page's inline `wpApiSettings`."""
        if self._wp_nonce:
            return
        match = NONCE_RE.search(html or "")
        if match:
            self._wp_nonce = match.group(1)

    def _expand_container(self, url: str) -> List[Dict[str, Any]]:
        """Expands a Formação/Trilha into one course per Curso/Projeto block."""
        soup = self._get_html(url)
        if soup is None:
            return []

        container_name = self._page_title(soup)
        courses: List[Dict[str, Any]] = []
        for ptype, slug, name, lessons_div in self._iter_curso_blocks(soup):
            modules = self._parse_curriculum([lessons_div]) if lessons_div else []
            if not modules:
                continue
            courses.append(
                {
                    "id": slug,
                    "name": name or slug,
                    "slug": slug,
                    "seller_name": "Asimov Academy",
                    "type": ptype,
                    "container": container_name,
                    "_url": f"{BASE_URL}/{ptype}/{slug}/",
                    "_modules": modules,
                }
            )

        logger.info(
            "Asimov: '%s' expandida em %d curso(s)/projeto(s).",
            container_name,
            len(courses),
        )
        return courses

    def _resolve_single_course(self, url: str) -> List[Dict[str, Any]]:
        soup = self._get_html(url)
        if soup is None:
            return []

        ptype = "projeto" if urlparse(url).path.startswith("/projeto/") else "curso"
        slug = self._slug_of(url)
        name = self._page_title(soup) or slug

        # The curriculum is the reusable `lessons-<hash>` widget; a curso page may
        # render one or several of them.
        lessons_divs = soup.find_all("div", id=lambda v: v and v.startswith("lessons-"))
        modules = self._parse_curriculum(lessons_divs)
        if not modules:
            # Fall back: the page may render itself as Curso/Projeto cards.
            blocks = list(self._iter_curso_blocks(soup))
            modules = self._parse_curriculum([d for _, _, _, d in blocks if d])

        if not modules:
            logger.warning("Asimov: nenhuma aula encontrada em %s", url)
            return []

        return [
            {
                "id": slug,
                "name": name,
                "slug": slug,
                "seller_name": "Asimov Academy",
                "type": ptype,
                "_url": url,
                "_modules": modules,
            }
        ]

    def _single_activity_course(self, url: str) -> List[Dict[str, Any]]:
        soup = self._get_html(url)
        if soup is None:
            return []
        slug = self._slug_of(url)
        title = self._page_title(soup) or slug
        ptype = "projeto" if "/projeto/" in urlparse(url).path else "curso"
        module = {
            "id": f"{slug}-m1",
            "title": title,
            "order": 1,
            "locked": False,
            "lessons": [
                {
                    "id": slug,
                    "title": title,
                    "order": 1,
                    "locked": False,
                    "kind": ptype,
                    "url": url,
                }
            ],
        }
        return [
            {
                "id": slug,
                "name": title,
                "slug": slug,
                "seller_name": "Asimov Academy",
                "type": ptype,
                "_url": url,
                "_modules": [module],
            }
        ]

    def _iter_curso_blocks(
        self, soup: BeautifulSoup
    ) -> List[Tuple[str, str, str, Optional[Tag]]]:
        """Yields (type, slug, name, lessons_div) for each Curso/Projeto card."""
        blocks: List[Tuple[str, str, str, Optional[Tag]]] = []
        seen: set[str] = set()
        for h2 in soup.find_all("h2"):
            anchor = h2.find("a", href=True)
            if not anchor:
                continue
            path = urlparse(anchor["href"]).path
            if "/atividade/" in path:
                continue
            if not (path.startswith("/curso/") or path.startswith("/projeto/")):
                continue
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            if slug in seen:
                continue
            seen.add(slug)
            ptype = "projeto" if path.startswith("/projeto/") else "curso"
            name = anchor.get_text(strip=True)
            lessons_div = self._find_lessons_div(h2)
            blocks.append((ptype, slug, name, lessons_div))
        return blocks

    @staticmethod
    def _find_lessons_div(node: Tag) -> Optional[Tag]:
        """Walks up from a card heading to find its `lessons-<hash>` widget."""
        card = node
        for _ in range(6):
            card = card.parent
            if card is None:
                return None
            found = card.find("div", id=lambda v: v and v.startswith("lessons-"))
            if found:
                return found
        return None

    def _parse_curriculum(self, containers: List[Optional[Tag]]) -> List[Dict[str, Any]]:
        """Builds modules from section headers + activity rows in one or more
        `lessons-<hash>` widgets."""
        modules: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        order = 1
        seen: set[str] = set()

        for container in containers:
            if not container:
                continue
            for el in container.descendants:
                if not isinstance(el, Tag):
                    continue
                if el.name == "span" and self._is_section_header(el):
                    title = el.get_text(strip=True)
                    if title:
                        current = {
                            "id": f"m{len(modules) + 1}",
                            "title": title,
                            "order": len(modules) + 1,
                            "locked": False,
                            "lessons": [],
                        }
                        modules.append(current)
                    continue
                if el.name == "a" and el.get("href"):
                    path = urlparse(el["href"]).path
                    if not ACTIVITY_PATH_RE.match(path):
                        continue
                    slug = path.rstrip("/").rsplit("/", 1)[-1]
                    if slug in seen:
                        continue
                    seen.add(slug)
                    title_span = el.find("span", class_="truncate")
                    title = (
                        title_span.get_text(strip=True)
                        if title_span
                        else el.get_text(" ", strip=True)
                    )
                    kind = "projeto" if "/projeto/" in path else "curso"
                    if current is None:
                        current = {
                            "id": f"m{len(modules) + 1}",
                            "title": "Aulas",
                            "order": len(modules) + 1,
                            "locked": False,
                            "lessons": [],
                        }
                        modules.append(current)
                    current["lessons"].append(
                        {
                            "id": slug,
                            "title": title or slug,
                            "order": order,
                            "locked": False,
                            "kind": kind,
                            "url": urljoin(BASE_URL, el["href"]),
                        }
                    )
                    order += 1

        # Drop empty modules (a header may precede no activities).
        return [m for m in modules if m["lessons"]]

    @staticmethod
    def _is_section_header(span: Tag) -> bool:
        classes = " ".join(span.get("class") or [])
        return "uppercase" in classes and "tracking-widest" in classes

    @staticmethod
    def _page_title(soup: BeautifulSoup) -> str:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            return og["content"].strip()
        if soup.title and soup.title.string:
            return soup.title.string.split("|")[0].strip()
        return ""

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}
        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            modules = course.get("_modules")
            if modules is None:
                # Course came without a stashed curriculum (e.g. resumed run).
                resolved = self._resolve_single_course(
                    course.get("_url") or f"{BASE_URL}/curso/{course.get('slug', course_id)}/"
                )
                modules = resolved[0]["_modules"] if resolved else []

            entry = dict(course)
            entry["title"] = course.get("name", "Curso")
            entry["modules"] = modules
            content[str(course_id)] = entry

        return content

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content = LessonContent()
        url = lesson.get("url")
        if not url:
            slug = lesson.get("id")
            kind = lesson.get("kind", "curso")
            url = f"{BASE_URL}/{kind}/atividade/{slug}/"

        soup = self._get_html(url)
        if soup is None:
            return content

        # Stash the numeric activity id + completion state so `mark_lesson_watched`
        # can hit `activity/register` without re-fetching this page.
        activity_id, parent_type, is_complete = self._resolve_activity_marker(soup)
        if activity_id:
            lesson["_activity_id"] = activity_id
            lesson["_parent_type"] = parent_type or self._parent_type_for_kind(
                lesson.get("kind", "curso")
            )
            lesson["_activity_complete"] = is_complete

        title = lesson.get("title") or self._page_title(soup) or "Aula"
        order = lesson.get("order", 1)

        # Description from the social meta (short, safe; the body is JS-rendered).
        og_desc = soup.find("meta", property="og:description")
        if og_desc and (og_desc.get("content") or "").strip():
            content.description = Description(
                text=og_desc["content"].strip(), description_type="text"
            )

        self._append_video(content, soup, title, order)
        self._append_materials(content, soup, title)

        return content

    def _append_video(
        self, content: LessonContent, soup: BeautifulSoup, title: str, order: int
    ) -> None:
        iframe = None
        player = soup.find(id="bunnyPlayer")
        if player:
            iframe = player.find("iframe", src=EMBED_SRC_RE)
        if iframe is None:
            iframe = soup.find("iframe", src=EMBED_SRC_RE)
        if iframe is None:
            return

        # The HTML entity-encodes the query separators; normalise to a real URL.
        embed_url = iframe["src"].replace("&#038;", "&").replace("&amp;", "&")
        guid_match = EMBED_GUID_RE.search(embed_url)
        video_id = guid_match.group(1) if guid_match else str(order)

        content.videos.append(
            Video(
                video_id=video_id,
                url=embed_url,
                order=order,
                title=title,
                size=0,
                duration=0,
                extra_props={"referer": BUNNY_REFERER, "parent_referer": BUNNY_REFERER},
            )
        )

    def _append_materials(
        self, content: LessonContent, soup: BeautifulSoup, title: str
    ) -> None:
        order = 1
        seen: set[str] = set()
        for link in soup.find_all("a", class_="activity-material", href=True):
            href = link["href"].strip()
            if not href or href in seen:
                continue
            seen.add(href)

            host = urlparse(href).netloc.lower()
            # Google Drive (and other external lockers) are not direct downloads,
            # so they ride along as auxiliary URLs rather than attachments.
            if "drive.google.com" in host or "docs.google.com" in host:
                content.auxiliary_urls.append(
                    AuxiliaryURL(
                        url_id=f"material-{order}",
                        url=href,
                        order=order,
                        title="Material complementar",
                        description=link.get("title", "Baixar material"),
                    )
                )
            else:
                filename = Path(urlparse(href).path).name or f"material-{order}"
                content.attachments.append(
                    Attachment(
                        attachment_id=f"material-{order}",
                        url=href,
                        filename=filename,
                        order=order,
                        extension=Path(filename).suffix.lstrip(".") or "bin",
                        size=0,
                    )
                )
            order += 1

    @staticmethod
    def _parent_type_for_kind(kind: str) -> str:
        # The REST API expects the post-type archive name (plural).
        return "projects" if kind == "projeto" else "courses"

    def _lesson_url(self, lesson: Dict[str, Any]) -> str:
        url = lesson.get("url")
        if url:
            return url
        kind = lesson.get("kind", "curso")
        return f"{BASE_URL}/{kind}/atividade/{lesson.get('id')}/"

    def _resolve_activity_marker(
        self, soup: Optional[BeautifulSoup]
    ) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
        """Reads (activity_id, parent_type, is_complete) for the page's current
        activity from `#completeActivityBtn` and its `[data-complete-activity]`
        checkbox in the curriculum nav (the `checked` attribute marks completion)."""
        if soup is None:
            return None, None, None

        btn = soup.find(id="completeActivityBtn")
        activity_id = btn.get("data-id") if btn else None
        parent_type = btn.get("parent-type") if btn else None

        checkbox = soup.select_one("#activityNav div.current [data-complete-activity]")
        if checkbox is None and activity_id:
            checkbox = soup.find(
                lambda t: isinstance(t, Tag)
                and t.name == "input"
                and t.has_attr("data-complete-activity")
                and t.get("data-id") == activity_id
            )

        if checkbox is not None:
            activity_id = activity_id or checkbox.get("data-id")
            parent_type = parent_type or checkbox.get("parent-type")
            is_complete: Optional[bool] = checkbox.has_attr("checked")
        elif btn is not None:
            is_complete = btn.get("data-complete") == "true"
        else:
            is_complete = None

        return activity_id, parent_type, is_complete

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        activity_id = lesson.get("_activity_id")
        parent_type = lesson.get("_parent_type")
        is_complete = lesson.get("_activity_complete")

        # Resume runs (or a direct call) may lack the stashed marker; resolve it from
        # the activity page on demand.
        if activity_id is None or is_complete is None:
            soup = self._get_html(self._lesson_url(lesson))
            resolved_id, resolved_type, resolved_state = self._resolve_activity_marker(soup)
            activity_id = activity_id or resolved_id
            parent_type = parent_type or resolved_type
            if is_complete is None:
                is_complete = resolved_state

        if not activity_id:
            logger.warning(
                "Asimov: não foi possível identificar a atividade de '%s'; status não atualizado.",
                lesson.get("title") or lesson.get("id"),
            )
            return

        parent_type = parent_type or self._parent_type_for_kind(lesson.get("kind", "curso"))

        # `activity/register` is a toggle: only POST when the current state differs
        # from the desired one, otherwise we would invert it.
        if is_complete is not None and bool(is_complete) == bool(watched):
            logger.info(
                "Asimov: atividade %s já está %s; nada a fazer.",
                activity_id,
                "concluída" if watched else "não concluída",
            )
            return

        self._register_activity_toggle(str(activity_id), parent_type)
        lesson["_activity_complete"] = bool(watched)
        logger.info(
            "Asimov: atividade %s marcada como %s.",
            activity_id,
            "concluída" if watched else "não concluída",
        )

    def _register_activity_toggle(self, activity_id: str, parent_type: str) -> None:
        if not self._wp_nonce:
            # The nonce is localized in every logged-in page; grab one if missing.
            self._get_html(f"{BASE_URL}/")
        if not self._wp_nonce:
            raise ConnectionError(
                "Asimov: nonce da API (X-WP-Nonce) não encontrado; faça login novamente."
            )

        response = self._session.post(
            ACTIVITY_REGISTER_URL,
            json={"id": activity_id, "parent_type": parent_type},
            headers={
                "X-WP-Nonce": self._wp_nonce,
                "Accept": "application/json, text/plain, */*",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
            },
            timeout=30,
        )
        try:
            response.raise_for_status()
        except Exception:
            # A rotated/expired nonce yields 403 (rest_cookie_invalid_nonce); drop it
            # so the next attempt re-harvests a fresh one.
            self._wp_nonce = None
            raise

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not attachment.url:
            logger.error("Asimov: anexo sem URL: %s", attachment.filename)
            return False
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        try:
            response = self._session.get(
                attachment.url,
                headers={"Referer": f"{BASE_URL}/"},
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
            logger.error("Asimov: falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


#PlatformFactory.register_platform("Asimov Academy", AsimovAcademyPlatform, slug=INTEGRATION_SLUG, version=INTEGRATION_VERSION, experimental=INTEGRATION_EXPERIMENTAL)
PlatformFactory.register_platform("Asimov Academy", AsimovAcademyPlatform)
