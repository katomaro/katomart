from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.app.api_service import ApiService
from src.app.models import Attachment, Description, LessonContent, Video
from src.config.settings_manager import SettingsManager
from src.platforms.base import BasePlatform, PlatformFactory

logger = logging.getLogger(__name__)

FIREBASE_API_KEY = "AIzaSyCQZKqdJO5aV64PEiWYrTZChJ3UP33-lB8"
FIREBASE_AUTH_URL = "https://identitytoolkit.googleapis.com/v1"
FIREBASE_TOKEN_URL = "https://securetoken.googleapis.com/v1"

BFF_WEB_URL = "https://backend-bff-web.platform.hub.la"
BFF_MEMBERS_URL = "https://backend-bff-members-area.platform.hub.la"
HUB_AUTH_URL = "https://hub.la/api/auth/get"


class HubLaPlatform(BasePlatform):
    """Hub.la platform. Firebase auth, Cloudflare Stream videos, GCS assets."""

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._refresh_token: Optional[str] = None

    @classmethod
    def auth_fields(cls) -> list:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """Para autenticação manual (Token Direto):
1. Acesse https://app.hub.la e faça login.
2. Abra o DevTools (F12) > aba Application > IndexedDB.
3. Abra o banco 'firebaseLocalStorageDb' > 'firebaseLocalStorage'.
4. Encontre a entrada que contém 'accessToken'.
5. Copie o valor do campo 'accessToken'.
6. Cole no campo de token.

Assinantes podem informar e-mail/senha para login automático.""".strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token = self.resolve_access_token(credentials, self._exchange_credentials_for_token)
        self._configure_session(token)

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        resp = requests.post(
            f"{FIREBASE_AUTH_URL}/accounts:signInWithPassword?key={FIREBASE_API_KEY}",
            json={
                "email": username,
                "password": password,
                "returnSecureToken": True,
                "clientType": "CLIENT_TYPE_WEB",
            },
            timeout=30,
        )
        if resp.status_code == 400:
            error_msg = resp.json().get("error", {}).get("message", "Erro desconhecido")
            raise ConnectionError(f"Falha ao autenticar no Hub.la: {error_msg}")
        resp.raise_for_status()
        data = resp.json()
        id_token = data["idToken"]
        self._refresh_token = data.get("refreshToken")

        try:
            requests.post(
                f"{BFF_WEB_URL}/api/v1/mfa/start",
                headers={"Authorization": f"Bearer {id_token}"},
                timeout=15,
            )
        except Exception:
            pass

        try:
            resp = requests.post(
                HUB_AUTH_URL,
                headers={
                    "Authorization": f"Bearer {id_token}",
                    "Origin": "https://app.hub.la",
                    "Referer": "https://app.hub.la/",
                },
                timeout=15,
            )
            resp.raise_for_status()
            custom_token = resp.json().get("token")
        except Exception:
            custom_token = None

        if custom_token:
            resp = requests.post(
                f"{FIREBASE_AUTH_URL}/accounts:signInWithCustomToken?key={FIREBASE_API_KEY}",
                json={"token": custom_token, "returnSecureToken": True},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            self._refresh_token = data.get("refreshToken", self._refresh_token)
            return data["idToken"]

        return id_token

    def _configure_session(self, token: str) -> None:
        token = token.strip()
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Origin": "https://app.hub.la",
            "Referer": "https://app.hub.la/",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self._settings.user_agent,
        })

    def refresh_auth(self) -> None:
        if self._refresh_token:
            try:
                resp = requests.post(
                    f"{FIREBASE_TOKEN_URL}/token?key={FIREBASE_API_KEY}",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                self._refresh_token = data.get("refresh_token", self._refresh_token)
                new_token = data.get("id_token")
                if new_token:
                    self._configure_session(new_token)
                    logger.info("Hub.la: sessão renovada via refresh token.")
                    return
            except Exception as e:
                logger.warning(f"Hub.la: falha ao renovar via refresh token: {e}")

        super().refresh_auth()

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        resp = self._session.get(f"{BFF_WEB_URL}/api/v1/payer/products", timeout=30)

        if resp.status_code == 401:
            raise ConnectionError("Token inválido (401).")

        resp.raise_for_status()
        data = resp.json()

        items = data if isinstance(data, list) else data.get("items", data.get("data", []))

        courses: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for item in items:
            product_id = str(item.get("productId") or item.get("id") or "")
            if not product_id or product_id in seen:
                continue
            seen.add(product_id)

            courses.append({
                "id": product_id,
                "name": item.get("name") or item.get("title") or f"Produto {product_id}",
                "seller_name": item.get("ownerName") or item.get("creatorName") or "",
                "slug": str(item.get("offerId") or item.get("externalId") or product_id),
            })

        return courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        all_content: Dict[str, Any] = {}

        for course in courses:
            product_id = course["id"]
            try:
                modules = self._fetch_sections(product_id)
            except Exception as e:
                logger.error(f"Hub.la: erro ao buscar módulos do produto {product_id}: {e}")
                continue

            course_content = course.copy()
            course_content["title"] = course.get("name")
            course_content["modules"] = modules
            all_content[str(product_id)] = course_content

            time.sleep(0.3)

        return all_content

    def _fetch_sections(self, product_id: str) -> List[Dict[str, Any]]:
        modules: List[Dict[str, Any]] = []
        page = 1
        page_size = 50

        while True:
            resp = self._session.get(
                f"{BFF_MEMBERS_URL}/api/v1/hub/sections/v2",
                params={
                    "productId": product_id,
                    "page": page,
                    "pageSize": page_size,
                    "postPageSize": 999,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            items = data.get("items", [])
            if not items:
                break

            for section in items:
                lessons = []
                for post in section.get("posts", []):
                    lessons.append({
                        "id": post["id"],
                        "title": post.get("title", ""),
                        "order": post.get("order", 1),
                    })

                if lessons:
                    modules.append({
                        "id": section["id"],
                        "title": section.get("name", ""),
                        "order": section.get("order", 1),
                        "lessons": lessons,
                    })

            last_page = data.get("lastPage", 1)
            if page >= last_page:
                break

            page += 1
            time.sleep(0.3)

        return modules

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        post_id = lesson.get("id")
        resp = self._session.get(
            f"{BFF_MEMBERS_URL}/api/v1/hub/posts/{post_id}",
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        content = LessonContent()

        html_content = data.get("content") or ""
        if not html_content:
            body = data.get("body")
            if body and isinstance(body, dict):
                html_content = self._blocks_to_html(body.get("blocks", []))

        if html_content:
            content.description = Description(text=html_content, description_type="html")

        cover = data.get("cover")
        if cover and cover.get("type") == "video":
            metadata = cover.get("metadata", {})
            content.videos.append(Video(
                video_id=cover.get("id", ""),
                url=cover.get("url", ""),
                title=data.get("title", ""),
                order=data.get("order", 1),
                size=metadata.get("size", 0),
                duration=int(metadata.get("duration", 0)),
                extra_props={"referer": "https://app.hub.la/"},
            ))

        for idx, att in enumerate(data.get("attachments", []), 1):
            att_meta = att.get("metadata", {})
            content.attachments.append(Attachment(
                attachment_id=att.get("id", ""),
                url=att.get("url", ""),
                filename=att.get("name", f"Anexo {idx}"),
                extension=att.get("subType", ""),
                order=idx,
                size=att_meta.get("size", 0),
            ))

        return content

    @staticmethod
    def _blocks_to_html(blocks: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for block in blocks:
            block_type = block.get("type", "")
            texts = []
            for item in block.get("content", []):
                if isinstance(item, dict):
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            text = "".join(texts)
            if not text:
                continue
            if block_type == "bulletListItem":
                parts.append(f"<li>{text}</li>")
            elif block_type == "heading":
                level = block.get("props", {}).get("level", 2)
                parts.append(f"<h{level}>{text}</h{level}>")
            else:
                parts.append(f"<p>{text}</p>")
        return "\n".join(parts)

    def download_attachment(self, attachment: Attachment, download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")

        download_url = f"{BFF_MEMBERS_URL}/api/v1/products/{course_id}/assets/{attachment.attachment_id}/download"

        try:
            resp = self._session.get(download_url, stream=True, timeout=120)
            resp.raise_for_status()
        except Exception:
            if not attachment.url:
                return False
            resp = self._session.get(attachment.url, stream=True, timeout=120)
            resp.raise_for_status()

        with open(download_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True


PlatformFactory.register_platform("Hub.la", HubLaPlatform)
