from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory, sanitize_token
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
)


class TutoryTokenFetcher(PlaywrightTokenFetcher):
    """Captures the NextAuth session-token cookie after logging into Tutory."""

    def __init__(self, domain: str) -> None:
        self._domain = domain.rstrip("/")

    @property
    def login_url(self) -> str:
        return f"https://{self._domain}/login"

    @property
    def target_endpoints(self) -> List[str]:
        # Tutory does not use Authorization headers; this is unused but required by the base.
        return [f"https://{self._domain}/api/"]

    async def dismiss_cookie_banner(self, page: Page) -> None:  # pragma: no cover - UI dependent
        try:
            button = page.get_by_role("button", name=re.compile("aceitar|accept|ok", re.IGNORECASE))
            if await button.count():
                await button.first.click()
        except Exception:
            return

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        await page.wait_for_selector("input[type='email'], input[name='email']", timeout=30_000)
        await page.fill("input[type='email'], input[name='email']", username)
        await page.fill("input[type='password'], input[name='password']", password)

    async def submit_login(self, page: Page) -> None:
        for selector in (
            "button[type='submit']",
            "button:has-text('Entrar')",
            "button:has-text('Acessar')",
            "button:has-text('Login')",
        ):
            try:
                await page.click(selector, timeout=2000)
                return
            except Exception:
                continue
        await page.press("body", "Enter")

    async def fetch_token_async(
        self,
        username: str,
        password: str,
        *,
        headless: bool = True,
        user_agent: Optional[str] = None,
        wait_for_user_confirmation: Optional[Callable[[], None]] = None,
    ) -> str:
        import asyncio

        from playwright.async_api import async_playwright

        manual_login = not (username and password)

        async with async_playwright() as playwright:
            args = ["--disable-blink-features=AutomationControlled"]
            browser = await playwright.chromium.launch(headless=headless, args=args)
            ua_to_use = user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            context = await browser.new_context(user_agent=ua_to_use)
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()

            try:
                await page.goto(self.login_url, wait_until="domcontentloaded")
                await page.wait_for_load_state(
                    "networkidle", timeout=self.network_idle_timeout_ms
                )
                await self.dismiss_cookie_banner(page)

                if not manual_login:
                    await self.fill_credentials(page, username, password)
                    await self.submit_login(page)

                try:
                    await page.wait_for_url(re.compile(r"/dash"), timeout=self.network_idle_timeout_ms)
                except PlaywrightTimeoutError:
                    pass

                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=self.network_idle_timeout_ms
                    )
                except PlaywrightTimeoutError:
                    pass

                cookie_value = await self._extract_session_cookie(context)

                if not cookie_value and wait_for_user_confirmation:
                    # Allow the user time to finish 2FA / captcha in the visible browser.
                    await asyncio.to_thread(wait_for_user_confirmation)
                    cookie_value = await self._extract_session_cookie(context)

                if not cookie_value:
                    raise ValueError(
                        "Não foi possível capturar o cookie de sessão da Tutory após o login."
                    )
                return cookie_value
            finally:
                if wait_for_user_confirmation and not manual_login:
                    try:
                        await asyncio.to_thread(wait_for_user_confirmation)
                    except Exception:
                        pass
                await browser.close()

    async def _extract_session_cookie(self, context: Any) -> Optional[str]:
        cookies = await context.cookies()
        for name in SESSION_COOKIE_NAMES:
            for cookie in cookies:
                if cookie.get("name") == name and cookie.get("value"):
                    return cookie["value"]
        return None


class TutoryPlatform(BasePlatform):
    """plataformatutory.com.br — Next.js course platform with NextAuth cookie sessions."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._domain: str = ""
        self._api_base: str = ""
        self._token_fetcher: Optional[TutoryTokenFetcher] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="domain",
                label="Domínio da Tutory (ex: fauth.plataformatutory.com.br)",
                field_type=AuthFieldType.TEXT,
                placeholder="seudominio.plataformatutory.com.br",
                required=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
A Tutory hospeda cada escola/professor em um subdomínio próprio
(ex: fauth.plataformatutory.com.br). Informe o domínio completo, sem https:// e sem barra final.

Token de Acesso (gratuito):
1) Acesse o domínio da sua escola e faça login normalmente.
2) Abra as Ferramentas de Desenvolvedor (F12) → aba Application/Armazenamento → Cookies.
3) Selecione o cookie chamado "__Secure-next-auth.session-token" (ou "next-auth.session-token").
4) Copie o valor completo (JWE longo iniciando com "eyJ...") e cole no campo Token.

Assinantes podem informar usuário/senha; o sistema abrirá um navegador para concluir o login
(útil em caso de 2FA/Captcha) e capturará o cookie automaticamente. Atenção: a Tutory marca
sessões como "singleLogin" — fazer login pelo Playwright pode derrubar sua sessão ativa no
navegador.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        domain = (credentials.get("domain") or "").strip()
        if not domain:
            raise ValueError("Informe o domínio da Tutory (ex: fauth.plataformatutory.com.br).")
        self._domain = self._normalize_domain(domain)
        self._api_base = f"https://{self._domain}"
        self._token_fetcher = TutoryTokenFetcher(self._domain)

        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)
        self._validate_session()

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        domain = domain.strip().rstrip("/")
        if "://" in domain:
            domain = urlparse(domain).netloc or domain
        return domain.lower()

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        if not self._token_fetcher:
            raise ConnectionError("Token fetcher não inicializado (informe o domínio).")

        use_browser_emulation = bool(credentials.get("browser_emulation"))
        confirmation_event = credentials.get("manual_auth_confirmation")
        custom_ua = self._settings.user_agent

        try:
            return self._token_fetcher.fetch_token(
                username,
                password,
                headless=not use_browser_emulation,
                user_agent=custom_ua,
                wait_for_user_confirmation=(
                    confirmation_event.wait if confirmation_event else None
                ),
            )
        except Exception as exc:
            raise ConnectionError(
                "Falha ao obter o cookie de sessão via Playwright. Revise usuário/senha ou utilize o token manual."
            ) from exc

    def _configure_session(self, token: str) -> None:
        token = sanitize_token(token)
        self._session = requests.Session()
        # Tutory accepts either cookie name; set both so the value works regardless of HTTPS/cookie-prefix policy.
        for cookie_name in SESSION_COOKIE_NAMES:
            self._session.cookies.set(cookie_name, token, domain=self._domain, path="/")
        self._session.headers.update(
            {
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": self._api_base,
                "Referer": f"{self._api_base}/dash",
            }
        )

    def _validate_session(self) -> None:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")
        response = self._session.get(f"{self._api_base}/api/auth/session", timeout=30)
        if response.status_code != 200:
            raise ConnectionError(
                f"Falha ao validar a sessão da Tutory (HTTP {response.status_code})."
            )
        data = response.json() or {}
        user = data.get("user") or {}
        if not user.get("id"):
            raise ConnectionError(
                "Cookie de sessão inválido ou expirado. Refaça o login na Tutory e copie um novo token."
            )
        logger.debug("Tutory autenticado como %s (%s)", user.get("name"), user.get("email"))

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        paid = self._session.get(f"{self._api_base}/api/courses", timeout=30)
        paid.raise_for_status()
        free = self._session.get(f"{self._api_base}/api/courses/free", timeout=30)
        free.raise_for_status()

        aggregated: Dict[str, Dict[str, Any]] = {}
        for item in (paid.json() or []) + (free.json() or []):
            if not isinstance(item, dict):
                continue
            course_id = item.get("id")
            if not course_id:
                continue
            aggregated[str(course_id)] = {
                "id": str(course_id),
                "name": item.get("titulo") or f"Curso {course_id}",
                "seller_name": "",
                "slug": str(course_id),
            }
        return sorted(aggregated.values(), key=lambda c: c.get("name", ""))

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        all_content: Dict[str, Any] = {}
        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            modules_resp = self._session.get(
                f"{self._api_base}/api/course/{course_id}/modules", timeout=30
            )
            modules_resp.raise_for_status()
            raw_modules = modules_resp.json() or []

            processed_modules: List[Dict[str, Any]] = []
            module_order = 0
            for raw_module in raw_modules:
                if not isinstance(raw_module, dict):
                    continue
                parent_title = raw_module.get("titulo") or "Módulo"
                playlists = raw_module.get("playlists") or []

                for playlist in playlists:
                    if not isinstance(playlist, dict):
                        continue
                    playlist_id = playlist.get("id")
                    if not playlist_id:
                        continue

                    lessons_resp = self._session.get(
                        f"{self._api_base}/api/playlist/{playlist_id}/lessons", timeout=30
                    )
                    lessons_resp.raise_for_status()
                    raw_lessons = lessons_resp.json() or []

                    lessons: List[Dict[str, Any]] = []
                    for lesson_index, lesson in enumerate(raw_lessons, start=1):
                        if not isinstance(lesson, dict):
                            continue
                        lessons.append(
                            {
                                "id": lesson.get("id"),
                                "title": lesson.get("titulo") or f"Aula {lesson_index}",
                                "order": lesson_index,
                                "video_origem": lesson.get("video_origem"),
                                "video_url": lesson.get("video_url"),
                                "descricao": lesson.get("descricao"),
                                "subtitulo": lesson.get("subtitulo"),
                                "locked": bool(lesson.get("data_liberacao"))
                                and not lesson.get("assistido"),
                            }
                        )

                    module_order += 1
                    playlist_title = playlist.get("titulo") or f"Playlist {module_order}"
                    if parent_title and parent_title != playlist_title:
                        composed_title = f"{parent_title} - {playlist_title}"
                    else:
                        composed_title = playlist_title

                    processed_modules.append(
                        {
                            "id": str(playlist_id),
                            "title": composed_title,
                            "order": playlist.get("indice", module_order),
                            "lessons": lessons,
                            "locked": raw_module.get("tipo_acesso") not in (None, "pago", "gratuito"),
                        }
                    )

            course_entry = course.copy()
            course_entry["title"] = course.get("name", f"Curso {course_id}")
            course_entry["modules"] = processed_modules
            all_content[str(course_id)] = course_entry

        return all_content

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        lesson_id = lesson.get("id")
        if not lesson_id:
            raise ValueError("ID da aula não encontrado.")

        detail_resp = self._session.get(
            f"{self._api_base}/api/lesson/{lesson_id}", timeout=30
        )
        detail_resp.raise_for_status()
        detail = detail_resp.json() or {}

        content = LessonContent()

        description_html = detail.get("descricao") or lesson.get("descricao") or ""
        subtitle = detail.get("subtitulo") or lesson.get("subtitulo") or ""
        if subtitle:
            description_html = f"<p><em>{subtitle}</em></p>{description_html}"
        if description_html.strip() and description_html.strip() not in ("<p></p>",):
            content.description = Description(text=description_html, description_type="html")

        video_origem = detail.get("video_origem") or lesson.get("video_origem")
        video_url = detail.get("video_url") or lesson.get("video_url") or ""
        lesson_title = detail.get("titulo") or lesson.get("title") or "Aula"

        if video_origem == "video":
            manifest_url, audio_url = self._resolve_video_manifest(str(lesson_id), video_url)
            chosen_url = manifest_url or video_url
            if chosen_url:
                if chosen_url.endswith(".m3u8"):
                    chosen_url = self._select_stream_by_quality(chosen_url)
                content.videos.append(
                    Video(
                        video_id=str(lesson_id),
                        url=chosen_url,
                        order=lesson.get("order", 1),
                        title=lesson_title,
                        size=0,
                        duration=0,
                        extra_props={
                            "referer": f"{self._api_base}/",
                            "audio_url": audio_url or "",
                        },
                    )
                )
        elif video_origem == "pdf" and video_url:
            filename = self._infer_filename(video_url, lesson_title, default_ext="pdf")
            content.attachments.append(
                Attachment(
                    attachment_id=f"lesson-{lesson_id}",
                    url=video_url,
                    filename=filename,
                    order=0,
                    extension=filename.split(".")[-1] if "." in filename else "pdf",
                    size=0,
                )
            )

        attachments_resp = self._session.get(
            f"{self._api_base}/api/lesson/{lesson_id}/attachments", timeout=30
        )
        attachments_resp.raise_for_status()
        for index, attachment in enumerate(attachments_resp.json() or [], start=1):
            if not isinstance(attachment, dict):
                continue
            anexo_url = attachment.get("anexo") or ""
            if not anexo_url:
                continue
            mime = attachment.get("tipo") or ""
            title = attachment.get("titulo") or f"anexo-{index}"
            filename = self._infer_filename(anexo_url, title, default_ext=self._ext_from_mime(mime))
            content.attachments.append(
                Attachment(
                    attachment_id=str(attachment.get("id", index)),
                    url=anexo_url,
                    filename=filename,
                    order=index,
                    extension=filename.split(".")[-1] if "." in filename else "",
                    size=0,
                )
            )

        return content

    def _resolve_video_manifest(
        self, lesson_id: str, fallback_url: str
    ) -> Tuple[Optional[str], Optional[str]]:
        if not self._session:
            return fallback_url or None, None
        try:
            response = self._session.get(
                f"{self._api_base}/api/student/video/{lesson_id}", timeout=30
            )
            response.raise_for_status()
            data = response.json() or {}
            return data.get("manifestUrl"), data.get("audioUrl")
        except requests.RequestException as exc:
            logger.debug("Tutory: falha ao obter manifest da aula %s: %s", lesson_id, exc)
            return fallback_url or None, None

    def _select_stream_by_quality(self, stream_url: str) -> str:
        if not self._session or not stream_url.endswith(".m3u8"):
            return stream_url
        try:
            response = self._session.get(stream_url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.debug("Tutory: falha ao obter master playlist %s: %s", stream_url, exc)
            return stream_url

        variants: List[Tuple[int, str]] = []
        lines = response.text.splitlines()
        for idx, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            match = re.search(r"RESOLUTION=\d+x(\d+)", line)
            height = int(match.group(1)) if match else 0
            if idx + 1 < len(lines):
                uri = lines[idx + 1].strip()
                if uri and not uri.startswith("#"):
                    variants.append((height, urljoin(stream_url, uri)))

        if not variants:
            return stream_url

        def _height(entry: Tuple[int, str]) -> int:
            if entry[0]:
                return entry[0]
            match = re.search(r"(\d{3,4})p", entry[1])
            return int(match.group(1)) if match else 0

        sorted_variants = sorted(variants, key=_height, reverse=True)
        preference = self._settings.video_quality
        if preference == "Mais baixa":
            return sorted_variants[-1][1]
        if preference == "Mais alta":
            return sorted_variants[0][1]
        try:
            target = int(str(preference).replace("p", ""))
        except (TypeError, ValueError):
            return sorted_variants[0][1]
        chosen = sorted_variants[-1]
        for variant in sorted_variants:
            if _height(variant) <= target:
                chosen = variant
                break
        return chosen[1]

    @staticmethod
    def _infer_filename(url: str, fallback_title: str, default_ext: str = "") -> str:
        path = urlparse(url).path
        candidate = Path(path).name
        if candidate and "." in candidate:
            return candidate
        title = fallback_title.strip() or "arquivo"
        if "." in title:
            return title
        ext = default_ext.lstrip(".")
        return f"{title}.{ext}" if ext else title

    @staticmethod
    def _ext_from_mime(mime: str) -> str:
        if not mime:
            return ""
        mapping = {
            "application/pdf": "pdf",
            "application/zip": "zip",
            "application/x-zip-compressed": "zip",
            "application/msword": "doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
            "application/vnd.ms-excel": "xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "audio/mpeg": "mp3",
            "video/mp4": "mp4",
            "text/plain": "txt",
        }
        if mime in mapping:
            return mapping[mime]
        if "/" in mime:
            return mime.split("/", 1)[1]
        return ""

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        url = attachment.url
        if not url:
            logger.error("Anexo sem URL: %s", attachment.filename)
            return False

        try:
            # S3 public bucket — fetch without session cookies to avoid signed-URL conflicts.
            headers = {
                "User-Agent": self._settings.user_agent,
                "Accept": "*/*",
                "Referer": f"{self._api_base}/",
            }
            if "amazonaws.com" in urlparse(url).netloc:
                response = requests.get(url, stream=True, headers=headers, timeout=60)
            else:
                response = self._session.get(url, stream=True, timeout=60)
            response.raise_for_status()

            with open(download_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        file_handle.write(chunk)
            return True
        except Exception as exc:
            logger.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Tutory", TutoryPlatform)
