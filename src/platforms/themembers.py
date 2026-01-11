from typing import Any, Dict, List, Optional
import requests
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.app.models import LessonContent, Description, AuxiliaryURL, Video, Attachment
from src.app.api_service import ApiService
from src.config.settings_manager import SettingsManager

class TheMembersPlatform(BasePlatform):
    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self.domain = ""
        self.tenant_id = ""
        self.organization_id = ""
        self.api_base = "https://api.themembers.com.br/api"
        self.backend_base = "https://backend.themembers.dev.br/api/platform"

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="domain",
                label="Domínio (ex: app.exemplo.com) sem / extra!",
                placeholder="Domínio da área de membros sem / extra",
                required=True
            )
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """
INFORME O DOMINIO DO SITE SEM NENHUM /LOGIN (exemplo: https://app.exemplo.com)
junto com seu e-mail e senha. Ou então, o TOKEN Authorization.
Para obter o token:
1) Abra o seu navegador e vá para a página de Login
2) Abra as Ferramentas de Desenvolvedor (F12) → aba Rede (também pode ser chamada de Requisições ou Network).
3) Faça o login normalmente sem fechar essa aba e aguarde aparecer a lista de produtos da conta.
4) Use a lupa para procurar a URL "https://api.themembers.com.br/api/".
5) Clique em qualquer requisição que tenha o indicativo GET ou POST e vá para a aba Headers (Cabeçalhos), em requisição lá em baixo.
6) Copie o valor do cabeçalho 'Authorization' — ele se parece com 'Bearer <token>'. Cole apenas a parte do token aqui.
"""

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.domain = credentials.get("domain", "").replace("https://", "").replace("http://", "").strip().rstrip("/")
        username = credentials.get("username")
        password = credentials.get("password")
        if not self.domain or not username or not password:
            raise ValueError("Domínio, email e senha são obrigatórios.")
        resolve_url = f"{self.api_base}/getTenant?domain={self.domain}"
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": f"https://{self.domain}",
            "Referer": f"https://{self.domain}/"
        })

        resp_resolve = self._session.get(resolve_url)
        if resp_resolve.status_code != 200:
            raise ValueError(f"Falha ao identificar o domínio: {resp_resolve.status_code}")

        try:
            tenant_data = resp_resolve.json()
            self.tenant_id = str(tenant_data["tenant"]["id"])
            self.organization_id = str(tenant_data["tenant"]["organization_id"])
        except (KeyError, ValueError) as e:
            raise ValueError(f"Erro ao processar dados do domínio: {e}")

        login_url = f"{self.api_base}/auth/login"
        payload = {
            "email": username,
            "password": password,
            "tenant_id": int(self.tenant_id)
        }

        self._session.headers.update({
            "x-platform-id": self.tenant_id,
            "x-tenant-id": self.tenant_id,
            "Tenant-ID": self.tenant_id,
            "orgId": self.organization_id
        })

        resp_login = self._session.post(login_url, json=payload)

        if resp_login.status_code != 200:
            raise ValueError(f"Falha no login: {resp_login.text}")

        try:
            auth_data = resp_login.json()
            token = auth_data.get("access_token")
            if not token:
                raise ValueError("Token não encontrado na resposta.")

            self._session.headers.update({
                "Authorization": f"Bearer {token}"
            })

        except ValueError:
            raise ValueError("Erro ao processar resposta de login.")

    def fetch_courses(self) -> List[Dict[str, Any]]:
        url = f"{self.backend_base}/auth/sideList/{self.tenant_id}/courses"
        resp = self._session.get(url)
        resp.raise_for_status()

        data = resp.json()
        # {"message": "...", "data": [...]}
        courses_data = data.get("data", [])
        
        courses = []
        for c in courses_data:
            courses.append({
                "id": c.get("id"),
                "name": c.get("title"),
                "description": c.get("description", ""),
                "_original": c
            })

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_content = {}

        for course in courses:
            course_id = course["id"]
            course_title = course["name"]
            modules_url = f"{self.backend_base}/auth/sideList/{course_id}/modules"
            resp_mod = self._session.get(modules_url)
            if resp_mod.status_code != 200:
                logging.error(f"Failed to fetch modules for course {course_title}: {resp_mod.status_code}")
                continue

            modules_data = resp_mod.json().get("data", [])

            processed_modules = []
            for mod in modules_data:
                module_id = mod["id"]
                module_title = mod["title"]
                lessons_url = f"{self.backend_base}/auth/sideList/{module_id}/lessons"
                resp_les = self._session.get(lessons_url)
                if resp_les.status_code != 200:
                    logging.error(f"Failed to fetch lessons for module {module_title}: {resp_les.status_code}")
                    continue
                    
                lessons_data = resp_les.json().get("data", [])
                processed_lessons = []
                for lesson in lessons_data:
                    processed_lessons.append({
                        "id": lesson["id"],
                        "title": lesson["title"],
                        "slug": lesson.get("slug"),
                        "module_id": module_id,
                        "course_id": course_id
                    })

                processed_modules.append({
                    "id": module_id,
                    "title": module_title,
                    "lessons": processed_lessons
                })

            all_content[course_id] = {
                "id": course_id,
                "title": course_title,
                "modules": processed_modules
            }
            
        return all_content
    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        lesson_id = lesson["id"]
        # URL: https://api.themembers.com.br/api/auth/home/class/{lesson_id}/{tenant_id}
        url = f"{self.api_base}/auth/home/class/{lesson_id}/{self.tenant_id}"
        
        resp = self._session.get(url)
        resp.raise_for_status()
        # {"class": {...}, "next_class": ...}
        data = resp.json().get("class", {})

        content = LessonContent(
            description=Description(text=data.get("description", "") or "", description_type="text")
        )

        video_url = data.get("url_video")
        host = data.get("host")
        
        if video_url:
            if host == "the-player-ai":
                player_url = f"https://player.themembers.com.br/api/video/{video_url}"
                try:
                    headers = self._session.headers.copy()
                    headers["tenantId"] = str(self.tenant_id)
                    resp_player = self._session.get(player_url, headers=headers)
                    resolved = False
                    if resp_player.status_code == 200:
                        player_data = resp_player.json()
                        urls = player_data.get("data", {}).get("urls", {})
                        final_url = urls.get("hls")
                        if not final_url:
                            for quality in ["1080p", "720p", "480p", "360p"]:
                                if urls.get(quality):
                                    final_url = urls.get(quality)
                                    break
                        if final_url:
                            resolved = True
                            content.videos.append(Video(
                                video_id=video_url,
                                url=final_url,
                                title=data.get("title"),
                                order=1,
                                size=0,
                                duration=0,
                                extra_props={
                                    "host": host, 
                                    "original_id": video_url,
                                    "referer": "https://player.themembers.com.br/"
                                }
                            ))
                    if not resolved:
                        logging.warning(f"Could not resolve video URL for {video_url} (status: {resp_player.status_code})")
                        content.videos.append(Video(
                            video_id=video_url,
                            url=video_url,
                            title=data.get("title"),
                            order=1,
                            size=0,
                            duration=0,
                            extra_props={"host": host}
                        ))

                except Exception as e:
                    logging.error(f"Exception resolving player URL: {e}")
                    content.videos.append(Video(
                        video_id=video_url,
                        url=video_url,
                        title=data.get("title"),
                        order=1,
                        size=0,
                        duration=0,
                        extra_props={"host": host}
                    ))
            elif host == "vimeo":
                 content.videos.append(Video(
                    video_id=video_url,
                    url=video_url,
                    title=data.get("title"),
                    order=1,
                    size=0,
                    duration=0,
                    extra_props={"host": host}
                ))
            else:
                 content.videos.append(Video(
                    video_id=video_url,
                    url=video_url,
                    title=data.get("title"),
                    order=1,
                    size=0,
                    duration=0,
                    extra_props={"host": host}
                ))
        pdf_url = data.get("url_pdf")
        if pdf_url:
             content.attachments.append(Attachment(
                attachment_id=lesson_id + "_pdf",
                url=pdf_url,
                filename=f"{lesson.get('slug', lesson_id)}.pdf",
                order=1,
                extension="pdf",
                size=0
             ))
        return content

    def download_attachment(self, attachment: "Attachment", download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        try:
             headers = {
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                 "Accept": "*/*",
                 "Referer": f"https://{self.domain}/",
                 "Origin": f"https://{self.domain}"
             }
             
             with requests.get(attachment.url, stream=True, headers=headers) as r:
                r.raise_for_status()
                with open(download_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
             return True
        except Exception as e:
            logging.error(f"Error downloading attachment: {e}")
            return False

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        url = f"{self.backend_base}/auth/lesson/updateProgress"
        # {"lesson_id":"...","finished":true,"module_id":"..."}
        lesson_id = lesson.get("id")
        module_id = lesson.get("module_id")
        
        if not lesson_id or not module_id:
             logging.warning("Cannot mark lesson watched: missing lesson_id or module_id")
             return

        payload = {
            "lesson_id": lesson_id,
            "finished": watched,
            "module_id": module_id
        }
        try:
            resp = self._session.post(url, json=payload)
            resp.raise_for_status()
        except Exception as e:
            logging.error(f"Failed to mark lesson {lesson_id} as watched: {e}")

PlatformFactory.register_platform("TheMembers", TheMembersPlatform)
