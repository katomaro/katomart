from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import (
    AuthField,
    AuthFieldType,
    BasePlatform,
    PlatformFactory,
    sanitize_token,
)

logger = logging.getLogger(__name__)

# Plataforma Assaad — frontend em https://app.plataformaassaad.com.br
# Backend "KickOps" (multi-tenant). A API REST fica num subdominio por tenant.
API_BASE = "https://assaad-api.kickops.dev"
APP_ORIGIN = "https://app.plataformaassaad.com.br"

# Tipos de grupo conhecidos (cada um e uma "aba" no app):
#   path          -> trilhas de estudo (Nivelamento, Basico I/II, ...)
#   course        -> materias (Matematica, Ciencias da Natureza, ...)
#   mentorship    -> mentorias (Treine sua mente, ...)
#   college_exams -> vestibulares (com grupos aninhados por assunto)
# O mesmo curso pode aparecer em mais de um tipo; consultamos todos e
# deduplicamos por id. Os grupos college_exams possuem subgrupos: pedimos
# child_groups=true e percorremos a arvore recursivamente.
GROUP_TYPES = ("path", "course", "mentorship", "college_exams")


class AssaadPlatform(BasePlatform):
    """Implementa a plataforma Assaad (app.plataformaassaad.com.br).

    Backend KickOps com autenticacao Supabase via OTP por e-mail. O token
    retornado e um JWT Supabase usado como ``Authorization: Bearer`` na API.

    Hierarquia de conteudo:
        course-group -> course -> module -> chapter -> lesson

    Para a GUI (que so tem 2 niveis: modulo -> aula) achatamos
    ``module``/``chapter`` num unico "modulo", concatenando os titulos.

    Videos sao hospedados no PandaVideo (a URL ``video_player`` ja vem
    embutida na arvore ``/v1/course/{id}/full``). Materiais (PDFs etc.) sao
    resolvidos sob demanda para URLs assinadas do Supabase Storage.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)

    # ------------------------------------------------------------------ #
    # Declaracao de campos de autenticacao
    # ------------------------------------------------------------------ #
    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="email",
                label="E-mail (login por codigo)",
                placeholder="Seu e-mail de acesso a plataforma",
                required=False,
                requires_membership=True,
            ),
            AuthField(
                name="otp_code",
                label="Codigo recebido por e-mail",
                placeholder="Deixe vazio na 1a vez para receber o codigo",
                required=False,
                requires_membership=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return (
            "Autenticacao via Token (qualquer usuario):\n"
            "1) Acesse https://app.plataformaassaad.com.br e faca login.\n"
            "2) Abra o DevTools (F12) > aba Rede (Network).\n"
            "3) Clique em qualquer requisicao para 'assaad-api.kickops.dev'.\n"
            "4) Em Cabecalhos, copie o valor de 'Authorization' (sem o 'Bearer ').\n"
            "5) Cole no campo Token. (O token expira em ~2h.)\n\n"
            "Login automatico por codigo (assinantes):\n"
            "1) Informe seu e-mail e autentique deixando o campo de codigo vazio.\n"
            "2) Um codigo sera enviado ao seu e-mail.\n"
            "3) Preencha o codigo recebido e autentique novamente."
        ).strip()

    # ------------------------------------------------------------------ #
    # Autenticacao
    # ------------------------------------------------------------------ #
    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        token = sanitize_token((credentials.get("token") or "").strip())
        email = (credentials.get("email") or "").strip()
        code = (credentials.get("otp_code") or "").strip()

        # Token tem prioridade quando informado diretamente.
        if token:
            self._configure_session(token)
            return

        if self._settings.has_full_permissions and email:
            if not code:
                self._request_otp(email)
                raise ValueError(
                    "Codigo enviado para o seu e-mail. Preencha o campo "
                    "'Codigo recebido por e-mail' e autentique novamente."
                )
            token = self._verify_otp(email, code)
            self._configure_session(token)
            return

        raise ValueError(
            "Informe um token de acesso ou, sendo assinante, seu e-mail para "
            "login por codigo."
        )

    def _build_anon_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3",
            "Content-Type": "application/json",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
        })
        return session

    def _request_otp(self, email: str) -> None:
        session = self._build_anon_session()
        try:
            resp = session.post(
                f"{API_BASE}/v1/auth/email-otp/request",
                json={"email": email},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ConnectionError(
                f"Falha ao solicitar o codigo de acesso: {exc}"
            ) from exc

    def _verify_otp(self, email: str, code: str) -> str:
        session = self._build_anon_session()
        try:
            resp = session.post(
                f"{API_BASE}/v1/auth/email-otp/verify",
                json={"email": email, "token": code},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ConnectionError(f"Falha ao verificar o codigo: {exc}") from exc

        if resp.status_code >= 400:
            raise ConnectionError(
                "Codigo invalido ou expirado. Solicite um novo codigo."
            )

        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            raise ConnectionError("Resposta de login sem 'access_token'.")
        return access_token

    def _configure_session(self, token: str) -> None:
        token = sanitize_token(token).strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3",
            "Authorization": f"Bearer {token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
        })

        # Valida o token com um endpoint leve.
        try:
            resp = self._session.get(f"{API_BASE}/v1/profile/me", timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ConnectionError(
                "Token invalido ou expirado. Refaca o login na plataforma."
            ) from exc

        logger.info("Assaad: autenticado com sucesso")

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    # ------------------------------------------------------------------ #
    # Listagem de cursos
    # ------------------------------------------------------------------ #
    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        courses: Dict[str, Dict[str, Any]] = {}

        for group_type in GROUP_TYPES:
            try:
                groups = self._fetch_groups(group_type)
            except Exception as exc:
                logger.warning("Assaad: falha ao listar grupos '%s': %s", group_type, exc)
                continue

            for group in groups:
                self._collect_group_courses(group, courses)

        logger.debug("Assaad: %d cursos encontrados", len(courses))
        return sorted(courses.values(), key=lambda c: c.get("name", ""))

    def _collect_group_courses(
        self, group: Dict[str, Any], courses: Dict[str, Dict[str, Any]]
    ) -> None:
        """Coleta cursos de um grupo e, recursivamente, dos subgrupos."""
        group_name = (group.get("name") or "").strip()
        for course in group.get("courses") or []:
            cid = course.get("id")
            if cid is None:
                continue
            key = str(cid)
            if key in courses:
                continue
            if course.get("hasAccess") is False:
                continue
            courses[key] = {
                "id": key,
                "name": (course.get("title") or f"Curso {key}").strip(),
                "slug": course.get("slug") or key,
                "seller_name": group_name,
                "extra": {"thumbnail": course.get("thumbnail")},
            }

        for child in group.get("children") or []:
            self._collect_group_courses(child, courses)

    def _fetch_groups(self, group_type: str) -> List[Dict[str, Any]]:
        params = {
            "page": 1,
            "perPage": 200,
            "courses": "true",
            "type": group_type,
            "full": "false",
            "instructors": "false",
            # Embute os subgrupos (necessario para college_exams/vestibulares).
            "child_groups": "true",
        }
        resp = self._session.get(
            f"{API_BASE}/v1/course-group", params=params, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return data.get("data") or []
        if isinstance(data, list):
            return data
        return []

    # ------------------------------------------------------------------ #
    # Conteudo dos cursos (modulos/aulas)
    # ------------------------------------------------------------------ #
    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if course_id is None:
                continue

            try:
                resp = self._session.get(
                    f"{API_BASE}/v1/course/{course_id}/full", timeout=30
                )
                resp.raise_for_status()
                tree = resp.json()
            except Exception as exc:
                logger.error("Assaad: falha ao buscar curso %s: %s", course_id, exc)
                continue

            # Materiais ancorados em modulo/capitulo (incl. modulos sem aulas).
            # Sempre consultamos: o endpoint /course/has/material so reflete
            # materiais de nivel aula/curso e retorna false quando os materiais
            # estao ancorados no modulo/capitulo — usa-lo como gate perderia
            # justamente esses (cursos de "Materiais", "Listas", "Mapas", etc.).
            material_index = self._fetch_container_materials(tree)

            modules = self._flatten_modules(tree, material_index)

            course_entry = course.copy()
            course_entry["title"] = (tree.get("title") or course.get("name") or "Curso").strip()
            course_entry["modules"] = modules
            all_content[str(course_id)] = course_entry

        return all_content

    def _fetch_container_materials(
        self, tree: Dict[str, Any]
    ) -> Dict[tuple, List[Dict[str, Any]]]:
        """Descobre materiais ancorados em modulos/capitulos (1 chamada por modulo).

        Retorna ``{(entity_type, entity_id): [material_dict, ...]}`` apenas para
        ``entity_type`` em ``module``/``chapter``. Materiais de aula NAO entram
        aqui — sao resolvidos sob demanda em ``fetch_lesson_details``.
        """
        index: Dict[tuple, List[Dict[str, Any]]] = {}

        module_nodes = [
            c for c in (tree.get("children") or []) if c.get("type") == "module"
        ]
        if not module_nodes:
            module_nodes = tree.get("children") or []

        for module in module_nodes:
            mid = module.get("id")
            if mid is None:
                continue
            params = {
                "root_type": "module",
                "root_id": str(mid),
                "full_search": "true",
            }
            try:
                resp = self._session.get(
                    f"{API_BASE}/v1/material/subtree", params=params, timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning(
                    "Assaad: falha ao listar materiais do modulo %s: %s", mid, exc
                )
                continue

            for material in data.get("materials") or []:
                src = material.get("source") or {}
                entity_type = src.get("entity_type")
                if entity_type not in ("module", "chapter"):
                    continue
                entity_id = str(src.get("entity_id"))
                index.setdefault((entity_type, entity_id), []).append(
                    self._material_to_dict(material)
                )

        return index

    @staticmethod
    def _material_to_dict(material: Dict[str, Any]) -> Dict[str, Any]:
        material_id = material.get("id")  # ex.: "uuid.pdf" ou "uuid" (sem ext)
        extension = (material.get("file_extension") or "").lower()
        if not extension and material_id and "." in material_id:
            extension = material_id.rsplit(".", 1)[-1].lower()

        name = (material.get("name") or material_id or "material").strip()
        filename = name
        if extension and not filename.lower().endswith(f".{extension}"):
            filename = f"{filename}.{extension}"

        return {
            "id": material_id,
            "filename": filename,
            "extension": extension,
            "size": int(material.get("size") or 0),
        }

    def _flatten_modules(
        self,
        course_node: Dict[str, Any],
        material_index: Optional[Dict[tuple, List[Dict[str, Any]]]] = None,
    ) -> List[Dict[str, Any]]:
        """Achata module->chapter->lesson em modulos planos (titulo concatenado).

        Materiais ancorados num modulo/capitulo viram uma aula sintetica
        "Materiais", garantindo que modulos/capitulos sem aulas (cursos so de
        material) tambem aparecam.
        """
        material_index = material_index or {}
        modules: List[Dict[str, Any]] = []

        def visit(node: Dict[str, Any], title_path: List[str]) -> None:
            children = node.get("children") or []
            lessons_raw = [c for c in children if c.get("type") == "lesson"]
            containers = [c for c in children if c.get("type") != "lesson"]

            gui_lessons = [
                self._build_lesson_dict(les, idx)
                for idx, les in enumerate(lessons_raw, start=1)
            ]

            node_materials = material_index.get(
                (node.get("type"), str(node.get("id"))), []
            )
            if node_materials:
                gui_lessons.append(
                    self._build_material_lesson(node, node_materials, len(gui_lessons) + 1)
                )

            if gui_lessons:
                modules.append({
                    "id": node.get("id"),
                    "title": " / ".join(t for t in title_path if t) or (node.get("title") or "Modulo"),
                    "order": len(modules) + 1,
                    "locked": node.get("hasAccess") is False,
                    "lessons": gui_lessons,
                })

            for child in containers:
                visit(child, title_path + [(child.get("title") or "").strip()])

        # Comeca no proprio curso (path vazio) para nao perder eventuais aulas
        # penduradas diretamente no curso; modulos descem com seu titulo.
        visit(course_node, [])

        return modules

    @staticmethod
    def _build_lesson_dict(lesson: Dict[str, Any], order: int) -> Dict[str, Any]:
        return {
            "id": str(lesson.get("id")),
            "title": (lesson.get("title") or f"Aula {order}").strip(),
            "order": lesson.get("order") or order,
            "locked": lesson.get("hasAccess") is False,
            "extra": {
                "video_player": lesson.get("video_player"),
                "video_id": lesson.get("video_id"),
                "video_thumbnail": lesson.get("video_thumbnail"),
                "duration": lesson.get("video_length") or 0,
                "materials_count": lesson.get("materials_count") or 0,
                "description": lesson.get("description") or "",
            },
        }

    @staticmethod
    def _build_material_lesson(
        node: Dict[str, Any], materials: List[Dict[str, Any]], order: int
    ) -> Dict[str, Any]:
        """Aula sintetica que carrega apenas os materiais de um modulo/capitulo."""
        return {
            "id": f"materials-{node.get('type')}-{node.get('id')}",
            "title": "Materiais",
            "order": order,
            "locked": False,
            "extra": {
                "materials_only": True,
                "materials": materials,
            },
        }

    # ------------------------------------------------------------------ #
    # Detalhes da aula (video + anexos)
    # ------------------------------------------------------------------ #
    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        lesson_id = lesson.get("id")
        extra = lesson.get("extra", {})
        order = lesson.get("order", 1)
        title = lesson.get("title", "Aula")

        content = LessonContent()

        desc_text = extra.get("description") or ""
        if desc_text:
            content.description = Description(text=desc_text, description_type="text")

        # Aulas sinteticas (materiais de modulo/capitulo): materiais ja resolvidos.
        preresolved = extra.get("materials")
        if preresolved:
            self._append_material_dicts(content, preresolved)

        if extra.get("materials_only"):
            return content

        video_player = extra.get("video_player")
        video_id = extra.get("video_id")

        # Se a arvore nao trouxe o player, busca o detalhe da aula.
        if not video_player:
            video_player, video_id = self._fetch_lesson_video(lesson_id)

        if video_player:
            content.videos.append(
                Video(
                    video_id=str(video_id or extra.get("video_id") or lesson_id),
                    url=video_player,
                    order=order,
                    title=title,
                    size=0,
                    duration=int(extra.get("duration") or 0),
                    extra_props={"referer": f"{APP_ORIGIN}/"},
                )
            )

        # Materiais de aula: fluxo provado (root_type=lesson, entity_only).
        if extra.get("materials_count") and not preresolved:
            self._fetch_lesson_materials(lesson_id, content)

        return content

    def _fetch_lesson_video(self, lesson_id: Any) -> tuple[Optional[str], Optional[str]]:
        try:
            resp = self._session.get(f"{API_BASE}/v1/lesson/{lesson_id}", timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Assaad: falha ao buscar video da aula %s: %s", lesson_id, exc)
            return None, None

        video = (data.get("content") or {}).get("video") or {}
        return video.get("video_player"), video.get("id")

    def _fetch_lesson_materials(self, lesson_id: Any, content: LessonContent) -> None:
        params = {
            "root_type": "lesson",
            "root_id": str(lesson_id),
            "entity_only": "true",
        }
        try:
            resp = self._session.get(
                f"{API_BASE}/v1/material/subtree", params=params, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Assaad: falha ao listar materiais da aula %s: %s", lesson_id, exc)
            return

        materials = [
            self._material_to_dict(m)
            for m in data.get("materials") or []
            if m.get("id")
        ]
        self._append_material_dicts(content, materials)

    @staticmethod
    def _unique_filename(filename: str, used: set) -> str:
        """Garante nome unico inserindo ' (n)' antes da extensao.

        A API da Assaad entrega varios materiais distintos com o MESMO ``name``
        (ex.: cursos "Mapas Mentais e Conceituais", onde o mapa mental e o mapa
        conceitual de um capitulo se chamam ambos "Sistema Nervoso"). Sem
        desambiguar, os arquivos colidem no disco e o usuario acha que um
        material "sumiu".
        """
        key = filename.lower()
        if key not in used:
            used.add(key)
            return filename

        if "." in filename:
            stem, ext = filename.rsplit(".", 1)
            suffix = f".{ext}"
        else:
            stem, suffix = filename, ""

        counter = 2
        while True:
            candidate = f"{stem} ({counter}){suffix}"
            if candidate.lower() not in used:
                used.add(candidate.lower())
                return candidate
            counter += 1

    @classmethod
    def _append_material_dicts(
        cls, content: LessonContent, materials: List[Dict[str, Any]]
    ) -> None:
        start = len(content.attachments)
        used = {a.filename.lower() for a in content.attachments}
        for offset, material in enumerate(materials, start=1):
            material_id = material.get("id")
            if not material_id:
                continue
            filename = cls._unique_filename(
                material.get("filename") or str(material_id), used
            )
            content.attachments.append(
                Attachment(
                    attachment_id=str(material_id),
                    url=f"{API_BASE}/v1/material/content/{material_id}",
                    filename=filename,
                    order=start + offset,
                    extension=material.get("extension") or "",
                    size=int(material.get("size") or 0),
                )
            )

    # ------------------------------------------------------------------ #
    # Download de anexos
    # ------------------------------------------------------------------ #
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

        try:
            # 1) Resolve a URL assinada do Supabase Storage (expira em ~60s).
            resp = self._session.get(attachment.url, timeout=30)
            resp.raise_for_status()
            signed_url = resp.json().get("url")
            if not signed_url:
                logger.error(
                    "Assaad: material '%s' sem URL de download.", attachment.filename
                )
                return False

            # 2) Baixa o arquivo assinado (sem auth — token na query string).
            with requests.get(
                signed_url,
                stream=True,
                timeout=120,
                headers={"User-Agent": self._settings.user_agent},
            ) as file_resp:
                file_resp.raise_for_status()
                with open(download_path, "wb") as f:
                    for chunk in file_resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return True

        except Exception as exc:
            logger.error(
                "Assaad: falha ao baixar anexo '%s': %s", attachment.filename, exc
            )
            return False


PlatformFactory.register_platform("Assaad", AssaadPlatform)
