from typing import Any, Dict, List, Optional
import requests
import logging
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, unquote

from bs4 import BeautifulSoup

from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.app.models import Attachment, Description, LessonContent, Video
from src.app.api_service import ApiService
from src.config.settings_manager import SettingsManager

logger = logging.getLogger(__name__)


BASE_URL = "https://padrepauloricardo.org"
LOGIN_PAGE_URL = f"{BASE_URL}/entrar"
LOGIN_POST_URL = f"{BASE_URL}/entrar"
ACCOUNT_URL = f"{BASE_URL}/minha_conta/perfil"
SESSION_COOKIE_NAME = "_padrepauloricardo_session"
REMEMBER_COOKIE_NAME = "remember_user_token"


class PadrePauloRicardoPlatform(BasePlatform):
    """
    Implements the Padre Paulo Ricardo platform via HTML scraping.

    Course catalog and module/lesson trees are public, but lesson video
    embeds and attachments require an active subscription. Authentication
    follows the standard Devise/Rails flow: GET /entrar to capture the
    authenticity_token, then POST /entrar with email/password.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self.base_url = BASE_URL

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
1) Usuário e senha: informe o e-mail e senha cadastrados em
   padrepauloricardo.org. O Katomart fará o login pelo formulário /entrar.
2) Login via navegador (Emular Navegador): marque a opção quando precisar
   resolver captcha ou 2FA. Uma janela do Chromium será aberta na página
   de login; complete o acesso normalmente e clique em "OK" no diálogo
   do Katomart para extrair os cookies da sessão.

3) Cookie de sessão (token): cole o valor do cookie
   "_padrepauloricardo_session" obtido em uma sessão já autenticada do
   navegador. Funciona como atalho quando o login automatizado falhar.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        cookie = (credentials.get("token") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()
        use_browser_emulation = bool(credentials.get("browser_emulation"))

        session = requests.Session()
        session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": self.base_url + "/",
        })
        self._session = session

        if cookie:
            session.cookies.set(
                SESSION_COOKIE_NAME, cookie, domain="padrepauloricardo.org"
            )
        elif use_browser_emulation:
            confirmation_event = credentials.get("manual_auth_confirmation")
            self._login_via_playwright(username, password, confirmation_event)
        elif username and password and self._settings.has_full_permissions:
            self._login_with_credentials(username, password)
        elif username or password:
            raise ValueError(
                "Informe usuário e senha completos, marque \"Emular Navegador\" "
                "ou utilize um cookie de sessão."
            )
        else:
            logger.info(
                "Padre Paulo Ricardo: prosseguindo sem credenciais "
                "(somente metadados públicos estarão disponíveis)."
            )
            return

        self._validate_session()
        logger.info("Padre Paulo Ricardo: sessão autenticada.")

    def _login_with_credentials(self, username: str, password: str) -> None:
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        page = self._session.get(LOGIN_PAGE_URL, timeout=30)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        csrf_input = soup.select_one("form#loginForm input[name='authenticity_token']")
        if not csrf_input or not csrf_input.get("value"):
            raise ValueError(
                "Não foi possível obter o authenticity_token de /entrar."
            )

        # The form is data-remote=true (Rails UJS) so the server replies with
        # JSON regardless of Accept: 401 + {"message":"error"} on failure,
        # 200/201 on success.
        data = {
            "authenticity_token": csrf_input["value"],
            "user[email]": username,
            "user[password]": password,
            "user[remember_me]": "1",
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.base_url,
            "Referer": LOGIN_PAGE_URL,
            "X-CSRF-Token": csrf_input["value"],
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        response = self._session.post(
            LOGIN_POST_URL, data=data, headers=headers, allow_redirects=False, timeout=30
        )

        if response.status_code >= 400:
            detail = self._extract_login_error(response)
            if "recaptcha" in (response.text or "").lower():
                raise ConnectionError(
                    "Login bloqueado por captcha. Marque \"Emular Navegador\" "
                    "ou cole o cookie _padrepauloricardo_session no campo Token."
                )
            raise ConnectionError(
                f"Falha no login Padre Paulo Ricardo. Verifique e-mail e senha. "
                f"({detail})"
            )

    @staticmethod
    def _extract_login_error(response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("message"):
                return f"HTTP {response.status_code}: {payload['message']}"
        except ValueError:
            pass
        return f"HTTP {response.status_code}"

    def _login_via_playwright(
        self,
        username: str,
        password: str,
        confirmation_event: Optional[threading.Event],
    ) -> None:
        """Opens a headful Chromium so the user can resolve captcha/2FA, then
        copies the resulting cookies into the requests.Session."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ConnectionError(
                "Playwright não está instalado. Execute 'playwright install'."
            ) from exc

        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        cookies_holder: List[Dict[str, Any]] = []
        error_holder: List[BaseException] = []
        finished = threading.Event()
        ua = self._settings.user_agent

        def _runner() -> None:
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(
                        headless=False,
                        args=["--disable-blink-features=AutomationControlled"],
                    )
                    context = browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        user_agent=ua,
                    )
                    page = context.new_page()
                    logger.info(
                        "Padre Paulo Ricardo: abrindo navegador para login..."
                    )
                    page.goto(LOGIN_PAGE_URL, wait_until="domcontentloaded", timeout=60000)

                    if username:
                        try:
                            page.fill("input#inputEmail", username)
                        except Exception:
                            pass
                    if password:
                        try:
                            page.fill("input#inputPassword", password)
                        except Exception:
                            pass

                    if confirmation_event is not None:
                        logger.info(
                            "Padre Paulo Ricardo: aguardando confirmação manual..."
                        )
                        confirmation_event.wait()
                    else:
                        logger.warning(
                            "Padre Paulo Ricardo: sem evento de confirmação. "
                            "Aguardando saída da página /entrar (5 min)..."
                        )
                        try:
                            page.wait_for_url(
                                lambda url: "/entrar" not in url, timeout=300_000
                            )
                        except Exception:
                            pass

                    cookies_holder.extend(context.cookies())
                    try:
                        context.close()
                        browser.close()
                    except Exception:
                        pass
            except BaseException as exc:
                error_holder.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(
            target=_runner, daemon=True, name="playwright-padrepauloricardo"
        )
        thread.start()
        finished.wait()

        if error_holder:
            raise ConnectionError(
                "Falha ao abrir o navegador para login Padre Paulo Ricardo."
            ) from error_holder[0]

        for c in cookies_holder:
            self._session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
            )

        if not any(c["name"] == SESSION_COOKIE_NAME for c in cookies_holder):
            raise ConnectionError(
                "Login pelo navegador não retornou um cookie de sessão. "
                "Confirme que você concluiu o login antes de clicar em OK."
            )

    def _validate_session(self) -> None:
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")
        response = self._session.get(ACCOUNT_URL, allow_redirects=False, timeout=30)
        if response.status_code in (301, 302, 303, 307, 308):
            target = response.headers.get("Location", "")
            if "/entrar" in target:
                raise ConnectionError(
                    "Sessão Padre Paulo Ricardo inválida ou expirada."
                )

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Session not authenticated.")

        response = self._session.get(self.base_url + "/cursos", timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        courses: List[Dict[str, Any]] = []

        course_links = soup.select("a.course-card") or soup.select("a.course")

        for link in course_links:
            href = (link.get("href") or "").strip()
            if not href:
                continue

            clean_path = href.split("?")[0].split("#")[0].strip("/")
            parts = clean_path.split("/")
            if len(parts) < 2 or parts[0] != "cursos" or parts[1] == "categoria":
                continue
            course_slug = "/".join(parts[1:])
            if not course_slug:
                continue

            title_elem = (
                link.select_one(".course-card__title")
                or link.select_one(".course__title")
            )
            course_title = title_elem.get_text(strip=True) if title_elem else f"Curso {course_slug}"

            desc_elem = link.select_one(".course__description")
            course_description = desc_elem.get_text(strip=True) if desc_elem else ""

            courses.append({
                "id": course_slug,
                "name": course_title,
                "slug": course_slug,
                "description": course_description,
                "seller_name": "Padre Paulo Ricardo",
            })

        logger.debug("Fetched %d courses from padrepauloricardo.org", len(courses))
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Session not authenticated.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_slug = course.get("slug", "")
            course_id = course.get("id", course_slug)
            course_url = urljoin(self.base_url, f"/cursos/{course_slug}")

            logger.debug("Fetching course content from %s", course_url)

            try:
                response = self._session.get(course_url, timeout=30)
                response.raise_for_status()
            except Exception as exc:
                logger.error("Failed to fetch course %s: %s", course_slug, exc)
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            modules = self._parse_course_modules(soup, course_slug)

            course_entry = course.copy()
            course_entry["title"] = course.get("name", f"Curso {course_slug}")
            course_entry["modules"] = modules
            all_content[course_id] = course_entry

        return all_content

    def _parse_course_modules(self, soup: BeautifulSoup, course_slug: str) -> List[Dict[str, Any]]:
        modules: List[Dict[str, Any]] = []

        categories = soup.select("div.category")

        for module_order, category in enumerate(categories, start=1):
            header = category.select_one(".category__header h4.category__title")
            module_title = header.get_text(strip=True) if header else f"Módulo {module_order}"

            module_id = re.sub(r"[^a-z0-9]+", "-", module_title.lower()).strip("-")

            lessons: List[Dict[str, Any]] = []
            lesson_links = category.select("a.class[href*='/aulas/']")

            for lesson_order, link in enumerate(lesson_links, start=1):
                href = (link.get("href") or "").strip()
                lesson_id = link.get("id", "")

                if not href:
                    continue

                lesson_slug = href.lstrip("/").replace("aulas/", "")

                title_elem = link.select_one(".class__title")
                if title_elem:
                    title_text = title_elem.get_text(strip=True)
                    title_text = re.sub(r"^\d+\.\s+", "", title_text)
                else:
                    title_text = f"Aula {lesson_order}"

                duration_elem = link.select_one(".class__duration")
                duration_text = duration_elem.get_text(strip=True) if duration_elem else ""

                lessons.append({
                    "id": lesson_id or lesson_slug,
                    "title": title_text,
                    "slug": lesson_slug,
                    "order": lesson_order,
                    "duration_text": duration_text,
                    "locked": False,
                })

            modules.append({
                "id": module_id,
                "title": module_title,
                "order": module_order,
                "lessons": lessons,
                "locked": False,
            })

        logger.debug("Parsed %d modules from course", len(modules))
        return modules

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Session not authenticated.")

        lesson_slug = lesson.get("slug", "")
        if not lesson_slug:
            raise ValueError("Lesson slug is missing.")

        lesson_url = urljoin(self.base_url, f"/aulas/{lesson_slug}")
        logger.debug("Fetching lesson details from %s", lesson_url)

        try:
            response = self._session.get(lesson_url, timeout=30)
            response.raise_for_status()
        except Exception as exc:
            logger.error("Failed to fetch lesson %s: %s", lesson_slug, exc)
            raise

        soup = BeautifulSoup(response.text, "html.parser")
        content = LessonContent()

        desc_div = soup.select_one("article.lesson-text__editor")
        if desc_div:
            cleaned = self._clean_description_html(desc_div)
            if cleaned.strip():
                content.description = Description(text=cleaned, description_type="html")

        video = self._extract_bunny_video(soup, lesson)
        if video:
            content.videos.append(video)

        attachments = self._extract_attachments(soup, lesson)
        content.attachments.extend(attachments)

        return content

    @staticmethod
    def _clean_description_html(desc_div) -> str:
        """Returns the description HTML with non-content nodes stripped.

        Inline SVG icons (used for the "Conteúdo gratuito" lock callout etc.)
        carry an ``xmlns="http://www.w3.org/2000/svg"`` attribute that the
        worker's URL extractor would otherwise pick up and feed to yt-dlp.
        Scripts and styles are stripped for the same reason.
        The "lesson-text__warning" callout shown to non-subscribers is also
        dropped — it is presentation noise, not lesson content.
        """
        from copy import copy

        cloned = copy(desc_div)
        for tag in cloned.find_all(["svg", "script", "style"]):
            tag.decompose()
        for tag in cloned.select(".lesson-text__warning"):
            tag.decompose()
        return str(cloned)

    def _extract_bunny_video(self, soup: BeautifulSoup, lesson: Dict[str, Any]) -> Optional[Video]:
        """Extracts the Bunny MediaDelivery iframe URL from the player wrapper.

        Restricted to the lesson header area (#video-container) so unrelated
        iframes elsewhere on the page cannot be mistaken for the lesson video.
        """

        scope = (
            soup.select_one("#video-container")
            or soup.select_one(".lesson-header__player-wrapper")
            or soup.select_one(".lesson-header")
        )
        if scope is None:
            logger.debug(
                "No video scope found for lesson %s (likely requires auth).",
                lesson.get("id"),
            )
            return None

        bunny_iframe = scope.select_one("iframe[src*='player.mediadelivery.net']")
        embed_url = bunny_iframe.get("src", "").strip() if bunny_iframe else ""

        if not embed_url:
            scope_html = str(scope)
            embed_url = self._find_bunny_embed_url(scope_html) or self._find_bunny_hls_url(scope_html)

        if not embed_url:
            logger.debug("No Bunny video found for lesson %s", lesson.get("id"))
            return None

        return self._build_bunny_video(embed_url, lesson)

    def _find_bunny_embed_url(self, html: str) -> Optional[str]:
        match = re.search(r'https://player\.mediadelivery\.net/embed/[^\s"\'<>]+', html)
        if not match:
            return None
        return match.group(0)

    def _find_bunny_hls_url(self, html: str) -> Optional[str]:
        match = re.search(r'https://[^\s"\'<>]+\.b-cdn\.net/[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html)
        if not match:
            return None
        return match.group(0)

    def _build_bunny_video(self, video_url: str, lesson: Dict[str, Any]) -> Optional[Video]:
        if not video_url:
            return None

        video_url = (
            video_url.replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
        )

        video_id_match = re.search(r"/embed/(\d+)/([a-f0-9\-]+)", video_url)
        if video_id_match:
            video_id = f"{video_id_match.group(1)}_{video_id_match.group(2)}"
        else:
            video_id = lesson.get("id", "bunny-video")

        return Video(
            video_id=video_id,
            url=video_url,
            title=lesson.get("title", "Aula"),
            order=1,
            size=0,
            duration=0,
            extra_props={
                "host": "bunny",
                "platform": "padrepauloricardo",
                "referer": self.base_url + "/",
            }
        )

    def _extract_attachments(self, soup: BeautifulSoup, lesson: Dict[str, Any]) -> List[Attachment]:
        """Extracts attachments from the lesson's "Material para Download" block.

        The page also surfaces R2 URLs as thumbnails inside the course's lesson
        list — those are filtered out by scoping to ``section.downloads-container
        a.downloads-item__link``.
        """

        attachments: List[Attachment] = []

        download_links = soup.select(
            "section.downloads-container a.downloads-item__link[href]"
        )

        for idx, link in enumerate(download_links, start=1):
            href = (link.get("href") or "").strip()
            if not href:
                continue

            href = (
                href.replace("&amp;", "&")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
            )

            filename = self._extract_filename_from_url(href)
            if not filename:
                slug = (link.get("download") or "").strip()
                link_text = link.get_text(strip=True)
                filename = slug or link_text or f"Anexo {idx}"

            extension = filename.split(".")[-1].lower() if "." in filename else ""

            attachment_id = f"{lesson.get('id', 'lesson')}_attachment_{idx}"

            attachments.append(
                Attachment(
                    attachment_id=attachment_id,
                    url=href,
                    filename=filename,
                    order=idx,
                    extension=extension,
                    size=0,
                )
            )

        logger.debug(
            "Found %d attachments for lesson %s", len(attachments), lesson.get("id")
        )
        return attachments

    @staticmethod
    def _extract_filename_from_url(url: str) -> Optional[str]:
        """Extracts filename from a Cloudflare R2 presigned URL."""
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        qs_lower = {k.lower(): v for k, v in qs.items()}

        disposition = qs_lower.get("response-content-disposition", [""])[0]
        if disposition:
            filename_match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
            if filename_match:
                return unquote(filename_match.group(1).strip().strip('"'))
            filename_match = re.search(r"filename=([^;]+)", disposition, re.IGNORECASE)
            if filename_match:
                return unquote(filename_match.group(1).strip().strip('"'))

        path = url.split("?")[0] if "?" in url else url
        if "/" in path:
            tail = path.split("/")[-1]
            if "." in tail:
                return tail

        return None

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            raise ConnectionError("Session not authenticated.")

        try:
            url = attachment.url
            if not url:
                logger.error(
                    "PadrePauloRicardo: attachment has no URL: %s", attachment.filename
                )
                return False

            logger.info("Downloading attachment: %s from %s", attachment.filename, url)

            response = self._session.get(url, stream=True, timeout=120)
            response.raise_for_status()

            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            logger.info("Successfully downloaded attachment: %s", attachment.filename)
            return True

        except Exception as exc:
            logger.error("Failed to download attachment %s: %s", attachment.filename, exc)
            return False

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def close(self) -> None:
        if self._session:
            self._session.close()


PlatformFactory.register_platform("Padre Paulo Ricardo", PadrePauloRicardoPlatform)
