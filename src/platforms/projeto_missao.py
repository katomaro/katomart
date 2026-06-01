from __future__ import annotations
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.app.api_service import ApiService
from src.app.models import Attachment, LessonContent
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

BASE_URL = "https://igestor.projetomissao.com.br"
LOGIN_URL = f"{BASE_URL}/Account/Login"
SISTEMA_URL = f"{BASE_URL}/sistema/"
MISSOES_URL = f"{BASE_URL}/sistema/MinhasMissoes"
DOWNLOAD_URL = f"{BASE_URL}/sistema/downloadFile"

MODULE_TYPES = ["RM", "LM", "RF", "EV", "VM", "MT"]


class ProjetoMissaoPlatform(BasePlatform):
    """Implements the Projeto Missão (igestor.projetomissao.com.br) platform.

    Backend is ASP.NET WebForms: login is form-based with VIEWSTATE/
    EVENTVALIDATION tokens, and the in-app data endpoints are AJAX POSTs
    with `frmigm`/`IND_ACAO` form fields returning JSON.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)

    @classmethod
    def all_auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="token",
                label="ASP.NET_SessionId (opcional)",
                field_type=AuthFieldType.TEXT,
                placeholder="Cole o valor do cookie ASP.NET_SessionId",
                required=False,
            ),
            AuthField(
                name="username",
                label="E-mail",
                placeholder="Digite o e-mail cadastrado no Projeto Missão",
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
Assinantes (R$ 9.90): informe e-mail e senha — o sistema fará o login automaticamente.

Para usuários gratuitos (apenas cookie de sessão):
1) Acesse https://igestor.projetomissao.com.br e faça login.
2) Abra as Ferramentas de Desenvolvedor (F12) → Aplicação/Armazenamento → Cookies.
3) Copie o valor do cookie chamado 'ASP.NET_SessionId'.
4) Cole o valor no campo acima. O cookie expira rapidamente; renove quando o login pedir.
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
            session.cookies.set("ASP.NET_SessionId", token, domain="igestor.projetomissao.com.br")
            if not self._session_is_valid():
                raise ConnectionError(
                    "Cookie ASP.NET_SessionId inválido ou expirado. Faça login novamente."
                )
        else:
            raise ValueError("Informe e-mail/senha ou o cookie ASP.NET_SessionId.")

        logging.info("Sessão autenticada no Projeto Missão.")

    def _login_with_credentials(self, username: str, password: str) -> None:
        get_resp = self._session.get(LOGIN_URL)
        get_resp.raise_for_status()
        soup = BeautifulSoup(get_resp.text, "html.parser")

        def hidden(name: str) -> str:
            tag = soup.find("input", {"name": name})
            return tag.get("value", "") if tag else ""

        form_data = {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": hidden("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": hidden("__VIEWSTATEGENERATOR"),
            "__EVENTVALIDATION": hidden("__EVENTVALIDATION"),
            "ctl00$MainContent$UserName": username,
            "ctl00$MainContent$Password": password,
            "ctl00$MainContent$Button1": "Entrar",
        }

        if not form_data["__VIEWSTATE"]:
            raise ConnectionError("Não foi possível ler __VIEWSTATE da página de login.")

        post_resp = self._session.post(
            LOGIN_URL,
            data=form_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": LOGIN_URL,
            },
            allow_redirects=False,
        )

        if post_resp.status_code != 302 or "/sistema" not in post_resp.headers.get("Location", ""):
            error_msg = self._extract_login_error(post_resp.text)
            raise ConnectionError(
                f"Falha no login do Projeto Missão. {error_msg}".strip()
            )

        follow = self._session.get(urljoin(BASE_URL, post_resp.headers["Location"]))
        follow.raise_for_status()

    def _extract_login_error(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        err = soup.find(class_=re.compile("error|alert|validation", re.IGNORECASE))
        if err and err.get_text(strip=True):
            return err.get_text(strip=True)
        return "Verifique e-mail/senha."

    def _session_is_valid(self) -> bool:
        try:
            resp = self._ajax_post(SISTEMA_URL, {"frmigm": "getMenu", "IND_ACAO": "INICIO"})
            data = resp.json()
            return bool(data.get("TXT_LOGIN"))
        except Exception:
            return False

    def _ajax_post(self, url: str, data: Dict[str, str]) -> requests.Response:
        if not self._session:
            raise ConnectionError("A sessão não está autenticada.")
        resp = self._session.post(
            url,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": url,
            },
        )
        resp.raise_for_status()
        return resp

    def fetch_courses(self) -> List[Dict[str, Any]]:
        resp = self._ajax_post(MISSOES_URL, {"IND_ACAO": "INICIO", "frmigm": "getInicio"})
        data = resp.json()
        logging.debug("Projeto Missão projects payload: %s", data)

        courses: List[Dict[str, Any]] = []
        for item in data.get("OBJ_CARREIRA_PRJ", []):
            prj_id = item.get("ID_PRJ")
            if not prj_id:
                continue
            courses.append(
                {
                    "id": prj_id,
                    "name": item.get("NOM_PRJ", "Missão"),
                    "slug": str(prj_id),
                    "seller_name": item.get("DES_CARREIRA", "Projeto Missão"),
                    "_status": item.get("STATUS", "1"),
                }
            )
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            if not course_id:
                continue

            available_types = self._discover_module_types(str(course_id))

            disciplines: Dict[str, Dict[str, Any]] = {}

            for tpmod in available_types:
                try:
                    resp = self._ajax_post(
                        MISSOES_URL,
                        {
                            "IND_ACAO": "GET_MOD_PED",
                            "ID_PRJ": str(course_id),
                            "TPMOD": tpmod,
                            "frmigm": "getContPed",
                        },
                    )
                    payload = resp.json()
                except Exception as exc:
                    logging.warning(
                        "Falha ao obter módulo %s do projeto %s: %s", tpmod, course_id, exc
                    )
                    continue

                logging.debug(
                    "Projeto Missão GET_MOD_PED %s/%s: %s itens",
                    course_id,
                    tpmod,
                    len(payload.get("OBJ_CONT_PEDAGOGICO", [])),
                )

                if str(payload.get("IND_SEM_ACESSO", "0")) == "1":
                    continue

                for entry in payload.get("OBJ_CONT_PEDAGOGICO", []):
                    file_id = entry.get("ID_ARQUIVO")
                    if not file_id:
                        continue

                    discipline_key = (
                        f"{tpmod}-{entry.get('ID_DIS_GRUPO_PRJ', '0')}"
                        f"-{entry.get('NOM_DISCIPLINA', 'Disciplina')}"
                    )
                    module = disciplines.setdefault(
                        discipline_key,
                        {
                            "id": discipline_key,
                            "title": f"[{tpmod}] {entry.get('NOM_DISCIPLINA', 'Disciplina')}",
                            "order": len(disciplines) + 1,
                            "lessons": [],
                            "locked": False,
                        },
                    )

                    module["lessons"].append(
                        {
                            "id": str(file_id),
                            "title": entry.get("NOM_ARQUIVO", f"Aula {file_id}"),
                            "order": len(module["lessons"]) + 1,
                            "locked": False,
                            "_tpmod": tpmod,
                            "_link": entry.get("LINK", ""),
                            "_pages": entry.get("QTD_PAG_ARQUIVO", 0),
                        }
                    )

            course_with_modules = course.copy()
            course_with_modules["modules"] = list(disciplines.values())
            course_with_modules["title"] = course.get("name", "Missão")
            all_content[course_id] = course_with_modules

        return all_content

    def _discover_module_types(self, course_id: str) -> List[str]:
        try:
            resp = self._ajax_post(
                MISSOES_URL,
                {"IND_ACAO": "GET_PRJ", "ID_PRJ": course_id, "frmigm": "getPrj"},
            )
            payload = resp.json()
        except Exception as exc:
            logging.warning("Falha ao consultar GET_PRJ %s: %s", course_id, exc)
            return list(MODULE_TYPES)

        projeto = (payload.get("OBJ_PROJETO") or [{}])[0]
        types: List[str] = []
        for tpmod in MODULE_TYPES:
            flag = projeto.get(f"IND_ACESSO_{tpmod}")
            if flag is None or str(flag) != "0":
                types.append(tpmod)
        return types or list(MODULE_TYPES)

    def fetch_lesson_details(
        self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str
    ) -> LessonContent:
        content = LessonContent()

        link = lesson.get("_link") or ""
        if not link:
            logging.warning("Aula sem LINK no Projeto Missão: %s", lesson.get("title"))
            return content

        filename = f"{lesson.get('title', 'aula')}.pdf"

        content.attachments.append(
            Attachment(
                attachment_id=str(lesson.get("id")),
                url=link,
                filename=filename,
                order=lesson.get("order", 1),
                extension="pdf",
                size=0,
            )
        )
        return content

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

        link = attachment.url or ""
        if not link:
            logging.error("Anexo sem URL: %s", attachment.filename)
            return False

        if link.startswith("http"):
            full_url = link
        else:
            # The server returns a relative link (e.g. "downloadFile?a=CPED&f=...").
            # In the browser this resolves against the page URL
            # ".../sistema/MinhasMissoes" (no trailing slash), yielding
            # ".../sistema/downloadFile?...". urljoin against MISSOES_URL without
            # an added trailing slash reproduces that. Appending "/" here would
            # treat "MinhasMissoes" as a directory and hit the MinhasMissoes
            # controller instead, which returns an HTML page full of scripts.
            full_url = urljoin(MISSOES_URL, link)

        try:
            resp = self._session.get(
                full_url,
                stream=True,
                headers={"Referer": MISSOES_URL},
            )
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                logging.error(
                    "Download de %s retornou HTML (%s) em vez do arquivo — "
                    "sessão expirada ou URL incorreta: %s",
                    attachment.filename,
                    content_type,
                    full_url,
                )
                return False

            with open(download_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    fh.write(chunk)
            return True
        except Exception as exc:
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Projeto Missão", ProjetoMissaoPlatform)
