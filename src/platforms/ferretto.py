from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import (
    AuthField,
    AuthFieldType,
    BasePlatform,
    PlatformFactory,
    sanitize_token,
)

logger = logging.getLogger(__name__)

# Professor Ferretto / ferrettocloud.com.br
APP_ORIGIN = "https://app.professorferretto.com.br"
API_URL = "https://api.ferrettocloud.com.br/graphql"
PDF_QUESTIONS_URL = "https://pdf.ferrettocloud.com.br/pdf/questions"
SPALLA_PLAYER_URL = "https://beyond.spalla.io/player/?video={uuid}"

# All class groups regardless of the user's plan (complete course vs. simplified).
CLASS_GROUP_TYPES = ["DISCIPLINE", "SIMPLIFIED"]

LOGIN_MUTATION = """mutation LoginMutation($input: LoginInput!) {
  login(input: $input) {
    token
    __typename
  }
}"""

GET_DISCIPLINES = """query GetDisciplines($filter: FilterDisciplinesInput) {
  disciplines(filter: $filter) {
    nodes {
      id
      name
      slug
      isPublished
      parentDisciplineId
      parentDisciplineName
      parentDisciplineSlug
      order
      __typename
    }
    __typename
  }
}"""

GET_CLASSES_GROUPS = """query GetClassesGroups($filter: FilterClassGroupsInput!, $pagination: PaginationInput) {
  classGroups(filter: $filter, pagination: $pagination) {
    pagination {
      total
      lastPage
      perPage
      page
      __typename
    }
    nodes {
      id
      title
      slug
      image
      amountOfResources
      allowedToFree
      discipline {
        id
        name
        parentDisciplineId
        __typename
      }
      totalVideoTime
      totalWatched
      state {
        name
        __typename
      }
      __typename
    }
    __typename
  }
}"""

GET_CLASS_GROUP = """query GetClassGroup($disciplineSlug: String!, $id: ID, $slug: String) {
  classGroup(input: {disciplineSlug: $disciplineSlug, id: $id, slug: $slug}) {
    meta {
      title
      slug
      discipline {
        id
        name
        slug
        __typename
      }
      __typename
    }
    resources {
      type
      item {
        ... on Class {
          id
          title
          watched
          slug
          enemRelevance
          attachments {
            type
            file
            __typename
          }
          mainVideo {
            id
            timeInSeconds
            thumbnail
            __typename
          }
          exercisesVideo {
            id
            title
            thumbnail
            timeInSeconds
            __typename
          }
          __typename
        }
        ... on Subject {
          id
          name
          slug
          watched
          discipline {
            name
            slug
            __typename
          }
          parentSubject {
            name
            slug
            __typename
          }
          __typename
        }
        ... on Pdfs {
          id
          title
          file
          pdfType
          discipline {
            name
            __typename
          }
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}"""


class FerretoPlatform(BasePlatform):
    """Implements the Professor Ferretto (ferrettocloud.com.br) platform.

    The backend is a GraphQL API (api.ferrettocloud.com.br). Content is
    organised as Discipline -> Class Group -> Resources, where each resource is
    one of:

    - a *Class* with a main video, an optional exercise-resolution video (both
      hosted on Spalla) and theory/exercise PDFs;
    - a *Subject* exposing an exercise list, whose PDF is generated on demand by
      pdf.ferrettocloud.com.br/pdf/questions;
    - a *Pdfs* resource (script / graphic summary) pointing straight at S3.

    Videos are delegated to the shared SpallaDownloader via the player URL.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)

    @classmethod
    def token_field(cls) -> AuthField:
        return AuthField(
            name="token",
            label="Token de Acesso (opcional)",
            field_type=AuthFieldType.PASSWORD,
            placeholder="Cole o token Bearer obtido na plataforma",
            required=False,
        )

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 9.90) ativos podem informar e-mail e senha — o login é feito
automaticamente via API.

Para obter o token manualmente (usuários gratuitos):
1) Acesse https://app.professorferretto.com.br e faça login.
2) Abra as Ferramentas de Desenvolvedor (F12) → aba Rede (Network).
3) Recarregue a página e localize uma requisição para
   api.ferrettocloud.com.br/graphql.
4) No cabeçalho da requisição, copie o valor de 'authorization' (sem o prefixo
   'Bearer ') e cole no campo acima.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self._settings.user_agent,
                "Accept": "*/*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": APP_ORIGIN,
                "Referer": f"{APP_ORIGIN}/",
            }
        )
        self._session = session

        token = self.resolve_access_token(credentials, self._login_with_credentials)
        token = sanitize_token((token or "").strip())
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not token:
            raise ValueError("Não foi possível obter o token de acesso da Ferretto.")

        session.headers["Authorization"] = f"Bearer {token}"
        self._validate_session()
        logger.info("Sessão autenticada na Ferretto.")

    def _login_with_credentials(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        """Credential provider: exchanges e-mail/senha for a Bearer token."""
        if not username or not password:
            raise ValueError("Informe e-mail e senha para autenticar na Ferretto.")

        data = self._graphql(
            API_URL,
            "LoginMutation",
            LOGIN_MUTATION,
            {
                "input": {
                    "email": username,
                    "password": password,
                    "origin": "WEB",
                    "params": {"urlParams": "", "formType": "login"},
                }
            },
            authenticated=False,
        )
        token = (((data or {}).get("login") or {}).get("token") or "").strip()
        if not token:
            raise ConnectionError("Falha no login da Ferretto. Verifique e-mail e senha.")
        return token

    def _validate_session(self) -> None:
        # `me` is the lightest authenticated query; a failure here means the
        # token is invalid or expired.
        try:
            self._graphql(API_URL, "GetMe", "query GetMe {\n  me {\n    id\n    __typename\n  }\n}", {})
        except Exception as exc:
            raise ConnectionError(
                "Token da Ferretto inválido ou expirado. Faça login novamente."
            ) from exc

    def _graphql(
        self,
        endpoint: str,
        operation_name: str,
        query: str,
        variables: Dict[str, Any],
        authenticated: bool = True,
    ) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        payload = {
            "operationName": operation_name,
            "variables": variables,
            "query": query,
        }
        response = self._session.post(endpoint, json=payload)
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            message = "; ".join(
                err.get("message", "erro desconhecido") for err in body["errors"]
            )
            raise ConnectionError(f"Erro GraphQL ({operation_name}): {message}")
        return body.get("data") or {}

    def fetch_courses(self) -> List[Dict[str, Any]]:
        data = self._graphql(API_URL, "GetDisciplines", GET_DISCIPLINES, {})
        nodes = ((data.get("disciplines") or {}).get("nodes")) or []

        courses: List[Dict[str, Any]] = []
        for node in nodes:
            if node.get("isPublished") is False:
                continue
            slug = node.get("slug")
            if not slug:
                continue
            name = node.get("name") or slug
            parent = node.get("parentDisciplineName")
            if parent and parent not in name:
                name = f"{parent} - {name}"
            courses.append(
                {
                    "id": node.get("id") or slug,
                    "name": name,
                    "slug": slug,
                    "seller_name": "Professor Ferretto",
                    "order": node.get("order", 0),
                }
            )

        courses.sort(key=lambda c: (c.get("order", 0), c.get("name", "")))
        logger.info("Ferretto: %d disciplina(s) encontrada(s).", len(courses))
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_content: Dict[str, Any] = {}

        for course in courses:
            discipline_slug = course.get("slug")
            if not discipline_slug:
                continue

            modules = self._fetch_discipline_modules(discipline_slug)
            course_entry = dict(course)
            course_entry["title"] = course.get("name", "Curso")
            course_entry["modules"] = modules
            all_content[str(course.get("id"))] = course_entry

        return all_content

    def _fetch_discipline_modules(self, discipline_slug: str) -> List[Dict[str, Any]]:
        groups = self._fetch_class_groups(discipline_slug)
        modules: List[Dict[str, Any]] = []

        for group in groups:
            group_id = group.get("id")
            group_slug = group.get("slug")
            if not group_id or not group_slug:
                continue

            lessons = self._fetch_group_lessons(discipline_slug, group_id, group_slug)
            if not lessons:
                continue

            modules.append(
                {
                    "id": group_id,
                    "title": group.get("title") or "Módulo",
                    "order": len(modules) + 1,
                    "locked": False,
                    "lessons": lessons,
                }
            )

        return modules

    def _fetch_class_groups(self, discipline_slug: str) -> List[Dict[str, Any]]:
        groups: List[Dict[str, Any]] = []
        page = 1
        while True:
            try:
                data = self._graphql(
                    API_URL,
                    "GetClassesGroups",
                    GET_CLASSES_GROUPS,
                    {
                        "filter": {
                            "disciplineSlug": discipline_slug,
                            "withTotalWatched": True,
                            "type": CLASS_GROUP_TYPES,
                        },
                        "pagination": {"page": page, "perPage": 100},
                    },
                )
            except Exception as exc:
                logger.warning(
                    "Ferretto: falha ao listar grupos da disciplina %s: %s",
                    discipline_slug,
                    exc,
                )
                break

            block = data.get("classGroups") or {}
            groups.extend(block.get("nodes") or [])

            pagination = block.get("pagination") or {}
            last_page = pagination.get("lastPage") or 1
            if page >= last_page:
                break
            page += 1

        return groups

    def _fetch_group_lessons(
        self, discipline_slug: str, group_id: str, group_slug: str
    ) -> List[Dict[str, Any]]:
        try:
            data = self._graphql(
                API_URL,
                "GetClassGroup",
                GET_CLASS_GROUP,
                {"disciplineSlug": discipline_slug, "id": group_id, "slug": group_slug},
            )
        except Exception as exc:
            logger.warning("Ferretto: falha ao abrir grupo %s: %s", group_slug, exc)
            return []

        class_group = data.get("classGroup") or {}
        resources = class_group.get("resources") or []

        lessons: List[Dict[str, Any]] = []
        for resource in resources:
            lesson = self._resource_to_lesson(resource, len(lessons) + 1)
            if lesson:
                lessons.append(lesson)
        return lessons

    @staticmethod
    def _resource_to_lesson(resource: Dict[str, Any], order: int) -> Optional[Dict[str, Any]]:
        item = resource.get("item") or {}
        typename = item.get("__typename")

        if typename == "Class":
            return {
                "id": item.get("id"),
                "title": item.get("title") or f"Aula {order}",
                "order": order,
                "locked": False,
                "_kind": "class",
                "_main_video": item.get("mainVideo"),
                "_exercises_video": item.get("exercisesVideo"),
                "_attachments": item.get("attachments") or [],
                "slug": item.get("slug"),
            }

        if typename == "Subject":
            discipline = item.get("discipline") or {}
            parent_subject = item.get("parentSubject") or {}
            return {
                "id": item.get("id"),
                "title": f"{item.get('name') or f'Lista {order}'} (Lista de Exercícios)",
                "order": order,
                "locked": False,
                "_kind": "questions",
                "_subject_name": item.get("name"),
                "_discipline_slug": discipline.get("slug"),
                "_parent_subject_slug": parent_subject.get("slug"),
                "_subject_slug": item.get("slug"),
            }

        if typename == "Pdfs":
            return {
                "id": item.get("id"),
                "title": item.get("title") or f"Material {order}",
                "order": order,
                "locked": False,
                "_kind": "pdf",
                "_file": item.get("file"),
                "_pdf_type": item.get("pdfType"),
            }

        # SimulatedMetaDataStudyPlan and any future types are interactive and
        # not downloadable as files; skip them.
        logger.debug("Ferretto: recurso ignorado (tipo %s).", typename)
        return None

    # ---------------------------------------------------------- lesson detail

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        kind = lesson.get("_kind")
        if kind == "class":
            return self._build_class_lesson(lesson)
        if kind == "questions":
            return self._build_questions_lesson(lesson)
        if kind == "pdf":
            return self._build_pdf_lesson(lesson)
        return LessonContent()

    def _build_class_lesson(self, lesson: Dict[str, Any]) -> LessonContent:
        content = LessonContent()
        title = lesson.get("title", "Aula")

        main_video = lesson.get("_main_video") or {}
        if main_video.get("id"):
            content.videos.append(self._spalla_video(main_video["id"], title, main_video, order=1))

        exercises_video = lesson.get("_exercises_video") or {}
        if exercises_video.get("id"):
            ex_title = exercises_video.get("title") or f"{title} - Resolução"
            content.videos.append(
                self._spalla_video(exercises_video["id"], ex_title, exercises_video, order=2)
            )

        order = 1
        for att in lesson.get("_attachments") or []:
            file_url = att.get("file")
            if not file_url:
                continue
            attachment = self._build_s3_attachment(
                file_url,
                order,
                label=self._attachment_label(att.get("type"), title),
            )
            content.attachments.append(attachment)
            order += 1

        return content

    def _build_questions_lesson(self, lesson: Dict[str, Any]) -> LessonContent:
        content = LessonContent()
        url = self._resolve_questions_pdf(lesson)
        if not url:
            logger.warning(
                "Ferretto: não foi possível gerar a lista de exercícios '%s'.",
                lesson.get("title"),
            )
            return content

        subject_name = lesson.get("_subject_name") or "lista"
        filename = f"{self._sanitize_filename(subject_name)} - Lista de Exercicios.pdf"
        content.attachments.append(
            Attachment(
                attachment_id=str(lesson.get("id") or filename),
                url=url,
                filename=filename,
                order=1,
                extension="pdf",
                size=0,
            )
        )
        return content

    def _build_pdf_lesson(self, lesson: Dict[str, Any]) -> LessonContent:
        content = LessonContent()
        file_url = lesson.get("_file")
        if not file_url:
            return content
        content.attachments.append(
            self._build_s3_attachment(file_url, 1, label=lesson.get("title", "Material"))
        )
        return content

    def _resolve_questions_pdf(self, lesson: Dict[str, Any]) -> Optional[str]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        discipline = lesson.get("_discipline_slug")
        subject = lesson.get("_subject_slug")
        parent_subject = lesson.get("_parent_subject_slug")
        if not discipline or not subject:
            return None

        if parent_subject:
            body = {"discipline": discipline, "subject1": parent_subject, "subject2": subject}
        else:
            body = {"discipline": discipline, "subject1": subject}
        body["showOnClassesMenuAs"] = ["SIMPLIFIED_COURSE"]

        try:
            response = self._session.post(
                PDF_QUESTIONS_URL,
                json=body,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return (response.json() or {}).get("url")
        except Exception as exc:
            logger.warning("Ferretto: falha ao gerar PDF de questões: %s", exc)
            return None

    def _spalla_video(
        self, raw_id: str, title: str, meta: Dict[str, Any], order: int
    ) -> Video:
        uuid = self._normalize_spalla_uuid(raw_id)
        return Video(
            video_id=uuid,
            url=SPALLA_PLAYER_URL.format(uuid=uuid),
            order=order,
            title=title,
            size=0,
            duration=int(meta.get("timeInSeconds") or 0),
            extra_props={
                "spalla_uuid": uuid,
                "origin_referer": f"{APP_ORIGIN}/",
                "referer": f"{APP_ORIGIN}/",
            },
        )

    @staticmethod
    def _normalize_spalla_uuid(raw_id: str) -> str:
        """Spalla video ids come either as a canonical UUID (simplified course)
        or as a dash-less 32-char hex string (complete course). The player and
        the SpallaDownloader expect the dashed UUID form."""
        value = (raw_id or "").strip()
        if re.fullmatch(r"[0-9a-fA-F]{32}", value):
            return f"{value[0:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:32]}"
        return value

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")
        if not attachment.url:
            logger.error("Ferretto: anexo sem URL: %s", attachment.filename)
            return False

        try:
            # S3 (public bucket and presigned URLs) reject the platform's
            # Authorization header, so use a clean session for the transfer.
            response = requests.get(
                attachment.url,
                headers={"User-Agent": self._settings.user_agent},
                stream=True,
                allow_redirects=True,
                timeout=60,
            )
            response.raise_for_status()

            if not download_path.suffix:
                parsed_suffix = Path(urlparse(response.url).path).suffix
                if parsed_suffix:
                    download_path = download_path.with_suffix(parsed_suffix)

            download_path.parent.mkdir(parents=True, exist_ok=True)
            with open(download_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)
            return True
        except Exception as exc:
            logger.error("Ferretto: falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False

    def _build_s3_attachment(self, file_url: str, order: int, label: str) -> Attachment:
        filename = self._filename_from_url(file_url)
        # When the S3 object is just a UUID (no human-readable name), fall back
        # to the lesson-derived label so the file is identifiable on disk.
        if not filename or self._looks_like_bare_uuid(filename):
            extension = filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else "pdf"
            filename = f"{self._sanitize_filename(label)}.{extension}"
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return Attachment(
            attachment_id=f"{order}-{filename}",
            url=file_url,
            filename=filename,
            order=order,
            extension=extension,
            size=0,
        )

    @staticmethod
    def _attachment_label(att_type: Optional[str], lesson_title: str) -> str:
        mapping = {
            "THEORY": "Teoria",
            "EXERCISES": "Lista de Exercicios",
            "LIST": "Lista de Exercicios",
            "LIST_RESOLUTION": "Resolucao da Lista",
        }
        suffix = mapping.get((att_type or "").upper())
        return f"{lesson_title} - {suffix}" if suffix else lesson_title

    @staticmethod
    def _filename_from_url(url: str) -> str:
        path = urlparse(url).path
        basename = path.rsplit("/", 1)[-1]
        # S3 object keys are prefixed with a UUID (e.g. "<uuid>-real-name.pdf");
        # strip it to keep the human-readable name.
        match = re.match(
            r"^[0-9a-fA-F-]{36}-(.+)$",
            basename,
        )
        if match:
            basename = match.group(1)
        return basename

    @staticmethod
    def _looks_like_bare_uuid(filename: str) -> bool:
        stem = filename.rsplit(".", 1)[0]
        return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", stem))

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        cleaned = re.sub(r"[\\/:*?\"<>|]+", "-", name).strip()
        return cleaned or "arquivo"


PlatformFactory.register_platform("Professor Ferretto", FerretoPlatform)
