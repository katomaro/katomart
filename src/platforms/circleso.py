from __future__ import annotations
import logging
import queue
import re
import threading
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional, TypeVar
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Playwright

from src.app.api_service import ApiService
from src.app.models import (
    Attachment,
    AuxiliaryURL,
    Description,
    LessonContent,
    Video,
)
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

T = TypeVar("T")


class CircleSoPlatform(BasePlatform):
    """
    Platform implementation for Circle.so communities.
    Uses a persistent Playwright browser for content listing and extraction.

    All Playwright operations run on a dedicated thread because sync_playwright
    uses greenlets that are bound to the thread that created them. Since katomart
    runs each worker (FetchCoursesWorker, FetchModulesWorker, DownloadWorker) in
    different QRunnable threads, we need a single persistent thread for Playwright.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._base_url: str = ""
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._browser_context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._pw_queue: Optional[queue.Queue] = None
        self._pw_thread: Optional[threading.Thread] = None

    # ── Auth fields ──────────────────────────────────────────────

    @classmethod
    def all_auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="site_url",
                label="URL do site Circle.so",
                field_type=AuthFieldType.TEXT,
                placeholder="Ex: https://www.techleads.club",
                required=True,
            ),
            AuthField(
                name="email",
                label="E-mail",
                field_type=AuthFieldType.TEXT,
                placeholder="seu@email.com",
                required=False,
            ),
            AuthField(
                name="browser_emulation",
                label="Login via navegador (obrigatório)",
                field_type=AuthFieldType.CHECKBOX,
                required=False,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return (
            "Plataformas Circle.so geralmente usam login por magic link.\n\n"
            "1. Informe a URL base do site (ex: https://www.techleads.club)\n"
            "2. Um navegador será aberto automaticamente\n"
            "3. Faça login normalmente (magic link, e-mail/senha, etc.)\n"
            "4. Se o link abrir em outro navegador, copie a URL e cole no navegador do katomart\n"
            "5. Após o login, clique em 'Confirmar' no katomart"
        )

    # ── Playwright thread management ─────────────────────────────

    def _start_pw_thread(self) -> None:
        """Start the dedicated Playwright thread."""
        self._pw_queue = queue.Queue()
        self._pw_thread = threading.Thread(
            target=self._pw_loop, daemon=True, name="playwright-circleso"
        )
        self._pw_thread.start()

    def _pw_loop(self) -> None:
        """Main loop for the Playwright thread — processes dispatched callables."""
        while True:
            item = self._pw_queue.get()
            if item is None:  # Shutdown signal
                break
            func, future = item
            try:
                result = func()
                future.set_result(result)
            except BaseException as e:
                future.set_exception(e)

    def _pw_exec(self, func: Callable[[], T]) -> T:
        """Dispatch a callable to the Playwright thread and block until done."""
        if not self._pw_thread or not self._pw_thread.is_alive():
            raise ConnectionError(
                "O navegador não está conectado. Reautentique na plataforma."
            )
        future: Future[T] = Future()
        self._pw_queue.put((func, future))
        return future.result(timeout=600)

    # ── Lifecycle ────────────────────────────────────────────────

    def close(self) -> None:
        if self._pw_queue and self._pw_thread and self._pw_thread.is_alive():
            # Dispatch cleanup TO the Playwright thread (resources are thread-bound)
            def _cleanup():
                if self._page:
                    try:
                        self._page.close()
                    except Exception:
                        pass
                    self._page = None
                if self._browser_context:
                    try:
                        self._browser_context.close()
                    except Exception:
                        pass
                    self._browser_context = None
                if self._browser:
                    try:
                        self._browser.close()
                    except Exception:
                        pass
                    self._browser = None
                if self._playwright:
                    try:
                        self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None

            try:
                self._pw_exec(_cleanup)
            except Exception:
                pass
            # Shut down the thread
            self._pw_queue.put(None)
            self._pw_thread.join(timeout=10)

        self._pw_thread = None
        self._pw_queue = None
        self._page = None
        self._browser_context = None
        self._browser = None
        self._playwright = None

    # ── Authentication ───────────────────────────────────────────

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        self.close()  # Clean up any prior browser + thread

        self._base_url = (credentials.get("site_url") or "").strip().rstrip("/")
        if not self._base_url:
            raise ValueError("A URL base do site Circle.so é obrigatória.")

        # Force browser emulation — this platform always needs headful browser
        credentials["browser_emulation"] = True

        logging.info("Circle.so: Iniciando navegador para autenticação...")

        # Start the dedicated Playwright thread
        self._start_pw_thread()

        # Launch browser and navigate to login — all in the PW thread
        login_url = f"{self._base_url}/sign_in"

        def _launch_and_navigate():
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._browser_context = self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=self._settings.user_agent,
            )
            self._page = self._browser_context.new_page()
            logging.info("Circle.so: Navegando para página de login...")
            self._page.goto(login_url, wait_until="networkidle", timeout=60000)

        try:
            self._pw_exec(_launch_and_navigate)
        except Exception:
            self.close()
            raise

        # Wait for user to complete login
        confirmation_event = credentials.get("manual_auth_confirmation")
        if confirmation_event:
            logging.info(
                "Circle.so: Aguardando confirmação de login do usuário..."
            )
            confirmation_event.wait()
        else:
            # Fallback: wait for URL to change away from login page (in PW thread)
            base_url = self._base_url
            logging.warning(
                "Circle.so: Sem evento de confirmação. "
                "Aguardando redirecionamento para /feed..."
            )
            self._pw_exec(
                lambda: self._page.wait_for_url(
                    f"{base_url}/feed**", timeout=300000
                )
            )

        logging.info("Circle.so: Login confirmado. Extraindo cookies...")

        # Extract cookies and set up requests.Session
        cookies = self._pw_exec(lambda: self._browser_context.cookies())
        self._setup_session(cookies)

        logging.info("Circle.so: Autenticação concluída com sucesso.")

    def _setup_session(self, cookies: List[Dict[str, Any]]) -> None:
        """Configure requests.Session from browser cookies."""
        session = requests.Session()
        session.headers.update({"User-Agent": self._settings.user_agent})
        for cookie in cookies:
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
        self._session = session

    def refresh_auth(self) -> None:
        """Refresh by navigating to home — don't relaunch the browser."""
        try:
            base_url = self._base_url
            self._pw_exec(
                lambda: self._page.goto(
                    f"{base_url}/feed", wait_until="networkidle", timeout=30000
                )
            )
            logging.info("Circle.so: Sessão atualizada com sucesso.")
        except Exception as e:
            logging.error(f"Circle.so: Falha ao atualizar sessão - {e}")
            raise ConnectionError(
                "Sessão expirada. Reautentique na plataforma."
            ) from e

    # ── Course listing ───────────────────────────────────────────

    def fetch_courses(self) -> List[Dict[str, Any]]:
        def _do_fetch() -> List[Dict[str, Any]]:
            page = self._page

            logging.info("Circle.so: Navegando para lista de cursos...")
            page.goto(f"{self._base_url}/courses", wait_until="networkidle", timeout=60000)

            # Wait for course cards to appear
            page.wait_for_selector("a[href*='/c/']", timeout=30000)

            # Scroll to bottom to ensure all courses are loaded
            prev_count = 0
            for _ in range(10):
                cards = page.query_selector_all("a[href*='/c/']")
                if len(cards) == prev_count and prev_count > 0:
                    break
                prev_count = len(cards)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)

            # Scrape course cards
            courses: List[Dict[str, Any]] = []
            seen_slugs: set = set()

            cards = page.query_selector_all("a[href*='/c/']")
            for card in cards:
                href = card.get_attribute("href") or ""
                match = re.match(r"^/c/([\w-]+)$", href)
                if not match:
                    continue

                slug = match.group(1)
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)

                # Get title from the card's heading element
                title_el = card.query_selector("h1, h2, h3, h4, span.font-bold")
                title = title_el.inner_text().strip() if title_el else slug

                # Get course type badge (Curso, Trilha, Gravação)
                badge_el = card.query_selector("span.text-xs, [class*='badge']")
                course_type = badge_el.inner_text().strip() if badge_el else ""

                courses.append(
                    {
                        "id": slug,
                        "name": title,
                        "slug": slug,
                        "seller_name": "Circle.so",
                        "course_type": course_type,
                        "lessons_count": 0,
                    }
                )

            logging.info(f"Circle.so: Encontrados {len(courses)} cursos.")
            return courses

        return self._pw_exec(_do_fetch)

    # ── Course content ───────────────────────────────────────────

    @staticmethod
    def _parse_duration(text: str) -> int:
        """Parse 'MM:SS' or 'HH:MM:SS' to seconds. Returns 0 on failure."""
        parts = text.strip().split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, IndexError):
            pass
        return 0

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        def _do_fetch() -> Dict[str, Any]:
            page = self._page
            content: Dict[str, Any] = {}

            for course in courses:
                slug = course.get("slug", "")
                course_name = course.get("name", slug)
                logging.info(f"Circle.so: Obtendo conteúdo de '{course_name}'...")

                page.goto(
                    f"{self._base_url}/c/{slug}", wait_until="networkidle", timeout=60000
                )
                page.wait_for_timeout(2000)

                # Ensure all sections are expanded
                try:
                    expand_btn = page.query_selector(
                        "button:has-text('Mostrar todas as seções'), "
                        "button:has-text('Exibir todas'), "
                        "[class*='expand-all']"
                    )
                    if expand_btn:
                        expand_btn.click()
                        page.wait_for_timeout(1000)
                    else:
                        collapsed = page.query_selector_all(
                            "[class*='section-header'][aria-expanded='false'], "
                            "[class*='collapsed'] [class*='section']"
                        )
                        for header in collapsed:
                            header.click()
                            page.wait_for_timeout(300)
                except Exception as e:
                    logging.warning(f"Circle.so: Falha ao expandir seções: {e}")

                # Scrape using Circle.so DOM structure:
                #   div.border-primary.rounded-md > div (section) > button.bg-secondary (header)
                #   + a[href] (lesson links) with div.ml-3 (title) and p.text-sm (duration)
                course_container = page.query_selector(
                    ".border-primary.overflow-hidden.rounded-md"
                )

                if not course_container:
                    logging.warning(
                        f"Circle.so: Container não encontrado para '{course_name}'."
                    )
                    content[slug] = {
                        "id": slug,
                        "name": course_name,
                        "slug": slug,
                        "seller_name": "Circle.so",
                        "title": course_name,
                        "modules": [],
                    }
                    continue

                section_wrappers = course_container.query_selector_all(":scope > div")
                modules: List[Dict[str, Any]] = []

                for section_index, section_wrapper in enumerate(section_wrappers, start=1):
                    header_btn = section_wrapper.query_selector("button.bg-secondary")
                    if header_btn:
                        title_div = header_btn.query_selector("div")
                        section_title = (
                            title_div.inner_text().strip()
                            if title_div
                            else f"Seção {section_index}"
                        )
                    else:
                        section_title = f"Seção {section_index}"

                    lesson_links = section_wrapper.query_selector_all(
                        f"a[href*='/c/{slug}/sections/']"
                    )
                    if not lesson_links:
                        continue

                    lessons: List[Dict[str, Any]] = []
                    section_id = ""

                    for lesson_index, link in enumerate(lesson_links, start=1):
                        href = link.get_attribute("href") or ""
                        match = re.search(r"/sections/(\d+)/lessons/(\d+)", href)
                        if not match:
                            continue

                        section_id = match.group(1)
                        lesson_id = match.group(2)

                        title_el = link.query_selector("div.ml-3, div.flex-1")
                        lesson_title = (
                            title_el.inner_text().strip()
                            if title_el
                            else link.inner_text().strip()
                        )

                        duration_text = ""
                        dur_el = link.query_selector("p.text-sm")
                        if dur_el:
                            duration_text = dur_el.inner_text().strip()

                        lessons.append(
                            {
                                "id": lesson_id,
                                "title": lesson_title,
                                "order": lesson_index,
                                "locked": False,
                                "duration": self._parse_duration(duration_text),
                                "section_id": section_id,
                                "course_slug": slug,
                            }
                        )

                    if lessons:
                        modules.append(
                            {
                                "id": section_id,
                                "title": section_title,
                                "order": section_index,
                                "lessons": lessons,
                            }
                        )

                content[slug] = {
                    "id": slug,
                    "name": course_name,
                    "slug": slug,
                    "seller_name": "Circle.so",
                    "title": course_name,
                    "modules": modules,
                }

                logging.info(
                    f"Circle.so: '{course_name}' - "
                    f"{len(modules)} seções, "
                    f"{sum(len(m['lessons']) for m in modules)} aulas."
                )

                page.wait_for_timeout(1500)

            return content

        return self._pw_exec(_do_fetch)

    # ── Lesson details ───────────────────────────────────────────

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        def _do_fetch() -> LessonContent:
            page = self._page

            section_id = lesson.get("section_id", module_id)
            lesson_id = lesson.get("id", "")
            lesson_title = lesson.get("title", "")
            course_slug_from_lesson = lesson.get("course_slug", course_slug)

            url = (
                f"{self._base_url}/c/{course_slug_from_lesson}"
                f"/sections/{section_id}/lessons/{lesson_id}"
            )
            logging.info(f"Circle.so: Obtendo detalhes de '{lesson_title}'...")
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)

            videos: List[Video] = []
            description: Optional[Description] = None
            auxiliary_urls: List[AuxiliaryURL] = []

            # ── Video extraction ──
            hls_video = page.query_selector("hls-video")
            if hls_video:
                video_url = hls_video.get_attribute("src") or ""
                if not video_url or ".m3u8" not in video_url:
                    video_url = page.evaluate(
                        'document.querySelector("hls-video")?.shadowRoot'
                        '?.querySelector("video")?.src || ""'
                    )
                if video_url and ".m3u8" in video_url:
                    videos.append(
                        Video(
                            video_id=f"lesson_{lesson_id}",
                            url=video_url,
                            order=1,
                            title=lesson_title,
                            size=0,
                            duration=lesson.get("duration", 0),
                        )
                    )
                    logging.info(
                        f"Circle.so: Vídeo HLS encontrado para '{lesson_title}'."
                    )

            # ── Transcript extraction ──
            transcript_html = ""
            transcript_btn = page.query_selector(
                "button:has-text('Mostrar transcrição'), "
                "button:has-text('transcrição')"
            )
            if transcript_btn:
                try:
                    transcript_btn.click()
                    page.wait_for_timeout(1500)
                    transcript_el = page.query_selector(
                        "[class*='transcript'], [class*='Transcript']"
                    )
                    if transcript_el:
                        entries = transcript_el.query_selector_all("p, div, span")
                        parts = []
                        for entry in entries:
                            text = entry.inner_text().strip()
                            if text:
                                parts.append(f"<p>{text}</p>")
                        if parts:
                            transcript_html = (
                                '\n<section class="transcript">\n'
                                "<h3>Transcrição</h3>\n"
                                + "\n".join(parts)
                                + "\n</section>"
                            )
                except Exception as e:
                    logging.warning(
                        f"Circle.so: Falha ao extrair transcrição: {e}"
                    )

            # ── Description extraction ──
            # Circle.so uses TipTap editor — content is in div.tiptap.ProseMirror
            body_el = page.query_selector(
                ".tiptap.ProseMirror, [class*='ProseMirror'], "
                "[class*='trix-content'], article, .prose"
            )
            if body_el:
                body_html = body_el.inner_html()
                if transcript_html:
                    body_html += transcript_html
                description = Description(text=body_html, description_type="html")
            elif transcript_html:
                description = Description(
                    text=transcript_html, description_type="html"
                )

            # ── Reference links extraction ──
            if body_el:
                links = body_el.query_selector_all("a[href]")
                ref_order = 0
                for link in links:
                    href = link.get_attribute("href") or ""
                    link_text = link.inner_text().strip()
                    if (
                        href
                        and not href.startswith("#")
                        and not href.startswith("/")
                        and link_text
                    ):
                        ref_order += 1
                        auxiliary_urls.append(
                            AuxiliaryURL(
                                url_id=f"ref_{lesson_id}_{ref_order}",
                                url=href,
                                order=ref_order,
                                title=link_text,
                                description="",
                            )
                        )

            logging.info(
                f"Circle.so: '{lesson_title}' - "
                f"{len(videos)} vídeo(s), "
                f"{len(auxiliary_urls)} link(s), "
                f"{'com' if description else 'sem'} descrição."
            )

            return LessonContent(
                description=description,
                videos=videos,
                attachments=[],
                auxiliary_urls=auxiliary_urls,
            )

        return self._pw_exec(_do_fetch)

    # ── Attachment download ──────────────────────────────────────

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        """Download attachment using requests session with extracted cookies."""
        if not self._session:
            logging.error("Circle.so: Sessão não disponível para download.")
            return False

        try:
            response = self._session.get(attachment.url, stream=True, timeout=120)
            response.raise_for_status()

            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logging.info(f"Circle.so: Anexo salvo com sucesso {download_path}")
            return True
        except Exception as e:
            logging.error(
                f"Circle.so: Falha ao baixar anexo {attachment.filename}: {e}"
            )
            return False


PlatformFactory.register_platform("Circle.so", CircleSoPlatform)
