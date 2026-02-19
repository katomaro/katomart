from __future__ import annotations
import logging
import random
import re
import secrets
import time
from typing import Any, Dict, List, Optional
from pathlib import Path

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory

HERMES_API_URL = "https://hermes-api.medcof.com.br"
LMS_API_URL = "https://lms-api.medcof.tech"
LOGIN_URL = "https://login.medcof.com.br"
AULAS_ORIGIN_URL = "https://aulas-prime.medcof.com.br"

AUTH_URL = f"{HERMES_API_URL}/auth/simple-sign-on"
SSO_CALLBACK_URL = f"{LOGIN_URL}/api/auth/callback/credentials"
SSO_EXCHANGE_URL = f"{LMS_API_URL}/auth/v2/sso-hermes"
USER_PROGRESS_URL = f"{LMS_API_URL}/product/user/progress"
AQFM_BY_BLOCK_URL = f"{LMS_API_URL}/aqfm/by-block-number"
VIMEO_GET_URL = f"{LMS_API_URL}/vimeo/get"


class MedcofPlatform(BasePlatform):
    """Implements the Medcof platform (aulas-prime.medcof.com.br)."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager) -> None:
        super().__init__(api_service, settings_manager)
        self._hermes_token: Optional[str] = None
        self._lms_token: Optional[str] = None
        self._product_id: Optional[str] = None

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return [
            AuthField(
                name="token",
                label="Token JWT (opcional)",
                field_type=AuthFieldType.MULTILINE,
                placeholder="Cole o token JWT aqui (aulas_auth_token do cookie)",
                required=False,
            ),
        ]

    @classmethod
    def auth_instructions(cls) -> str:
        return """Essa plataforma requer que voce use delay entre as aulas. Nao use proxy tambem.
Opção 2 - Token JWT:
1) Acesse https://aulas-prime.medcof.com.br e faça login.
2) Abra o DevTools (F12) > Application > Cookies.
3) Copie o valor do cookie 'aulas_auth_token'.
4) Cole no campo Token acima.
""".strip()

    def _get_hermes_headers(self) -> Dict[str, str]:
        """Returns headers for Hermes API requests."""
        return {
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://login.medcof.com.br",
            "Referer": "https://login.medcof.com.br/",
        }

    def _get_lms_headers(self) -> Dict[str, str]:
        """Returns headers for LMS API requests."""
        headers = {
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Origin": AULAS_ORIGIN_URL,
            "Referer": f"{AULAS_ORIGIN_URL}/",
        }
        if self._lms_token:
            headers["Authorization"] = f"Bearer {self._lms_token}"
        return headers

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = (credentials.get("token") or "").strip()
        username = (credentials.get("username") or "").strip()
        password = (credentials.get("password") or "").strip()

        session = requests.Session()

        if token:
            self._lms_token = token
            session.headers.update(self._get_lms_headers())
            self._session = session
            self._validate_token()
            logging.info("Sessao autenticada no Medcof via token JWT.")
            return

        if not username or not password:
            raise ValueError("Informe um token JWT valido ou credenciais (email/senha) para autenticar.")

        # Step 1: Authenticate with Hermes API
        session.headers.update(self._get_hermes_headers())
        auth_payload = {
            "email": username,
            "password": password,
            "rememberMe": False,
        }

        response = session.post(AUTH_URL, json=auth_payload)
        response.raise_for_status()
        data = response.json()

        if not data.get("authenticated"):
            raise ValueError("Falha na autenticacao. Verifique suas credenciais.")

        self._hermes_token = data.get("authorization")
        refresh_token = data.get("refreshToken")

        if not self._hermes_token:
            raise ValueError("Falha ao obter token de autorizacao.")

        logging.info("Sessao autenticada no Medcof via credenciais.")

        # Step 2: Try direct LMS login first (simpler if available)
        lms_token = self._try_direct_lms_login(session, username, password)

        if not lms_token:
            # Step 3: Fallback to SSO flow if direct login fails
            lms_token = self._exchange_hermes_for_lms_token(
                session, self._hermes_token, refresh_token
            )

        self._lms_token = lms_token
        session.headers.update(self._get_lms_headers())
        self._session = session

    def _try_direct_lms_login(
        self, session: requests.Session, email: str, password: str
    ) -> Optional[str]:
        """Try direct authentication with LMS API."""
        try:
            lms_login_url = f"{LMS_API_URL}/auth/v1/email"
            payload = {
                "email": email,
                "password": password,
                "rememberMe": False,
            }
            headers = {
                "User-Agent": self._settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": AULAS_ORIGIN_URL,
                "Referer": f"{AULAS_ORIGIN_URL}/",
            }

            resp = session.post(lms_login_url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("jwtToken") or data.get("token") or data.get("accessToken")
                if token:
                    logging.info("Token LMS obtido via login direto.")
                    return token
        except Exception as e:
            logging.debug(f"Login direto LMS falhou: {e}")

        return None

    def _exchange_hermes_for_lms_token(
        self, session: requests.Session, hermes_token: str, refresh_token: str
    ) -> str:
        """Exchange hermes token for LMS token via SSO flow."""
        # Generate a random state for SSO
        state = secrets.token_hex(40)

        # Step 1: Get CSRF token from login page
        login_session = requests.Session()
        login_page_resp = login_session.get(
            f"{LOGIN_URL}/login",
            headers={
                "User-Agent": self._settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        login_page_resp.raise_for_status()

        # Extract CSRF token from cookies
        csrf_token = None
        for cookie in login_session.cookies:
            if "csrf-token" in cookie.name:
                csrf_value = cookie.value
                # The CSRF token is URL encoded and has format: token|hash
                if "%7C" in csrf_value:
                    csrf_token = csrf_value.split("%7C")[0]
                elif "|" in csrf_value:
                    csrf_token = csrf_value.split("|")[0]
                break

        if not csrf_token:
            logging.warning("CSRF token nao encontrado, tentando continuar sem ele.")
            csrf_token = ""

        # Step 2: Call the credentials callback to establish session
        callback_payload = {
            "authorization": hermes_token,
            "refreshToken": refresh_token or hermes_token,
            "redirect": "false",
            "csrfToken": csrf_token,
            "callbackUrl": f"{LOGIN_URL}/login",
        }

        callback_headers = {
            "User-Agent": self._settings.user_agent,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": LOGIN_URL,
            "Referer": f"{LOGIN_URL}/login",
            "X-Auth-Return-Redirect": "1",
        }

        callback_resp = login_session.post(
            SSO_CALLBACK_URL,
            data=callback_payload,
            headers=callback_headers,
            allow_redirects=False
        )

        if callback_resp.status_code not in (200, 302):
            logging.warning(f"SSO callback retornou status {callback_resp.status_code}")

        # Step 3: Request the OAuth authorize page to get the code
        authorize_url = f"{LOGIN_URL}/?client_id=apollo&redirect_uri={AULAS_ORIGIN_URL}&state={state}"

        authorize_headers = {
            "User-Agent": self._settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": f"{AULAS_ORIGIN_URL}/",
        }

        # First try without redirects to capture the redirect URL
        authorize_resp = login_session.get(
            authorize_url,
            headers=authorize_headers,
            allow_redirects=False
        )

        # Check for redirect with code
        code = None
        if authorize_resp.status_code in (301, 302, 303, 307, 308):
            location = authorize_resp.headers.get("Location", "")
            code_match = re.search(r"[?&]code=([^&]+)", location)
            if code_match:
                code = code_match.group(1)

        # If no redirect, try following redirects and check final URL
        if not code:
            authorize_resp_follow = login_session.get(
                authorize_url,
                headers=authorize_headers,
                allow_redirects=True
            )
            final_url = authorize_resp_follow.url
            code_match = re.search(r"[?&]code=([^&]+)", final_url)
            if code_match:
                code = code_match.group(1)

            # Still no code? Try extracting from HTML response
            if not code:
                html_content = authorize_resp_follow.text

                # Try different patterns to find the code
                patterns = [
                    r'[?&]code=([a-f0-9-]{36})',  # UUID format
                    r'"code"\s*:\s*"([a-f0-9-]{36})"',  # JSON format
                    r'code=([a-f0-9-]+)&state=',  # URL format
                    r"redirect.*[?&]code=([^&\"']+)",  # Redirect URL
                    r'window\.location.*code=([a-f0-9-]+)',  # JS redirect
                ]

                for pattern in patterns:
                    code_match = re.search(pattern, html_content, re.IGNORECASE)
                    if code_match:
                        code = code_match.group(1)
                        break

        if not code:
            logging.debug(f"SSO authorize response status: {authorize_resp.status_code}")
            if 'authorize_resp_follow' in locals():
                logging.debug(f"SSO authorize final URL: {authorize_resp_follow.url}")
            raise ValueError(
                "Falha ao obter codigo SSO. O fluxo de autenticacao pode ter mudado."
            )

        # Step 4: Exchange code for LMS token
        sso_payload = {
            "code": code,
            "state": state,
        }

        sso_headers = {
            "User-Agent": self._settings.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": AULAS_ORIGIN_URL,
            "Referer": f"{AULAS_ORIGIN_URL}/",
        }

        sso_resp = session.post(
            SSO_EXCHANGE_URL,
            json=sso_payload,
            headers=sso_headers
        )
        sso_resp.raise_for_status()
        sso_data = sso_resp.json()

        jwt_token_data = sso_data.get("jwtToken", {})
        lms_token = jwt_token_data.get("jwtToken") if isinstance(jwt_token_data, dict) else None

        if not lms_token:
            raise ValueError("Falha ao obter token LMS do SSO.")

        logging.info("Token LMS obtido com sucesso via SSO.")
        return lms_token

    def _validate_token(self) -> None:
        """Validates the token by making a test request."""
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        response = self._session.get(
            f"{LMS_API_URL}/auth/v1/me",
            headers=self._get_lms_headers()
        )
        if response.status_code != 200:
            raise ValueError("Token invalido ou expirado.")

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        # Get all products from /auth/v1/me
        me_response = self._session.get(
            f"{LMS_API_URL}/auth/v1/me",
            headers=self._get_lms_headers()
        )
        me_response.raise_for_status()
        me_data = me_response.json()

        products = me_data.get("products", [])
        if not products:
            logging.warning("Nenhum produto encontrado para o usuario.")
            return []

        courses: List[Dict[str, Any]] = []

        for product in products:
            product_id = product.get("identifier")
            product_name = product.get("name", "Curso Medcof")
            is_active = product.get("isActive", True)

            if not product_id or not is_active:
                continue

            # Fetch blocks info for this product
            blocks_info = self._fetch_product_blocks(product_id)

            courses.append({
                "id": product_id,
                "name": product_name,
                "slug": product_id,
                "seller_name": "Medcof",
                "blocks_info": blocks_info,
                "blocks_count": len(blocks_info),
                "lessons_count": 0,
                "thumbnail": product.get("thumbnail", ""),
                "has_dashboard": product.get("hasDashboard", False),
            })

        # Store first product id as default
        if courses:
            self._product_id = courses[0]["id"]

        return courses

    def _fetch_product_blocks(self, product_id: str) -> List[Dict[str, Any]]:
        """Fetch blocks info for a specific product."""
        try:
            # First, set this product as the active one
            self._session.patch(
                f"{LMS_API_URL}/user/use/product",
                json={"productId": product_id},
                headers=self._get_lms_headers()
            )

            # Small delay to ensure product switch is processed
            time.sleep(0.5)

            # Now fetch progress for this product
            response = self._session.get(
                USER_PROGRESS_URL,
                headers=self._get_lms_headers()
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("blocksInfo", [])

        except Exception as e:
            logging.warning(f"Erro ao buscar blocos do produto {product_id}: {e}")

        return []

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        content: Dict[str, Any] = {}

        for course in courses:
            course_id = course.get("id")
            course_name = course.get("name", "Curso")
            blocks_info = course.get("blocks_info", [])
            product_id = course_id or self._product_id

            # Switch to this product before fetching its content
            try:
                self._session.patch(
                    f"{LMS_API_URL}/user/use/product",
                    json={"productId": product_id},
                    headers=self._get_lms_headers()
                )
                time.sleep(0.3)
            except Exception as e:
                logging.warning(f"Erro ao trocar para produto {product_id}: {e}")

            modules: List[Dict[str, Any]] = []

            for block in blocks_info:
                block_number = block.get("number")
                block_name = block.get("blockName", f"Bloco {block_number}")
                block_available = block.get("available", True)
                block_is_active = block.get("isActive", True)

                if not block_available or not block_is_active:
                    logging.info("Bloco %s nao disponivel, ignorando.", block_name)
                    continue

                delay = random.uniform(1, 5)
                logging.debug("Aguardando %.2f segundos antes de buscar bloco %s", delay, block_name)
                time.sleep(delay)

                aqfm_data = self._fetch_block_content(block_number, product_id)
                lessons = self._build_lessons_from_aqfm(aqfm_data, block_number)

                modules.append({
                    "id": str(block.get("identifier", block_number)),
                    "title": block_name.strip(),
                    "order": block_number,
                    "lessons": lessons,
                    "locked": not block_available,
                })

            content[str(course_id)] = {
                "id": course_id,
                "name": course_name,
                "slug": course_id,
                "title": course_name,
                "modules": modules,
            }

        return content

    def _fetch_block_content(self, block_number: int, product_id: str) -> Dict[str, Any]:
        """Fetches the content (aqfm) for a specific block."""
        params = {"productId": product_id}

        response = self._session.get(
            f"{AQFM_BY_BLOCK_URL}/{block_number}",
            params=params,
            headers=self._get_lms_headers()
        )
        response.raise_for_status()
        return response.json()

    def _build_lessons_from_aqfm(self, aqfm_data: Dict[str, Any], block_number: int) -> List[Dict[str, Any]]:
        """Builds lesson entries from AQFM data."""
        lessons: List[Dict[str, Any]] = []
        lesson_order = 1

        specialties = aqfm_data.get("specialties", [])

        for specialty in specialties:
            aqfms = specialty.get("aqfms", [])

            for aqfm in aqfms:
                aqfm_id = aqfm.get("identifier")
                aqfm_name = aqfm.get("name") or aqfm.get("adminName", f"Aula {lesson_order}")
                videos = aqfm.get("videos", [])
                support_materials = aqfm.get("supportMaterials", [])
                duration = aqfm.get("duration", 0)

                if not videos:
                    continue

                for video_index, video in enumerate(videos, start=1):
                    video_id = video.get("identifier")
                    video_external_id = video.get("externalId")
                    video_title = video.get("title", aqfm_name)
                    video_duration = video.get("duration", duration)
                    video_thumbnail = video.get("thumbnail", "")
                    professor_name = video.get("professorName", "")

                    if not video_id or not video_external_id:
                        continue

                    lesson_title = video_title
                    if professor_name:
                        lesson_title = f"{video_title}"

                    lessons.append({
                        "id": video_id,
                        "title": lesson_title.strip(),
                        "order": lesson_order,
                        "locked": False,
                        "aqfm_id": aqfm_id,
                        "vimeo_id": video_external_id,
                        "duration": video_duration,
                        "thumbnail": video_thumbnail,
                        "professor": professor_name,
                        "support_materials": support_materials if video_index == 1 else [],
                        "block_number": block_number,
                    })
                    lesson_order += 1

        return lessons

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        content = LessonContent()
        vimeo_id = lesson.get("vimeo_id")
        support_materials = lesson.get("support_materials", [])

        for mat_index, material in enumerate(support_materials, start=1):
            mat_url = material.get("url", "")
            mat_name = material.get("name", f"Material {mat_index}")

            if mat_url:
                extension = mat_url.rsplit(".", 1)[-1].split("?")[0] if "." in mat_url else "pdf"
                filename = f"{mat_name}.{extension}"

                content.attachments.append(
                    Attachment(
                        attachment_id=f"{lesson.get('id')}-mat-{mat_index}",
                        url=mat_url,
                        filename=filename,
                        order=mat_index,
                        extension=extension,
                        size=0,
                    )
                )

        if vimeo_id:
            video_data = self._fetch_vimeo_video(vimeo_id)

            if video_data:
                playback_url = video_data.get("playbackUrl", "")
                embedded_uri = video_data.get("embeddedUri", "")
                video_url = playback_url or embedded_uri

                if video_url:
                    content.videos.append(
                        Video(
                            video_id=str(lesson.get("id")),
                            url=video_url,
                            order=lesson.get("order", 1),
                            title=lesson.get("title", "Aula"),
                            size=0,
                            duration=video_data.get("duration", 0),
                            extra_props={
                                "referer": f"{AULAS_ORIGIN_URL}/",
                            }
                        )
                    )

                description_text = video_data.get("description", "")
                if description_text:
                    content.description = Description(
                        text=description_text,
                        description_type="text"
                    )

        return content

    def _fetch_vimeo_video(self, vimeo_id: str) -> Optional[Dict[str, Any]]:
        """Fetches video playback data from Vimeo via LMS API."""
        try:
            response = self._session.get(
                f"{VIMEO_GET_URL}/{vimeo_id}",
                headers=self._get_lms_headers()
            )
            response.raise_for_status()
            data = response.json()
            return data.get("vimeoVideoInfo", data)
        except Exception as exc:
            logging.error("Erro ao obter dados do video Vimeo %s: %s", vimeo_id, exc)
            return None

    def download_attachment(
        self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str
    ) -> bool:
        if not self._session:
            raise ConnectionError("A sessao nao esta autenticada.")

        if not attachment.url:
            logging.error("Anexo sem URL disponivel: %s", attachment.filename)
            return False

        try:
            response = self._session.get(attachment.url, stream=True)
            response.raise_for_status()
            with open(download_path, "wb") as file_handle:
                for chunk in response.iter_content(chunk_size=8192):
                    file_handle.write(chunk)
            return True
        except Exception as exc:
            logging.error("Falha ao baixar anexo %s: %s", attachment.filename, exc)
            return False


PlatformFactory.register_platform("Medcof", MedcofPlatform)
