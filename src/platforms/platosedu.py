from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.app.api_service import ApiService
from src.app.models import (
    Attachment,
    AuxiliaryURL,
    Description,
    LessonContent,
    Video,
)
from src.config.settings_manager import SettingsManager
from src.platforms.base import (
    AuthField,
    AuthFieldType,
    BasePlatform,
    PlatformFactory,
)

INTEGRATION_SLUG = "platosedu"
INTEGRATION_VERSION = "1.0.0"
# Marcada como experimental: o fluxo de vídeo (mdstrm/HLS) está validado a partir
# do HAR, mas a URL real de download dos anexos (PDFs de "Arquivos do curso")
# ainda não foi capturada. Ver MATERIAL_BASE_URL abaixo.
INTEGRATION_EXPERIMENTAL = True

logger = logging.getLogger(__name__)

# Plato Edu / Cogna LMS (infoprod.platosedu.io). Usado por marcas de pós-graduação
# EAD (ex.: "Instituto Futurum" / Anhanguera). O frontend é um Next.js servido em
# /v2/lms/, mas todo o conteúdo vem de uma API JSON limpa em /lms/*, autenticada
# por um único cookie de sessão `SESSION`.
#
# Hierarquia:
#   Pessoa -> Matrícula (programa/curso de pós) -> Disciplina (boletim)
#          -> Categoria (módulo) -> Conteúdo (aula)
#
# Cada disciplina (boletim) é mapeada como UM "curso" do app, porque o modelo do
# app só tem dois níveis abaixo do curso (módulo/aula) e a disciplina é a unidade
# natural que carrega seus próprios módulos/aulas via listCategories.
#
# Vídeos são embeds Media Stream (mdstrm.com), HLS sem criptografia. Basta extrair
# o media id do iframe e montar https://mdstrm.com/video/{id}.m3u8 — o
# DownloaderFactory já roteia .m3u8 para o YtdlpDownloader.
BASE_URL = "https://infoprod.platosedu.io"
API = f"{BASE_URL}/lms"
COOKIE_DOMAIN = "infoprod.platosedu.io"
REFERER = f"{BASE_URL}/v2/lms/aluno/dashboard"

# media id de um embed mdstrm: <iframe src='//mdstrm.com/embed/{id}' ...>
MDSTRM_EMBED_RE = re.compile(r"mdstrm\.com/embed/([0-9a-fA-F]+)")
# fallback: qualquer outro iframe dentro do campo `embed`
IFRAME_SRC_RE = re.compile(r"<iframe[^>]+src=['\"]([^'\"]+)['\"]", re.IGNORECASE)

# TODO(anexos): a base de download dos arquivos internos ("path" em
# listCourseFiles/listDocuments, ex.: "9643/6cc99eaa-....pdf", onde 9643 é o
# cronogramaId) NÃO foi capturada no HAR analisado — só a listagem. Quando um HAR
# com o clique de download for capturado, preencher MATERIAL_BASE_URL com a base
# correta (host/CDN/blob) e os anexos passam a baixar automaticamente. Enquanto
# None, os arquivos internos são apenas listados como URLs auxiliares (referência).
MATERIAL_BASE_URL: Optional[str] = None


class PlatosEduPlatform(BasePlatform):
    """Implementa a plataforma Plato Edu / Cogna (infoprod.platosedu.io).

    Autenticação por cookie `SESSION` (token colável). O usuário cola o cabeçalho
    Cookie inteiro ou apenas o valor de `SESSION`. Login por usuário/senha não é
    suportado nesta versão (o SSO é Keycloak — sso.platosedu.io/realms/cognaai —
    e exigiria emulação de navegador).
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)

    @classmethod
    def token_field(cls) -> AuthField:
        return AuthField(
            name="token",
            label="Cookie de sessão (SESSION)",
            field_type=AuthFieldType.PASSWORD,
            placeholder="Cole o cabeçalho Cookie inteiro ou apenas o valor de SESSION",
            required=False,
        )

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Esta plataforma usa o cookie de sessão `SESSION` para autenticar.

Como obter o cookie:
1) Acesse https://infoprod.platosedu.io e faça login normalmente.
2) Abra o DevTools (F12) -> aba Rede (Network) e atualize a página.
3) Clique em qualquer requisição para infoprod.platosedu.io, vá em Cabeçalhos
   (Headers) e localize o cabeçalho de requisição "Cookie".
4) Copie o valor inteiro (ou apenas o trecho SESSION=...) e cole no campo acima.

Observação: login por usuário/senha ainda não é suportado nesta plataforma
(o SSO é via Keycloak). Use o cookie de sessão.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        cookie = (credentials.get("token") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        if not cookie and (username or password):
            raise ValueError(
                "Login por usuário/senha não é suportado nesta plataforma. "
                "Informe o cookie de sessão (SESSION)."
            )
        if not cookie:
            raise ValueError("Informe o cookie de sessão (SESSION).")

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Referer": REFERER,
            }
        )
        self._session = session
        self._apply_cookie_string(cookie)
        self._validate_session()
        logger.info("Sessão autenticada na Plato Edu.")

    def _apply_cookie_string(self, cookie: str) -> None:
        """Carrega um cabeçalho Cookie colado (ou um valor de SESSION puro)."""
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        if "=" in cookie:
            for part in cookie.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                name, value = part.split("=", 1)
                name = name.strip()
                value = value.strip()
                if name:
                    self._session.cookies.set(name, value, domain=COOKIE_DOMAIN)
        else:
            # Valor isolado é tratado como o cookie SESSION.
            self._session.cookies.set("SESSION", cookie, domain=COOKIE_DOMAIN)

    def _validate_session(self) -> None:
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")
        try:
            response = self._session.get(f"{API}/pessoa/show.json", timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError(
                "Sessão da Plato Edu inválida ou expirada. Faça login novamente."
            ) from exc
        if not isinstance(data, dict) or not data.get("id"):
            raise ConnectionError(
                "Sessão da Plato Edu inválida ou expirada. Faça login novamente."
            )

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")
        url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
        response = self._session.get(url, params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """Lista uma 'curso' por disciplina (boletim) de cada matrícula ativa."""
        try:
            programs = self._get_json("matricula/listCoursesByPessoaId.json")
        except Exception as exc:  # noqa: BLE001
            logger.error("Plato: falha ao listar matrículas: %s", exc)
            return []
        if not isinstance(programs, list):
            return []

        courses: List[Dict[str, Any]] = []
        for program in programs:
            matricula_id = program.get("id")
            program_name = (program.get("course") or "").strip() or str(matricula_id)
            if matricula_id is None:
                continue

            try:
                disciplinas = self._get_json(
                    "boletim/listDisciplinasByMatricula.json", {"id": matricula_id}
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Plato: falha ao listar disciplinas da matrícula %s: %s",
                    matricula_id,
                    exc,
                )
                continue
            if not isinstance(disciplinas, list):
                continue

            for disc in disciplinas:
                boletim_id = disc.get("id")
                if boletim_id is None:
                    continue
                disc_name = (disc.get("nomeDisciplina") or "").strip() or str(boletim_id)
                courses.append(
                    {
                        "id": str(boletim_id),
                        "name": f"{program_name} - {disc_name}",
                        "slug": str(boletim_id),
                        "seller_name": program_name,
                        "matricula_id": matricula_id,
                        "program_name": program_name,
                        "disciplina_name": disc_name,
                    }
                )

        logger.info("Plato: %d disciplina(s) encontradas.", len(courses))
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}
        for course in courses:
            boletim_id = course.get("id")
            if not boletim_id:
                continue

            try:
                payload = self._get_json(
                    "cronograma/listCategories.json", {"id": boletim_id}
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Plato: falha ao listar categorias do boletim %s: %s",
                    boletim_id,
                    exc,
                )
                payload = {}

            categories = (payload or {}).get("categoriesList") or []
            modules: List[Dict[str, Any]] = []
            for m_order, category in enumerate(categories, start=1):
                lessons: List[Dict[str, Any]] = []
                for l_order, item in enumerate(category.get("contentList") or [], start=1):
                    lessons.append(
                        {
                            "id": str(item.get("id")),
                            "title": (item.get("nome") or "Aula").strip(),
                            "order": l_order,
                            "locked": False,
                            "tipo_conteudo": item.get("tipoConteudo"),
                            "descricao": item.get("descricao") or "",
                            "embed": item.get("embed") or "",
                            "boletim_id": str(boletim_id),
                        }
                    )
                modules.append(
                    {
                        "id": str(category.get("id")),
                        "title": (category.get("nome") or "Módulo").strip(),
                        "order": m_order,
                        "locked": False,
                        "lessons": lessons,
                    }
                )

            entry = dict(course)
            entry["title"] = course.get("name", "Curso")
            entry["modules"] = modules
            content[str(boletim_id)] = entry

        return content

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content = LessonContent()
        title = lesson.get("title") or "Aula"
        order = lesson.get("order", 1)
        boletim_id = lesson.get("boletim_id") or course_id

        descricao = (lesson.get("descricao") or "").strip()
        if descricao:
            content.description = Description(text=descricao, description_type="text")

        self._append_video(content, lesson, title, order)
        self._append_files(content, lesson, boletim_id)

        return content

    def _append_video(
        self, content: LessonContent, lesson: Dict[str, Any], title: str, order: int
    ) -> None:
        embed = lesson.get("embed") or ""
        if not embed:
            return

        match = MDSTRM_EMBED_RE.search(embed)
        if match:
            media_id = match.group(1)
            content.videos.append(
                Video(
                    video_id=media_id,
                    url=f"https://mdstrm.com/video/{media_id}.m3u8",
                    order=order,
                    title=title,
                    size=0,
                    duration=0,
                    extra_props={"referer": f"{BASE_URL}/"},
                )
            )
            return

        iframe = IFRAME_SRC_RE.search(embed)
        if iframe:
            src = iframe.group(1)
            if src.startswith("//"):
                src = "https:" + src
            content.auxiliary_urls.append(
                AuxiliaryURL(
                    url_id=f"embed-{lesson.get('id')}",
                    url=src,
                    order=order,
                    title=title,
                    description="Embed não-mdstrm (verificar provedor)",
                )
            )

    def _append_files(
        self, content: LessonContent, lesson: Dict[str, Any], boletim_id: str
    ) -> None:
        """Lista anexos da aula (documentos + arquivos do curso)."""
        content_id = lesson.get("id")
        if not content_id:
            return

        docs: List[Dict[str, Any]] = []
        for endpoint in ("cronograma/listDocuments.json", "cronograma/listCourseFiles.json"):
            try:
                payload = self._get_json(
                    endpoint, {"cronogramaAnexoId": content_id, "boletimId": boletim_id}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Plato: falha em %s para aula %s: %s", endpoint, content_id, exc)
                continue
            docs.extend((payload or {}).get("cronogramaAnexoDocsList") or [])

        order = 1
        seen: set[str] = set()
        for doc in docs:
            doc_id = str(doc.get("id"))
            if doc_id in seen:
                continue
            seen.add(doc_id)

            nome = (doc.get("nome") or f"anexo-{doc_id}").strip()
            link = (doc.get("link") or "").strip()
            path = (doc.get("path") or "").strip()

            if link:
                # Material externo (não é download direto): vai como URL auxiliar.
                content.auxiliary_urls.append(
                    AuxiliaryURL(
                        url_id=f"doc-{doc_id}",
                        url=link,
                        order=order,
                        title=nome,
                        description=doc.get("descricao") or "Material complementar",
                    )
                )
            elif path:
                extension = Path(path).suffix.lstrip(".") or "bin"
                filename = nome if Path(nome).suffix else f"{nome}.{extension}"
                if MATERIAL_BASE_URL:
                    content.attachments.append(
                        Attachment(
                            attachment_id=f"doc-{doc_id}",
                            url=f"{MATERIAL_BASE_URL.rstrip('/')}/{path.lstrip('/')}",
                            filename=filename,
                            order=order,
                            extension=extension,
                            size=0,
                        )
                    )
                else:
                    # Base de download ainda desconhecida: registra a referência.
                    logger.warning(
                        "Plato: anexo '%s' (path=%s) não baixado — "
                        "MATERIAL_BASE_URL não configurada.",
                        nome,
                        path,
                    )
                    content.auxiliary_urls.append(
                        AuxiliaryURL(
                            url_id=f"doc-{doc_id}",
                            url=path,
                            order=order,
                            title=nome,
                            description="Arquivo interno (base de download a confirmar)",
                        )
                    )
            order += 1

    def download_attachment(
        self,
        attachment: Attachment,
        download_path: Path,
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> bool:
        if not attachment.url:
            logger.error("Plato: anexo sem URL: %s", attachment.filename)
            return False
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        try:
            response = self._session.get(
                attachment.url,
                headers={"Referer": REFERER},
                stream=True,
                allow_redirects=True,
                timeout=60,
            )
            response.raise_for_status()

            download_path.parent.mkdir(parents=True, exist_ok=True)
            with open(download_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Plato: falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


# PlatformFactory.register_platform("Plato Edu", PlatosEduPlatform, slug=INTEGRATION_SLUG, version=INTEGRATION_VERSION, experimental=INTEGRATION_EXPERIMENTAL,)
PlatformFactory.register_platform("Plato Edu (Cogna LMS)", PlatosEduPlatform)
