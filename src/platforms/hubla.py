from __future__ import annotations

import html
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlparse

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

YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube(?:-nocookie)?\.com|youtu\.be)/[^\s\"'<>]+",
    re.IGNORECASE,
)

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
    "www.youtu.be",
}


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
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Sec-Ch-Ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": self._settings.user_agent,
        })

    def _get_with_refresh(self, url: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 30, stream: bool = False) -> requests.Response:
        """GET with automatic refresh on 401/403 (Hub.la rejects stale tokens with 403)."""
        if not self._session:
            raise ConnectionError("Sessão não autenticada.")
        resp = self._session.get(url, params=params, timeout=timeout, stream=stream)
        if resp.status_code in (401, 403):
            logger.info(f"Hub.la: {resp.status_code} em {url} — tentando refresh_auth e nova tentativa.")
            try:
                self.refresh_auth()
            except Exception as e:
                logger.warning(f"Hub.la: refresh_auth falhou: {e}")
                resp.raise_for_status()
                return resp
            resp = self._session.get(url, params=params, timeout=timeout, stream=stream)
        return resp

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
        resp = self._get_with_refresh(f"{BFF_WEB_URL}/api/v1/payer/products", timeout=30)

        if resp.status_code in (401, 403):
            raise ConnectionError(f"Token inválido ({resp.status_code}).")

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
            resp = self._get_with_refresh(
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
        resp = self._get_with_refresh(
            f"{BFF_MEMBERS_URL}/api/v1/hub/posts/{post_id}",
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        content = LessonContent()

        body = data.get("body") if isinstance(data.get("body"), dict) else {}
        blocks = body.get("blocks", []) if isinstance(body, dict) else []

        html_content = data.get("content") or ""
        if not html_content and blocks:
            html_content = self._blocks_to_html(blocks)

        if html_content:
            content.description = Description(text=html_content, description_type="html")

        seen_video_urls: Set[str] = set()

        cover = data.get("cover")
        if cover and cover.get("type") == "video":
            metadata = cover.get("metadata", {})
            cover_url = cover.get("url", "")
            if cover_url:
                seen_video_urls.add(cover_url)
            content.videos.append(Video(
                video_id=cover.get("id", ""),
                url=cover_url,
                title=data.get("title", ""),
                order=data.get("order", 1),
                size=metadata.get("size", 0),
                duration=int(metadata.get("duration", 0)),
                extra_props={"referer": "https://app.hub.la/"},
            ))

        # Hub.la também pode guardar vídeos do YouTube dentro do corpo da aula
        # como body.blocks[].type == "custom_video" com props.url.
        # Importante: não varrer o JSON inteiro de `data`, porque a resposta
        # também traz paginate.previous/paginate.next e isso adiciona vídeos
        # de aulas vizinhas como se fossem da aula atual.
        for yt_order, youtube_url in enumerate(self._extract_current_lesson_youtube_urls(data), 1):
            normalized_url = self._normalize_youtube_url(youtube_url)
            if not normalized_url or normalized_url in seen_video_urls:
                continue
            seen_video_urls.add(normalized_url)

            youtube_id = self._extract_youtube_video_id(normalized_url) or str(yt_order)
            content.videos.append(Video(
                video_id=f"youtube-{youtube_id}",
                url=normalized_url,
                title=data.get("title", ""),
                order=int(data.get("order", 1) or 1) + yt_order - 1,
                size=0,
                duration=0,
                extra_props={
                    "referer": "https://app.hub.la/",
                    "provider": "youtube",
                    "download_method": "yt-dlp",
                },
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

            if block_type in {"custom_video", "video", "embed"}:
                # Não colocar o link do YouTube na descrição/html.
                # O app também baixa "conteúdo linkado" encontrado na descrição;
                # se deixarmos o URL aqui, o mesmo vídeo é baixado duas vezes.
                continue

            texts = []
            for item in block.get("content", []):
                if isinstance(item, dict):
                    texts.append(item.get("text", ""))
                elif isinstance(item, str):
                    texts.append(item)
            text = "".join(texts)
            if not text:
                continue
            safe_text = html.escape(text)
            if block_type == "bulletListItem":
                parts.append(f"<li>{safe_text}</li>")
            elif block_type == "heading":
                level = block.get("props", {}).get("level", 2)
                parts.append(f"<h{level}>{safe_text}</h{level}>")
            else:
                parts.append(f"<p>{safe_text}</p>")
        return "\n".join(parts)

    @classmethod
    def _extract_current_lesson_youtube_urls(cls, data: Dict[str, Any]) -> List[str]:
        """Extrai YouTube somente da aula atual, ignorando paginate.previous/next."""
        candidates: List[Any] = []

        body = data.get("body")
        if isinstance(body, dict):
            candidates.append(body.get("blocks", []))

        content = data.get("content")
        if isinstance(content, str) and content:
            candidates.append(content)

        # Alguns retornos podem guardar o player como cover da própria aula.
        # Não inclui paginate, product ou qualquer aula vizinha.
        cover = data.get("cover")
        if isinstance(cover, dict):
            candidates.append(cover)

        urls: List[str] = []
        seen: Set[str] = set()
        for candidate in candidates:
            for url in cls._extract_youtube_urls(candidate):
                normalized = cls._normalize_youtube_url(url)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    urls.append(normalized)
        return urls

    @classmethod
    def _extract_youtube_urls(cls, value: Any) -> List[str]:
        urls: List[str] = []
        seen: Set[str] = set()

        def add_url(raw_url: str) -> None:
            cleaned = raw_url.strip().rstrip("),.;]")
            if not cls._is_youtube_url(cleaned):
                return
            normalized = cls._normalize_youtube_url(cleaned)
            if normalized and normalized not in seen:
                seen.add(normalized)
                urls.append(normalized)

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    if isinstance(child, str):
                        if key.lower() in {"url", "src", "href", "videourl", "embedurl"}:
                            add_url(child)
                        for match in YOUTUBE_URL_RE.findall(child):
                            add_url(match)
                    else:
                        walk(child)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, str):
                for match in YOUTUBE_URL_RE.findall(node):
                    add_url(match)

        walk(value)
        return urls

    @staticmethod
    def _is_youtube_url(url: str) -> bool:
        if not url:
            return False
        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            return False
        return host in YOUTUBE_HOSTS or host.endswith(".youtube.com") or host.endswith(".youtube-nocookie.com")

    @staticmethod
    def _extract_youtube_video_id(url: str) -> Optional[str]:
        try:
            parsed = urlparse(url)
        except Exception:
            return None

        host = parsed.netloc.lower()
        path_parts = [part for part in parsed.path.split("/") if part]

        if host in {"youtu.be", "www.youtu.be"} and path_parts:
            return path_parts[0]

        query_video_id = parse_qs(parsed.query).get("v")
        if query_video_id and query_video_id[0]:
            return query_video_id[0]

        if path_parts:
            if path_parts[0] in {"embed", "shorts", "live", "v"} and len(path_parts) > 1:
                return path_parts[1]
            if len(path_parts[0]) == 11:
                return path_parts[0]

        match = re.search(r"(?:v=|/embed/|youtu\.be/|/shorts/|/live/)([A-Za-z0-9_-]{11})", url)
        return match.group(1) if match else None

    @classmethod
    def _normalize_youtube_url(cls, url: str) -> str:
        youtube_id = cls._extract_youtube_video_id(url)
        if youtube_id:
            return f"https://www.youtube.com/watch?v={youtube_id}"
        return url.strip()

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
