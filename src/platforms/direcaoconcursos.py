from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from playwright.async_api import Page

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
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher

logger = logging.getLogger(__name__)

API_BASE = "https://prod-api.direcaoconcursos.com.br"
APP_ORIGIN = "https://aluno.direcaoconcursos.com.br"
PDF_BASE = "https://pdf.direcaoconcursos.com.br"

# Endpoint cujo header Authorization e capturado pelo fetcher durante o login.
TARGET_ENDPOINTS = [f"{API_BASE}/learning"]


class DirecaoConcursosTokenFetcher(PlaywrightTokenFetcher):
    """Captura o bearer token logando no painel do aluno (ou aguardando login manual).

    O formulario de login nao foi capturado no HAR; os seletores abaixo sao
    best-effort. Para assinantes com 2FA/captcha, o modo "Emular Navegador"
    abre o browser e deixa o usuario logar manualmente — o token e capturado
    da primeira requisicao a ``prod-api.direcaoconcursos.com.br/learning``.
    """

    @property
    def login_url(self) -> str:
        # Abrimos a raiz da SPA (e nao /login direto): um hard-load em "/login"
        # deixa o app React em branco. A partir de "/", a SPA roteia sozinha
        # para a tela de login quando o usuario nao esta autenticado.
        return f"{APP_ORIGIN}/"

    @property
    def login_urls(self) -> List[str]:
        return [f"{APP_ORIGIN}/", f"{APP_ORIGIN}/login"]

    @property
    def target_endpoints(self) -> List[str]:
        return TARGET_ENDPOINTS

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        email_selector = (
            "input[type='email'], input[name='email'], "
            "input[name='username'], input[id*='email' i]"
        )
        await page.wait_for_selector(email_selector)
        await page.fill(email_selector, username)

        password_selector = "input[type='password'], input[name='password']"
        await page.wait_for_selector(password_selector)
        await page.fill(password_selector, password)

    async def submit_login(self, page: Page) -> None:
        try:
            await page.click("button[type='submit']", timeout=5000)
        except Exception:
            await page.keyboard.press("Enter")


class DirecaoConcursosPlatform(BasePlatform):
    """Implementa a plataforma Direção Concursos (aluno.direcaoconcursos.com.br).

    Hierarquia de conteudo na API:
        course -> module (materia) -> lesson (aula) -> chapter -> objeto (video/excerto)

    A GUI tem 3 niveis (modulo -> aula -> conteudo). Mapeamos:
        modulo GUI  = ``module`` (materia)
        aula  GUI   = ``lesson`` (aula, ex.: "Ortografia Oficial")
    e agregamos, em cada aula, o conteudo de TODOS os seus ``chapters``
    (videos HLS, slides em PDF, excertos em PDF e a apostila consolidada).
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = DirecaoConcursosTokenFetcher()
        self._subscription_id: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="subscription_id",
                label="ID da Assinatura",
                placeholder="Ex.: 65cf723ec3a23700355f014d",
                required=False,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return (
            "Como obter o Token e o ID da Assinatura:\n"
            "1) Acesse https://aluno.direcaoconcursos.com.br e faca login.\n"
            "2) Abra o DevTools (F12) > aba Rede (Network).\n"
            "3) Navegue ate a sua lista de cursos.\n"
            "4) Procure uma requisicao para 'prod-api.direcaoconcursos.com.br'\n"
            "   (ex.: 'course/subscriptions' ou 'favorite-courses/...').\n"
            "5) Token: em Cabecalhos, copie o valor de 'Authorization'\n"
            "   (sem o prefixo 'Bearer ') e cole no campo Token.\n"
            "6) ID da Assinatura: na requisicao 'course/subscriptions', veja o\n"
            "   corpo enviado (campo 'subscription'); ou pegue da URL\n"
            "   'favorite-courses/<ID>'. Cole esse ID no campo 'ID da Assinatura'.\n\n"
            "Assinantes podem informar usuario/senha (ou marcar 'Emular Navegador'\n"
            "para logar manualmente com 2FA) para obter o token automaticamente."
        ).strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        self._subscription_id = (credentials.get("subscription_id") or "").strip()
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _configure_session(self, token: str) -> None:
        token = sanitize_token(token).strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
        })

        try:
            resp = self._session.get(f"{API_BASE}/learning/notification", timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ConnectionError(
                "Token invalido ou expirado. Refaca o login na plataforma."
            ) from exc

        logger.info("Direção Concursos: autenticado com sucesso")

    def _exchange_credentials_for_token(
        self, username: str, password: str, credentials: Dict[str, Any]
    ) -> str:
        use_browser_emulation = bool(credentials.get("browser_emulation"))
        confirmation_event = credentials.get("manual_auth_confirmation")
        try:
            return self._token_fetcher.fetch_token(
                username,
                password,
                headless=not use_browser_emulation,
                user_agent=self._settings.user_agent,
                wait_for_user_confirmation=(
                    confirmation_event.wait if confirmation_event else None
                ),
            )
        except Exception as exc:
            raise ConnectionError(
                "Falha ao obter o token via Playwright. Revise usuário/senha."
            ) from exc

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")
        if not self._subscription_id:
            raise ValueError(
                "Informe o 'ID da Assinatura' para listar os cursos. Veja as "
                "instrucoes de autenticacao (corpo da requisicao "
                "'course/subscriptions' ou URL 'favorite-courses/<ID>')."
            )

        courses: Dict[str, Dict[str, Any]] = {}
        page = 1
        limit = 100
        while True:
            payload = {
                "page": page,
                "limit": limit,
                "filter": {"name": "", "state": "", "career": ""},
                "subscription": self._subscription_id,
            }
            try:
                resp = self._session.post(
                    f"{API_BASE}/learning/course/subscriptions", json=payload, timeout=30
                )
                resp.raise_for_status()
                batch = resp.json()
            except Exception as exc:
                if page == 1:
                    raise ConnectionError(
                        f"Falha ao listar cursos da assinatura: {exc}"
                    ) from exc
                logger.warning("Direção Concursos: falha na pagina %d: %s", page, exc)
                break

            if not isinstance(batch, list) or not batch:
                break

            for course in batch:
                cid = course.get("courseId") or course.get("_id")
                if not cid:
                    continue
                cid = str(cid)
                if cid in courses:
                    continue
                courses[cid] = {
                    "id": cid,
                    "name": (course.get("name") or f"Curso {cid}").strip(),
                    "slug": course.get("permanentLink") or cid,
                    "seller_name": (course.get("publicTender") or "Direção Concursos"),
                    "extra": {
                        "image": course.get("image"),
                        "number_of_modules": course.get("numberOfModules"),
                    },
                }

            if len(batch) < limit:
                break
            page += 1

        logger.debug("Direção Concursos: %d cursos encontrados", len(courses))
        return sorted(courses.values(), key=lambda c: c.get("name", ""))

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue
            course_id = str(course_id)

            try:
                modules = self._fetch_modules(course_id)
            except Exception as exc:
                logger.error(
                    "Direção Concursos: falha ao buscar curso %s: %s", course_id, exc
                )
                continue

            course_entry = course.copy()
            course_entry["title"] = course.get("name") or "Curso"
            course_entry["modules"] = modules
            all_content[course_id] = course_entry

        return all_content

    def _fetch_modules(self, course_id: str) -> List[Dict[str, Any]]:
        """Enumera os modulos (materias) e, por modulo, as aulas e seus capitulos."""
        resp = self._session.get(
            f"{API_BASE}/learning/course/{course_id}/basic", timeout=60
        )
        resp.raise_for_status()
        basic = resp.json()

        modules: List[Dict[str, Any]] = []
        for order, child in enumerate(basic.get("children") or [], start=1):
            # O id real do modulo usado pela API e o campo "ref".
            module_id = child.get("ref") or child.get("_id")
            if not module_id:
                continue
            module_id = str(module_id)
            module_name = (child.get("name") or f"Módulo {order}").strip()

            try:
                lessons = self._fetch_module_lessons(course_id, module_id)
            except Exception as exc:
                logger.warning(
                    "Direção Concursos: falha no menu do modulo %s: %s", module_id, exc
                )
                continue

            if not lessons:
                continue

            modules.append({
                "id": module_id,
                "title": module_name,
                "order": child.get("order", order) or order,
                "lessons": lessons,
            })

        return modules

    def _fetch_module_lessons(
        self, course_id: str, module_id: str
    ) -> List[Dict[str, Any]]:
        """Busca o menu do modulo: aulas (lessons) e seus capitulos (chapters)."""
        resp = self._session.get(
            f"{API_BASE}/learning/content/menu/{module_id}",
            params={"courseId": course_id},
            timeout=60,
        )
        resp.raise_for_status()
        menu = resp.json()

        lessons: List[Dict[str, Any]] = []
        for order, lesson in enumerate(menu.get("children") or [], start=1):
            lesson_id = lesson.get("_id")
            if not lesson_id:
                continue

            chapters = []
            for chapter in lesson.get("children") or []:
                chapter_id = chapter.get("_id")
                if not chapter_id:
                    continue
                chapters.append({
                    "id": str(chapter_id),
                    "name": (chapter.get("name") or "").strip(),
                    "types": chapter.get("types") or [],
                })

            lessons.append({
                "id": str(lesson_id),
                "title": (lesson.get("name") or f"Aula {order}").strip(),
                "order": order,
                "extra": {
                    "course_id": course_id,
                    "module_id": module_id,
                    "chapters": chapters,
                },
            })

        return lessons

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        extra = lesson.get("extra", {})
        lesson_id = lesson.get("id")
        course = extra.get("course_id") or course_id
        module = extra.get("module_id") or module_id
        chapters = extra.get("chapters") or []

        content = LessonContent()
        seen_video_ids: set = set()
        seen_attachment_urls: set = set()
        video_order = 0
        attach_order = 0

        for chapter in chapters:
            chapter_id = chapter.get("id")
            if not chapter_id:
                continue
            try:
                data = self._fetch_chapter_content(course, module, lesson_id, chapter_id)
            except Exception as exc:
                logger.warning(
                    "Direção Concursos: falha no conteudo do capitulo %s: %s",
                    chapter_id, exc,
                )
                continue

            for obj in data.get("contents") or []:
                obj_type = obj.get("type")
                obj_id = str(obj.get("_id") or "")
                name = (obj.get("name") or "").strip()

                if obj_type == "video":
                    media = obj.get("media") or {}
                    url = media.get("url")
                    if url and obj_id not in seen_video_ids:
                        seen_video_ids.add(obj_id)
                        video_order += 1
                        content.videos.append(Video(
                            video_id=obj_id or str(video_order),
                            url=url,
                            order=video_order,
                            title=name or f"Vídeo {video_order}",
                            size=0,
                            duration=int(float(media.get("total") or 0)),
                            extra_props={"referer": f"{APP_ORIGIN}/"},
                        ))

                    slide = media.get("slide") or {}
                    slide_url = slide.get("url")
                    if slide_url and slide_url not in seen_attachment_urls:
                        seen_attachment_urls.add(slide_url)
                        attach_order += 1
                        content.attachments.append(self._make_pdf_attachment(
                            slide_url,
                            slide.get("name") or f"{name} - slides",
                            obj_id or f"slide-{attach_order}",
                            attach_order,
                        ))

                elif obj_type == "excerto":
                    pdf = obj.get("pdf")
                    if pdf:
                        pdf_url = pdf if pdf.startswith("http") else f"{PDF_BASE}{pdf}"
                        if pdf_url not in seen_attachment_urls:
                            seen_attachment_urls.add(pdf_url)
                            attach_order += 1
                            content.attachments.append(self._make_pdf_attachment(
                                pdf_url,
                                name or f"Material {attach_order}",
                                obj_id or f"excerto-{attach_order}",
                                attach_order,
                            ))

        lesson_pdf = self._fetch_lesson_pdf(course, module, lesson_id)
        if lesson_pdf and lesson_pdf not in seen_attachment_urls:
            seen_attachment_urls.add(lesson_pdf)
            attach_order += 1
            content.attachments.append(self._make_pdf_attachment(
                lesson_pdf,
                f"{lesson.get('title', 'Aula')} - Apostila",
                f"apostila-{lesson_id}",
                attach_order,
            ))

        return content

    def _fetch_chapter_content(
        self, course: str, module: str, lesson_id: str, chapter_id: str
    ) -> Dict[str, Any]:
        payload = {
            "course": course,
            "studyModule": module,
            "lesson": lesson_id,
            "minimumApprenticeshipUnit": chapter_id,
        }
        resp = self._session.post(
            f"{API_BASE}/learning/content", json=payload, timeout=60
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_lesson_pdf(
        self, course: str, module: str, lesson_id: str
    ) -> Optional[str]:
        payload = {"course": course, "module": module, "lesson": lesson_id}
        try:
            resp = self._session.post(
                f"{API_BASE}/learning/content/lesson-pdf", json=payload, timeout=30
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.debug(
                "Direção Concursos: sem apostila para a aula %s: %s", lesson_id, exc
            )
            return None

        url = (resp.text or "").strip().strip('"')
        return url if url.startswith("http") else None

    @staticmethod
    def _make_pdf_attachment(
        url: str, name: str, attachment_id: str, order: int
    ) -> Attachment:
        filename = (name or "material").strip()
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        return Attachment(
            attachment_id=str(attachment_id),
            url=url,
            filename=filename,
            order=order,
            extension="pdf",
            size=0,
        )

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

        headers = {
            "User-Agent": self._settings.user_agent,
            "Referer": f"{APP_ORIGIN}/",
        }
        with requests.get(
            attachment.url, headers=headers, stream=True, timeout=120
        ) as resp:
            if resp.status_code != 200:
                raise ConnectionError(
                    f"HTTP {resp.status_code} ao baixar '{attachment.filename}'."
                )
            with open(download_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        return True


PlatformFactory.register_platform("Direção Concursos", DirecaoConcursosPlatform)
