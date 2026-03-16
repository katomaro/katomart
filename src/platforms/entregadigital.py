from typing import Any, Dict, List, Optional
from pathlib import Path
from urllib.parse import urlparse
import requests
import logging

from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.app.models import LessonContent, Description, Video, Attachment
from src.config.settings_manager import SettingsManager
from src.app.api_service import ApiService


class EntregaDigitalPlatform(BasePlatform):
    """Implements scraping logic for EntregaDigital whitelabel platforms."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._api_base: str = ""
        self._site_url: str = ""
        self._app_version: str = ""
        self._device_id: str = ""
        self._os_value: str = ""

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="site_url",
                label="URL do Site",
                field_type=AuthFieldType.TEXT,
                placeholder="https://exemplo.entregadigital.app.br",
                required=True,
            ),
            AuthField(
                name="app_version",
                label="App Version",
                field_type=AuthFieldType.TEXT,
                placeholder="?.??.?",
                required=True,
            ),
            AuthField(
                name="device_id",
                label="Device ID",
                field_type=AuthFieldType.TEXT,
                placeholder="???-???-???-???",
                required=True,
            ),
            AuthField(
                name="os_field",
                label="OS",
                field_type=AuthFieldType.TEXT,
                placeholder="Web",
                required=True,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Entrega Digital - Como obter os headers (F12 bloqueado):

1) Use o Google Chrome. Feche TODAS as outras abas mantendo apenas a aba da plataforma aberta..
2) Deslogue da plataforma.
3) Abra uma nova aba e acesse chrome://net-export/
4) Marque a opcao "Include raw bytes" e clique em "Start Logging to Disk".
5) Volte na aba da plataforma, atualize (F5) e faca login normalmente.
6) Apos logar, volte na aba do net-export e clique em "Stop Logging".
7) Abra o arquivo gerado como texto (Bloco de Notas) e use Ctrl+F para buscar:
   - app-version → copie o valor e cole no campo App Version
   - device-id → copie o valor e cole no campo Device ID
   - os → copie o valor e cole no campo OS (geralmente "Web")
8) Informe a URL do site (ex: https://exemplo.entregadigital.app.br).
9) Preencha e-mail e senha, ou copie o token Authorization (sem "Bearer ") para o campo Token.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials

        site_url = (credentials.get("site_url") or "").strip().rstrip("/")
        if not site_url:
            raise ValueError("A URL do site Entrega Digital e obrigatoria.")

        self._site_url = site_url
        self._api_base = self._derive_api_base(site_url)

        self._app_version = (credentials.get("app_version") or "").strip()
        self._device_id = (credentials.get("device_id") or "").strip()
        self._os_value = (credentials.get("os_field") or "").strip()

        if not self._app_version or not self._device_id or not self._os_value:
            raise ValueError("Os campos App Version, Device ID e OS sao obrigatorios.")

        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _derive_api_base(self, site_url: str) -> str:
        """Derives the API base URL from the site URL.

        Example: https://exemplo.entregadigital.app.br
              -> https://api-exemplo.entregadigital.app.br/api/v1/app
        """
        parsed = urlparse(site_url)
        api_host = f"api-{parsed.netloc}"
        return f"{parsed.scheme}://{api_host}/api/v1/app"

    def _configure_session(self, token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "app-version": self._app_version,
            "device-id": self._device_id,
            "os": self._os_value,
            "Origin": self._site_url,
            "Referer": f"{self._site_url}/",
        })

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        """Logs in via the EntregaDigital API and returns the bearer token."""
        login_payload = {
            "email": username,
            "password": password,
            "type": "PWA",
        }
        headers = {
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "app-version": self._app_version,
            "device-id": self._device_id,
            "os": self._os_value,
        }

        resp = requests.post(f"{self._api_base}/login", json=login_payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        token = data.get("api_token")
        if not token:
            raise ConnectionError("Login nao retornou api_token. Verifique as credenciais.")
        return token

    def get_session(self) -> Optional[requests.Session]:
        return self._session

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        response = self._session.get(f"{self._api_base}/products")
        response.raise_for_status()
        data = response.json()

        products = data if isinstance(data, list) else data.get("data", data.get("products", []))

        courses = []
        for product in products:
            pid = product.get("id")
            if pid:
                courses.append({
                    "id": pid,
                    "name": product.get("name", "Sem Nome"),
                    "slug": str(pid),
                })
        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        all_content = {}
        for course in courses:
            course_id = course["id"]
            try:
                response = self._session.get(f"{self._api_base}/products/{course_id}")
                response.raise_for_status()
                product_data = response.json()

                processed_modules = []
                for module_data in product_data.get("modules", []):
                    lessons = []
                    for lesson_data in module_data.get("lessons", []):
                        lessons.append({
                            "id": lesson_data.get("id"),
                            "name": lesson_data.get("name", "Sem Nome"),
                            "title": lesson_data.get("name", "Sem Nome"),
                        })

                    processed_modules.append({
                        "id": module_data.get("id"),
                        "name": module_data.get("name", "Modulo"),
                        "title": module_data.get("name", "Modulo"),
                        "order": module_data.get("order", 0),
                        "lessons": lessons,
                    })

                all_content[course_id] = {
                    "id": course_id,
                    "name": product_data.get("name", "Sem Nome"),
                    "title": product_data.get("name", "Sem Nome"),
                    "modules": processed_modules,
                }
            except Exception as e:
                logging.error(f"Erro ao buscar conteudo do produto {course_id}: {e}")

        return all_content

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        lesson_id = lesson.get("id")
        if not lesson_id:
            raise ValueError("ID da aula nao encontrado.")

        response = self._session.get(f"{self._api_base}/lessons/{lesson_id}/auth")
        response.raise_for_status()
        data = response.json()

        content = LessonContent()

        if description := data.get("description"):
            content.description = Description(text=description, description_type="html")

        # Video extraction: panda > panda-live > root video_player > action URL
        detail = data.get("detail") or {}
        panda = detail.get("panda") or {}
        panda_live = detail.get("panda-live") or {}
        action = data.get("action") or {}

        video_url = (
            panda.get("video_hls")
            or panda.get("video_player")
            or panda_live.get("video_hls")
            or panda_live.get("video_player")
            or data.get("video_player")
        )

        if not video_url:
            action_url = action.get("url", "")
            if action_url and ("pandavideo" in action_url or "player-vz" in action_url):
                video_url = action_url

        if video_url:
            extra_props = {"referer": f"{self._site_url}/"}
            action_url = action.get("url", "")
            if action_url:
                extra_props["action_url"] = action_url

            content.videos.append(Video(
                video_id=str(lesson_id),
                url=video_url,
                title=lesson.get("name", "Aula"),
                order=1,
                size=0,
                duration=0,
                extra_props=extra_props,
            ))

        for att_idx, att in enumerate(data.get("attachments", []), start=1):
            file_url = att.get("url", "")
            filename = att.get("name", att.get("filename", "arquivo"))
            extension = filename.split(".")[-1] if "." in filename else ""
            content.attachments.append(Attachment(
                attachment_id=str(att.get("id", att_idx)),
                url=file_url,
                filename=filename,
                order=att_idx,
                extension=extension,
                size=att.get("size", 0),
            ))

        return content

    def download_attachment(self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        if not self._session:
            raise ConnectionError("Sessao nao autenticada.")

        try:
            response = self._session.get(attachment.url, stream=True)
            response.raise_for_status()
            with open(download_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception as e:
            logging.error(f"Erro ao baixar anexo '{attachment.filename}': {e}")
            return False


PlatformFactory.register_platform("Entrega Digital", EntregaDigitalPlatform)
