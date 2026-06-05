from __future__ import annotations
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

BASE_URL = "https://www.neafconcursos.com.br"
LOGIN_URL = f"{BASE_URL}/accounts/login/"
EAD_URL = f"{BASE_URL}/ead/"
COURSE_URL = f"{BASE_URL}/ead/cursos-online/{{course_id}}/"
DISCIPLINES_URL = f"{BASE_URL}/ead/cursos-online/{{course_id}}/disciplinas/"
DISCIPLINE_URL = f"{BASE_URL}/ead/cursos-online/{{course_id}}/disciplinas/{{discipline_id}}/"
VIDEO_PAGE_URL = (
    f"{BASE_URL}/ead/cursos-online/{{course_id}}/aula/{{lesson_id}}/videos/{{video_id}}/p/"
)

VIDEOTECA_EMBED_URL = "https://neaf.videotecaead.com.br/Embed/code/{identifier}"
VIDEOTECA_REFERER = "https://neaf.videotecaead.com.br/"
VIDEOTECA_HLS_URL = "https://cy0sg6qy8j.map.azionedge.net/neaf/{identifier}/master.m3u8"


class NeafConcursosPlatform(BasePlatform):
    """Implements the NEAF Concursos (www.neafconcursos.com.br) platform."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)

    @classmethod
    def all_auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="token",
                label="sessionid (opcional)",
                field_type=AuthFieldType.TEXT,
                placeholder="Cole o valor do cookie sessionid",
                required=False,
            ),
            AuthField(
                name="username",
                label="CPF ou e-mail",
                placeholder="Digite o CPF ou e-mail cadastrado",
                requires_membership=True,
            ),
            AuthField(
                name="password",
                label="Senha",
                field_type=AuthFieldType.PASSWORD,
                placeholder="Digite a senha da plataforma",
                requires_membership=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 9.90): informe CPF/e-mail e senha — o sistema fará o login automaticamente.

Para usuários gratuitos (cookie de sessão):
1) Acesse https://www.neafconcursos.com.br/accounts/login/ e faça login.
2) Abra as Ferramentas de Desenvolvedor (F12) → Aplicação/Armazenamento → Cookies.
3) Copie o valor do cookie chamado 'sessionid'.
4) Cole o valor no campo acima. Renove o cookie quando o login pedir.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        session = requests.Session()
        session.headers.update({"User-Agent": self._settings.user_agent})
        self._session = session

        token = (credentials.get("token") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        if username and password:
            self._login_with_credentials(username, password)
        elif token:
            session.cookies.set("sessionid", token, domain=".neafconcursos.com.br")
            if not self._session_is_valid():
                raise ConnectionError(
                    "Cookie sessionid inválido ou expirado. Faça login novamente."
                )
        else:
            raise ValueError("Informe CPF/e-mail e senha, ou o cookie sessionid.")

        logging.info("Sessão autenticada no NEAF Concursos.")

    def _login_with_credentials(self, username: str, password: str) -> None:
        get_resp = self._session.get(LOGIN_URL)
        get_resp.raise_for_status()
        soup = BeautifulSoup(get_resp.text, "html.parser")

        csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
        csrf_token = csrf_input.get("value", "") if csrf_input else ""
        if not csrf_token:
            csrf_token = self._session.cookies.get("csrftoken", "")
        if not csrf_token:
            raise ConnectionError("Não foi possível obter o CSRF da página de login.")

        post_resp = self._session.post(
            LOGIN_URL,
            data={
                "csrfmiddlewaretoken": csrf_token,
                "login": username,
                "password": password,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": LOGIN_URL,
                "Origin": BASE_URL,
            },
            allow_redirects=False,
        )

        if post_resp.status_code != 302:
            error_msg = self._extract_login_error(post_resp.text)
            raise ConnectionError(f"Falha no login do NEAF Concursos. {error_msg}".strip())

        location = post_resp.headers.get("Location", "/")
        if "login" in location:
            raise ConnectionError("Falha no login do NEAF Concursos. Verifique as credenciais.")

    def _extract_login_error(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        err = soup.find(class_=re.compile("error|alert|invalid", re.IGNORECASE))
        if err and err.get_text(strip=True):
            return err.get_text(strip=True)
        return "Verifique CPF/e-mail e senha."

    def _session_is_valid(self) -> bool:
        try:
            resp = self._session.get(EAD_URL, allow_redirects=False)
            return resp.status_code == 200 and "login" not in resp.headers.get("Location", "")
        except Exception:
            return False

    def _get_html(self, url: str) -> BeautifulSoup:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")
        resp = self._session.get(url, headers={"Referer": EAD_URL})
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def fetch_courses(self) -> List[Dict[str, Any]]:
        soup = self._get_html(EAD_URL)
        courses: List[Dict[str, Any]] = []
        seen: set = set()

        for anchor in soup.find_all("a", href=re.compile(r"^/ead/cursos-online/(\d+)/$")):
            match = re.match(r"^/ead/cursos-online/(\d+)/$", anchor.get("href", ""))
            if not match:
                continue
            course_id = match.group(1)
            if course_id in seen:
                continue
            seen.add(course_id)

            descricao = anchor.find_parent(class_="descricao") or anchor.find_parent()
            title_el = descricao.find("h2") if descricao else None
            name = title_el.get_text(strip=True) if title_el else f"Curso {course_id}"

            courses.append(
                {
                    "id": course_id,
                    "name": name,
                    "slug": course_id,
                    "seller_name": "NEAF Concursos",
                }
            )

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = str(course.get("id") or "").strip()
            if not course_id:
                continue

            modules = self._fetch_course_modules(course_id)

            course_with_modules = dict(course)
            course_with_modules["modules"] = modules
            course_with_modules["title"] = course.get("name", "Curso")
            all_content[course_id] = course_with_modules

        return all_content

    def _fetch_course_modules(self, course_id: str) -> List[Dict[str, Any]]:
        try:
            soup = self._get_html(DISCIPLINES_URL.format(course_id=course_id))
        except Exception as exc:
            logging.warning("Falha ao listar disciplinas do curso %s: %s", course_id, exc)
            return []

        modules: List[Dict[str, Any]] = []
        seen: set = set()

        for box in soup.find_all("div", class_="box-disciplina-curso"):
            link = box.find("a", href=re.compile(r"/disciplinas/(\d+)/"))
            if not link:
                continue
            match = re.search(r"/disciplinas/(\d+)/", link.get("href", ""))
            if not match:
                continue
            discipline_id = match.group(1)
            if discipline_id in seen:
                continue
            seen.add(discipline_id)

            title_el = box.find("h2")
            discipline_name = title_el.get_text(strip=True) if title_el else f"Disciplina {discipline_id}"

            lessons = self._fetch_discipline_lessons(course_id, discipline_id)
            modules.append(
                {
                    "id": discipline_id,
                    "title": discipline_name,
                    "order": len(modules) + 1,
                    "lessons": lessons,
                    "locked": False,
                }
            )

        return modules

    def _fetch_discipline_lessons(self, course_id: str, discipline_id: str) -> List[Dict[str, Any]]:
        try:
            soup = self._get_html(DISCIPLINE_URL.format(course_id=course_id, discipline_id=discipline_id))
        except Exception as exc:
            logging.warning(
                "Falha ao listar aulas da disciplina %s/%s: %s", course_id, discipline_id, exc
            )
            return []

        lessons: List[Dict[str, Any]] = []

        for collapse in soup.find_all("div", id=re.compile(r"^collapse(\d+)$")):
            match = re.match(r"^collapse(\d+)$", collapse.get("id", ""))
            if not match:
                continue
            
            lesson_id = match.group(1)
            lesson_title = ""

            # 1. Tentativa pelo ID de cabeçalho padrão
            heading_el = soup.find(id=f"heading{lesson_id}")
            if heading_el:
                lesson_title = heading_el.get_text(separator=" ", strip=True)

            # 2. Tentativa por âncoras/botões que apontam para o collapse
            if not lesson_title:
                trigger = soup.find(attrs={"href": re.compile(rf"#collapse{lesson_id}$")})
                if trigger:
                    lesson_title = trigger.get_text(separator=" ", strip=True)
                    
            if not lesson_title:
                trigger = soup.find(attrs={"data-target": re.compile(rf"#collapse{lesson_id}$")})
                if trigger:
                    lesson_title = trigger.get_text(separator=" ", strip=True)

            if not lesson_title:
                trigger = soup.find(attrs={"aria-controls": f"collapse{lesson_id}"})
                if trigger:
                    lesson_title = trigger.get_text(separator=" ", strip=True)

            # 3. O "Plano C": Pegar o primeiro parágrafo interno se os outros falharem
            if not lesson_title:
                first_p = collapse.find("p")
                if first_p and first_p.get_text(strip=True):
                    lesson_title = first_p.get_text(separator=" ", strip=True)

            # --- LIMPEZA AVANÇADA E COMPLETA DO NOME DA AULA ---
            if lesson_title:
                lesson_title = re.sub(r"[\n\r\t]+", " ", lesson_title)
                
                # Executa uma limpeza em loop para remover múltiplos prefixos acumulados
                while True:
                    old_title = lesson_title
                    # Remove termos estruturais do site (independente de maiúsculas/minúsculas)
                    lesson_title = re.sub(
                        r"(?i)^(baixe[\s\w]*material|material[\s\w]*apoio|aula\s*\d+|bloco\s*\d+|parte\s*\d+)", 
                        "", 
                        lesson_title
                    ).strip()
                    # Remove pontuações e símbolos que restarem pendentes no início do texto
                    lesson_title = re.sub(r"^[\s\-\|:\.]+", "", lesson_title).strip()
                    
                    # Se o texto parar de mudar, significa que está 100% limpo
                    if lesson_title == old_title:
                        break
                
                # Consolida múltiplos espaços em branco internos
                lesson_title = re.sub(r"\s+", " ", lesson_title).strip()

            if not lesson_title or lesson_title == "" or lesson_title == "_":
                lesson_title = f"Aula {lesson_id}"

            videos: List[Dict[str, Any]] = []
            attachments: List[Dict[str, Any]] = []

            for row in collapse.find_all("tr"):
                # --- EXTRAÇÃO DOS VÍDEOS ---
                btn = row.find("button", id=re.compile(r"^btn_kmodal-(\d+)$"))
                if btn:
                    vmatch = re.match(r"^btn_kmodal-(\d+)$", btn.get("id", ""))
                    if vmatch:
                        video_id = vmatch.group(1)
                        title_p = row.find("p")
                        
                        part_title = ""
                        if title_p and title_p.get_text(strip=True):
                            part_title = title_p.get_text(separator=" ", strip=True)
                        else:
                            part_title = lesson_title
                            
                        videos.append({
                            "video_id": video_id,
                            "title": part_title,
                            "order": len(videos) + 1,
                        })

                # --- EXTRAÇÃO DOS ANEXOS ---
                materials_td = row.find("td", class_="materiais")
                if materials_td:
                    for anchor in materials_td.find_all("a", href=True):
                        href = anchor.get("href", "").strip()
                        if not href:
                            continue
                        
                        filename = anchor.get("title")
                        if not filename:
                            filename = anchor.get_text(strip=True)
                        if not filename:
                            filename = href.split("?")[0].rsplit("/", 1)[-1]
                            
                        filename = filename.strip().replace('\r', '').replace('\n', '')
                        
                        url_filename = href.split("?")[0].rsplit("/", 1)[-1]
                        file_ext = ""
                        if "." in url_filename:
                            file_ext = url_filename.rsplit(".", 1)[-1].lower()
                            
                        if file_ext and not filename.lower().endswith(f".{file_ext}"):
                            filename = f"{filename}.{file_ext}"

                        attachments.append({
                            "id": f"{lesson_id}-mat-{len(attachments) + 1}",
                            "url": href,
                            "filename": filename,
                            "order": len(attachments) + 1,
                        })

            lessons.append({
                "id": lesson_id,
                "title": lesson_title,
                "order": len(lessons) + 1,
                "locked": False,
                "_videos": videos,
                "_attachments": attachments,
            })

        return lessons

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        content = LessonContent()
        lesson_id = str(lesson.get("id") or "")

        for video in lesson.get("_videos", []) or []:
            video_id = str(video.get("video_id") or "")
            if not video_id:
                continue
            identifier = self._resolve_video_identifier(course_id, lesson_id, video_id)
            if not identifier:
                continue
            content.videos.append(
                Video(
                    video_id=video_id,
                    url=VIDEOTECA_HLS_URL.format(identifier=identifier),
                    order=video.get("order", 1),
                    title=video.get("title", lesson.get("title", "Aula")),
                    size=0,
                    duration=0,
                    extra_props={
                        "referer": VIDEOTECA_REFERER,
                        "embed_url": VIDEOTECA_EMBED_URL.format(identifier=identifier),
                        "videoteca_identifier": identifier,
                    },
                )
            )

        for att in lesson.get("_attachments", []) or []:
            url = att.get("url") or ""
            if not url:
                continue
            filename = att.get("filename") or url.rsplit("/", 1)[-1]
            extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
            content.attachments.append(
                Attachment(
                    attachment_id=str(att.get("id") or f"{lesson_id}-att"),
                    url=url,
                    filename=filename,
                    order=att.get("order", 1),
                    extension=extension,
                    size=0,
                )
            )

        return content

    def _resolve_video_identifier(self, course_id: str, lesson_id: str, video_id: str) -> Optional[str]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")
        page_url = VIDEO_PAGE_URL.format(course_id=course_id, lesson_id=lesson_id, video_id=video_id)
        try:
            resp = self._session.get(page_url, headers={"Referer": EAD_URL})
            resp.raise_for_status()
        except Exception as exc:
            logging.warning("Falha ao abrir página do vídeo %s: %s", video_id, exc)
            return None

        match = re.search(r'data-identifier=["\']([A-Za-z0-9_-]+)["\']', resp.text)
        if match:
            return match.group(1)

        match = re.search(r"videotecaead\.com\.br/Embed/code/([A-Za-z0-9_-]+)", resp.text)
        if match:
            return match.group(1)

        logging.warning("Identificador de vídeo não encontrado na página %s", page_url)
        return None

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
            logging.error("Anexo sem URL: %s", attachment.filename)
            return False

        try:
            resp = self._session.get(
                attachment.url,
                stream=True,
                headers={"Referer": EAD_URL},
                timeout=30,
            )
            resp.raise_for_status()
            with open(download_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)
            return True
        except Exception as exc:
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("NEAF Concursos", NeafConcursosPlatform)
