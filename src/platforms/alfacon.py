from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)

WWW_BASE = "https://www.alfaconcursos.com.br"
PERFIL_BASE = "https://perfil.alfaconcursos.com.br"
LOGIN_PAGE_URL = f"{WWW_BASE}/sessions/new?scrollto=signin"
LOGIN_URL = f"{WWW_BASE}/sessions?scrollto=signin"
VLE_ROOT = f"{PERFIL_BASE}/vle"
SPALLA_PLAYER_URL = "https://beyond.spalla.io/player/?video={uuid}"


class AlfaConPlatform(BasePlatform):
    """Scrapes the AlfaCon students' VLE (perfil.alfaconcursos.com.br)."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._enrolment_ids: List[str] = []
        self._course_enrolment: Dict[str, str] = {}

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def token_field(cls) -> AuthField:
        return AuthField(
            name="token",
            label="Cookie alfacon_session",
            field_type=AuthFieldType.PASSWORD,
            placeholder="Cole aqui o valor do cookie 'alfacon_session'",
            required=False,
        )

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 9.90) ativos podem informar usuário/senha. Atenção: o login com
usuário/senha pode falhar quando a AlfaCon ativa o reCAPTCHA invisível - nesse
caso, use o cookie de sessão.

Para usuários gratuitos: como obter o cookie alfacon_session:
1) Acesse https://perfil.alfaconcursos.com.br e faça login normalmente.
2) Abra o DevTools (F12) → aba Aplicação (Application) → Cookies.
3) Selecione "https://perfil.alfaconcursos.com.br" e localize o cookie
   chamado "alfacon_session".
4) Copie o valor inteiro e cole no campo acima.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        cookie = (credentials.get("token") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        session = requests.Session()
        session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        if cookie:
            session.cookies.set(
                "alfacon_session", cookie, domain=".alfaconcursos.com.br"
            )
            self._session = session
        elif username and password and self._settings.has_full_permissions:
            self._session = session
            self._login_with_credentials(username, password)
        else:
            raise ValueError(
                "Informe o cookie 'alfacon_session' ou utilize as credenciais."
            )

        self._validate_session()
        logger.info("Sessão autenticada na AlfaCon.")

    def _login_with_credentials(self, username: str, password: str) -> None:
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")

        page = self._session.get(LOGIN_PAGE_URL)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        csrf_input = soup.select_one("form#login input[name='authenticity_token']")
        if not csrf_input or not csrf_input.get("value"):
            raise ValueError("Não foi possível obter o authenticity_token da AlfaCon.")

        data = {
            "utf8": "\u2713",
            "authenticity_token": csrf_input["value"],
            "email": username,
            "password": password,
            "button": "",
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": WWW_BASE,
            "Referer": LOGIN_PAGE_URL,
        }
        response = self._session.post(LOGIN_URL, data=data, headers=headers, allow_redirects=True)
        response.raise_for_status()

        session_cookie = any(
            cookie.name == "alfacon_session" and (cookie.domain or "").endswith("alfaconcursos.com.br")
            for cookie in self._session.cookies
        )
        if "/sessions" in response.url or not session_cookie:
            if "recaptcha" in response.text.lower() or "robô" in response.text.lower():
                raise ConnectionError(
                    "Login bloqueado pelo reCAPTCHA. Use o cookie alfacon_session."
                )
            raise ConnectionError("Falha no login AlfaCon. Verifique usuário/senha.")

    def _validate_session(self) -> None:
        if not self._session:
            raise ConnectionError("Sessão não inicializada.")
        response = self._session.get(VLE_ROOT, allow_redirects=True)
        response.raise_for_status()
        if "/sessions" in response.url:
            raise ConnectionError("Cookie alfacon_session inválido ou expirado.")

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        enrolments = self._discover_enrolments()
        self._enrolment_ids = [e["id"] for e in enrolments]
        if not enrolments:
            logger.warning("Nenhuma matrícula encontrada no painel AlfaCon.")
            return []

        courses: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for enrolment in enrolments:
            enrolment_id = enrolment["id"]
            enrolment_courses = self._fetch_enrolment_courses(enrolment_id, enrolment.get("title", ""))
            for course in enrolment_courses:
                key = f"{enrolment_id}:{course['id']}"
                if key in seen:
                    continue
                seen.add(key)
                self._course_enrolment[str(course["id"])] = enrolment_id
                courses.append(course)

        logger.info("AlfaCon: %d curso(s) encontrados.", len(courses))
        return courses

    def _fetch_enrolment_courses(self, enrolment_id: str, enrolment_title: str) -> List[Dict[str, Any]]:
        """Lists courses under an enrolment. Falls back to the enrolment root
        page when the /courses index is unavailable (e.g. single-course free
        enrolments return 500 on that endpoint)."""

        index_url = f"{VLE_ROOT}/{enrolment_id}/courses"
        try:
            response = self._session.get(index_url)
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            logger.warning(
                "AlfaCon: /vle/%s/courses retornou %s; usando página da matrícula.",
                enrolment_id, status,
            )
            return self._fetch_enrolment_courses_fallback(enrolment_id, enrolment_title)

        cards = self._parse_course_cards(enrolment_id, response.text)
        if cards:
            return cards

        logger.info(
            "AlfaCon: nenhum card em /vle/%s/courses; tentando página da matrícula.",
            enrolment_id,
        )
        return self._fetch_enrolment_courses_fallback(enrolment_id, enrolment_title)

    def _fetch_enrolment_courses_fallback(self, enrolment_id: str, enrolment_title: str) -> List[Dict[str, Any]]:
        try:
            response = self._session.get(f"{VLE_ROOT}/{enrolment_id}", allow_redirects=True)
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            logger.warning("AlfaCon: matrícula %s inacessível (HTTP %s).", enrolment_id, status)
            return []

        cards = self._parse_course_cards(enrolment_id, response.text)
        if cards:
            return cards

        final_path = urlparse(response.url).path
        overview_match = re.match(
            rf"^/vle/{re.escape(enrolment_id)}/(\d+)/overview/?$",
            final_path,
        )
        if overview_match:
            course_id = overview_match.group(1)
            title = self._extract_overview_title(response.text) or enrolment_title or f"Curso {course_id}"
            return [{
                "id": course_id,
                "name": title,
                "slug": f"{enrolment_id}-{course_id}",
                "seller_name": "AlfaCon",
                "enrolment_id": enrolment_id,
            }]

        logger.warning(
            "AlfaCon: não foi possível identificar cursos na matrícula %s.",
            enrolment_id,
        )
        return []

    def _discover_enrolments(self) -> List[Dict[str, str]]:
        response = self._session.get(VLE_ROOT, allow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        enrolment_pattern = re.compile(r"^/vle/(\d{4,})/?$")
        enrolments: Dict[str, str] = {}
        for anchor in soup.select("a[href]"):
            match = enrolment_pattern.match(anchor.get("href", ""))
            if not match:
                continue
            enrolment_id = match.group(1)
            title = anchor.get_text(" ", strip=True)
            if enrolment_id not in enrolments or (title and not enrolments[enrolment_id]):
                enrolments[enrolment_id] = title

        # Include IDs that only appear embedded in deeper URLs
        for match in re.findall(r'/vle/(\d{4,})/\d+/overview', response.text):
            enrolments.setdefault(match, "")

        return [{"id": eid, "title": title} for eid, title in sorted(enrolments.items())]

    @staticmethod
    def _extract_overview_title(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for selector in ("h1", "h2", "title"):
            node = soup.find(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return text
        return ""

    def _parse_course_cards(self, enrolment_id: str, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        courses: List[Dict[str, Any]] = []
        seen: set[str] = set()

        pattern = re.compile(rf"^/vle/{re.escape(enrolment_id)}/(\d+)/overview$")
        for anchor in soup.select("a[href]"):
            match = pattern.match(anchor.get("href", ""))
            if not match:
                continue
            course_id = match.group(1)
            if course_id in seen:
                continue
            title = self._extract_course_title(anchor)
            if not title:
                continue
            seen.add(course_id)
            courses.append({
                "id": course_id,
                "name": title,
                "slug": f"{enrolment_id}-{course_id}",
                "seller_name": "AlfaCon",
                "enrolment_id": enrolment_id,
            })
        return courses

    @staticmethod
    def _extract_course_title(anchor) -> str:
        for target in (anchor, *anchor.find_parents(limit=4)):
            heading = target.find(["h3", "h4", "h5", "strong"]) if target else None
            if heading:
                text = heading.get_text(" ", strip=True)
                if text:
                    return text
        text = anchor.get_text(" ", strip=True)
        return text or ""

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content: Dict[str, Any] = {}
        for course in courses:
            course_id = str(course.get("id"))
            enrolment_id = course.get("enrolment_id") or self._course_enrolment.get(course_id)
            if not enrolment_id:
                logger.warning("Sem enrolment_id para o curso %s, pulando.", course_id)
                continue

            modules = self._fetch_course_modules(enrolment_id, course_id)
            course_entry = course.copy()
            course_entry["enrolment_id"] = enrolment_id
            course_entry["title"] = course.get("name", "Curso")
            course_entry["modules"] = modules
            content[course_id] = course_entry
        return content

    def _fetch_course_modules(self, enrolment_id: str, course_id: str) -> List[Dict[str, Any]]:
        area_ids = self._discover_subject_areas(enrolment_id, course_id)
        modules: List[Dict[str, Any]] = []
        module_order = 0
        for area_order, area_id in enumerate(area_ids, start=1):
            area_html = self._get_vle_fragment(
                f"/vle/{enrolment_id}/{course_id}/subject_areas/{area_id}"
            )
            area_title = self._extract_area_title(area_html) or f"Grupo {area_order}"
            for subject_id, subject_title in self._iter_subjects(area_html):
                subject_html = self._get_vle_fragment(
                    f"/vle/{enrolment_id}/{course_id}/subjects/{subject_id}"
                )
                module_order += 1
                lessons = self._parse_lessons(subject_html, enrolment_id, course_id, subject_id)
                if not lessons:
                    continue
                modules.append({
                    "id": f"{area_id}-{subject_id}",
                    "title": f"{area_title} - {subject_title}".strip(" -"),
                    "order": module_order,
                    "lessons": lessons,
                    "locked": False,
                    "subject_id": subject_id,
                    "area_id": area_id,
                })
        return modules

    def _discover_subject_areas(self, enrolment_id: str, course_id: str) -> List[str]:
        html = self._get_vle_fragment(f"/vle/{enrolment_id}/{course_id}/subject_areas")
        ids = re.findall(rf"/vle/{enrolment_id}/{course_id}/subject_areas/(\d+)", html)
        if ids:
            return list(dict.fromkeys(ids))

        # fallback: the course_contents lazy shell carries the current area id.
        shell = self._get_vle_fragment(f"/vle/{enrolment_id}/{course_id}/course_contents")
        fallback = re.findall(rf"/vle/{enrolment_id}/{course_id}/subject_areas/(\d+)", shell)
        return list(dict.fromkeys(fallback))

    def _get_vle_fragment(self, path: str) -> str:
        url = urljoin(PERFIL_BASE, path)
        response = self._session.get(
            url,
            headers={"Accept": "text/html, */*", "Referer": f"{PERFIL_BASE}/vle"},
        )
        response.raise_for_status()
        return response.text

    @staticmethod
    def _extract_area_title(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")
        accordion = soup.select_one("div[id^='accordion_discipline_group_'] h4")
        if accordion:
            text = accordion.get_text(" ", strip=True)
            if text:
                return text
        toggle = soup.select_one("button.accordion-toggle span.text-base")
        return toggle.get_text(" ", strip=True) if toggle else None

    @staticmethod
    def _iter_subjects(html: str):
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        for node in soup.select("div[data-content-loader-url-value]"):
            url = node.get("data-content-loader-url-value", "")
            match = re.search(r"/subjects/(\d+)", url)
            if not match:
                continue
            subject_id = match.group(1)
            if subject_id in seen:
                continue
            seen.add(subject_id)
            title = ""
            container = node.find_parent("div", class_="accordion-item")
            if container:
                heading = container.select_one("button.accordion-toggle span.text-base")
                if heading:
                    title = heading.get_text(" ", strip=True)
            if not title:
                title = f"Disciplina {subject_id}"
            yield subject_id, title

    def _parse_lessons(
        self,
        html: str,
        enrolment_id: str,
        course_id: str,
        subject_id: str,
    ) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        lessons: List[Dict[str, Any]] = []
        order = 0

        for meeting in soup.select("div[id^='accordion_item_meeting_']"):
            meeting_title_el = meeting.select_one("button.accordion-toggle span.text-base")
            meeting_title = meeting_title_el.get_text(" ", strip=True) if meeting_title_el else ""
            meeting_id = (meeting.get("id") or "").replace("accordion_item_meeting_", "")

            for anchor in meeting.select("a[data-lesson-meeting-id]"):
                order += 1
                lesson_meeting_id = anchor.get("data-lesson-meeting-id")
                lesson_subject_id = anchor.get("data-subject-id") or subject_id
                title = anchor.get("data-lesson-title") or meeting_title or f"Aula {order}"
                description = anchor.get("data-lesson-description") or ""
                attachments = self._collect_lesson_attachments(anchor, enrolment_id, course_id)
                lesson_href = anchor.get("href", "")
                internal_lesson_id = ""
                if attachments:
                    internal_lesson_id = attachments[0]["lesson_id"]

                lessons.append({
                    "id": lesson_meeting_id,
                    "title": title,
                    "description": description,
                    "order": order,
                    "locked": False,
                    "meeting_id": meeting_id,
                    "meeting_title": meeting_title,
                    "subject_id": lesson_subject_id,
                    "lesson_meeting_id": lesson_meeting_id,
                    "lesson_id": internal_lesson_id,
                    "lesson_url": urljoin(PERFIL_BASE, lesson_href),
                    "attachment_refs": attachments,
                })
        return lessons

    @staticmethod
    def _collect_lesson_attachments(anchor, enrolment_id: str, course_id: str) -> List[Dict[str, str]]:
        attachments: List[Dict[str, str]] = []
        seen: set[str] = set()
        container = anchor.find_parent("div", class_="lesson") or anchor.parent
        if not container:
            return attachments
        pattern = re.compile(
            rf"/meus-cursos/{re.escape(enrolment_id)}/lessons/(\d+)/attachments/([^?\"']+)"
        )
        for att_anchor in container.select("a[href*='/attachments/']"):
            href = att_anchor.get("href", "")
            match = pattern.search(href)
            if not match:
                continue
            lesson_id, attachment_token = match.group(1), match.group(2)
            dedup = f"{lesson_id}:{attachment_token}"
            if dedup in seen:
                continue
            seen.add(dedup)
            attachments.append({
                "lesson_id": lesson_id,
                "attachment_token": attachment_token,
                "url": urljoin(PERFIL_BASE, href),
                "label": att_anchor.get_text(" ", strip=True) or "anexo",
            })
        return attachments

    def fetch_lesson_details(
        self,
        lesson: Dict[str, Any],
        course_slug: str,
        course_id: str,
        module_id: str,
    ) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")

        content = LessonContent()
        enrolment_id = self._course_enrolment.get(str(course_id)) or self._enrolment_id_from_slug(course_slug)
        if not enrolment_id:
            logger.warning("Não foi possível determinar enrolment_id para o curso %s.", course_id)
            return content

        lesson_meeting_id = lesson.get("lesson_meeting_id") or lesson.get("id")
        subject_id = lesson.get("subject_id")
        lesson_url = lesson.get("lesson_url") or (
            f"{PERFIL_BASE}/vle/{enrolment_id}/{course_id}/lesson"
            f"?discipline_id={subject_id}&id={lesson_meeting_id}"
        )
        response = self._session.get(
            lesson_url,
            headers={"Referer": f"{PERFIL_BASE}/vle/{enrolment_id}/{course_id}/overview"},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        spalla_uuid = self._extract_spalla_uuid(soup)
        if spalla_uuid:
            content.videos.append(Video(
                video_id=spalla_uuid,
                url=SPALLA_PLAYER_URL.format(uuid=spalla_uuid),
                order=lesson.get("order", 1),
                title=lesson.get("title", "Aula"),
                size=0,
                duration=0,
                extra_props={
                    "spalla_uuid": spalla_uuid,
                    "origin_referer": f"{PERFIL_BASE}/",
                    "referer": f"{PERFIL_BASE}/",
                },
            ))
        else:
            logger.debug("Aula %s sem iframe Spalla.", lesson.get("title"))

        attachments_seen: set[str] = set()
        order_counter = 1
        for ref in lesson.get("attachment_refs") or []:
            attachment = self._build_attachment(ref, order_counter)
            if attachment and attachment.attachment_id not in attachments_seen:
                attachments_seen.add(attachment.attachment_id)
                content.attachments.append(attachment)
                order_counter += 1

        pattern = re.compile(
            rf"/meus-cursos/{re.escape(enrolment_id)}/lessons/(\d+)/attachments/([^?\"']+)"
        )
        for att_anchor in soup.select("a[href*='/attachments/']"):
            href = att_anchor.get("href", "")
            match = pattern.search(href)
            if not match:
                continue
            ref = {
                "lesson_id": match.group(1),
                "attachment_token": match.group(2),
                "url": urljoin(PERFIL_BASE, href),
                "label": att_anchor.get_text(" ", strip=True) or "anexo",
            }
            attachment = self._build_attachment(ref, order_counter)
            if attachment and attachment.attachment_id not in attachments_seen:
                attachments_seen.add(attachment.attachment_id)
                content.attachments.append(attachment)
                order_counter += 1

        return content

    @staticmethod
    def _enrolment_id_from_slug(course_slug: str) -> Optional[str]:
        if not course_slug:
            return None
        head, _, _ = course_slug.partition("-")
        return head or None

    @staticmethod
    def _extract_spalla_uuid(soup: BeautifulSoup) -> Optional[str]:
        for iframe in soup.select("iframe[src]"):
            src = iframe.get("src", "")
            if "spalla.io/player" not in src:
                continue
            match = re.search(r"video=([0-9a-fA-F-]{32,})", src)
            if match:
                return match.group(1)
        match = re.search(r"spalla\.io/player/?\?video=([0-9a-fA-F-]{32,})", str(soup))
        return match.group(1) if match else None

    def _build_attachment(self, ref: Dict[str, str], order: int) -> Optional[Attachment]:
        token = ref.get("attachment_token") or ""
        lesson_id = ref.get("lesson_id") or ""
        if not token:
            return None
        attachment_id = f"{lesson_id}-{token}"
        label = ref.get("label") or f"anexo-{order}"
        filename = self._guess_attachment_filename(token, lesson_id, label)
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return Attachment(
            attachment_id=attachment_id,
            url=ref.get("url") or "",
            filename=filename,
            order=order,
            extension=extension,
            size=0,
        )

    @staticmethod
    def _guess_attachment_filename(token: str, lesson_id: str, label: str) -> str:
        token_clean = token.replace("%0A", "").replace("%3D", "=")
        safe_label = re.sub(r"[\\/:*?\"<>|]+", "-", label).strip() or "anexo"
        if "questions" in token_clean.lower() or "question" in safe_label.lower():
            return f"{lesson_id}-questoes.pdf"
        if "examination" in token_clean.lower() or "simulado" in safe_label.lower():
            return f"{lesson_id}-simulado.pdf"
        return f"{safe_label}.pdf"

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
            logger.error("Anexo sem URL: %s", attachment.filename)
            return False

        try:
            response = self._session.get(
                attachment.url,
                headers={"Referer": f"{PERFIL_BASE}/vle"},
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()

            disposition = response.headers.get("Content-Disposition", "")
            filename_match = re.search(r'filename="?([^";]+)"?', disposition)
            if filename_match:
                suggested = filename_match.group(1).strip()
                if suggested:
                    resolved_path = download_path.with_name(suggested)
                    download_path = resolved_path

            parsed_path = Path(urlparse(response.url).path)
            if not download_path.suffix and parsed_path.suffix:
                download_path = download_path.with_suffix(parsed_path.suffix)

            download_path.parent.mkdir(parents=True, exist_ok=True)
            with open(download_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        handle.write(chunk)
            return True
        except Exception as exc:
            logger.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("AlfaCon", AlfaConPlatform)
