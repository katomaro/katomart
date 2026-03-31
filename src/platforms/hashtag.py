from __future__ import annotations

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

T = TypeVar("T")

_LOOKUP_SEP = "__LOOKUP__"


def _resolve_lookup(ref: str) -> str:
    """Extract the actual Bubble ID from a ``prefix__LOOKUP__id`` reference."""
    if not ref:
        return ""
    if _LOOKUP_SEP in ref:
        return ref.split(_LOOKUP_SEP, 1)[1]
    return ref


class HashtagPlatform(BasePlatform):
    """
    Platform for portalhashtag.com (Bubble.io / Lira EAD).

    Uses Playwright for authentication and a persistent response interceptor
    that caches Bubble Elasticsearch data.  Course structure is discovered by
    following the lesson linked-list via the ``init/data`` REST API.  Videos
    are hosted on PandaVideo.
    """

    def __init__(
        self, api_service: ApiService, settings_manager: SettingsManager
    ) -> None:
        super().__init__(api_service, settings_manager)
        self._base_url: str = ""

        # Playwright resources (thread-bound)
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._browser_context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._pw_queue: Optional[queue.Queue] = None
        self._pw_thread: Optional[threading.Thread] = None

        # Intercepted-data caches  (written by PW thread, read after _pw_exec)
        self._courses_cache: Dict[str, Dict[str, Any]] = {}
        self._enrollments_cache: Dict[str, Dict[str, Any]] = {}  # course_id → enroll
        self._modules_cache: Dict[str, Dict[str, Any]] = {}
        self._blocos_cache: Dict[str, Dict[str, Any]] = {}
        self._aulas_cache: Dict[str, Dict[str, Any]] = {}

        # PandaVideo player hostname (detected lazily)
        self._panda_player_host: str = ""

    # ── Auth fields ──────────────────────────────────────────────

    @classmethod
    def all_auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="site_url",
                label="URL do site",
                field_type=AuthFieldType.TEXT,
                placeholder="Ex: https://portalhashtag.com",
                required=True,
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
            "Portal Hashtag Treinamentos (Bubble.io / Lira EAD).\n\n"
            "1. Informe a URL do portal (ex: https://portalhashtag.com)\n"
            "2. Um navegador será aberto automaticamente\n"
            "3. Faça login normalmente\n"
            "4. Após o login, clique em 'Confirmar' no katomart"
        )

    # ── Playwright thread management ─────────────────────────────

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

    # ── Elasticsearch response interceptor ───────────────────────

    def _handle_response(self, response: PwResponse) -> None:
        """Intercept Bubble Elasticsearch responses and cache items by type."""
        if "/elasticsearch/" not in response.url:
            return
        try:
            data = response.json()
            self._ingest_es_payload(data)
        except Exception:
            pass

    def _ingest_es_payload(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        # search  → {"hits": {"hits": [...]}}
        hits_obj = data.get("hits")
        if isinstance(hits_obj, dict):
            for hit in hits_obj.get("hits", []):
                self._cache_item(hit.get("_source") or hit)
        # mget    → {"docs": [...]}
        docs = data.get("docs")
        if isinstance(docs, list):
            for doc in docs:
                if doc.get("found"):
                    self._cache_item(doc.get("_source") or doc)
        # msearch → {"responses": [...]}
        responses = data.get("responses")
        if isinstance(responses, list):
            for resp in responses:
                self._ingest_es_payload(resp)

    def _cache_item(self, src: Any) -> None:
        if not isinstance(src, dict):
            return
        item_type = src.get("_type", "")
        item_id = src.get("_id", "")
        if not item_type or not item_id:
            return

        if item_type == "custom.curso":
            self._courses_cache[item_id] = src
        elif item_type == "custom.matr_cula":
            course_id = _resolve_lookup(src.get("curso_custom_curso", ""))
            if course_id and src.get("ativa_boolean"):
                self._enrollments_cache[course_id] = src
        elif item_type == "custom.modulo":
            self._modules_cache[item_id] = src
        elif item_type == "custom.bloco":
            self._blocos_cache[item_id] = src
        elif item_type == "custom.aula":
            self._aulas_cache[item_id] = src

    # ── Lifecycle ────────────────────────────────────────────────

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

    # ── Authentication ───────────────────────────────────────────

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        self.close()

        self._base_url = (credentials.get("site_url") or "").strip().rstrip("/")
        if not self._base_url:
            raise ValueError("A URL do site é obrigatória.")
        if not self._base_url.startswith("http"):
            self._base_url = f"https://{self._base_url}"

        credentials["browser_emulation"] = True

        logging.info("Hashtag: Iniciando navegador para autenticação...")
        self._start_pw_thread()

        login_url = f"{self._base_url}/login"

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
            logging.info("Hashtag: Navegando para página de login...")
            self._page.goto(login_url, wait_until="networkidle", timeout=60000)

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
            base = self._base_url
            logging.warning("Hashtag: Sem evento de confirmação. Aguardando redirect...")
            self._pw_exec(lambda: self._page.wait_for_url(f"{base}/**", timeout=300000))

        logging.info("Hashtag: Login confirmado. Extraindo cookies...")
        cookies = self._pw_exec(lambda: self._browser_context.cookies())
        self._setup_session(cookies)
        logging.info("Hashtag: Autenticação concluída.")

    def _setup_session(self, cookies: List[Dict[str, Any]]) -> None:
        session = requests.Session()
        session.headers.update({"User-Agent": self._settings.user_agent})
        for c in cookies:
            session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
            )
        self._session = session

    def refresh_auth(self) -> None:
        try:
            base = self._base_url
            self._pw_exec(
                lambda: self._page.goto(base, wait_until="networkidle", timeout=30000)
            )
            cookies = self._pw_exec(lambda: self._browser_context.cookies())
            self._setup_session(cookies)
            logging.info("Hashtag: Sessão atualizada.")
        except Exception as exc:
            logging.error(f"Hashtag: Falha ao atualizar sessão – {exc}")
            raise ConnectionError("Sessão expirada. Reautentique.") from exc

    # ── Course listing ───────────────────────────────────────────

    def fetch_courses(self) -> List[Dict[str, Any]]:
        def _navigate_home() -> None:
            self._page.goto(
                f"{self._base_url}/", wait_until="networkidle", timeout=60000
            )
            self._page.wait_for_timeout(5000)

        self._pw_exec(_navigate_home)

        courses: List[Dict[str, Any]] = []
        for cid, curso in self._courses_cache.items():
            if not curso.get("publicado_boolean"):
                continue
            courses.append(
                {
                    "id": cid,
                    "name": curso.get("nome_text") or curso.get("name_text") or cid,
                    "slug": cid,
                    "seller_name": "Hashtag Treinamentos",
                    "lessons_count": curso.get("total_aulas_number", 0),
                    "order": curso.get("ordem_number", 999),
                    "enrolled": cid in self._enrollments_cache,
                }
            )

        courses.sort(key=lambda c: c.get("order", 999))
        enrolled = sum(1 for c in courses if c.get("enrolled"))
        logging.info(
            f"Hashtag: {len(courses)} cursos encontrados "
            f"({enrolled} com matrícula ativa)."
        )
        return courses

    # ── init/data helper ─────────────────────────────────────────

    def _fetch_aula_via_init(self, lesson_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single lesson via Bubble's ``/api/1.1/init/data`` endpoint."""
        if not self._session:
            return None
        location = urllib.parse.quote(
            f"{self._base_url}/aulas/{lesson_id}", safe=""
        )
        url = f"{self._base_url}/api/1.1/init/data?location={location}"
        try:
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, list):
                for item in payload:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "custom.aula"
                    ):
                        aula = item.get("data", {})
                        aula["_id"] = item.get("id", lesson_id)
                        aula["_type"] = "custom.aula"
                        self._aulas_cache[aula["_id"]] = aula
                        return aula
        except Exception as exc:
            logging.debug(f"Hashtag: init/data falhou para aula {lesson_id}: {exc}")
        return None

    # ── Lesson chain traversal ───────────────────────────────────

    def _get_aula(self, lesson_id: str) -> Optional[Dict[str, Any]]:
        """Return cached aula or fetch via init/data."""
        return self._aulas_cache.get(lesson_id) or self._fetch_aula_via_init(
            lesson_id
        )

    def _traverse_lessons(
        self, start_id: str, course_id: str
    ) -> List[Dict[str, Any]]:
        """Walk the prev/next linked-list to discover every lesson in a course."""
        collected: Dict[str, Dict[str, Any]] = {}

        current = self._get_aula(start_id)
        if not current:
            return []
        collected[start_id] = current

        # ── walk backward ────────────────────────────────────────
        prev_id = current.get("id_anterior_aula_text", "")
        while prev_id and prev_id not in collected:
            prev = self._get_aula(prev_id)
            if not prev:
                break
            # stop if we left the course
            if _resolve_lookup(prev.get("curso_custom_curso", "")) != course_id:
                break
            collected[prev_id] = prev
            prev_id = prev.get("id_anterior_aula_text", "")

        # ── walk forward ─────────────────────────────────────────
        next_id = current.get("id_prox_aula_text", "")
        while next_id and next_id not in collected:
            nxt = self._get_aula(next_id)
            if not nxt:
                break
            if _resolve_lookup(nxt.get("curso_custom_curso", "")) != course_id:
                break
            collected[next_id] = nxt
            next_id = nxt.get("id_prox_aula_text", "")
            if len(collected) % 50 == 0:
                logging.info(
                    f"Hashtag: … {len(collected)} aulas descobertas até agora."
                )

        # ── build ordered list ───────────────────────────────────
        # find the head (no predecessor inside collected)
        head_id = start_id
        for lid, ldata in collected.items():
            prev = ldata.get("id_anterior_aula_text", "")
            if not prev or prev not in collected:
                head_id = lid
                break

        ordered: List[Dict[str, Any]] = []
        visited: set = set()
        cur = head_id
        while cur and cur in collected and cur not in visited:
            visited.add(cur)
            ordered.append(collected[cur])
            cur = collected[cur].get("id_prox_aula_text", "")

        # any straggler not reachable from head
        for lid, ldata in collected.items():
            if lid not in visited:
                ordered.append(ldata)

        return ordered

    # ── Course content ───────────────────────────────────────────

    def fetch_course_content(
        self, courses: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        content: Dict[str, Any] = {}

        for course in courses:
            course_id = course["id"]
            course_name = course.get("name", course_id)
            logging.info(f"Hashtag: Obtendo conteúdo de '{course_name}'...")

            # 1 — find a starting lesson
            start_id = self._find_start_lesson(course_id)
            if not start_id:
                logging.warning(
                    f"Hashtag: Nenhuma aula encontrada para '{course_name}'. "
                    "Verifique se você tem matrícula ativa."
                )
                content[course_id] = self._empty_course(course_id, course_name)
                continue

            # 2 — navigate once via Playwright to prime the ES cache
            self._navigate_to_lesson_pw(start_id)

            # 3 — traverse the linked list via HTTP
            all_lessons = self._traverse_lessons(start_id, course_id)
            logging.info(
                f"Hashtag: '{course_name}' – {len(all_lessons)} aulas descobertas."
            )

            # 4 — group into modules
            modules_by_id: Dict[str, Dict[str, Any]] = {}
            appearance_order = 0

            for aula in all_lessons:
                mod_id = _resolve_lookup(
                    aula.get("modulo_custom_modulo", "")
                ) or "unknown"

                if mod_id not in modules_by_id:
                    appearance_order += 1
                    mod_data = self._modules_cache.get(mod_id, {})
                    modules_by_id[mod_id] = {
                        "id": mod_id,
                        "title": (
                            mod_data.get("nome_text")
                            or f"Módulo {appearance_order}"
                        ),
                        "order": mod_data.get("ordem_number", appearance_order),
                        "lessons": [],
                    }

                lesson_entry = self._aula_to_lesson_dict(aula, course_id)
                lesson_entry["order"] = len(modules_by_id[mod_id]["lessons"]) + 1
                modules_by_id[mod_id]["lessons"].append(lesson_entry)

            modules_list = sorted(
                modules_by_id.values(), key=lambda m: m["order"]
            )

            content[course_id] = {
                "id": course_id,
                "name": course_name,
                "slug": course_id,
                "seller_name": "Hashtag Treinamentos",
                "title": course_name,
                "modules": modules_list,
            }

            total_lessons = sum(len(m["lessons"]) for m in modules_list)
            logging.info(
                f"Hashtag: '{course_name}' – "
                f"{len(modules_list)} módulos, {total_lessons} aulas."
            )

        return content

    # ── helpers for fetch_course_content ─────────────────────────

    def _find_start_lesson(self, course_id: str) -> str:
        """Return a lesson ID to start traversal from."""
        # 1 – enrollment's "currently watching" lesson
        enrollment = self._enrollments_cache.get(course_id)
        if enrollment:
            ref = enrollment.get("aula_assistindo_custom_aula", "")
            lid = _resolve_lookup(ref)
            if lid:
                return lid

        # 2 – any cached aula belonging to this course
        for aid, aula in self._aulas_cache.items():
            if _resolve_lookup(aula.get("curso_custom_curso", "")) == course_id:
                return aid

        return ""

    def _navigate_to_lesson_pw(self, lesson_id: str) -> None:
        """Navigate to a lesson page via Playwright to trigger ES data loading
        and detect the PandaVideo player host."""
        def _do() -> None:
            self._page.goto(
                f"{self._base_url}/aulas/{lesson_id}",
                wait_until="networkidle",
                timeout=60000,
            )
            self._page.wait_for_timeout(3000)

            if not self._panda_player_host:
                self._try_detect_panda_host()

        try:
            self._pw_exec(_do)
        except Exception as exc:
            logging.warning(f"Hashtag: Falha ao navegar para aula {lesson_id}: {exc}")

    def _try_detect_panda_host(self) -> None:
        """Look for a PandaVideo iframe on the current page (PW thread)."""
        iframe = self._page.query_selector("iframe[src*='pandavideo']")
        if iframe:
            src = iframe.get_attribute("src") or ""
            if src:
                parsed = urllib.parse.urlparse(src)
                self._panda_player_host = parsed.netloc
                logging.info(
                    f"Hashtag: PandaVideo host detectado: {self._panda_player_host}"
                )

    @staticmethod
    def _aula_to_lesson_dict(
        aula: Dict[str, Any], course_id: str
    ) -> Dict[str, Any]:
        return {
            "id": aula.get("_id", ""),
            "title": aula.get("nome_text", "Aula"),
            "order": aula.get("ordem_number", 1),
            "duration": aula.get("duracao_number", 0),
            "locked": not aula.get("publicada_boolean", True),
            "has_video": not aula.get("sem_video_boolean", False),
            "video_otp": aula.get("video_otp_text", ""),
            "attachments_raw": aula.get("anexos_list_file") or [],
            "course_id": course_id,
        }

    @staticmethod
    def _empty_course(course_id: str, name: str) -> Dict[str, Any]:
        return {
            "id": course_id,
            "name": name,
            "slug": course_id,
            "seller_name": "Hashtag Treinamentos",
            "title": name,
            "modules": [],
        }

    # ── Lesson details ───────────────────────────────────────────

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        lesson_id = lesson.get("id", "")
        lesson_title = lesson.get("title", "")

        # Data should already be cached from traverse (init/data returns full data).
        cached = self._aulas_cache.get(lesson_id)
        if not cached:
            cached = self._fetch_aula_via_init(lesson_id) or {}
        if not cached:
            # Last resort — navigate via Playwright to trigger ES data load
            self._navigate_to_lesson_pw(lesson_id)
            cached = self._aulas_cache.get(lesson_id, {})

        # ── video ────────────────────────────────────────────────
        videos: List[Video] = []
        video_otp = (
            lesson.get("video_otp")
            or cached.get("video_otp_text", "")
        )
        has_video = lesson.get("has_video", True) and not cached.get(
            "sem_video_boolean", False
        )

        if video_otp and has_video:
            video_url = self._build_panda_url(video_otp, lesson_id)
            if video_url:
                videos.append(
                    Video(
                        video_id=f"panda_{video_otp}",
                        url=video_url,
                        order=1,
                        title=lesson_title,
                        size=0,
                        duration=lesson.get("duration", 0)
                        or cached.get("duracao_number", 0),
                        extra_props={"referer": f"{self._base_url}/"},
                    )
                )

        # ── attachments ──────────────────────────────────────────
        attachments: List[Attachment] = []
        raw_atts: list = (
            lesson.get("attachments_raw")
            or cached.get("anexos_list_file")
            or []
        )
        for idx, att_url in enumerate(raw_atts, start=1):
            if not isinstance(att_url, str) or not att_url:
                continue
            if att_url.startswith("//"):
                att_url = f"https:{att_url}"
            filename = urllib.parse.unquote(att_url.rsplit("/", 1)[-1])
            extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
            attachments.append(
                Attachment(
                    attachment_id=f"att_{lesson_id}_{idx}",
                    url=att_url,
                    filename=filename,
                    order=idx,
                    extension=extension,
                    size=0,
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

    def _build_panda_url(self, video_otp: str, lesson_id: str) -> str:
        """Construct a PandaVideo embed URL, detecting the host if needed."""
        if not self._panda_player_host:
            # Try to detect by navigating to the lesson page
            self._navigate_to_lesson_pw(lesson_id)

        if self._panda_player_host:
            return (
                f"https://{self._panda_player_host}/embed/?v={video_otp}"
            )

        logging.warning(
            "Hashtag: PandaVideo host não detectado. "
            "Tentando URL genérica."
        )
        # Generic fallback – may not work for all sites
        return f"https://player.pandavideo.com.br/embed/?v={video_otp}"

    # ── Attachment download ──────────────────────────────────────

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            logging.error("Hashtag: Sessão não disponível para download.")
            return False
        try:
            url = attachment.url
            if url.startswith("//"):
                url = f"https:{url}"

            resp = self._session.get(url, stream=True, timeout=120)
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


PlatformFactory.register_platform("Hashtag Treinamentos (Bubble)", HashtagPlatform)
