from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

import requests
from playwright.async_api import Page

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

# Base URLs
BASE_URL = "https://www.estrategiaconcursos.com.br"
API_URL = "https://api.estrategiaconcursos.com.br"
ACCOUNTS_API_URL = "https://api.accounts.estrategia.com"
LOGIN_URL = "https://perfil.estrategia.com/login"
PLAYWRIGHT_LOGIN_URL = f"{LOGIN_URL}?source=legado-polvo&target={BASE_URL}/accounts/login/"

# API Endpoints
OAUTH_TOKEN_URL = f"{BASE_URL}/oauth/token/"
COURSES_URL = f"{API_URL}/api/aluno/curso"
COURSE_DETAILS_URL = f"{API_URL}/api/aluno/curso/{{course_id}}"
LESSON_DETAILS_URL = f"{API_URL}/api/aluno/aula/{{lesson_id}}"


class EstrategiaTokenFetcher(PlaywrightTokenFetcher):
    """Automates Estrategia login with a real browser to capture the bearer token."""

    network_idle_timeout_ms: int = 600_000  # 10 minutes for manual login with captcha

    @property
    def login_url(self) -> str:
        return PLAYWRIGHT_LOGIN_URL

    @property
    def target_endpoints(self) -> list[str]:
        return [
            f"{API_URL}/api/aluno/",
            f"{API_URL}/api/",
        ]

    async def dismiss_cookie_banner(self, page: Page) -> None:
        try:
            cookie_btn = page.locator('button:has-text("Aceitar")')
            if await cookie_btn.count() > 0:
                await cookie_btn.first.click()
        except Exception:
            pass

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        pass

    async def submit_login(self, page: Page) -> None:
        pass

    async def fetch_token_async(
        self,
        username: str,
        password: str,
        *,
        headless: bool = True,
        user_agent: Optional[str] = None,
        wait_for_user_confirmation: Optional[Callable[[], None]] = None,
    ) -> str:
        """Custom fetch for Estrategia - waits for manual login then captures token."""
        from playwright.async_api import async_playwright
        import asyncio

        async with async_playwright() as playwright:
            args = ["--disable-blink-features=AutomationControlled"]
            browser = await playwright.chromium.launch(headless=headless, args=args)
            ua_to_use = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            context = await browser.new_context(user_agent=ua_to_use)
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()

            captured_token: Optional[str] = None

            def on_request(request):
                nonlocal captured_token
                if captured_token:
                    return
                url = request.url
                if any(url.startswith(ep) for ep in self.target_endpoints):
                    auth = request.headers.get("authorization", "")
                    if auth.lower().startswith("bearer "):
                        captured_token = auth[7:].strip()
                        logger.info("Token captured from request to %s", url)

            page.on("request", on_request)

            try:
                # Navigate to login page
                await page.goto(self.login_url, wait_until="domcontentloaded")
                await self.dismiss_cookie_banner(page)

                logger.info(
                    "Aguardando login manual. Complete o login no navegador e "
                    "navegue até seus cursos..."
                )

                # Wait for user to complete login and navigate to a page with API calls
                # We detect this by waiting for a request to our target endpoints
                for _ in range(600):  # 10 minutes max (600 x 1 second)
                    if captured_token:
                        break
                    await asyncio.sleep(1)

                    # Check if page navigated to main site, then go to courses
                    if BASE_URL in page.url and "/aluno" not in page.url:
                        try:
                            logger.info("Redirecionado para site principal, acessando cursos...")
                            await page.goto(
                                f"{BASE_URL}/aluno/cursos",
                                wait_until="domcontentloaded",
                                timeout=30000,
                            )
                        except Exception:
                            pass

                if not captured_token:
                    raise ValueError(
                        "Não foi possível capturar o token. Certifique-se de fazer login "
                        "e navegar até a página de cursos."
                    )

                return captured_token

            finally:
                if wait_for_user_confirmation:
                    try:
                        await asyncio.to_thread(wait_for_user_confirmation)
                    except Exception:
                        pass
                await browser.close()


class EstrategiaPlatform(BasePlatform):
    """Implements the Estrategia Concursos platform."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._access_token: Optional[str] = None
        self._session_id: Optional[str] = None
        self._token_fetcher = EstrategiaTokenFetcher()

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="phpsessid",
                label="Cookie PHPSESSID",
                field_type=AuthFieldType.PASSWORD,
                placeholder="Cole o valor do cookie 'PHPSESSID'",
                required=False,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como obter as credenciais do Estratégia Concursos:

1. Acesse https://www.estrategiaconcursos.com.br e faça login normalmente.
2. Abra o DevTools (F12) e vá para a aba Network (Rede).
3. Recarregue a página e procure por requisições para "api.estrategiaconcursos.com.br".
4. Clique em uma requisição e veja os Headers.
5. Copie o valor do header "Authorization" (sem o "Bearer ").
6. Navegue até a aba Aplicação (Application).
7. No menu lateral, clique em Cookies > www.estrategiaconcursos.com.br.
8. Encontre o cookie chamado "PHPSESSID" e copie seu valor.
9. Cole o valor do cookie no campo "Cookie PHPSESSID".

**Assinantes:**
Marque a opção "Emular Navegador" e faça login manualmente no navegador, após isso, clique em OK no pop-up.

**IMPORTANTE:** Este modulo apenas lista o que voce estiver matriculado no site, devido a restricao de 3 matriculas.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        phpsessid = (credentials.get("phpsessid") or "").strip()
        token = (credentials.get("token") or "").strip()
        use_browser_emulation = bool(credentials.get("browser_emulation"))

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
        })

        # Method 1: Direct token provided
        if token:
            self._access_token = token
            self._configure_api_session()
            self._validate_session()
            logger.info("Sessão autenticada no Estratégia via access token.")
            return

        # Method 2: PHPSESSID cookie
        if phpsessid:
            self._session.cookies.set("PHPSESSID", phpsessid, domain=".estrategiaconcursos.com.br")
            self._fetch_oauth_token()
            self._validate_session()
            logger.info("Sessão autenticada no Estratégia via cookie PHPSESSID.")
            return

        # Method 3: Browser emulation for login with captcha (requires membership)
        if self._settings.has_full_permissions and use_browser_emulation:
            obtained_token = self._exchange_credentials_for_token(
                credentials.get("username", ""),
                credentials.get("password", ""),
                credentials,
            )
            self._access_token = obtained_token
            self._configure_api_session()
            self._validate_session()
            logger.info("Sessão autenticada no Estratégia via emulação de navegador.")
            return

        raise ValueError(
            "Informe um cookie PHPSESSID, access token, ou utilize emulação de navegador "
            "(assinantes) para autenticação."
        )

    def _fetch_oauth_token(self) -> None:
        """Fetches OAuth token from the session cookie."""
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        response = self._session.get(OAUTH_TOKEN_URL)
        response.raise_for_status()

        try:
            data = response.json()
        except Exception:
            raise ValueError("Falha ao obter token OAuth. Verifique o cookie de sessão.")

        self._access_token = data.get("access_token")
        self._session_id = data.get("session_id")

        if not self._access_token:
            raise ValueError("Token de acesso não encontrado na resposta OAuth.")

        self._configure_api_session()

    def _configure_api_session(self) -> None:
        """Configures session headers for API requests."""
        if not self._session or not self._access_token:
            raise ConnectionError("Sessão ou token não configurados.")

        self._session.headers.update({
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        })

        if self._session_id:
            self._session.headers.update({
                "Session": self._session_id,
                "Personificado": "false",
            })

    def _validate_session(self) -> None:
        """Validates the session by fetching user profile."""
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        try:
            response = self._session.get(f"{API_URL}/api/aluno/perfil/detalhes")
            response.raise_for_status()
            data = response.json()

            user_data = data.get("data", {})
            user_name = user_data.get("nome", "Usuário")
            logger.debug("Usuário autenticado: %s", user_name)
        except Exception as exc:
            logger.warning("Falha ao validar sessão: %s", exc)
            # Don't raise - the token might still work for course access

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        """Exchanges credentials for an access token using browser emulation."""
        confirmation_event = credentials.get("manual_auth_confirmation")

        try:
            return self._token_fetcher.fetch_token(
                username,
                password,
                headless=False,
                user_agent=self._settings.user_agent,
                wait_for_user_confirmation=(
                    confirmation_event.wait if confirmation_event else None
                ),
            )
        except Exception as exc:
            raise ConnectionError(
                "Falha ao obter o token do Estratégia via emulação de navegador. "
                "Realize o login/captcha manualmente no navegador e clique OK na aplicação."
            ) from exc

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Fetches all enrolled courses from the platform."""
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        response = self._session.get(COURSES_URL)
        response.raise_for_status()
        data = response.json()

        courses: List[Dict[str, Any]] = []
        concursos = data.get("data", {}).get("concursos", [])

        for concurso in concursos:
            concurso_titulo = concurso.get("titulo", "Concurso")

            for curso in concurso.get("cursos", []):
                course_id = curso.get("id")
                if not course_id:
                    continue

                # Skip redirect courses (external links)
                if curso.get("redirect_area_aluno"):
                    continue

                courses.append({
                    "id": str(course_id),
                    "name": curso.get("nome", "Curso"),
                    "slug": str(course_id),
                    "seller_name": concurso_titulo,
                    "tipo": curso.get("tipo", ""),
                    "modalidade": curso.get("modalidade", ""),
                    "icone": curso.get("icone"),
                    "total_aulas": curso.get("total_aulas", 0),
                    "total_aulas_visualizadas": curso.get("total_aulas_visualizadas", 0),
                    "arquivado": curso.get("arquivado", False),
                    "favorito": curso.get("favorito", False),
                    "data_inicio": curso.get("data_inicio"),
                    "data_retirada": curso.get("data_retirada"),
                })

        logger.info("Encontrados %d cursos no Estratégia.", len(courses))
        return courses

    def search_courses(self, query: str) -> List[Dict[str, Any]]:
        """Searches courses by name (local filtering)."""
        all_courses = self.fetch_courses()
        if not query or not query.strip():
            return all_courses

        query_lower = query.lower()
        return [
            c for c in all_courses
            if query_lower in c.get("name", "").lower()
            or query_lower in c.get("seller_name", "").lower()
        ]

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fetches modules and lessons for selected courses."""
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                logger.warning("Curso sem ID encontrado, ignorando.")
                continue

            url = COURSE_DETAILS_URL.format(course_id=course_id)

            try:
                response = self._session.get(url)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.error("Falha ao obter detalhes do curso %s: %s", course_id, exc)
                continue

            course_data = data.get("data", {})
            aulas = course_data.get("aulas", [])

            # Estratégia doesn't have explicit modules, so we create a single module
            # with all lessons, or group by professor if there are many lessons
            lessons: List[Dict[str, Any]] = []

            for idx, aula in enumerate(aulas, start=1):
                aula_id = aula.get("id")
                if not aula_id:
                    continue

                # Skip unavailable lessons
                if not aula.get("is_disponivel", True):
                    continue

                lessons.append({
                    "id": str(aula_id),
                    "title": aula.get("nome", f"Aula {idx}"),
                    "order": idx,
                    "locked": not aula.get("is_disponivel", True),
                    "conteudo": aula.get("conteudo", ""),
                    "data_publicacao": aula.get("data_publicacao"),
                    "pdf_url": aula.get("pdf"),
                    "pdf_grifado": aula.get("pdf_grifado"),
                    "pdf_simplificado": aula.get("pdf_simplificado"),
                    "visualizada": aula.get("visualizada", False),
                    "is_aluno_finalizado": aula.get("is_aluno_finalizado", False),
                })

            modules = [{
                "id": f"modulo_{course_id}",
                "title": course_data.get("nome", course.get("name", "Curso")),
                "order": 1,
                "lessons": lessons,
                "locked": False,
            }]

            course_entry = {
                "id": str(course_id),
                "name": course_data.get("nome", course.get("name", "Curso")),
                "slug": str(course_id),
                "title": course_data.get("nome", course.get("name", "Curso")),
                "modules": modules,
                "professores": course_data.get("professores", []),
                "downloads_restantes": course_data.get("downloads_restantes", 0),
            }

            content[str(course_id)] = course_entry

        return content

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        """Fetches detailed information for a lesson including videos and attachments."""
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content = LessonContent()
        lesson_id = lesson.get("id")
        lesson_title = lesson.get("title", "Aula")

        if not lesson_id:
            logger.warning("Aula sem ID: %s", lesson_title)
            return content

        url = LESSON_DETAILS_URL.format(lesson_id=lesson_id)

        try:
            response = self._session.get(url)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.error("Falha ao obter detalhes da aula %s: %s", lesson_id, exc)
            return content

        lesson_data = data.get("data", {})
        videos_data = lesson_data.get("videos", [])
        attachment_order = 10  # Start after PDFs (1, 2, 3)

        # Process videos
        for video_idx, video in enumerate(videos_data, start=1):
            video_id = video.get("id")
            if not video_id:
                continue

            video_title = video.get("titulo", f"Vídeo {video_idx}")
            resolucoes = video.get("resolucoes", {})

            # Select best quality based on settings
            video_url = self._select_video_quality(resolucoes)

            if video_url:
                content.videos.append(
                    Video(
                        video_id=str(video_id),
                        url=video_url,
                        order=video_idx,
                        title=video_title,
                        size=0,
                        duration=0,
                        extra_props={
                            "resolucoes": resolucoes,
                            "thumbnail": video.get("thumbnail"),
                            "audio_url": video.get("audio"),
                            "slide_url": video.get("slide"),
                            "posicao": video.get("posicao", 0),
                        }
                    )
                )

            # Add audio as attachment
            audio_url = video.get("audio")
            if audio_url:
                content.attachments.append(
                    Attachment(
                        attachment_id=f"audio_{video_id}",
                        url=audio_url,
                        filename=f"{video_idx:02d} - Áudio - {video_title}.mp3",
                        order=attachment_order,
                        extension="mp3",
                        size=0
                    )
                )
                attachment_order += 1

            # Add slide as attachment
            slide_url = video.get("slide")
            if slide_url:
                content.attachments.append(
                    Attachment(
                        attachment_id=f"slide_{video_id}",
                        url=slide_url,
                        filename=f"{video_idx:02d} - Slide - {video_title}.pdf",
                        order=attachment_order,
                        extension="pdf",
                        size=0
                    )
                )
                attachment_order += 1

        # Add main PDF (livro eletrônico)
        pdf_url = lesson_data.get("pdf") or lesson.get("pdf_url")
        if pdf_url:
            content.attachments.append(
                Attachment(
                    attachment_id=f"pdf_{lesson_id}",
                    url=pdf_url,
                    filename=f"Livro - {lesson_title}.pdf",
                    order=1,
                    extension="pdf",
                    size=0
                )
            )

        # Add highlighted PDF if available
        pdf_grifado = lesson_data.get("pdf_grifado") or lesson.get("pdf_grifado")
        if pdf_grifado:
            content.attachments.append(
                Attachment(
                    attachment_id=f"pdf_grifado_{lesson_id}",
                    url=pdf_grifado,
                    filename=f"Livro Grifado - {lesson_title}.pdf",
                    order=2,
                    extension="pdf",
                    size=0
                )
            )

        # Add simplified PDF if available
        pdf_simplificado = lesson_data.get("pdf_simplificado") or lesson.get("pdf_simplificado")
        if pdf_simplificado:
            content.attachments.append(
                Attachment(
                    attachment_id=f"pdf_simplificado_{lesson_id}",
                    url=pdf_simplificado,
                    filename=f"Livro Simplificado - {lesson_title}.pdf",
                    order=3,
                    extension="pdf",
                    size=0
                )
            )

        return content

    def _select_video_quality(self, resolucoes: Dict[str, str]) -> Optional[str]:
        """Selects video URL based on quality preference."""
        if not resolucoes:
            return None

        # Quality preference order
        quality_order = ["720p", "480p", "360p"]

        # Check user preference from settings
        video_quality = getattr(self._settings, "video_quality", "Mais alta")

        if video_quality == "Mais baixa":
            quality_order = ["360p", "480p", "720p"]
        elif video_quality and video_quality.endswith("p"):
            # User specified a specific quality
            target = video_quality
            if target in resolucoes:
                return resolucoes[target]
            # Fall through to default order

        # Return first available quality
        for quality in quality_order:
            if quality in resolucoes and resolucoes[quality]:
                return resolucoes[quality]

        # Return any available
        for url in resolucoes.values():
            if url:
                return url

        return None

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        """Downloads an attachment from the platform."""
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        if not attachment.url:
            logger.error("Anexo sem URL disponível: %s", attachment.filename)
            return False

        try:
            # First, get the initial response which may contain the actual PDF URL
            initial_response = self._session.get(attachment.url)
            initial_response.raise_for_status()

            content_type = initial_response.headers.get("Content-Type", "").lower()

            # Check if response is a URL (text) rather than binary PDF content
            if "text" in content_type or "html" in content_type or "json" in content_type:
                pdf_url = initial_response.text.strip()

                # Verify it looks like a URL
                if pdf_url.startswith("http://") or pdf_url.startswith("https://"):
                    logger.info("Anexo retornou URL, baixando PDF de: %s", pdf_url)

                    # Create a new session with user preferences to download the actual PDF
                    download_session = requests.Session()
                    if self._settings:
                        download_session.headers.update({
                            "User-Agent": getattr(self._settings, "user_agent", "Mozilla/5.0")
                        })

                    response = download_session.get(pdf_url, stream=True, timeout=60)
                    response.raise_for_status()

                    with open(download_path, "wb") as file_handle:
                        for chunk in response.iter_content(chunk_size=8192):
                            file_handle.write(chunk)

                    return True

            # If it's already binary content, save directly
            with open(download_path, "wb") as file_handle:
                file_handle.write(initial_response.content)

            return True
        except Exception as exc:
            logger.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        """Marks a lesson as watched on the platform."""
        if not self._session:
            logger.warning("Sessão não autenticada para marcar aula como assistida.")
            return

        lesson_id = lesson.get("id")
        if not lesson_id:
            return

        logger.debug("Marcar aula %s como assistida: %s", lesson_id, watched)


PlatformFactory.register_platform("Estratégia Concursos", EstrategiaPlatform)
