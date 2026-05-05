from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from playwright.async_api import Page

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

AUTH_BASE = "https://auth.elevify.com"
APP_ORIGIN = "https://app.elevify.com"
API_BASE = "https://lms-bff.apoia.com.br"

USER_ME_URL = f"{API_BASE}/v1/user/me"
HOME_URL = f"{API_BASE}/v1/home"
COURSE_ENROLLMENT_URL = f"{API_BASE}/v1/course-enrollment/course/{{course_id}}"
PROXY_URL = f"{API_BASE}/proxy"

ASSET_TEXT_PATH = "/asset-service/asset/text/{content_id}"
ASSET_CARDS_PATH = "/asset-service/asset/cards/{content_id}"
ASSET_VIDEO_PATH = "/asset-service/asset/video/{content_id}"


def _build_proxy_url(path: str, query: Optional[Dict[str, str]] = None) -> str:
    """Builds the lms-bff /proxy?url=... URL with the inner path/query encoded."""
    inner = path
    if query:
        inner += "?" + urllib.parse.urlencode(query)
    return f"{PROXY_URL}?url={urllib.parse.quote(inner, safe='')}"


class ElevifyTokenFetcher(PlaywrightTokenFetcher):
    """Opens https://auth.elevify.com/login so the user can complete the
    email + e-mail-code flow. Captures the JWT bearer from the first call to
    lms-bff.apoia.com.br after the redirect to app.elevify.com.
    """

    network_idle_timeout_ms: int = 600_000

    @property
    def login_url(self) -> str:
        return f"{AUTH_BASE}/login?locale=pt-br"

    @property
    def target_endpoints(self) -> list[str]:
        return [
            f"{API_BASE}/v1/user/me",
            f"{API_BASE}/v1/home",
            f"{API_BASE}/v1/",
            f"{API_BASE}/proxy",
        ]

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        if username:
            try:
                await page.fill(
                    "input[type='email'], input[name='email'], input#email",
                    username,
                )
            except Exception:
                logger.debug("Elevify: prefill de e-mail falhou (campo nao localizado).")

    async def submit_login(self, page: Page) -> None:
        # The login uses an email + emailed-code flow that requires manual
        # interaction (the code is delivered out-of-band). We do not submit
        # automatically — the user must complete the flow in the browser.
        return None


class ElevifyPlatform(BasePlatform):
    """Elevify (powered by Apoia) LMS implementation.

    Course catalog and contents come from https://lms-bff.apoia.com.br with a
    Bearer JWT obtained from https://auth.elevify.com. Inside Elevify the
    hierarchy is Course -> Chapter -> Lesson -> LearningGranularUnit (LGU) ->
    Asset -> AssetContent (VIDEO/TEXT/CARDS/MULTIPLE_CHOICE).

    For Katomart's flatter Course/Module/Lesson model we map:
    - Chapter -> Module
    - LGU    -> Lesson
    Each LGU's assets are bundled into a single LessonContent (videos collected
    from the VIDEO assetContents, descriptions joined from TEXT/CARDS).
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._token: str = ""
        self._language: str = "PT"
        self._lesson_assets: Dict[str, List[Dict[str, Any]]] = {}
        self._token_fetcher = ElevifyTokenFetcher()

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
A Elevify usa login por codigo enviado por e-mail (sem senha tradicional).

Opcao 1 - Token (recomendado, dura ~90 dias):
1) Acesse https://app.elevify.com e faca login normalmente.
2) Abra o DevTools (F12) > aba Rede (Network).
3) Filtre por 'lms-bff' e clique em qualquer requisicao para
   lms-bff.apoia.com.br/v1/...
4) Em Cabecalhos copie o valor de 'Authorization' (apenas o JWT
   apos a palavra 'Bearer ') e cole no campo Token.

Opcao 2 - Login automatico (Emular Navegador):
1) Marque a opcao 'Emular Navegador' e informe seu e-mail no campo Usuario
   (a senha pode ficar em branco).
2) Uma janela do Chromium sera aberta na pagina de login.
3) Confirme o e-mail, abra a caixa de entrada para receber o codigo e
   informe-o no formulario.
4) Apos o redirecionamento para app.elevify.com o token sera capturado
   automaticamente.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)
        self._validate_session()

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        use_browser_emulation = bool(credentials.get("browser_emulation"))
        if not use_browser_emulation:
            raise ValueError(
                "A Elevify nao aceita login por usuario/senha (autentica por codigo "
                "no e-mail). Marque 'Emular Navegador' ou cole um token (JWT) no "
                "campo Token."
            )

        confirmation_event = credentials.get("manual_auth_confirmation")
        try:
            return self._token_fetcher.fetch_token(
                username,
                password or "",
                headless=False,
                wait_for_user_confirmation=(
                    confirmation_event.wait if confirmation_event else None
                ),
            )
        except Exception as exc:
            raise ConnectionError("Falha ao autenticar na Elevify via navegador.") from exc

    def _configure_session(self, token: str) -> None:
        self._token = token.strip()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json",
                "Accept-Language": "pt-BR,pt;q=0.9",
                "Origin": APP_ORIGIN,
                "Referer": f"{APP_ORIGIN}/",
                "Content-Type": "application/json",
            }
        )

    def _validate_session(self) -> None:
        try:
            response = self._session.get(USER_ME_URL, timeout=30)
        except requests.RequestException as exc:
            raise ConnectionError(f"Falha ao validar sessao Elevify: {exc}") from exc

        if response.status_code == 401:
            raise ConnectionError(
                "Token Elevify invalido ou expirado. Faca login novamente e copie "
                "o cabecalho Authorization da aba Rede."
            )
        response.raise_for_status()
        data = response.json()
        logger.info(
            "Elevify: sessao autenticada como %s (%s).",
            data.get("fullName"),
            data.get("email"),
        )

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        response = self._session.get(HOME_URL, timeout=30)
        response.raise_for_status()
        data = response.json()

        self._language = (data.get("language") or "PT").upper()

        courses: List[Dict[str, Any]] = []
        for entry in data.get("courses", []) or []:
            course_id = entry.get("id")
            if not course_id:
                continue
            courses.append(
                {
                    "id": course_id,
                    "name": entry.get("title", "Curso"),
                    "slug": str(course_id),
                    "seller_name": "Elevify",
                    "thumbnail": entry.get("thumbnailURL"),
                    "language": (entry.get("language") or self._language).upper(),
                    "enrollment_id": entry.get("enrollmentId"),
                }
            )
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            try:
                response = self._session.get(
                    COURSE_ENROLLMENT_URL.format(course_id=course_id), timeout=60
                )
                response.raise_for_status()
                payload = response.json()
            except requests.RequestException as exc:
                logger.error("Elevify: falha ao obter curso %s: %s", course_id, exc)
                continue

            course_language = (payload.get("language") or course.get("language") or self._language).upper()

            modules: List[Dict[str, Any]] = []
            for module_index, chapter in enumerate(payload.get("chapters", []) or [], start=1):
                lessons: List[Dict[str, Any]] = []
                for lesson_obj in chapter.get("lessons", []) or []:
                    for lgu_index, lgu in enumerate(
                        lesson_obj.get("learningGranularUnits", []) or [], start=1
                    ):
                        lgu_id = lgu.get("id")
                        if not lgu_id:
                            continue

                        order = lgu.get("sequence") or len(lessons) + 1
                        title_parts = [lesson_obj.get("title"), lgu.get("title")]
                        title = " - ".join(p for p in title_parts if p) or f"Aula {order}"

                        self._lesson_assets[str(lgu_id)] = lgu.get("assets", []) or []

                        lessons.append(
                            {
                                "id": lgu_id,
                                "title": title,
                                "order": order,
                                "duration": lgu.get("durationInSeconds") or 0,
                                "type": lgu.get("type"),
                                "language": course_language,
                                "locked": lgu.get("hasUserAccess") is False,
                            }
                        )

                modules.append(
                    {
                        "id": chapter.get("id"),
                        "title": chapter.get("title", f"Capitulo {module_index}"),
                        "order": chapter.get("sequence") or module_index,
                        "lessons": lessons,
                        "locked": False,
                    }
                )

            course_entry = course.copy()
            course_entry["title"] = payload.get("title", course.get("name", "Curso"))
            course_entry["modules"] = modules
            course_entry["language"] = course_language
            all_content[str(course_id)] = course_entry

        return all_content

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        lgu_id = str(lesson.get("id") or "")
        assets = self._lesson_assets.get(lgu_id, [])
        language = (lesson.get("language") or self._language or "PT").upper()
        params = {
            "language": language,
            "mediaLanguage": language,
            "subtitleLanguage": language,
        }

        content = LessonContent()
        description_parts: List[str] = []
        video_index = 0

        for asset in assets:
            asset_title = asset.get("title") or lesson.get("title") or "Asset"
            asset_sequence = asset.get("sequence") or 0

            for asset_content in asset.get("assetContents", []) or []:
                content_id = asset_content.get("id")
                content_type = (asset_content.get("assetContentType") or "").upper()
                if not content_id:
                    continue

                if content_type == "VIDEO":
                    video = self._fetch_video_asset(
                        content_id, params, asset_title, asset_sequence, language
                    )
                    if video:
                        video_index += 1
                        video.order = video_index
                        content.videos.append(video)

                elif content_type == "TEXT":
                    text_md = self._fetch_text_asset(content_id, params)
                    if text_md:
                        description_parts.append(text_md)

                elif content_type == "CARDS":
                    cards_md = self._fetch_cards_asset(content_id, params)
                    if cards_md:
                        description_parts.append(cards_md)

                # MULTIPLE_CHOICE and other interactive types are skipped — they
                # cannot be downloaded as static content.

        if description_parts:
            content.description = Description(
                text="\n\n---\n\n".join(description_parts),
                description_type="markdown",
            )

        return content

    def _fetch_video_asset(
        self,
        content_id: str,
        params: Dict[str, str],
        title: str,
        sequence: int,
        language: str,
    ) -> Optional[Video]:
        url = _build_proxy_url(ASSET_VIDEO_PATH.format(content_id=content_id), params)
        try:
            response = self._session.get(url, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            logger.warning("Elevify: falha ao obter asset video %s: %s", content_id, exc)
            return None

        video_url = payload.get("contentURL")
        if not video_url:
            content_urls = payload.get("contentUrls") or []
            for entry in content_urls:
                if (entry.get("language") or "").upper() == language:
                    video_url = entry.get("url")
                    break
            if not video_url and content_urls:
                video_url = content_urls[0].get("url")

        if not video_url:
            video_block = payload.get("video") or {}
            video_url = video_block.get("highRes") or video_block.get("lowRes")

        if not video_url:
            logger.warning("Elevify: nenhum URL de video encontrado para %s.", content_id)
            return None

        duration = int(payload.get("duration") or 0)

        extra_props: Dict[str, Any] = {
            "referer": f"{APP_ORIGIN}/",
            "origin": APP_ORIGIN,
        }

        transcription = payload.get("transcription") or []
        if isinstance(transcription, list) and transcription:
            extra_props["transcription"] = transcription

        return Video(
            video_id=content_id,
            url=video_url,
            order=sequence,
            title=title,
            size=0,
            duration=duration,
            extra_props=extra_props,
        )

    def _fetch_text_asset(self, content_id: str, params: Dict[str, str]) -> Optional[str]:
        url = _build_proxy_url(ASSET_TEXT_PATH.format(content_id=content_id), params)
        try:
            response = self._session.get(url, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            logger.warning("Elevify: falha ao obter asset text %s: %s", content_id, exc)
            return None
        return payload.get("content") or None

    def _fetch_cards_asset(self, content_id: str, params: Dict[str, str]) -> Optional[str]:
        url = _build_proxy_url(ASSET_CARDS_PATH.format(content_id=content_id), params)
        try:
            response = self._session.get(url, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            logger.warning("Elevify: falha ao obter asset cards %s: %s", content_id, exc)
            return None

        cards = payload.get("cards") or []
        if not cards:
            return payload.get("text") or None

        sections: List[str] = []
        for card in cards:
            title = (card.get("title") or "").strip()
            text = (card.get("text") or "").strip()
            if title:
                sections.append(f"## {title}\n\n{text}".strip())
            elif text:
                sections.append(text)
        return "\n\n".join(sections) if sections else None

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
        if not attachment.url:
            return False

        try:
            with self._session.get(attachment.url, stream=True, timeout=120) as response:
                response.raise_for_status()
                with open(download_path, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            handle.write(chunk)
            return True
        except requests.RequestException as exc:
            logger.error("Elevify: falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Elevify", ElevifyPlatform)
