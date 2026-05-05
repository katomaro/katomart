from typing import Any, Dict, List, Optional
import requests
import json
import logging
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.app.models import LessonContent, Attachment, Video, Description, AuxiliaryURL
from src.app.api_service import ApiService
from src.config.settings_manager import SettingsManager

class FluencyAcademyPlatform(BasePlatform):
    """
    Platform implementation for Fluency Academy.
    Uses 'Bifrost' API for product list and content.
    """
    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self.domain = "academy.fluency.io"
        self.api_bifrost = "https://bifrost.fluencyacademy.io"
        self.api_accounts = "https://accounts-api.fluency.io"

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """
Para obter o token da Fluency Academy:
1) Acesse https://accounts.fluency.io/ e faça login.
2) Abra as Ferramentas de Desenvolvedor (F12) -> aba Rede (Network).
3) Procure por uma requisição para "users/me" ou "platforms".
4) Copie o valor do cabeçalho "Authorization" (começa com Bearer ...).
5) Cole o token completo aqui.
""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token_data = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token_data)

    def _configure_session(self, token_data: Any) -> None:
        self._session = requests.Session()
        
        access_token = ""
        refresh_token = ""
        email = self.credentials.get("username", "")

        if isinstance(token_data, dict):
            access_token = token_data.get("access_token", "")
            refresh_token = token_data.get("refresh_token", "")
            if "email" in token_data:
                email = token_data["email"]
        elif isinstance(token_data, str):
            access_token = token_data
        
        clean_token = access_token.replace("Bearer ", "").strip()
        
        self._session.headers.update({
            "Authorization": f"Bearer {clean_token}",
            "User-Agent": self._settings.user_agent,
            "Origin": f"https://{self.domain}",
            "Referer": f"https://{self.domain}/",
        })

        domain = ".fluency.io"
        self._session.cookies.set("@Accounts:jwt_token", clean_token, domain=domain)
        if email:
             self._session.cookies.set("@Accounts:email", email, domain=domain)
        if refresh_token:
            self._session.cookies.set("@Accounts:refresh_token", refresh_token, domain=domain)

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> Dict[str, str]:
        """Exchanges username and password for tokens."""
        url = f"{self.api_accounts}/auth/sign-in/"
        payload = {"email": username, "password": password}
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://accounts.fluency.io",
            "Referer": "https://accounts.fluency.io/"
        }

        try:
            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            session_data = data.get("session", {})
            access_token = session_data.get("access_token")
            
            if not access_token:
                raise ValueError("Token de acesso não encontrado na resposta.")
            
            return {
                "access_token": access_token,
                "refresh_token": session_data.get("refresh_token", ""),
                "id_token": session_data.get("id_token", ""),
                "email": username
            }
        except requests.RequestException as e:
            msg = f"Falha na autenticação: {e}"
            logging.error(msg)
            raise ConnectionError(msg) from e

    def fetch_courses(self) -> List[Dict[str, Any]]:
        """
        Fetches 'Programs' (Languages) and expands them into Courses (Main + Complementary).
        """
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")
        
        courses = []
        
        # 1. Fetch Programs (e.g. English, Spanish)
        programs_url = f"{self.api_bifrost}/programs"
        try:
            logging.info("Buscando programas/idiomas disponíveis...")
            
            # Update referer for Bifrost
            self._session.headers.update({
                "Origin": "https://academy.fluency.io",
                "Referer": "https://academy.fluency.io/"
            })

            resp = self._session.get(programs_url)
            resp.raise_for_status()
            
            # Structure: {"programs": [...]}
            data = resp.json()
            programs_data = data.get("programs", [])
            
            for prog in programs_data:
                prog_name = prog.get("name", "Unknown Program")
                prog_id = prog.get("id")

                if not prog_id: 
                    continue

                if not prog.get("has_access", False):
                    logging.info(f"Sem acesso ao programa: {prog_name}")
                    continue

                logging.info(f"Processando programa: {prog_name} ({prog_id})")

                try:
                    main_url = f"{self.api_bifrost}/programs/{prog_id}/courses/main"
                    r_main = self._session.get(main_url)
                    if r_main.status_code == 200:
                        main_course = r_main.json()
                        courses.append(self._normalize_course(main_course, parent_program=prog_name, is_complementary=False))
                except Exception as e:
                    logging.warning(f"Erro ao buscar curso principal de {prog_name}: {e}")
                try:
                    comp_url = f"{self.api_bifrost}/programs/{prog_id}/courses/complement?retrieve_progress=True"
                    r_comp = self._session.get(comp_url)
                    if r_comp.status_code == 200:
                        comps = r_comp.json()
                        if isinstance(comps, list):
                            for c in comps:
                                courses.append(self._normalize_course(c, parent_program=prog_name, is_complementary=True))
                except Exception as e:
                    logging.warning(f"Erro ao buscar cursos complementares de {prog_name}: {e}")

            if not courses:
                logging.warning("Nenhum curso encontrado através da API de programas.")
                
            return courses

        except Exception as e:
            logging.error(f"Erro ao buscar lista de cursos: {e}")
            return []

    def _normalize_course(self, api_data: Dict[str, Any], parent_program: str, is_complementary: bool) -> Dict[str, Any]:
        """Normalizes API course object to internal structure."""
        c_id = api_data.get("id")
        c_name = api_data.get("name", "Sem Nome")
        
        display_name = f"{parent_program} - {c_name}" if is_complementary else f"{parent_program} (Principal)"
        if c_name == parent_program:
             display_name = c_name

        return {
            "id": c_id,
            "name": display_name,
            "title": display_name,
            "slug": f"{parent_program.lower()}-{c_id}",
            "description": api_data.get("description", ""),
            "platform_url": f"https://academy.fluency.io/programs/{api_data.get('program', {}).get('id')}/courses/{c_id}", # Web URL for reference
            "original_data": api_data,
            "api_course_id": c_id 
        }

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Fetches modules and lessons using the 'learning-path' API.
        Handles pagination since API limits page size.
        """
        if not self._session:
             raise ConnectionError("Sessão não autenticada.")

        all_content = {}

        for course in courses:
            api_cid = course.get("api_course_id") or course["original_data"].get("id")
            if not api_cid:
                logging.warning(f"ID do curso não encontrado para {course['name']}")
                continue

            # /courses/{id}/learning-path
            url = f"{self.api_bifrost}/courses/{api_cid}/learning-path"
            
            logging.info(f"Buscando estrutura para: {course['name']} via API...")
            
            accumulated_modules = {}
            page = 1
            has_next = True
            
            try:
                while has_next:
                    logging.info(f"Buscando página {page} do learning-path...")
                    resp = self._session.get(url, params={"size": 50, "page": page})
                    if resp.status_code == 422:
                        logging.warning("Erro 422 com size=50. Tentando size=20.")
                        resp = self._session.get(url, params={"size": 20, "page": page})
                    
                    resp.raise_for_status()
                    data = resp.json()
                    
                    # 'modules' contains the hierarchical structure for this page
                    # 'path' contains the flat list of lessons for this page
                    page_modules = data.get("modules", [])
                    page_path = data.get("path", [])
                    
                    # If modules is empty but path exists, we might need a dummy module
                    if not page_modules and page_path:
                         page_modules = [{
                            "id": "flat-list",
                            "name": "Aulas",
                            "units": [{"name": "Default Unit", "lessons": page_path}]
                        }]

                    for mod in page_modules:
                        mod_id = mod.get("id", "flat-list")
                        parsed = self._parse_module(mod)
                        
                        if mod_id not in accumulated_modules:
                            accumulated_modules[mod_id] = parsed
                        else:
                            accumulated_modules[mod_id]["lessons"].extend(parsed["lessons"])
                    
                    metadata = data.get("metadata", {})
                    next_url = metadata.get("next")
                    if next_url:
                        page += 1
                    else:
                        has_next = False
                
                if accumulated_modules:
                    processed = course.copy()
                    processed["modules"] = list(accumulated_modules.values())
                    all_content[course["id"]] = processed
                    logging.info(f"Conteúdo carregado para {course['name']}: {len(processed['modules'])} módulos.")
                    
            except Exception as e:
                logging.error(f"Erro ao buscar conteúdo do curso {course['name']}: {e}")

        return all_content

    def _parse_module(self, mod_data: Dict[str, Any]) -> Dict[str, Any]:
        """Parses a module object from the API."""
        units = []
        
        final_lessons = []
        for unit in mod_data.get("units", []):
            unit_name = unit.get("name", "")
            for lesson in unit.get("lessons", []):
                l_id = lesson.get("id")
                l_name = lesson.get("name")
                final_lessons.append({
                    "id": l_id,
                    "name": l_name,
                    "title": l_name,
                    "type": "lesson",
                    "original_data": lesson
                })
        
        return {
            "id": mod_data.get("id"),
            "name": mod_data.get("name"),
            "title": mod_data.get("name"),
            "lessons": final_lessons
        }

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        """
        Fetches lesson details including video URL and attachments.
        1. List tasks for the lesson.
        2. Find video task.
        3. Get task details (video sources).
        """
        if not self._session:
             raise ConnectionError("Sessão não autenticada.")

        lesson_id = lesson.get("id") or lesson["original_data"].get("id")
        title = lesson.get("name", "Aula sem nome")
        content = LessonContent(
            description=None,
            auxiliary_urls=[],
            videos=[],
            attachments=[]
        )
        
        try:
            tasks_url = f"{self.api_bifrost}/lessons/{lesson_id}/tasks"
            logging.info(f"Buscando tasks para aula {title} ({lesson_id})...")
            
            resp = self._session.get(tasks_url)
            resp.raise_for_status()
            data = resp.json()
            
            tasks = data.get("tasks", [])
            video_task = next((t for t in tasks if t.get("type") == "video"), None)

            if video_task:
                task_id = video_task.get("id")
                task_detail_url = f"{self.api_bifrost}/tasks/{task_id}"
                
                t_resp = self._session.get(task_detail_url)
                t_resp.raise_for_status()
                t_data = t_resp.json()

                meta = t_data.get("meta", {})
                sources = meta.get("sources", [])
                
                selected_video = None
                
                for q in ["1080", "720", "540", "360", "240"]:
                    for s in sources:
                        if s.get("quality") == q and ".mp4" in s.get("url", ""):
                            selected_video = s.get("url")
                            break
                    if selected_video: break

                if not selected_video:
                    for s in sources:
                         if ".m3u8" in s.get("url", ""):
                             selected_video = s.get("url")
                             break
                
                if selected_video:
                    content.videos.append(Video(
                        video_id=task_id,
                        url=selected_video,
                        order=1,
                        title=f"Vídeo ({title})",
                        size=0,
                        duration=data.get("duration", 0)
                    ))

                description_data = t_data.get("description", {})
                html_content = description_data.get("content", "")
                if html_content:
                    content.description = Description(
                        text=html_content,
                        description_type="html"
                    )

                audios = description_data.get("audios", [])
                for idx, audio in enumerate(audios):
                    a_url = audio.get("url")
                    if a_url:
                        ext = "mp3"
                        if ".m3u8" in a_url: ext = "m3u8"
                        
                        a_name = audio.get("transcription", f"Audio {idx+1}")
                        if not a_name.strip(): a_name = f"Audio {idx+1}"

                        content.attachments.append(Attachment(
                            attachment_id=audio.get("id", str(idx)),
                            url=a_url,
                            filename=f"audio_{idx+1}",
                            order=idx+1,
                            extension=ext,
                            size=0
                        ))
                    
        except Exception as e:
            logging.error(f"Erro ao buscar detalhes da aula {title}: {e}")
            
        return content

    def download_attachment(self, attachment: Attachment, download_path: Any, course_slug: str, course_id: str, module_id: str) -> bool:
        return False

PlatformFactory.register_platform("Fluency Academy", FluencyAcademyPlatform)
