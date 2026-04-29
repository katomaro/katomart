from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, BasePlatform, PlatformFactory, sanitize_token

logger = logging.getLogger(__name__)

API_BASE = "https://core.finclass.com"
LOGIN_URL = f"{API_BASE}/users/user/login"
ME_URL = f"{API_BASE}/users/user/readMe"
COURSES_URL = f"{API_BASE}/learning/courses"
COURSE_DETAIL_URL = f"{API_BASE}/learning/courses/{{course_id}}"
CONTENT_FOLLOW_URL = f"{API_BASE}/learning/content-follow"

WEB_ORIGIN = "https://app.finclass.com"
GPRIM_SPECVER = "2.2.1"

# Pagination wired but disabled — single page covers full catalog as of capture.
# Flip to True if FinClass starts paginating.
_PAGINATION_ENABLED = False
_PAGE_SIZE = 100

# Whitelist of course statuses to surface. Extend as new statuses appear.
_ALLOWED_COURSE_STATUSES = {"published"}


class FinClassPlatform(BasePlatform):
    """
    FinClass platform integration.

    Auth: POST core.finclass.com/users/user/login -> response header `sessionid`.
    All authed requests carry `sessionid` + X-gprim-spec{dev,nav,ver} headers.

    Catalog: GET /learning/courses (single page, paginates trivially if needed).
    Course detail: GET /learning/courses/{id} returns moduleEntities w/ full lessons.
    Video: lessonMedia.mediaDash (DASH, no DRM observed) primary;
           mediaSource (direct MP4) and mediaHls available as fallbacks.
    Attachments: lessonFiles[].fileAddress (public URLs on assets.finclass.com).
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._sessionid: Optional[str] = None
        self._user_id: Optional[str] = None
        # Mark-watched gated until per-platform settings exist (see mark_lesson_watched).
        # TODO: expose via SettingsManager per-platform settings once supported.
        self._mark_watched_enabled: bool = False

    # ------------------------------------------------------------------ auth

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Como autenticar no FinClass:

Opção 1 — Usuário e senha (recomendado):
1) Informe o e-mail e a senha cadastrados na FinClass.
2) O Katomart fará login em https://core.finclass.com/users/user/login e
   capturará o cabeçalho `sessionid` da resposta automaticamente.

Opção 2 — Colar o `sessionid` manualmente (caso já tenha capturado):
1) Abra https://app.finclass.com em seu navegador e faça login normalmente.
2) Abra as Ferramentas de Desenvolvedor (F12) -> aba Rede (Network).
3) Selecione qualquer requisição feita para o domínio `core.finclass.com`.
4) Na aba "Cabeçalhos" (Headers) -> "Cabeçalhos da requisição" copie o valor
   do header `sessionid` (formato UUID, ex.: `e5ae88ee-4217-43b4-8979-b4de160b4517`).
5) Cole esse valor no campo "Token de Acesso".

Observação: a FinClass não oferece 2FA ou OTP no momento.
"""

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        token = sanitize_token((credentials.get("token") or "").strip())
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        sessionid: Optional[str] = None

        if username and password:
            sessionid = self._login_with_credentials(username, password)
            self.credentials = credentials
        elif token:
            sessionid = token
        else:
            raise ValueError("Informe e-mail e senha ou um token (sessionid) válido.")

        if not sessionid:
            raise ConnectionError("Não foi possível obter o sessionid da FinClass.")

        self._configure_session(sessionid)

        try:
            self._user_id = self._fetch_user_id()
            if self._user_id:
                logger.info("FinClass: userID resolvido (%s).", self._user_id[:8])
        except Exception as exc:
            logger.warning("FinClass: falha ao resolver userID via /users/user/readMe: %s", exc)

    def refresh_auth(self) -> None:
        if self.credentials:
            self.authenticate(self.credentials)
        else:
            raise ValueError("Sem credenciais armazenadas para renovar a sessão FinClass.")

    def _login_with_credentials(self, email: str, password: str) -> str:
        payload = {"email": email, "password": password}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": f"{WEB_ORIGIN}/",
            "User-Agent": self._settings.user_agent,
            "X-gprim-specdev": "WEB",
            "X-gprim-specnav": self._settings.user_agent,
            "X-gprim-specver": GPRIM_SPECVER,
        }
        try:
            response = requests.post(LOGIN_URL, json=payload, headers=headers, timeout=30)
        except requests.RequestException as exc:
            logger.error("FinClass: erro de rede no login: %s", exc)
            raise ConnectionError(f"Falha ao autenticar no FinClass: {exc}") from exc

        if response.status_code == 401 or response.status_code == 403:
            raise ValueError("Credenciais FinClass inválidas.")
        if not response.ok:
            raise ConnectionError(
                f"Login FinClass retornou status {response.status_code}: {response.text[:200]}"
            )

        # sessionid is delivered in the response *headers*, not in the body.
        sessionid = response.headers.get("sessionid") or response.headers.get("Sessionid")
        if not sessionid:
            raise ConnectionError(
                "Login FinClass não retornou o cabeçalho `sessionid`. "
                "Verifique se há proxy/CDN reescrevendo headers."
            )
        return sanitize_token(sessionid.strip())

    def _configure_session(self, sessionid: str) -> None:
        self._sessionid = sessionid
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Origin": WEB_ORIGIN,
            "Referer": f"{WEB_ORIGIN}/",
            "User-Agent": self._settings.user_agent,
            "sessionid": sessionid,
            "X-gprim-specdev": "WEB",
            "X-gprim-specnav": self._settings.user_agent,
            "X-gprim-specver": GPRIM_SPECVER,
        })

    def _fetch_user_id(self) -> Optional[str]:
        if not self._session:
            return None
        response = self._session.get(ME_URL, timeout=30)
        if not response.ok:
            return None
        try:
            data = response.json().get("data") or {}
        except ValueError:
            return None
        return data.get("userID") or data.get("userId") or data.get("id")

    # -------------------------------------------------------------- catalog

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessão FinClass não autenticada.")

        courses: List[Dict[str, Any]] = []
        page = 1
        while True:
            params = {"page": page, "perPage": _PAGE_SIZE}
            response = self._session.get(COURSES_URL, params=params, timeout=30)
            response.raise_for_status()
            try:
                payload = response.json() or {}
            except ValueError as exc:
                raise ConnectionError(f"Resposta inválida em /learning/courses: {exc}") from exc

            data = payload.get("data") or []
            for raw in data:
                normalized = self._normalize_course(raw)
                if normalized is not None:
                    courses.append(normalized)

            if not _PAGINATION_ENABLED or not data or len(data) < _PAGE_SIZE:
                break
            page += 1

        logger.info("FinClass: %d cursos retornados.", len(courses))
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessão FinClass não autenticada.")

        all_content: Dict[str, Any] = {}
        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue
            try:
                detail = self._fetch_course_detail(course_id)
            except Exception as exc:
                logger.error("FinClass: falha ao buscar detalhes do curso %s: %s", course_id, exc)
                continue

            modules = self._build_modules(course_id, detail)
            entry = course.copy()
            entry["modules"] = modules
            entry["raw_detail"] = detail
            all_content[str(course_id)] = entry

        return all_content

    # -------------------------------------------------------- catalog helpers

    def _normalize_course(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        status = (raw.get("courseStatus") or "").lower()
        if status not in _ALLOWED_COURSE_STATUSES:
            logger.debug(
                "FinClass: curso %s ignorado (status=%s).",
                raw.get("courseID"),
                status,
            )
            return None

        course_id = raw.get("courseID")
        if not course_id:
            return None

        medias = raw.get("courseMedias") or {}
        cover = medias.get("thumb") or medias.get("poster") or medias.get("banner")

        return {
            "id": course_id,
            "name": raw.get("courseTitle") or "Curso sem nome",
            "title": raw.get("courseTitle") or "Curso sem nome",
            "description": raw.get("courseDescription") or "",
            "cover_url": cover,
            "seller_name": raw.get("courseCenter") or "FinClass",
            "raw": raw,
        }

    def _fetch_course_detail(self, course_id: str) -> Dict[str, Any]:
        url = COURSE_DETAIL_URL.format(course_id=course_id)
        response = self._session.get(url, timeout=30)
        response.raise_for_status()
        payload = response.json() or {}
        return payload.get("data") or {}

    def _build_modules(self, course_id: str, detail: Dict[str, Any]) -> List[Dict[str, Any]]:
        modules_out: List[Dict[str, Any]] = []
        course_modules = detail.get("courseModules") or []

        for mod_index, mod in enumerate(course_modules, start=1):
            entities_by_id: Dict[str, Dict[str, Any]] = {
                e.get("lessonID"): e for e in (mod.get("moduleEntities") or []) if e.get("lessonID")
            }

            order = mod.get("moduleOrder") or []
            ordered_lesson_ids = [item.get("lessonID") for item in order if item.get("lessonID")]
            if not ordered_lesson_ids:
                ordered_lesson_ids = mod.get("moduleLessonsID") or list(entities_by_id.keys())

            module_id = mod.get("moduleID") or f"{course_id}:{mod_index}"
            module_title = mod.get("moduleTitle") or f"Módulo {mod_index}"

            lessons: List[Dict[str, Any]] = []
            for lesson_index, lesson_id in enumerate(ordered_lesson_ids, start=1):
                entity = entities_by_id.get(lesson_id)
                if not entity:
                    logger.debug(
                        "FinClass: lessonID %s presente em moduleOrder mas sem moduleEntities (curso=%s).",
                        lesson_id,
                        course_id,
                    )
                    continue
                lessons.append({
                    "id": lesson_id,
                    "title": entity.get("lessonTitle") or f"Aula {lesson_index}",
                    "order": lesson_index,
                    "course_id": course_id,
                    "module_id": module_id,
                    "locked": False,
                    "extra_props": {
                        "is_trailer": entity.get("lessonType") == "trailer",
                    },
                    "raw": entity,
                })

            modules_out.append({
                "id": module_id,
                "title": module_title,
                "order": mod_index,
                "locked": False,
                "lessons": lessons,
            })

        return modules_out

    # ------------------------------------------------------- lesson details

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        content = LessonContent()
        raw = lesson.get("raw") or {}
        lesson_id = lesson.get("id") or raw.get("lessonID") or ""
        title = lesson.get("title") or raw.get("lessonTitle") or "Aula"

        description_text = raw.get("lessonDescription") or ""
        if description_text:
            # FinClass returns plain text; expose as text so workers save .txt.
            content.description = Description(text=description_text, description_type="text")

        media = raw.get("lessonMedia") or {}
        video_url = media.get("mediaDash") or media.get("mediaSource") or media.get("mediaHls")

        if video_url:
            duration_ms = media.get("mediaMilliseconds") or 0
            duration_s = int(duration_ms / 1000) if duration_ms else 0
            fallback_urls = [
                u for u in (media.get("mediaSource"), media.get("mediaHls"))
                if u and u != video_url
            ]
            content.videos.append(Video(
                video_id=str(media.get("mediaID") or lesson_id),
                url=video_url,
                order=lesson.get("order", 1) or 1,
                title=title,
                size=0,
                duration=duration_s,
                extra_props={
                    "referer": f"{WEB_ORIGIN}/",
                    # NOTE: workers iterate Video list one-shot; fallback URLs are
                    # carried here for any future fallback-aware downloader logic.
                    # If yt-dlp fails on the .mpd, these are the next URLs to try.
                    "fallback_urls": fallback_urls,
                },
            ))
        else:
            logger.warning(
                "FinClass: aula %s sem lessonMedia utilizável (curso=%s).",
                lesson_id,
                course_id,
            )

        for idx, file_entry in enumerate(raw.get("lessonFiles") or [], start=1):
            url = file_entry.get("fileAddress") or file_entry.get("fileURL")
            if not url:
                continue
            filename = (
                file_entry.get("filePublicName")
                or file_entry.get("fileName")
                or f"anexo-{idx}"
            )
            extension = ""
            if "." in filename:
                extension = filename.rsplit(".", 1)[-1]
            elif "." in url.split("?")[0]:
                extension = url.split("?")[0].rsplit(".", 1)[-1]
            content.attachments.append(Attachment(
                attachment_id=f"{lesson_id}:file:{idx}",
                url=url,
                filename=filename,
                order=idx,
                extension=extension,
                size=int(file_entry.get("fileSize") or 0),
            ))

        # Subtitles: best-effort. FinClass may expose them via either field.
        subtitle_entries = list(raw.get("lessonSubtitles") or []) + list(media.get("mediaSubtitle") or [])
        if subtitle_entries:
            for sub_idx, sub in enumerate(subtitle_entries, start=1):
                sub_url, sub_lang = self._extract_subtitle(sub)
                if not sub_url:
                    continue
                ext = "vtt"
                clean = sub_url.split("?")[0]
                if "." in clean:
                    ext = clean.rsplit(".", 1)[-1].lower()
                content.attachments.append(Attachment(
                    attachment_id=f"{lesson_id}:sub:{sub_idx}",
                    url=sub_url,
                    filename=f"legenda-{sub_lang or sub_idx}.{ext}",
                    order=900 + sub_idx,
                    extension=ext,
                    size=0,
                ))
        else:
            logger.info("FinClass: aula %s sem legendas disponíveis.", lesson_id)

        return content

    def _extract_subtitle(self, sub: Any) -> tuple[Optional[str], Optional[str]]:
        """Best-effort extraction of (url, language) from heterogeneous subtitle entries."""
        if isinstance(sub, str):
            return sub, None
        if not isinstance(sub, dict):
            return None, None
        url = (
            sub.get("subtitleURL")
            or sub.get("url")
            or sub.get("fileAddress")
            or sub.get("fileURL")
        )
        lang = sub.get("subtitleLanguage") or sub.get("language") or sub.get("lang")
        return url, lang

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            raise ConnectionError("Sessão FinClass não autenticada.")
        url = attachment.url
        if not url:
            logger.error("FinClass: anexo sem URL: %s", attachment.filename)
            return False
        try:
            # Public URLs on assets.finclass.com don't strictly need auth, but
            # using the platform session preserves UA/referer parity.
            with self._session.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                with open(download_path, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            fh.write(chunk)
            return True
        except Exception as exc:
            logger.error("FinClass: falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False

    # ------------------------------------------- mark watched (Batch 01.4)

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        # Gated by self._mark_watched_enabled. Until per-platform settings exist,
        # default behaviour is to skip without erroring.
        # TODO: expose self._mark_watched_enabled via SettingsManager per-platform
        #       settings once supported, allowing users to opt in.
        if not self._mark_watched_enabled:
            logger.info(
                "FinClass: mark_lesson_watched desativado por padrão; ignorando aula %s.",
                lesson.get("id"),
            )
            return
        raise NotImplementedError("FinClass.mark_lesson_watched ativa pendente (Batch 01.4).")


PlatformFactory.register_platform("FinClass", FinClassPlatform)
