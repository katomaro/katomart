from typing import Any, Dict, List, Optional
from pathlib import Path
import requests
import logging
import json
import re

from playwright.async_api import Page
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher
from src.app.models import LessonContent, Attachment, Video, Description, AuxiliaryURL
from src.app.api_service import ApiService
from src.config.settings_manager import SettingsManager

BASE_URL = "https://app.rocketseat.com.br"
LOGIN_URL = "https://app.rocketseat.com.br/entrar"
API_SEARCH_URL = "https://skylab-api.rocketseat.com.br/v2/search/multi-search"

TARGET_ENDPOINTS = [
    "https://skylab-api.rocketseat.com.br/v2/users/me",
    "https://skylab-api.rocketseat.com.br/v2/notifications",
    "https://skylab-api.rocketseat.com.br/**"
]

class RocketseatTokenFetcher(PlaywrightTokenFetcher):
    """
    Automação via Playwright para realizar login na Rocketseat e capturar o token.
    """
    captured_cookies: List[Dict[str, Any]] = []

    @property
    def login_url(self) -> str:
        return LOGIN_URL

    @property
    def target_endpoints(self) -> List[str]:
        return TARGET_ENDPOINTS

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        await page.wait_for_selector('input[name="email"]')
        await page.fill('input[name="email"]', username)

        await page.wait_for_selector('input[name="password"]')
        await page.fill('input[name="password"]', password)

    async def submit_login(self, page: Page) -> None:
        submit_btn = page.locator('button[type="submit"]')
        if await submit_btn.count() > 0:
            await submit_btn.first.click()
        else:
            await page.click('button:has-text("Entrar")')

    async def _capture_authorization_header(self, page: Page) -> Any:
        result = await super()._capture_authorization_header(page)
        auth_header, url = result

        if auth_header:
            try:
                self.captured_cookies = await page.context.cookies()
                logging.info(f"Capturados {len(self.captured_cookies)} cookies da sessão.")
            except Exception as e:
                logging.warning(f"Não foi possível capturar cookies: {e}")
        
        return result


class RocketSeatPlatform(BasePlatform):
    """
    Implementação da plataforma Rocketseat.
    Usa Playwright para login (devido às Server Actions do Next.js) e API para busca.
    """

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = RocketseatTokenFetcher()
        self._cookies = []

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        """Campos adicionais de autenticação (nenhum extra necessário)."""
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return (
            "O sistema abrirá um navegador invisível para realizar o login na Rocketseat. "
            "Certifique-se de que suas credenciais estão corretas."
        )

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        """
        Realiza o login na plataforma Rocketseat via Playwright.
        """
        email = credentials.get("username")
        password = credentials.get("password")
        use_browser_emulation = bool(credentials.get("browser_emulation"))
        confirmation_event = credentials.get("manual_auth_confirmation")
        custom_ua = self._settings.user_agent

        if not email or not password:
            raise ValueError("E-mail e senha são obrigatórios.")

        try:
            logging.info("Iniciando login via Playwright na Rocketseat...")

            token = self._token_fetcher.fetch_token(
                username=email,
                password=password,
                headless=not use_browser_emulation,
                user_agent=custom_ua,
                wait_for_user_confirmation=(
                    confirmation_event.wait if confirmation_event else None
                ),
            )
            
            if not token:
                raise ConnectionError("Não foi possível capturar o token de acesso.")

            self._cookies = self._token_fetcher.captured_cookies
            
            self._configure_session(token)
            logging.info("Login na Rocketseat realizado com sucesso!")
            self.credentials = credentials

        except Exception as e:
            raise ConnectionError(f"Erro ao autenticar na Rocketseat: {e}")

    def _configure_session(self, token: str) -> None:
        """Configura a sessão requests com o token obtido."""
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Authorization": f"Bearer {token}",
            "Origin": BASE_URL,
            "Referer": BASE_URL
        })

        if self._cookies:
            for cookie in self._cookies:
                self._session.cookies.set(
                    cookie['name'], 
                    cookie['value'], 
                    domain=cookie.get('domain'),
                    path=cookie.get('path')
                )

    def fetch_courses(self) -> List[Dict[str, Any]]:
        logging.info("Rocketseat: Utilize a caixa de pesquisa para encontrar conteúdos.")
        return []

    def search_courses(self, query: str) -> List[Dict[str, Any]]:
        """
        Busca cursos (jornadas) na API da Rocketseat.
        Inclui proteção contra loops infinitos caso a paginação falhe.
        """
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        logging.info(f"Buscando por '{query}' na Rocketseat...")

        courses = []
        seen_ids = set()
        page = 1
    
        headers = self._session.headers.copy()
        headers["Accept"] = "application/json"
        headers["Host"] = "skylab-api.rocketseat.com.br"

        while True:
            params = {"query": query, "page": page}
            
            try:
                response = self._session.get(API_SEARCH_URL, params=params, headers=headers)
                response.raise_for_status()
                data = response.json()
            except requests.RequestException as e:
                logging.error(f"Erro na busca (página {page}): {e}")
                break

            journeys = data.get("journeys", [])

            if not journeys:
                break

            new_items_count = 0
            for item in journeys:
                c_id = item.get("id")

                if c_id in seen_ids:
                    continue

                seen_ids.add(c_id)
                new_items_count += 1

                educators_list = item.get("educators") or []
                seller_name = ", ".join([edu.get("name", "") for edu in educators_list])

                courses.append({
                    "id": c_id,
                    "title": item.get("title"),
                    "name": item.get("title"),
                    "seller_name": seller_name or "Rocketseat",
                    "slug": item.get("slug"),
                    "description": item.get("description", "")
                })

            if new_items_count == 0:
                logging.info(f"Paginação encerrada: Página {page} não trouxe novos resultados.")
                break

            meta = data.get("meta", {}).get("journeys", {})
            if not meta.get("hasMore", False) or page > 50:
                break
                
            page += 1

        logging.info(f"Encontrados {len(courses)} cursos para '{query}'.")
        return courses

    def _parse_rsc_response(self, text: str) -> dict:
        """
        Parses the RSC (React Server Components) response text to extract json data.
        Searches for lines starting with '2:' or similar.
        """
        lines = text.split('\n')
        #'journeyContents' ou 'lessonGroups'
        
        for line in lines:
            if not line: continue
            # "ID:JSON"
            parts = line.split(':', 1)
            if len(parts) < 2: continue
            
            try:
                data = json.loads(parts[1])
                if isinstance(data, list) and len(data) > 3 and isinstance(data[3], dict):
                     payload = data[3]

                     if 'journeyContents' in payload:
                         return payload
                     if 'lessonGroups' in payload:
                         return payload
                     inner_data = payload.get('data')
                     if isinstance(inner_data, dict):
                         if 'journeyContents' in inner_data:
                             return inner_data
                         if 'lessonGroups' in inner_data:
                             return inner_data

            except (json.JSONDecodeError, IndexError):
                continue
        return {}

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Busca o conteúdo (módulos e aulas) para a lista de cursos fornecida.
        Usa endpoints RSC (Server Actions) para navegar na jornada.
        """
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        all_content = {}
        
        headers = self._session.headers.copy()
        headers.update({
            "RSC": "1",
            "Accept": "*/*",
            "Host": "app.rocketseat.com.br"
        })

        for course in courses:
            slug = course.get("slug")
            c_id = course.get("id")
            course_name = course.get("title")
            
            if not slug:
                logging.warning(f"Curso {course_name} sem slug, pulando.")
                continue

            logging.info(f"Buscando conteúdo para: {course_name} ({slug})")
            
            try:
                url_contents = f"{BASE_URL}/jornada/{slug}/conteudos"
                resp = self._session.get(url_contents, headers=headers, params={"_rsc": "1"})
                resp.raise_for_status()
                resp.encoding = "utf-8"

                data = self._parse_rsc_response(resp.text)
                nodes = data.get("journeyContents", {}).get("nodes", [])

                processed_modules = []
                for idx, node in enumerate(nodes, start=1):
                    node_type = node.get("type")
                    node_slug = node.get("slug")
                    node_title = node.get("title")
                    sub_modules = node.get("contents", [])
                    if not sub_modules:
                        sub_modules = [node]
                    
                    for sub in sub_modules:
                        # /sala/{slug}
                        module_slug = sub.get("slug")
                        module_title = sub.get("title")

                        if not module_slug:
                            continue
                        logging.info(f"  -> Buscando aulas do módulo: {module_title}")

                        context_meta = {
                            "journey_id": c_id,
                            "journey_title": course.get("title"),
                            "journey_slug": slug,
                            "node_id": sub.get("id"),
                            "node_title": sub.get("title"),
                            "node_slug": sub.get("slug"),
                            "cluster_id": node.get("id") if node != sub else None,
                            "cluster_title": node.get("title") if node != sub else None,
                            "cluster_slug": node.get("slug") if node != sub else None,
                        }
                        
                        lessons = self._fetch_lessons_from_module(slug, module_slug, headers, context_meta)
                        
                        if lessons:
                            formatted_title = f"{idx:02d}. {module_title}"
                            processed_modules.append({
                                "id": sub.get("id") or module_slug,
                                "title": formatted_title,
                                "lessons": lessons
                            })
                            
                course_entry = course.copy()
                course_entry["modules"] = processed_modules
                all_content[str(c_id)] = course_entry

            except Exception as e:
                logging.error(f"Erro ao buscar conteúdo do curso {course_name}: {e}")
                continue

        return all_content

    def _fetch_lessons_from_module(self, journey_slug: str, module_slug: str, headers: dict, context_meta: dict = None) -> List[Dict[str, Any]]:
        """
        Busca as aulas de um módulo específico (visitando a 'sala').
        """
        context_meta = context_meta or {}
        # https://app.rocketseat.com.br/jornada/react-2025/sala/fundamentos-9?_rsc=...
        url_module = f"{BASE_URL}/jornada/{journey_slug}/sala/{module_slug}"
        
        try:
            resp = self._session.get(url_module, headers=headers, params={"_rsc": "1"})
            if resp.status_code != 200:
                return []
            resp.encoding = "utf-8"
            data = self._parse_rsc_response(resp.text)
            lesson_groups = data.get("lessonGroups", [])

            lessons_list = []

            for grp_idx, group in enumerate(lesson_groups, start=1):
                group_title = group.get("title", "")
                group_slug = group.get("slug", "")
                group_id = group.get("id")
                
                for lesson_idx, lesson in enumerate(group.get("lessons", []), start=len(lessons_list) + 1):
                    l_id = lesson.get("id")
                    l_title = lesson.get("title")
                    l_slug = lesson.get("slug")
                    l_type = lesson.get("type", "VIDEO")

                    video_info = lesson.get("video", {})
                    # jupiterVideoId
                    formatted_title = l_title
                    full_meta = context_meta.copy()
                    full_meta.update({
                        "lesson_group_id": group_id,
                        "group_slug": group_slug,
                        "group_title": group_title,
                        "lesson_id": l_id, 
                        "lesson_slug": l_slug,
                        "lesson_title": l_title,
                    })

                    lessons_list.append({
                        "id": l_id,
                        "title": formatted_title, 
                        "type": l_type,
                        "slug": l_slug,
                        "duration": lesson.get("duration"),
                        "video_data": video_info,
                        "group_title": group_title, 
                        "journey_slug": journey_slug,
                        "module_slug": module_slug,
                        "order": lesson_idx,
                        "description": lesson.get("description"),
                        "raw_data": lesson,
                        "tracker_meta": full_meta
                    })
            
            return lessons_list
            
        except Exception as e:
            logging.warning(f"Falha ao buscar aulas de {module_slug}: {e}")
            return []

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        """
        Retorna os detalhes da aula. Tenta resolver a URL do vídeo se possível.
        """
        if isinstance(lesson, str):
             logging.warning(f"fetch_lesson_details recebeu string: {lesson}")
             return LessonContent()

        video_data = lesson.get("video_data", {})
        if isinstance(video_data, str):
            video_data = {}

        vid_id = video_data.get("jupiterVideoId")

        if vid_id:
            # URL de embed do Bunny.net usado pela Rocketseat (ID de biblioteca 212524 fixo)
            # Requer Referer: https://app.rocketseat.com.br/ (configurado na sessão)
            url = f"https://iframe.mediadelivery.net/embed/212524/{vid_id}"
        elif lesson.get("type") == "LINK":
             # Caso seja um link externo (Notion, etc)
             url = lesson.get("slug", "")
             if not url.startswith("http"):
                  url = f"{BASE_URL}/aula/{url}" # Fallback
        else:
            url = lesson.get("slug", "")

        main_video = Video(
            video_id=str(lesson.get("id")),
            url=url,
            order=1,
            title=lesson.get("title", ""),
            size=0,
            duration=lesson.get("duration") or 0
        )
        
        description_text = lesson.get("description") or ""
        desc_obj = Description(text=description_text, description_type="markdown") if description_text else None
        
        aux_urls = []
        if description_text:
            found_links = re.findall(r'\[([^\]]+)\]\((https?://[^)\s]+)\)', description_text)
            for idx, (link_title, link_url) in enumerate(found_links, start=1):
                link_url = link_url.rstrip(').,;')
                aux_urls.append(AuxiliaryURL(
                    url_id=f"link-{lesson.get('id') or 'unknown'}-{idx}",
                    url=link_url,
                    order=idx,
                    title=link_title,
                    description="Material Complementar identificado na descrição"
                ))

        return LessonContent(
            description=desc_obj,
            auxiliary_urls=aux_urls,
            videos=[main_video],
            attachments=[] 
        )

    def download_attachment(self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        """
        Baixa um anexo genérico.
        """
        if not self._session:
             return False
             
        try:
            with self._session.get(attachment.url, stream=True) as r:
                r.raise_for_status()
                with open(download_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as e:
            logging.error(f"Erro ao baixar anexo {attachment.name}: {e}")
            return False

    def mark_lesson_watched(self, lesson: Dict[str, Any], watched: bool) -> None:
        """
        Marca a aula como assistida na Rocketseat.
        """
        if not self._session:
            logging.warning("Sessão não iniciada, não é possível marcar progresso.")
            return
        meta = lesson.get("tracker_meta")
        if not meta:
            logging.warning(f"Metadata de rastreamento não encontrado para aula {lesson.get('title')}")
            return

        lesson_id = str(lesson.get("id"))
        duration = int(lesson.get("duration") or 0)

        # POST https://skylab-api.rocketseat.com.br/progress/{lesson_id}
        url = f"https://skylab-api.rocketseat.com.br/progress/{lesson_id}"
        
        payload = {
            "completed": watched,
            "percentage": 100 if watched else 0,
            "progress_time": duration if watched else 0,
            "completing_node": False,
            "meta": {
                "journey_id": meta.get("journey_id"),
                "journey_title": meta.get("journey_title"),
                "type": "cluster",
                "is_expert_content": False,
                "node_id": meta.get("node_id"),
                "node_title": meta.get("node_title"),
                "node_slug": meta.get("node_slug"),
                "roadmap_slug": None,
                "cluster_id": meta.get("cluster_id") or meta.get("node_id"),
                "cluster_title": meta.get("cluster_title") or meta.get("node_title"),
                "cluster_slug": meta.get("cluster_slug") or meta.get("node_slug"),
                "cluster_thumbnail_url": None,

                "lesson_group_id": meta.get("lesson_group_id"),
                "group_slug": meta.get("group_slug"),
                "group_title": meta.get("group_title"),

                "lesson_id": meta.get("lesson_id"),
                "lesson_slug": meta.get("lesson_slug"),
                "lesson_title": meta.get("lesson_title")
            }
        }
        
        headers = self._session.headers.copy()
        headers["Host"] = "skylab-api.rocketseat.com.br"
        headers["Content-Type"] = "application/json"
        headers["Referer"] = "https://app.rocketseat.com.br/"
        
        try:
            resp = self._session.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            verb = "assistida" if watched else "não assistida"
            logging.info(f"Aula '{lesson.get('title')}' marcada como {verb} com sucesso.")
        except Exception as e:
            logging.error(f"Erro ao atualizar progresso da aula {lesson.get('title')}: {e}")

PlatformFactory.register_platform("Rocketseat Journeys", RocketSeatPlatform)
