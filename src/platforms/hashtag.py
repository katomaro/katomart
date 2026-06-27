from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import urllib.parse
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

import requests
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response as PwResponse,
    sync_playwright,
)

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

INTEGRATION_SLUG = "hashtag"
INTEGRATION_VERSION = "2.0.0"
INTEGRATION_EXPERIMENTAL = True

T = TypeVar("T")

# The portal SPA lives on PORTAL_ORIGIN, authenticates through an AWS Cognito
# OAuth/PKCE flow served by LOGIN_HOST, and consumes a REST API on API_BASE.
# Videos are hosted on PandaVideo.  These hosts are independent of each other;
# update them together if Hashtag migrates its frontend.
PORTAL_ORIGIN = "https://portal.hashtagtreinamentos.com"
LOGIN_URL = "https://portal.hashtagtreinamentos.com/"
API_BASE_DEFAULT = "https://student-prod.hashtaglms.com"
PANDA_API_BASE = "https://api-v2.pandavideo.com.br"

# Public PandaVideo read key embedded in the portal frontend.  Captured live
# from intercepted player requests when available; this hard-coded default is
# the fallback so lesson resolution keeps working before any video is opened.
# Bump it if PandaVideo starts rejecting the requests.
PANDA_API_KEY = (
    "panda-56436abe1e335f58d8839bbdcb08fa8f3781d8cf91e528c3754135e74118b517"
)


class HashtagPlatform(BasePlatform):
    """
    Platform for Hashtag Treinamentos (portal.hashtagtreinamentos.com).

    Authentication is an AWS Cognito OAuth/PKCE flow driven through a real
    browser (Playwright).  A response interceptor sniffs the ``Authorization:
    Bearer`` header the SPA sends to the ``hashtaglms.com`` REST API, and the
    ``panda-*`` key it sends to the PandaVideo API.  Course structure comes
    from a single ``/course/{id}`` call whose tree already carries every
    lesson's PandaVideo id and attachment list.
    """

    def __init__(
        self, api_service: ApiService, settings_manager: SettingsManager
    ) -> None:
        super().__init__(api_service, settings_manager)

        # Captured auth material (written by the PW interceptor thread).
        self._bearer_token: str = ""
        self._api_base: str = API_BASE_DEFAULT
        self._panda_token: str = PANDA_API_KEY

        # PandaVideo resolution cache: videoId -> playable HLS/player URL.
        self._panda_cache: Dict[str, str] = {}

        # Playwright resources (thread-bound).
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._browser_context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._pw_queue: Optional[queue.Queue] = None
        self._pw_thread: Optional[threading.Thread] = None

    @classmethod
    def all_auth_fields(cls) -> List[AuthField]:
        return [
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
            "Hashtag Treinamentos (portal.hashtagtreinamentos.com).\n\n"
            "1. Um navegador será aberto automaticamente\n"
            "2. Faça login normalmente (e-mail/senha)\n"
            "3. Aguarde o portal carregar completamente\n"
            "4. Clique em 'Confirmar' no katomart"
        )

    def _start_pw_thread(self) -> None:
        self._pw_queue = queue.Queue()
        self._pw_thread = threading.Thread(
            target=self._pw_loop, daemon=True, name="playwright-hashtag"
        )
        self._pw_thread.start()

    def _pw_loop(self) -> None:
        while True:
            item = self._pw_queue.get()
            if item is None:
                break
            func, future = item
            try:
                result = func()
                future.set_result(result)
            except BaseException as exc:
                future.set_exception(exc)

    def _pw_exec(self, func: Callable[[], T]) -> T:
        if not self._pw_thread or not self._pw_thread.is_alive():
            raise ConnectionError(
                "O navegador não está conectado. Reautentique na plataforma."
            )
        future: Future[T] = Future()
        self._pw_queue.put((func, future))
        return future.result(timeout=600)

    def _handle_response(self, response: PwResponse) -> None:
        """Sniff the Bearer (LMS API) and panda-* (PandaVideo) auth tokens."""
        try:
            req = response.request
            parsed = urllib.parse.urlparse(req.url)
            host = parsed.netloc
            # NB: req.headers (the property) strips security headers such as
            # Authorization; all_headers() returns the real header set.
            try:
                headers = req.all_headers()
            except Exception:
                headers = req.headers
            auth = headers.get("authorization", "")

            if "hashtaglms.com" in host and auth.lower().startswith("bearer "):
                self._bearer_token = auth.split(" ", 1)[1].strip()
                self._api_base = f"{parsed.scheme}://{host}"
            elif host == urllib.parse.urlparse(PANDA_API_BASE).netloc:
                if auth.startswith("panda-"):
                    self._panda_token = auth
                if "/videos/" in parsed.path:
                    try:
                        data = response.json()
                        vid = data.get("id")
                        hls = data.get("video_hls") or data.get("video_player")
                        if vid and hls:
                            self._panda_cache[vid] = hls
                    except Exception:
                        pass
        except Exception:
            pass

    def close(self) -> None:
        if self._pw_queue and self._pw_thread and self._pw_thread.is_alive():
            def _cleanup() -> None:
                for attr in ("_page", "_browser_context", "_browser"):
                    obj = getattr(self, attr, None)
                    if obj:
                        try:
                            obj.close()
                        except Exception:
                            pass
                        setattr(self, attr, None)
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
            self._pw_queue.put(None)
            self._pw_thread.join(timeout=10)

        self._pw_thread = None
        self._pw_queue = None
        self._page = None
        self._browser_context = None
        self._browser = None
        self._playwright = None

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        self.close()

        credentials["browser_emulation"] = True
        self._bearer_token = ""

        logging.info("Hashtag: Iniciando navegador para autenticação...")
        self._start_pw_thread()

        def _launch() -> None:
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
            self._page.on("response", self._handle_response)
            logging.info("Hashtag: Navegando para o portal...")
            self._page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

        try:
            self._pw_exec(_launch)
        except Exception:
            self.close()
            raise

        confirmation_event = credentials.get("manual_auth_confirmation")
        if confirmation_event:
            logging.info("Hashtag: Aguardando confirmação de login...")
            confirmation_event.wait()
        else:
            logging.warning(
                "Hashtag: Sem evento de confirmação. Aguardando captura do token..."
            )

        logging.info("Hashtag: Confirmado. Capturando token de acesso...")
        self._capture_bearer_token()
        if not self._bearer_token:
            raise ConnectionError(
                "Não foi possível capturar o token de acesso. "
                "Confirme que o login foi concluído e o portal carregou."
            )
        self._setup_session()
        logging.info("Hashtag: Autenticação concluída.")

    def _capture_bearer_token(self, timeout_ms: int = 30000) -> None:
        """Wait for the SPA to emit a Bearer request the interceptor can sniff.

        The token is usually already captured during the login wait; if not,
        reload the portal once to nudge the SPA, then fall back to reading the
        access token straight out of ``localStorage``.
        """
        def _do() -> None:
            deadline = timeout_ms
            step = 500
            reloaded = False
            while not self._bearer_token and deadline > 0:
                self._page.wait_for_timeout(step)
                deadline -= step
                if not self._bearer_token and not reloaded and deadline <= timeout_ms // 2:
                    reloaded = True
                    try:
                        self._page.goto(
                            LOGIN_URL, wait_until="networkidle", timeout=15000
                        )
                    except Exception:
                        pass

        try:
            self._pw_exec(_do)
        except Exception as exc:
            logging.warning(f"Hashtag: Falha ao capturar token via navegação: {exc}")

        if not self._bearer_token:
            token = self._extract_token_from_storage()
            if token:
                self._bearer_token = token
                logging.info("Hashtag: Token capturado via localStorage.")

    @staticmethod
    def _is_access_jwt(value: str) -> bool:
        """True if ``value`` is a Cognito access JWT (token_use == access)."""
        if not isinstance(value, str) or not value.startswith("eyJ"):
            return False
        parts = value.split(".")
        if len(parts) != 3:
            return False
        try:
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(pad))
            return payload.get("token_use") == "access"
        except Exception:
            return False

    def _extract_token_from_storage(self) -> str:
        """Scan the SPA's localStorage for a Cognito access token."""
        def _do() -> str:
            try:
                raw = self._page.evaluate("() => JSON.stringify(window.localStorage)")
            except Exception:
                return ""
            try:
                store = json.loads(raw)
            except Exception:
                return ""

            found: List[str] = []

            def collect(val: Any) -> None:
                if found:
                    return
                if isinstance(val, str):
                    if self._is_access_jwt(val):
                        found.append(val)
                        return
                    # The token may be nested inside a JSON-encoded value.
                    try:
                        collect(json.loads(val))
                    except Exception:
                        pass
                elif isinstance(val, dict):
                    for v in val.values():
                        collect(v)
                elif isinstance(val, list):
                    for v in val:
                        collect(v)

            collect(store)
            return found[0] if found else ""

        try:
            return self._pw_exec(_do)
        except Exception as exc:
            logging.warning(f"Hashtag: Falha ao ler token do localStorage: {exc}")
            return ""

    def _setup_session(self) -> None:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._bearer_token}",
                "Origin": PORTAL_ORIGIN,
                "Referer": f"{PORTAL_ORIGIN}/",
                "x-time-zone": "America/Sao_Paulo",
            }
        )
        self._session = session

    def refresh_auth(self) -> None:
        """Re-capture a fresh Bearer token.  The SPA refreshes the Cognito
        token automatically on load, so re-navigating yields a new one."""
        try:
            self._bearer_token = ""
            self._capture_bearer_token()
            if not self._bearer_token:
                raise ConnectionError("Token não recapturado.")
            self._setup_session()
            logging.info("Hashtag: Sessão atualizada.")
        except Exception as exc:
            logging.error(f"Hashtag: Falha ao atualizar sessão – {exc}")
            raise ConnectionError("Sessão expirada. Reautentique.") from exc

    def _api_get(self, path: str) -> Any:
        if not self._session:
            raise ConnectionError("Sessão não disponível. Reautentique.")
        url = f"{self._api_base}{path}"
        resp = self._session.get(url, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def fetch_courses(self) -> List[Dict[str, Any]]:
        courses: Dict[str, Dict[str, Any]] = {}

        # 1 courses granted by the active subscription(s).
        try:
            data = self._api_get("/self/signature/with-courses-and-contract")
            for sig in data.get("signatures", []) or []:
                seller = sig.get("packageName") or "Hashtag Treinamentos"
                for c in sig.get("courses", []) or []:
                    cid = str(c.get("id"))
                    if cid and cid != "None":
                        courses.setdefault(
                            cid,
                            {
                                "id": cid,
                                "name": c.get("name") or cid,
                                "slug": cid,
                                "seller_name": seller,
                            },
                        )
        except Exception as exc:
            logging.warning(f"Hashtag: Falha ao listar assinaturas: {exc}")

        # 2 individually enrolled courses (matriculation with a course).
        try:
            data = self._api_get("/self/matriculation")
            for m in data.get("data", []) or []:
                course = m.get("course")
                if isinstance(course, dict) and course.get("id"):
                    cid = str(course["id"])
                    courses.setdefault(
                        cid,
                        {
                            "id": cid,
                            "name": course.get("name") or cid,
                            "slug": cid,
                            "seller_name": "Hashtag Treinamentos",
                        },
                    )
        except Exception as exc:
            logging.warning(f"Hashtag: Falha ao listar matrículas: {exc}")

        result = sorted(courses.values(), key=lambda c: c["name"].lower())
        logging.info(f"Hashtag: {len(result)} cursos encontrados.")
        return result

    def fetch_course_content(
        self, courses: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        content: Dict[str, Any] = {}

        for course in courses:
            course_id = str(course["id"])
            course_name = course.get("name", course_id)
            seller = course.get("seller_name", "Hashtag Treinamentos")
            logging.info(f"Hashtag: Obtendo conteúdo de '{course_name}'...")

            try:
                data = self._api_get(f"/course/{course_id}")
            except Exception as exc:
                logging.error(
                    f"Hashtag: Falha ao obter curso {course_id}: {exc}"
                )
                content[course_id] = self._empty_course(
                    course_id, course_name, seller
                )
                continue

            tree = data.get("courseTree") or {}
            modules = self._build_modules(tree, course_id)

            content[course_id] = {
                "id": course_id,
                "name": course_name,
                "slug": course_id,
                "seller_name": seller,
                "title": tree.get("name") or course_name,
                "modules": modules,
            }

            total_lessons = sum(len(m["lessons"]) for m in modules)
            logging.info(
                f"Hashtag: '{course_name}' – "
                f"{len(modules)} módulos, {total_lessons} aulas."
            )

        return content

    def _build_modules(
        self, tree: Dict[str, Any], course_id: str
    ) -> List[Dict[str, Any]]:
        """Flatten the course tree into ``[{title, lessons}]``.

        The tree nests as ``course → level → module → lesson_theory``.  We
        treat every node that *directly* contains lessons as a module, which
        tolerates courses whose lessons sit under a level (no module) or any
        other depth.  Modules are kept in tree (DFS) order.
        """
        modules: List[Dict[str, Any]] = []

        def visit(node: Dict[str, Any]) -> None:
            children = node.get("children") or []
            lesson_children = [
                c for c in children if c.get("type") == "lesson_theory"
            ]
            if lesson_children:
                lesson_children.sort(key=lambda c: c.get("order") or 0)
                lessons = [
                    self._node_to_lesson(c, course_id, idx)
                    for idx, c in enumerate(lesson_children, start=1)
                ]
                modules.append(
                    {
                        "id": str(node.get("id", "")),
                        "title": node.get("name") or f"Módulo {len(modules) + 1}",
                        "order": len(modules) + 1,
                        "lessons": lessons,
                    }
                )
            for c in children:
                if c.get("type") != "lesson_theory":
                    visit(c)

        visit(tree)
        return modules

    @staticmethod
    def _node_to_lesson(
        node: Dict[str, Any], course_id: str, order: int
    ) -> Dict[str, Any]:
        md = node.get("metadata") or {}
        total_minutes = md.get("totalInMinutes") or 0
        return {
            "id": str(node.get("id", "")),
            "title": node.get("name", "Aula"),
            "order": order,
            "duration": int(round(float(total_minutes) * 60)),
            "video_id": md.get("videoId") or "",
            "attachments_raw": md.get("attachments") or [],
            "locked": node.get("status") != "published"
            or not node.get("active", True),
            "watched": bool(node.get("watched")),
            "course_id": course_id,
        }

    @staticmethod
    def _empty_course(
        course_id: str, name: str, seller: str = "Hashtag Treinamentos"
    ) -> Dict[str, Any]:
        return {
            "id": course_id,
            "name": name,
            "slug": course_id,
            "seller_name": seller,
            "title": name,
            "modules": [],
        }

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        lesson_id = str(lesson.get("id", ""))
        lesson_title = lesson.get("title", "")

        video_id = lesson.get("video_id") or ""
        raw_atts = lesson.get("attachments_raw") or []
        duration = lesson.get("duration", 0)

        # If the lesson dict came in thin (e.g. re-download flow), enrich it
        # from the per-lesson endpoint.
        if not video_id and not raw_atts:
            detail = self._fetch_lesson_theory(lesson_id)
            md = (detail or {}).get("metadata") or {}
            video_id = md.get("videoId") or ""
            raw_atts = md.get("attachments") or []
            if not duration:
                duration = int(round(float(md.get("totalInMinutes") or 0) * 60))

        videos: List[Video] = []
        if video_id:
            video_url = self._resolve_panda_url(video_id)
            if video_url:
                videos.append(
                    Video(
                        video_id=f"panda_{video_id}",
                        url=video_url,
                        order=1,
                        title=lesson_title,
                        size=0,
                        duration=duration,
                        extra_props={"referer": f"{PORTAL_ORIGIN}/"},
                    )
                )
            else:
                logging.warning(
                    f"Hashtag: '{lesson_title}' – vídeo {video_id} não resolvido."
                )

        attachments: List[Attachment] = []
        for idx, att in enumerate(raw_atts, start=1):
            if not isinstance(att, dict):
                continue
            url = att.get("url") or ""
            if not url:
                continue
            if url.startswith("//"):
                url = f"https:{url}"
            raw_name = att.get("name") or url.rsplit("/", 1)[-1]
            filename = urllib.parse.unquote(raw_name)
            extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
            attachments.append(
                Attachment(
                    attachment_id=f"att_{lesson_id}_{idx}",
                    url=url,
                    filename=filename,
                    order=idx,
                    extension=extension,
                    size=int(att.get("size") or 0),
                )
            )

        logging.info(
            f"Hashtag: '{lesson_title}' – "
            f"{len(videos)} vídeo(s), {len(attachments)} anexo(s)."
        )

        return LessonContent(
            description=None,
            videos=videos,
            attachments=attachments,
            auxiliary_urls=[],
        )

    def _fetch_lesson_theory(self, lesson_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self._api_get(f"/lesson-theory/{lesson_id}")
        except Exception as exc:
            logging.warning(
                f"Hashtag: Falha ao obter detalhes da aula {lesson_id}: {exc}"
            )
            return None

    def _resolve_panda_url(self, video_id: str) -> str:
        """Resolve a PandaVideo id to a playable HLS URL via the PandaVideo API.

        Results are cached; the interceptor may also have pre-cached it from a
        player request observed in the browser.
        """
        if video_id in self._panda_cache:
            return self._panda_cache[video_id]

        url = f"{PANDA_API_BASE}/videos/{video_id}"
        headers = {
            "Authorization": self._panda_token,
            "Accept": "application/json",
            "User-Agent": self._settings.user_agent,
            "Origin": PORTAL_ORIGIN,
            "Referer": f"{PORTAL_ORIGIN}/",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            hls = data.get("video_hls") or data.get("video_player") or ""
            if hls:
                self._panda_cache[video_id] = hls
            return hls
        except Exception as exc:
            logging.warning(
                f"Hashtag: Falha ao resolver vídeo PandaVideo {video_id}: {exc}"
            )
            return ""

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        try:
            url = attachment.url
            if url.startswith("//"):
                url = f"https:{url}"

            # Attachments are public S3 URLs; download with a clean request so
            # the LMS Bearer header isn't sent to S3.
            resp = requests.get(
                url,
                headers={"User-Agent": self._settings.user_agent},
                stream=True,
                timeout=120,
            )
            resp.raise_for_status()

            with open(download_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)

            logging.info(f"Hashtag: Anexo salvo em {download_path}")
            return True
        except Exception as exc:
            logging.error(
                f"Hashtag: Falha ao baixar anexo {attachment.filename}: {exc}"
            )
            return False


# PlatformFactory.register_platform("Hashtag Treinamentos", HashtagPlatform, slug=INTEGRATION_SLUG, version=INTEGRATION_VERSION, experimental=INTEGRATION_EXPERIMENTAL)
PlatformFactory.register_platform("Hashtag Treinamentos", HashtagPlatform)
