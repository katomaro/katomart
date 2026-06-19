from typing import Any, Dict, List, Optional
import json
import logging
import re
from pathlib import Path

import requests

from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory, sanitize_token
from src.app.models import LessonContent, Description, Video, Attachment
from src.app.api_service import ApiService
from src.config.settings_manager import SettingsManager

INTEGRATION_SLUG = "unhideschool"
INTEGRATION_VERSION = "1.0.0"
INTEGRATION_EXPERIMENTAL = False

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.unhideschool.com/api/graphql"
ORIGIN = "https://unhideschool.com"
REFERER = "https://unhideschool.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Synthetic module id used to expose course-wide downloadable materials, which
# the API returns detached from any lesson (playerFilesFragment).
COURSE_FILES_MODULE_ID = "__course_files__"

LOGIN_MUTATION = """
  mutation LoginMutation($login: String!, $password: String!) {
    Login(login: $login, password: $password) {
      authtoken {
        token
      }
    }
  }
"""

GET_MY_COURSES = """
  query GetMyCourses($page: Int, $perpage: Int) {
    MyCourses(page: $page, perpage: $perpage) {
      page
      perpage
      totalCount
      courses {
        postid
        title
        headline
        datepublished
        postthumbnail
        creator {
          uid
          alias
        }
      }
    }
  }
"""

PLAYLIST_FRAGMENT = """
  fragment playerPlaylistFragment on VideoPlayer {
    course {
      playlist(page: $page, perpage: $perpage) {
        totalCount
        page
        perpage
        pages
        items {
          title
          description
          orderedpostpartid
          postpartid
          thumbnail
          groupname
          position
          content
          files {
            content
            name
            size
          }
        }
      }
    }
  }

  query playerPlaylistFragment($postid: Int, $page: Int, $perpage: Int) {
    VideoPlayer(postid: $postid) {
      ...playerPlaylistFragment
    }
  }
"""

FILES_FRAGMENT = """
  fragment playerFilesFragment on VideoPlayer {
    course {
      files {
        content
        name
        size
      }
    }
  }

  query playerFilesFragment($postid: Int) {
    VideoPlayer(postid: $postid) {
      ...playerFilesFragment
    }
  }
"""

VIDEO_OTP_QUERY = """
  query($orderedpostpartid: Int) {
    Video(orderedpostpartid: $orderedpostpartid) {
      groupname
      otp {
        otp
        playbackInfo
      }
      mediaadapter {
        internalname
      }
    }
  }
"""

UPDATE_PROGRESS_MUTATION = """
  mutation updateVideoProgressMutation($orderedpostpartid: Int, $timecode: String, $timespent: Int) {
    UpdateVideoProgress(orderedpostpartid: $orderedpostpartid, timecode: $timecode, timespent: $timespent) {
      postpartview {
        timecode
        finished
      }
    }
  }
"""


class UnhideSchoolPlatform(BasePlatform):
    """
    Platform for Unhide School (unhideschool.com), a Brazilian 3D/2D art
    course platform. The backend is a single GraphQL endpoint at
    api.unhideschool.com/api/graphql. Authentication is a custom header,
    ``Authorization: Unhide tok = <token>``, and videos are resolved per
    lesson through the ``Video(orderedpostpartid)`` query which returns a
    signed Vimeo DASH manifest in ``otp.playbackInfo``.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token: str = ""

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Faça login com seu e-mail e senha (recomendado), ou preencha apenas o campo Token.

Para obter o Token manualmente:
1) Abra o navegador e faça login em https://unhideschool.com
2) Abra as Ferramentas de Desenvolvedor (F12) → aba Rede (Network).
3) Use a lupa para procurar requisições para "api.unhideschool.com/api/graphql".
4) Clique em uma requisição POST e vá em Cabeçalhos (Headers) → Cabeçalhos da requisição.
5) Localize o cabeçalho 'Authorization', cujo valor é parecido com 'Unhide tok = <token>'.
6) Copie SOMENTE o valor do token (a parte longa após 'tok = ') e cole no campo Token.
"""

    def _configure_session(self, token: str) -> None:
        self._token = token
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": f"Unhide tok = {token}",
            "Origin": ORIGIN,
            "Referer": REFERER,
        })

    def _login_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        """Mints a fresh token via the GraphQL LoginMutation."""
        try:
            resp = requests.post(
                f"{GRAPHQL_URL}?e=LoginMutation",
                json={"query": LOGIN_MUTATION, "variables": {"login": username, "password": password}},
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/json",
                    "Origin": ORIGIN,
                    "Referer": REFERER,
                },
                timeout=getattr(self._settings, "timeout_seconds", 30),
            )
        except requests.RequestException as exc:
            raise ConnectionError(f"Falha ao conectar para login no Unhide School: {exc}")

        if resp.status_code != 200:
            raise ValueError(f"Falha no login (HTTP {resp.status_code}): {resp.text[:300]}")

        try:
            payload = resp.json()
        except ValueError:
            raise ValueError("Resposta de login inválida (não é JSON).")

        if payload.get("errors"):
            raise ValueError(f"Login rejeitado: {payload['errors']}")

        token = (
            payload.get("data", {})
            .get("Login", {})
            .get("authtoken", {})
            .get("token")
        )
        if not token:
            raise ValueError("Token não encontrado na resposta de login. Revise usuário/senha.")
        return token

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._login_for_token)
        token = sanitize_token((token or "").strip())
        # Tolerate users pasting the full header value.
        token = re.sub(r"^Unhide\s+tok\s*=\s*", "", token, flags=re.IGNORECASE).strip()
        if not token:
            raise ValueError("Token de acesso vazio após autenticação.")
        self._configure_session(token)

    def _graphql(self, query: str, variables: Optional[Dict[str, Any]] = None,
                 op_name: Optional[str] = None) -> Dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Sessão não autenticada. Chame authenticate() primeiro.")

        url = f"{GRAPHQL_URL}?e={op_name}" if op_name else GRAPHQL_URL
        resp = self._session.post(
            url,
            json={"query": query, "variables": variables or {}},
            timeout=getattr(self._settings, "timeout_seconds", 30),
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise ValueError(f"Erro GraphQL ({op_name or 'query'}): {payload['errors']}")
        return payload.get("data", {}) or {}

    def fetch_courses(self) -> List[Dict[str, Any]]:
        courses: List[Dict[str, Any]] = []
        seen: set = set()
        page = 1
        perpage = 100
        total = None

        while True:
            data = self._graphql(
                GET_MY_COURSES,
                {"page": page, "perpage": perpage},
                op_name="GetMyCourses",
            )
            block = data.get("MyCourses") or {}
            total = block.get("totalCount", total)
            batch = block.get("courses") or []
            if not batch:
                break

            for c in batch:
                pid = c.get("postid")
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                creator = c.get("creator") or {}
                courses.append({
                    "id": str(pid),
                    "name": c.get("title") or f"Curso {pid}",
                    "seller_name": creator.get("alias") or "Unhide School",
                    "slug": str(pid),
                })

            if total is not None and len(seen) >= total:
                break
            page += 1
            if page > 100:  # hard stop against a misbehaving server
                break

        return courses

    def _fetch_playlist_items(self, postid: int) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        perpage = 200
        while True:
            data = self._graphql(
                PLAYLIST_FRAGMENT,
                {"postid": postid, "page": page, "perpage": perpage},
                op_name="playerPlaylistFragment",
            )
            playlist = (
                (data.get("VideoPlayer") or {}).get("course") or {}
            ).get("playlist") or {}
            batch = playlist.get("items") or []
            items.extend(batch)
            pages = playlist.get("pages") or 1
            if page >= pages or not batch:
                break
            page += 1
        return items

    def _fetch_course_files(self, postid: int) -> List[Dict[str, Any]]:
        try:
            data = self._graphql(
                FILES_FRAGMENT,
                {"postid": postid},
                op_name="playerFilesFragment",
            )
            return (
                (data.get("VideoPlayer") or {}).get("course") or {}
            ).get("files") or []
        except Exception as exc:
            logger.warning("Falha ao buscar materiais do curso %s: %s", postid, exc)
            return []

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = str(course["id"])
            course_title = course.get("name") or f"Curso {course_id}"
            try:
                postid = int(course_id)
            except (TypeError, ValueError):
                logger.error("ID de curso inválido: %r", course_id)
                continue

            items = self._fetch_playlist_items(postid)

            # Preserve first-seen group order; lessons keep their server order.
            modules: "Dict[str, Dict[str, Any]]" = {}
            for item in items:
                group = item.get("groupname") or "Aulas"
                module = modules.get(group)
                if module is None:
                    module = {
                        "id": f"{course_id}::{group}",
                        "title": group,
                        "lessons": [],
                    }
                    modules[group] = module

                postpartid = item.get("postpartid")
                ordered = item.get("orderedpostpartid")
                duration = self._parse_duration(item.get("content"))
                module["lessons"].append({
                    "id": str(postpartid),
                    "title": item.get("title") or f"Aula {postpartid}",
                    "orderedpostpartid": str(ordered) if ordered is not None else None,
                    "content": item.get("content"),
                    "description": item.get("description") or "",
                    "duration": duration,
                    "files": item.get("files") or [],
                    "course_id": course_id,
                })

            processed_modules = list(modules.values())

            # Course-wide materials are not tied to any lesson; expose them in a
            # synthetic trailing module so they remain downloadable.
            course_files = self._fetch_course_files(postid)
            if course_files:
                processed_modules.append({
                    "id": COURSE_FILES_MODULE_ID,
                    "title": "Materiais do Curso",
                    "lessons": [{
                        "id": COURSE_FILES_MODULE_ID,
                        "title": "Materiais do Curso",
                        "course_id": course_id,
                        "course_files": course_files,
                    }],
                })

            all_content[course_id] = {
                "id": course_id,
                "title": course_title,
                "modules": processed_modules,
            }

        return all_content

    @staticmethod
    def _parse_duration(content: Any) -> int:
        """The ``content`` field is a JSON array ``[videoRef, durationSeconds]``."""
        try:
            arr = json.loads(content) if isinstance(content, str) else content
            if isinstance(arr, (list, tuple)) and len(arr) >= 2:
                return int(float(arr[1]))
        except (ValueError, TypeError):
            pass
        return 0

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str,
                             course_id: str, module_id: str) -> LessonContent:
        content = LessonContent()

        # Synthetic course-materials lesson: only attachments, no video.
        course_files = lesson.get("course_files")
        if course_files:
            for idx, f in enumerate(course_files, start=1):
                self._append_attachment(content, lesson, f, idx, prefix="course")
            return content

        description = lesson.get("description") or ""
        if description:
            content.description = Description(text=description, description_type="html")

        # Per-lesson attachments (usually empty; course files live separately).
        for idx, f in enumerate(lesson.get("files") or [], start=1):
            self._append_attachment(content, lesson, f, idx, prefix="lesson")

        ordered = lesson.get("orderedpostpartid")
        if ordered is None:
            return content

        video = self._resolve_video(lesson, ordered)
        if video is not None:
            content.videos.append(video)

        return content

    def _resolve_video(self, lesson: Dict[str, Any], ordered: Any) -> Optional[Video]:
        title = lesson.get("title") or ""
        duration = int(lesson.get("duration") or 0)
        try:
            ordered_int = int(ordered)
        except (TypeError, ValueError):
            ordered_int = ordered

        playback_url = None
        adapter = None
        try:
            data = self._graphql(
                VIDEO_OTP_QUERY,
                {"orderedpostpartid": ordered_int},
            )
            video_node = data.get("Video") or {}
            otp = video_node.get("otp") or {}
            playback_url = otp.get("playbackInfo")
            adapter = (video_node.get("mediaadapter") or {}).get("internalname")
        except Exception as exc:
            logger.warning("Falha ao resolver vídeo da aula %s: %s", lesson.get("id"), exc)

        # Fallback: derive a Vimeo URL from the numeric content reference when
        # the OTP query yields no playable manifest (e.g. free preview lessons).
        if not playback_url:
            playback_url = self._vimeo_url_from_content(lesson.get("content"))

        if not playback_url:
            logger.warning("Sem URL de vídeo para a aula %s (%s)", lesson.get("id"), title)
            return None

        return Video(
            video_id=str(ordered),
            url=playback_url,
            order=1,
            title=title,
            size=0,
            duration=duration,
            extra_props={
                "referer": REFERER,
                "mediaadapter": adapter or "",
            },
        )

    @staticmethod
    def _vimeo_url_from_content(content: Any) -> Optional[str]:
        try:
            arr = json.loads(content) if isinstance(content, str) else content
            ref = str(arr[0]) if isinstance(arr, (list, tuple)) and arr else None
        except (ValueError, TypeError):
            ref = None
        if ref and ref.isdigit():
            return f"https://player.vimeo.com/video/{ref}"
        return None

    @staticmethod
    def _append_attachment(content: LessonContent, lesson: Dict[str, Any],
                           f: Dict[str, Any], idx: int, prefix: str) -> None:
        url = f.get("content")
        name = f.get("name") or f"material_{idx}"
        if not url:
            return
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        try:
            size = int(float(f.get("size") or 0))
        except (ValueError, TypeError):
            size = 0
        content.attachments.append(Attachment(
            attachment_id=f"{lesson.get('id')}_{prefix}_{idx}",
            url=url,
            filename=name,
            order=idx,
            extension=ext,
            size=size,
        ))

    def download_attachment(self, attachment: "Attachment", download_path: Path,
                            course_slug: str, course_id: str, module_id: str) -> bool:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Referer": REFERER,
            "Origin": ORIGIN,
        }
        try:
            with requests.get(attachment.url, stream=True, headers=headers,
                              timeout=getattr(self._settings, "timeout_seconds", 60)) as r:
                r.raise_for_status()
                with open(download_path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            fh.write(chunk)
            return True
        except Exception as exc:
            logger.error("Erro ao baixar anexo %s: %s", attachment.filename, exc)
            return False

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        ordered = lesson.get("orderedpostpartid")
        if ordered is None:
            return
        try:
            ordered_int = int(ordered)
        except (TypeError, ValueError):
            return
        timecode = str(int(lesson.get("duration") or 0)) if watched else "0"
        try:
            self._graphql(
                UPDATE_PROGRESS_MUTATION,
                {"orderedpostpartid": ordered_int, "timecode": timecode, "timespent": 0},
                op_name="updateVideoProgressMutation",
            )
        except Exception as exc:
            logger.error("Falha ao marcar aula %s como assistida: %s", lesson.get("id"), exc)


# PlatformFactory.register_platform("Unhide School", UnhideSchoolPlatform, slug=INTEGRATION_SLUG, version=INTEGRATION_VERSION, experimental=INTEGRATION_EXPERIMENTAL)
PlatformFactory.register_platform("Unhide School", UnhideSchoolPlatform)
