from __future__ import annotations
import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)

LOGIN_URL = "https://app-api.kirvano.com/users/login/otp"
REFRESH_URL = "https://app-api.kirvano.com/users/refresh-token"
PURCHASES_LIST_URL = "https://app-api.kirvano.com/purchases"
PURCHASE_DETAILS_URL = "https://app-api.kirvano.com/purchases/{uuid}"

MEMBERS_API_BASE = "https://members-api.kirvano.com/v1"
MODULES_URL = MEMBERS_API_BASE + "/courses/{course_uuid}/lessons/modules"
CONTENTS_URL = MEMBERS_API_BASE + "/courses/{course_uuid}/lessons/modules/{module_uuid}/contents"
RESOURCES_URL = MEMBERS_API_BASE + "/courses/{course_uuid}/lessons/modules/{module_uuid}/contents/{content_uuid}/resources"

class KirvanoPlatform(BasePlatform):
    """
    Implements the Kirvano platform integration.
    Uses app-api.kirvano.com for auth and purchases,
    and members-api.kirvano.com for course content.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Assinantes (R$ 9.90) ativos podem informar usuário/senha. O sistema irá trocar essas credenciais automaticamente pelo token da etapa acima, além de usar alguns algoritmos melhores e ter funcionalidades extras na aplicação, e obter suporte prioritário.

Para usuários gratuitos: Como obter o token da Kirvano?:
1) Abra o seu navegador e vá para https://app.kirvano.com.
2) Abra as Ferramentas de Desenvolvedor (F12) → aba Rede (também pode ser chamada de Requisições ou Network).
3) Faça o login normalmente sem fechar essa aba e aguarde aparecer a lista de produtos da conta.
4) Use a lupa para procurar a URL "https://app-api.kirvano.com/users/login/otp".
5) Clique nessa requisição que tenha o indicativo POST e vá para a aba RESPOSTA (Response), em requisição lá em baixo.
6) Copie o valor da linha 'token' sem manter as aspas— ele se parece com 'ey........'. Cole-o aqui.
"""

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        email = credentials.get("username")
        password = credentials.get("password")

        if not email or not password:
            token = credentials.get("token")
            if token:
                self._configure_session(token)
                return
            raise ValueError("Email e senha são obrigatórios.")

        try:
            payload = {
                "email": email,
                "password": password,
                "source": "app-web",
                "fingerprint": None
            }
            response = requests.post(
                LOGIN_URL,
                json=payload,
                headers={"User-Agent": self._settings.user_agent},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            
            token = data.get("token")
            self.refresh_token = data.get("refreshToken")
            
            if not token:
                raise ValueError("Token não retornado na autenticação.")
                
            self.credentials = credentials
            self._configure_session(token)
            
        except Exception as e:
            logger.error("Falha na autenticação Kirvano: %s", e)
            raise ConnectionError(f"Falha ao autenticar: {e}")

    def refresh_auth(self) -> None:
        """
        Refreshes the authentication session using the refresh token if available.
        Otherwise falls back to full re-authentication.
        """
        if self.refresh_token:
            try:
                logger.info("Tentando renovar token via refresh_token...")
                payload = {"refreshToken": self.refresh_token}
                
                headers = {
                    "User-Agent": self._settings.user_agent,
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                }

                if self.access_token:
                    headers["Authorization"] = f"Bearer {self.access_token}"

                response = requests.post(
                    REFRESH_URL,
                    json=payload,
                    headers=headers,
                    timeout=30
                )

                if response.status_code == 200:
                    data = response.json()
                    new_token = data.get("token")
                    new_refresh = data.get("refreshToken")

                    if new_token:
                        self.access_token = new_token
                        if new_refresh:
                            self.refresh_token = new_refresh
                        self._configure_session(new_token)
                        logger.info("Token renovado com sucesso via refresh_token.")
                        return
                else:
                    logger.warning(
                        "Falha ao renovar token (Status %s): %s", 
                        response.status_code, 
                        response.text
                    )
            except Exception as e:
                logger.warning("Erro ao tentar renovar via refresh_token: %s", e)

        logger.info("Fallback: Executando re-autenticação padrão.")
        super().refresh_auth()

    def _configure_session(self, token: str) -> None:
        self.access_token = token
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://app.kirvano.com",
            "Referer": "https://app.kirvano.com/",
            "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "priority": "u=1, i",
        })

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """
        Fetches the list of available courses (purchases) from app-api.
        """
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        courses = []
        page = 1

        while True:
            try:
                url = f"{PURCHASES_LIST_URL}?page={page}&pageSize=99"
                response = self._session.get(url)
                response.raise_for_status()
                data = response.json()

                items = data.get("data", [])
                if not items:
                    break

                for item in items:
                    purchase_uuid = item.get("uuid")
                    product_name = item.get("product", "Produto sem nome")
                    cover_url = item.get("photo")

                    seller_name = "Desconhecido"
                    course_uuid = None
                    try:
                        detail_url = PURCHASE_DETAILS_URL.format(uuid=purchase_uuid)
                        detail_resp = self._session.get(detail_url)
                        if detail_resp.ok:
                            detail_data = detail_resp.json()
                            seller_name = detail_data.get("sellerName", "Desconhecido")
                            course_uuid = detail_data.get("courseUuid") or detail_data.get("productUuid")
                    except Exception:
                        pass

                    courses.append({
                        "id": purchase_uuid,
                        "name": product_name,
                        "title": product_name,
                        "cover_url": cover_url,
                        "seller_name": seller_name,
                        "course_uuid": course_uuid 
                    })

                meta = data.get("meta", {})
                current_page = meta.get("page", page)
                total_pages = meta.get("pages", 1)
                
                if current_page >= total_pages or len(items) == 0:
                    break
                page += 1

            except Exception as e:
                logger.error("Erro ao buscar cursos: %s", e)
                break

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Enriches the course list with modules and lessons structure.
        Uses members-api.
        """
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            purchase_uuid = course.get("id")
            course_name = course.get("name")

            try:
                course_uuid = course.get("course_uuid")
                if not course_uuid:
                    response = self._session.get(PURCHASE_DETAILS_URL.format(uuid=purchase_uuid))
                    response.raise_for_status()
                    detail_data = response.json()
                    course_uuid = detail_data.get("courseUuid") or detail_data.get("productUuid") 

                if not course_uuid:
                    logger.warning("Não foi possível encontrar courseUuid para compra %s", purchase_uuid)
                    continue
                modules = self._fetch_modules(course_uuid)

                processed_modules = []
                for mod in modules:
                    mod_uuid = mod.get("uuid")
                    mod_name = mod.get("name", "Módulo")
                    mod_order = mod.get("order", 0)
                    
                    lessons_data = self._fetch_lessons(course_uuid, mod_uuid)
                    
                    lessons = []
                    for less in lessons_data:
                        less_uuid = less.get("uuid")
                        less_name = less.get("name") or less.get("title") or "Aula"
                        less_order = less.get("order", 0)
                        
                        lessons.append({
                            "id": less_uuid,
                            "title": less_name,
                            "order": less_order,
                            "locked": not less.get("available", True),
                            "videoUrl": less.get("videoUrl"),
                            "description": less.get("description"),
                            "course_uuid": course_uuid,
                            "module_uuid": mod_uuid,
                            "student_uuid": less.get("studentUuid") or less.get("enrollmentUuid"), # Tentativa de captura antecipada
                        })

                    processed_modules.append({
                        "id": mod_uuid,
                        "title": mod_name,
                        "order": mod_order,
                        "lessons": lessons,
                        "locked": False
                    })

                course_entry = course.copy()
                course_entry["modules"] = processed_modules
                course_entry["course_uuid"] = course_uuid

                all_content[str(purchase_uuid)] = course_entry

            except Exception as e:
                logger.error("Erro ao processar conteúdo do curso %s: %s", course_name, e)
                continue

        return all_content

    def _fetch_modules(self, course_uuid: str) -> List[Dict[str, Any]]:
        modules = []
        page = 1
        while True:
            url = f"{MODULES_URL.format(course_uuid=course_uuid)}?page={page}&pageSize=99"
            response = self._session.get(url)
            response.raise_for_status()
            data = response.json()

            items = data.get("data", [])
            if not items:
                break
            modules.extend(items)

            meta = data.get("meta", {})
            if meta.get("page", page) >= meta.get("pages", 1):
                break
            page += 1
        return modules

    def _fetch_lessons(self, course_uuid: str, module_uuid: str) -> List[Dict[str, Any]]:
        lessons = []
        page = 1
        while True:
            url = f"{CONTENTS_URL.format(course_uuid=course_uuid, module_uuid=module_uuid)}?page={page}&pageSize=99"
            response = self._session.get(url)
            response.raise_for_status()
            data = response.json()

            items = data.get("data", [])
            if not items:
                break
            lessons.extend(items)

            meta = data.get("meta", {})
            if meta.get("page", page) >= meta.get("pages", 1):
                break
            page += 1
        return lessons

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        """
        Marks a lesson as watched or unwatched.
        """
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        lesson_id = lesson.get("id")
        course_uuid = lesson.get("course_uuid")
        
        # Tentativa de recuperar student_uuid que pode ter vindo na lista de aulas
        student_uuid = lesson.get("student_uuid")

        if not student_uuid:
            # Fallback: Tentar descobrir o student_uuid consultando a API, se possível
            # URL hipotética baseada no padrão REST da Kirvano: /courses/{course}/current-student
            # Mas como não temos certeza, vamos tentar pegar do token, ou de um endpoint de "me"
            
            # Decodificando token apenas para pegar o 'sub', caso seja o student_id (testamos e não era, mas...)
            # O melhor aqui é tentar obter via um endpoint de "quem sou eu no curso"
            # Vamos tentar listar 'viewers' ou 'students/me'.
            try:
                # Tentativa 1: Endpoint viewer/student do curso
                url = f"{MEMBERS_API_BASE}/courses/{course_uuid}/students/me"
                resp = self._session.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    student_uuid = data.get("uuid") or data.get("id")
            except Exception:
                pass

        if not student_uuid:
            # Se ainda não temos, tentamos uma abordagem de "adivinhação" ou logamos erro
            # O HAR mostrava um ID diferente do SUB do token.
            # Vamos tentar usar o SUB do token como fallback, talvez funcione em alguns casos?
            # Ou logar erro.
            logger.warning("Não foi possível identificar o ID do aluno (student_uuid) para marcar aula na Kirvano.")
            return

        url = f"{MEMBERS_API_BASE}/courses/{course_uuid}/students/{student_uuid}/{lesson_id}"
        
        payload = {"isWatched": watched}
        
        # Headers específicos vistos no HAR
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://app.kirvano.com",
            "Referer": "https://app.kirvano.com/",
        }
        
        response = self._session.patch(url, json=payload, headers=headers)
        if response.status_code not in (200, 202, 204):
            logger.error(f"Erro ao atualizar status da aula {lesson_id}: {response.status_code} - {response.text}")
            response.raise_for_status()
        
        logger.info(f"Aula {lesson_id} marcada como {'assistida' if watched else 'não assistida'} com sucesso.")

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        """
        Fetches full details including video URL and attachments.
        """
        content = LessonContent()

        video_url = lesson.get("videoUrl")
        description_text = lesson.get("description", "")

        lesson_uuid = lesson.get("id")
        course_uuid = lesson.get("course_uuid")
        module_uuid = lesson.get("module_uuid")

        if not course_uuid:
             pass

        if description_text:
            content.description = Description(text=description_text, description_type="html")

        if video_url:
            content.videos.append(
                Video(
                    video_id=lesson_uuid,
                    url=video_url,
                    order=1,
                    title=lesson.get("title", "Video"),
                    size=0,
                    duration=0,
                    extra_props={
                        "referer": "https://app.kirvano.com/"
                    }
                )
            )

        if course_uuid and module_uuid and lesson_uuid:
            try:
                resources = self._fetch_resources(course_uuid, module_uuid, lesson_uuid)
                for res in resources:
                    res_type = res.get("type", "").upper()
                    
                    url = None
                    file_prop = res.get("file")
                    if isinstance(file_prop, str) and file_prop.startswith("http"):
                        url = file_prop
                    elif isinstance(file_prop, dict) and file_prop.get("url"):
                        url = file_prop.get("url")
                    elif res.get("url"):
                        url = res.get("url")

                    if url:
                        name = res.get("title") or res.get("name") or "Anexo"
                        res_uuid = res.get("uuid")
                        order = res.get("order", 1)
                        extension = ""
                        path_name = url.split("?")[0].split("/")[-1]
                        if "." in path_name:
                             extension = path_name.split(".")[-1]

                        if not extension and "." in name:
                             extension = name.split(".")[-1]

                        if not extension:
                             mime_type = res.get("mimeType")
                             if mime_type:
                                 guessed = mimetypes.guess_extension(mime_type)
                                 if guessed:
                                     extension = guessed.lstrip(".")

                        size = res.get("size", 0)

                        content.attachments.append(
                            Attachment(
                                attachment_id=str(res_uuid),
                                url=url,
                                filename=name,
                                order=order,
                                extension=extension,
                                size=size
                            )
                        )
            except Exception as e:
                logger.warning("Falha ao buscar anexos para aula %s: %s", lesson_uuid, e)
        return content

    def _fetch_resources(self, course_uuid: str, module_uuid: str, content_uuid: str) -> List[Dict[str, Any]]:
        resources = []
        page = 1
        while True:
            url = f"{RESOURCES_URL.format(course_uuid=course_uuid, module_uuid=module_uuid, content_uuid=content_uuid)}?page={page}&pageSize=99"
            response = self._session.get(url)
            if response.status_code == 404:
                return []
            response.raise_for_status()
            data = response.json()
            items = data.get("data", [])
            if not items:
                break
            resources.extend(items)
            meta = data.get("meta", {})
            if meta.get("page", page) >= meta.get("pages", 1):
                break
            page += 1
        return resources

    def download_attachment(self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        """Downloads an attachment using the authenticated session."""
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")
        try:
            download_url = attachment.url
            if not download_url:
                logger.error("Anexo sem URL disponível: %s", attachment.filename)
                return False
            logger.info("Baixando anexo %s de %s", attachment.filename, download_url)
            is_signed = "amazonaws.com" in download_url or "Signature=" in download_url

            if is_signed:
                clean_session = requests.Session()
                clean_session.headers.update({"User-Agent": self._settings.user_agent})
                response = clean_session.get(download_url, stream=True)
            else:
                response = self._session.get(download_url, stream=True)

            response.raise_for_status()

            with open(download_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    file_handle.write(chunk)
            return True
        except Exception as exc:
            logger.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False

PlatformFactory.register_platform("Kirvano", KirvanoPlatform)
