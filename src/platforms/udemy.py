from typing import Any, Dict, List, Optional
import logging
import asyncio
import time
import json
import re
from pathlib import Path
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from curl_cffi import requests

from src.platforms.base import AuthField, AuthFieldType, BasePlatform, PlatformFactory
from src.platforms.playwright_token_fetcher import PlaywrightTokenFetcher
from src.app.models import LessonContent, Attachment, Video
from src.app.api_service import ApiService
from src.config.settings_manager import SettingsManager


class UdemyTokenFetcher(PlaywrightTokenFetcher):
    @property
    def login_url(self) -> str:
        return "https://www.udemy.com/join/passwordless-auth/?locale=pt_BR&next=%2Fhome%2Fmy-courses%2Flearning%2F&response_type=html&action=login"

    @property
    def target_endpoints(self) -> List[str]:
        return ["api-2.0/"]

    async def fill_credentials(self, page: Page, username: str, password: str) -> None:
        return None

    async def submit_login(self, page: Page) -> None:
        try:
            await page.click('button[type="submit"]', timeout=3000)
        except:
            pass

    async def is_logged_in(self, page: Page) -> bool:
        try:
            current_url = page.url
            if "/join/" in current_url or "/login" in current_url:
                return False

            selectors = [
                '[data-purpose="header-user-avatar"]',
                'a[href*="/home/my-courses/"]',
                '[data-purpose="user-dropdown"]',
                '.ud-avatar',
                '[data-purpose="header-my-learning"]',
            ]

            for selector in selectors:
                try:
                    count = await page.locator(selector).count()
                    if count > 0:
                        return True
                except:
                    continue

            if "/home/my-courses" in current_url or "/learning/" in current_url:
                return True

            return False
        except Exception:
            return False

    async def _capture_authorization_header(self, page: Page) -> tuple[Optional[str], Optional[str]]:
        found_token_queue: asyncio.Queue = asyncio.Queue()
        bearer_captured = {"value": False}

        async def handle_request(request):
            try:
                url = request.url
                if "udemy.com" in url and "api-2.0" in url:
                    auth = request.headers.get("authorization")
                    if auth and auth.lower().startswith("bearer "):
                        logging.info("Udemy: Capturado Bearer token de %s", url)
                        bearer_captured["value"] = True

                        cookies = await page.context.cookies()
                        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

                        payload = {
                            "token_type": "bearer_with_cookies",
                            "bearer": auth,
                            "cookie": cookie_str,
                        }
                        await found_token_queue.put((json.dumps(payload), url))
                        return
            except Exception as e:
                logging.debug(f"Udemy: Erro ao processar request: {e}")

        page.on("request", handle_request)

        end_time = time.time() + (self.network_idle_timeout_ms / 1000)
        last_cookie_check = 0

        while time.time() < end_time:
            try:
                token_data = found_token_queue.get_nowait()
                if token_data:
                    return token_data
            except asyncio.QueueEmpty:
                pass

            current_time = time.time()
            if current_time - last_cookie_check >= 2:
                last_cookie_check = current_time

                if await self.is_logged_in(page):
                    logging.info("Udemy: Login detectado, tentando capturar Bearer token...")

                    try:
                        current = page.url
                        if "/home/my-courses" not in current:
                            await page.goto(
                                "https://www.udemy.com/home/my-courses/learning/",
                                wait_until="domcontentloaded"
                            )
                            await asyncio.sleep(2)
                    except Exception as e:
                        logging.debug(f"Udemy: Erro ao navegar: {e}")

                    for _ in range(5):
                        await asyncio.sleep(1)
                        try:
                            token_data = found_token_queue.get_nowait()
                            if token_data:
                                return token_data
                        except asyncio.QueueEmpty:
                            pass

                    logging.info("Udemy: Bearer token não encontrado, usando cookies")
                    cookies = await page.context.cookies()
                    cookie_dict = {c['name']: c['value'] for c in cookies}

                    if self._has_minimum_cookies(cookie_dict):
                        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
                        payload = await self._build_cookie_payload(page, cookie_str)
                        return payload, page.url

            await asyncio.sleep(0.5)

        return None, None

    def _has_minimum_cookies(self, cookie_dict: Dict[str, str]) -> bool:
        has_csrf = "csrftoken" in cookie_dict
        has_session = any(k in cookie_dict for k in ["dj_session_id", "ud_cache_user"])
        return has_csrf and has_session

    async def _build_cookie_payload(self, page: Page, cookie_header: str) -> str:
        local_storage = await self._get_local_storage(page)
        session_storage = await self._get_session_storage(page)
        payload = {
            "token_type": "cookie",
            "cookie": cookie_header,
            "local_storage": local_storage,
            "session_storage": session_storage,
        }
        return json.dumps(payload)

    async def _get_local_storage(self, page: Page) -> Dict[str, str]:
        try:
            return await page.evaluate("() => JSON.parse(JSON.stringify(localStorage))")
        except Exception:
            return {}

    async def _get_session_storage(self, page: Page) -> Dict[str, str]:
        try:
            return await page.evaluate("() => JSON.parse(JSON.stringify(sessionStorage))")
        except Exception:
            return {}


class UdemyPlatform(BasePlatform):
    ASSETS_FIELDS = "asset_type,title,filename,body,captions,media_sources,stream_urls,download_urls,external_url,media_license_token"

    def __init__(self, api_service: ApiService, settings_manager: SettingsManager):
        super().__init__(api_service, settings_manager)
        self._token_fetcher = UdemyTokenFetcher()
        self._captured_local_storage: Dict[str, Any] = {}
        self._captured_session_storage: Dict[str, Any] = {}
        self._course_cache: Dict[str, List[Dict[str, Any]]] = {}

    @classmethod
    def auth_fields(cls) -> List[AuthField]:
        return []

    @classmethod
    def auth_instructions(cls) -> str:
        return """Para baixar a maior dos cursos da Udemy é necessário ter uma CDM válida, a grande maioria está atrás do widevine.
AVISO: Nas configurações utilize delay de acesso nas aulas para evitar que a udemy bloqueie a sua CDM, se ela bloquear, apenas outra vai conseguir baixar de lá. Também evite usar muitos segmentos concorrentes.
1. Marque obrigatoriamente a opção "Emular Navegador".
2. Uma janela do navegador será aberta. Realize TODO o processo manualmente (incluindo 2FA).
3. Após acessar https://www.udemy.com/home/my-courses/learning/ (aba meu aprendizado), clique em OK na aplicação.
    """.strip()

    def authenticate(self, credentials: Dict[str, Any]) -> None:
        self.credentials = credentials
        token_data = self.resolve_access_token(credentials, self._exchange_credentials_for_token)

        self._session = requests.Session(impersonate="chrome120")
        self._session.headers.update({
            "User-Agent": self._settings.user_agent,
            "Referer": "https://www.udemy.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.udemy.com",
        })

        payload = self._try_parse_auth_payload(token_data)

        if payload:
            token_type = payload.get("token_type", "")

            if token_type == "bearer_with_cookies":
                logging.info("Udemy: Usando Bearer token com cookies")
                self._session.headers["Authorization"] = payload.get("bearer")
                if payload.get("cookie"):
                    self._apply_cookie_headers(payload.get("cookie"))

            elif token_type == "cookie":
                logging.info("Udemy: Usando autenticação baseada em cookies")
                cookie_content = payload.get("cookie")
                self._captured_local_storage = payload.get("local_storage", {}) or {}
                self._captured_session_storage = payload.get("session_storage", {}) or {}
                self._apply_cookie_headers(cookie_content)

        elif token_data.lower().startswith("bearer "):
            logging.info("Udemy: Usando autenticação Bearer token")
            self._session.headers["Authorization"] = token_data
        elif token_data.startswith("Cookie:"):
            logging.info("Udemy: Usando cookies manuais")
            self._apply_cookie_headers(token_data[7:].strip())
        elif ";" in token_data and "=" in token_data:
            logging.info("Udemy: Usando cookies fornecidos")
            self._apply_cookie_headers(token_data)
        else:
            logging.info("Udemy: Usando token como Bearer")
            self._session.headers["Authorization"] = f"Bearer {token_data}"

        self._validate_auth()

    def _validate_auth(self) -> None:
        try:
            url = "https://www.udemy.com/api-2.0/users/me/subscribed-courses/"
            params = {"page_size": 1}
            resp = self._session.get(url, params=params, timeout=30)

            if resp.status_code == 403:
                alt_url = "https://www.udemy.com/api-2.0/contexts/me/?header=True"
                resp = self._session.get(alt_url, timeout=30)

            resp.raise_for_status()
            data = resp.json()

            if "results" in data:
                logging.info("Udemy: Autenticação validada com sucesso")
            elif "header" in data:
                header = data.get("header", {})
                is_logged_in = header.get("isLoggedIn", False)
                if not is_logged_in:
                    raise ConnectionError("Sessão não está logada. Faça login novamente.")
                user = header.get("user", {})
                logging.info("Udemy: Autenticado como %s", user.get("display_name", "usuário"))
            else:
                logging.info("Udemy: Autenticação parece válida")

        except Exception as e:
            status = getattr(getattr(e, 'response', None), 'status_code', 0)
            if status == 401:
                raise ConnectionError("Token inválido ou expirado. Faça login novamente.")
            elif status == 403:
                raise ConnectionError(
                    "Acesso negado (403). Isso pode ser proteção anti-bot. "
                    "Tente usar 'Emular Navegador' para fazer login."
                )
            if "HTTPError" in type(e).__name__ or status > 0:
                raise ConnectionError(f"Erro ao validar autenticação: {e}")
        except ConnectionError:
            raise
        except Exception as e:
            logging.warning(f"Udemy: Não foi possível validar autenticação: {e}")

    def _apply_cookie_headers(self, cookie_content: str) -> None:
        self._session.headers["Cookie"] = cookie_content
        self._session.headers["X-Requested-With"] = "XMLHttpRequest"
        self._session.headers["Accept"] = "application/json, text/plain, */*"
        self._session.headers["X-Udemy-Cache-Logged-In"] = "1"

        if "csrftoken=" in cookie_content:
            try:
                parts = cookie_content.split(";")
                for p in parts:
                    p = p.strip()
                    if p.startswith("csrftoken="):
                        csrf = p.split("=", 1)[1]
                        self._session.headers["X-CSRFToken"] = csrf
                        break
            except Exception:
                pass

        try:
            parts = [seg.strip() for seg in cookie_content.split(";") if seg.strip()]
            kv = {}
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    kv[k] = v

            def _set_if_present(header_name: str, cookie_key: str) -> None:
                value = kv.get(cookie_key)
                if value:
                    self._session.headers[header_name] = value

            _set_if_present("X-Udemy-Cache-Release", "ud_cache_release")
            _set_if_present("X-Udemy-Cache-User", "ud_cache_user")
            _set_if_present("X-Udemy-Cache-Brand", "ud_cache_brand")
            _set_if_present("X-Udemy-Cache-Marketplace-Country", "ud_cache_marketplace_country")
            _set_if_present("X-Udemy-Cache-Price-Country", "ud_cache_price_country")
            _set_if_present("X-Udemy-Cache-Version", "ud_cache_version")
            _set_if_present("X-Udemy-Cache-Language", "ud_cache_language")
            _set_if_present("X-Udemy-Cache-Device", "ud_cache_device")
            _set_if_present("X-Udemy-Cache-Campaign-Code", "ud_cache_campaign_code")
            _set_if_present("X-Udemy-Client-Id", "client_id")
        except Exception:
            pass

    def _try_parse_auth_payload(self, token_data: str) -> Optional[Dict[str, Any]]:
        try:
            parsed = json.loads(token_data)
        except Exception:
            return None

        if isinstance(parsed, dict) and parsed.get("token_type"):
            return parsed

        return None

    def _exchange_credentials_for_token(self, username: str, password: str, credentials: Dict[str, Any]) -> str:
        use_browser_emulation = bool(credentials.get("browser_emulation"))
        confirmation_event = credentials.get("manual_auth_confirmation")
        custom_ua = self._settings.user_agent

        if not use_browser_emulation:
            raise ConnectionError("Para Udemy é obrigatório habilitar 'Emular Navegador' e efetuar login manualmente.")

        try:
            return self._token_fetcher.fetch_token(
                username,
                password,
                headless=not use_browser_emulation,
                user_agent=custom_ua,
                wait_for_user_confirmation=(
                    confirmation_event.wait if confirmation_event else None
                ),
            )
        except Exception as exc:
            raise ConnectionError("Falha no login via Browser. Verifique credenciais ou 2FA.") from exc

    def fetch_courses(self) -> List[Dict[str, Any]]:
        if not self._session:
            raise ConnectionError("A sessão não foi autenticada.")

        url = "https://www.udemy.com/api-2.0/users/me/subscribed-courses/"
        params = {
            "ordering": "-last_accessed",
            "fields[course]": "id,title,url,image_480x270",
            "page": 1,
            "page_size": 100,
            "is_archived": False
        }

        all_courses = []
        while url:
            is_initial = "api-2.0/users/me/subscribed-courses/" in url and "page=" not in url
            p = params if is_initial else None

            resp = self._session.get(url, params=p)
            resp.raise_for_status()
            data = resp.json()

            for result in data.get("results", []):
                all_courses.append({
                    "id": str(result.get("id")),
                    "name": result.get("title"),
                    "url": f"https://www.udemy.com{result.get('url')}",
                    "image": result.get("image_480x270"),
                    "slug": result.get("url", "").strip("/").split("/")[-2] if result.get("url") else ""
                })

            url = data.get("next")

        return all_courses

    def fetch_course_content(self, courses: List[Dict[str, Any]]) -> Dict[str, Any]:
        all_content = {}

        for course in courses:
            course_id = course["id"]
            try:
                curriculum = self._fetch_curriculum(course_id)
                self._course_cache[course_id] = curriculum

                processed_modules = []
                current_module = None

                if not any(item.get("_class") == "chapter" for item in curriculum):
                    current_module = {
                        "id": f"c_{course_id}_default",
                        "title": course.get("name", "Course Content"),
                        "lessons": []
                    }
                    processed_modules.append(current_module)

                for item in curriculum:
                    _class = item.get("_class")

                    if _class == "chapter":
                        current_module = {
                            "id": str(item.get("id")),
                            "title": item.get("title", "Untitled Module"),
                            "lessons": []
                        }
                        processed_modules.append(current_module)

                    elif _class == "lecture":
                        if current_module is None:
                            current_module = {
                                "id": f"c_{course_id}_startup",
                                "title": "Introduction",
                                "lessons": []
                            }
                            processed_modules.append(current_module)

                        asset = item.get("asset") or {}
                        is_encrypted = bool(asset.get("media_license_token"))

                        current_module["lessons"].append({
                            "id": str(item.get("id")),
                            "title": item.get("title", "Untitled Lesson"),
                            "name": item.get("title", "Untitled Lesson"),
                            "asset": asset,
                            "supplementary_assets": item.get("supplementary_assets", []),
                            "is_encrypted": is_encrypted,
                            "_course_id": course_id,
                        })

                course_with_modules = course.copy()
                course_with_modules["modules"] = processed_modules
                course_with_modules["title"] = course.get("name", "Untitled Course")

                all_content[course_id] = course_with_modules

            except Exception as e:
                logging.error(f"Error fetching curriculum for course {course.get('name', course_id)}: {e}")

        return all_content

    def _fetch_curriculum(self, course_id: str) -> List[Dict[str, Any]]:
        url = f"https://www.udemy.com/api-2.0/courses/{course_id}/cached-subscriber-curriculum-items"
        params = {
            "page_size": 100,
            "fields[lecture]": "id,title,asset,supplementary_assets",
            "fields[chapter]": "id,title",
            "fields[asset]": self.ASSETS_FIELDS,
        }

        items = []
        current_url = url
        first_call = True
        retry_count = 0
        max_retries = 3

        while current_url:
            try:
                p = params if first_call else None
                resp = self._session.get(current_url, params=p, timeout=60)
                resp.raise_for_status()
                data = resp.json()

                items.extend(data.get("results", []))
                retry_count = 0

                next_url = data.get("next")
                if next_url:
                    current_url = next_url.replace("%5B", "[").replace("%5D", "]").replace("%2C", ",")
                else:
                    current_url = None
                first_call = False
            except Exception as e:
                error_str = str(e).lower()
                status = getattr(getattr(e, 'response', None), 'status_code', 0)

                if status == 503:
                    logging.warning(f"Cached endpoint returned 503, falling back for course {course_id}")
                    return self._fetch_curriculum_fallback(course_id)

                if "timeout" in error_str or "timed out" in error_str:
                    retry_count += 1
                    if retry_count <= max_retries:
                        logging.warning(f"Timeout fetching curriculum, retry {retry_count}/{max_retries}")
                        import time
                        time.sleep(2 * retry_count)
                        continue
                    else:
                        logging.error(f"Max retries reached for curriculum fetch")
                        raise

                if "HTTPError" in type(e).__name__ or status > 0:
                    raise

        return items

    def _fetch_curriculum_fallback(self, course_id: str) -> List[Dict[str, Any]]:
        url = f"https://www.udemy.com/api-2.0/courses/{course_id}/cached-subscriber-curriculum-items"
        params = {
            "page_size": 1000,
            "fields[lecture]": "id,title,asset",
        }
        resp = self._session.get(url, params=params, timeout=120)
        resp.raise_for_status()
        return resp.json().get("results", [])

    def fetch_lesson_details(self, lesson: Dict[str, Any], course_slug: str, course_id: str, module_id: str) -> LessonContent:
        content = LessonContent()
        lesson_id = lesson.get("id")

        asset = lesson.get("asset")
        supplementary_assets = lesson.get("supplementary_assets", [])

        if asset and asset.get("asset_type", "").lower() in ("video", "videomashup"):
            has_sources = asset.get("media_sources") or asset.get("stream_urls")
            has_license = asset.get("media_license_token")

            needs_fetch = not has_sources or (has_license and not asset.get("media_sources"))

            if needs_fetch:
                try:
                    lecture_data = self._fetch_lecture(course_id, lesson_id)
                    asset = lecture_data.get("asset", asset)
                    supplementary_assets = lecture_data.get("supplementary_assets", supplementary_assets)
                except Exception as e:
                    logging.warning(f"Failed to fetch lecture details for {lesson_id}: {e}")

        if asset:
            asset_type = asset.get("asset_type", "").lower()
            asset_id = asset.get("id")

            if asset_type in ("video", "videomashup"):
                video_url, is_encrypted, mpd_url = self._extract_video_url(asset)
                media_license_token = asset.get("media_license_token")

                extra_props = {
                    "is_encrypted": is_encrypted,
                }

                if is_encrypted and media_license_token:
                    extra_props["media_license_token"] = media_license_token
                    extra_props["mpd_url"] = mpd_url
                    extra_props["hls_url"] = video_url
                    extra_props["course_id"] = course_id
                    extra_props["lecture_id"] = str(lesson_id)

                if video_url or mpd_url:
                    content.videos.append(Video(
                        video_id=str(asset_id),
                        url=mpd_url if is_encrypted else video_url,
                        order=1,
                        title=lesson.get('name', 'Video'),
                        size=0,
                        duration=asset.get("length", 0) or asset.get("time_estimation", 0) or 0,
                        extra_props=extra_props
                    ))
            elif asset_type == "article":
                data_field = asset.get("data") or {}
                body = asset.get("body") or data_field.get("body", "")
                if body:
                    from src.app.models import Description
                    content.description = Description(text=body, description_type="html")
            elif asset_type in ("file", "e-book"):
                download_urls = asset.get("download_urls") or {}
                file_urls = download_urls.get(asset.get("asset_type"), []) if download_urls else []
                if file_urls:
                    filename = asset.get("filename", "file")
                    content.attachments.append(Attachment(
                        attachment_id=str(asset_id),
                        url=file_urls[0].get("file", ""),
                        filename=filename,
                        order=1,
                        extension=filename.split(".")[-1] if "." in filename else "",
                        size=0
                    ))
            elif asset_type == "presentation":
                url_set = asset.get("url_set") or {}
                pres_urls = url_set.get("Presentation", []) if url_set else []
                if pres_urls:
                    filename = asset.get("filename", "presentation.pdf")
                    content.attachments.append(Attachment(
                        attachment_id=str(asset_id),
                        url=pres_urls[0].get("file", ""),
                        filename=filename,
                        order=1,
                        extension=filename.split(".")[-1] if "." in filename else "pdf",
                        size=0
                    ))

        for idx, supp in enumerate(supplementary_assets or [], 1):
            supp_type = supp.get("asset_type", "")
            supp_id = supp.get("id")
            filename = supp.get("filename") or supp.get("title", f"attachment_{idx}")

            download_urls = supp.get("download_urls") or {}
            if download_urls:
                file_urls = download_urls.get(supp_type) or download_urls.get("File", [])
                if file_urls:
                    content.attachments.append(Attachment(
                        attachment_id=str(supp_id),
                        url=file_urls[0].get("file", ""),
                        filename=filename,
                        order=idx,
                        extension=filename.split(".")[-1] if "." in filename else "",
                        size=0
                    ))
            elif supp.get("external_url"):
                from src.app.models import AuxiliaryURL
                content.auxiliary_urls.append(AuxiliaryURL(
                    url_id=str(supp_id),
                    url=supp.get("external_url"),
                    order=idx,
                    title=supp.get("title", "External Link"),
                    description=""
                ))

        return content

    def _fetch_lecture(self, course_id: str, lecture_id: str) -> Dict[str, Any]:
        url = f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/lectures/{lecture_id}"
        params = {
            "fields[lecture]": "id,title,asset,supplementary_assets,description,download_url,is_free",
            "fields[asset]": "asset_type,length,media_license_token,course_is_drmed,media_sources,captions,thumbnail_sprite,slides,slide_urls,download_urls,external_url,stream_urls",
        }
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def refresh_license_token(self, course_id: str, lecture_id: str) -> Optional[str]:
        """Fetch a fresh media_license_token for a specific lecture."""
        try:
            lecture_data = self._fetch_lecture(course_id, lecture_id)
            asset = lecture_data.get("asset", {})
            return asset.get("media_license_token")
        except Exception as e:
            logging.warning(f"Failed to refresh license token for lecture {lecture_id}: {e}")
            return None

    def _extract_video_url(self, asset: Dict[str, Any]) -> tuple[Optional[str], bool, Optional[str]]:
        """
        Extract video URL from asset. Returns (hls_url, is_encrypted, mpd_url).
        Quality selection is handled by the downloader.
        """
        has_license_token = bool(asset.get("media_license_token"))

        media_sources = asset.get("media_sources") or []
        stream_urls = asset.get("stream_urls") or {}
        video_streams = stream_urls.get("Video", []) if stream_urls else []

        sources = media_sources if media_sources else video_streams

        if not sources:
            return None, has_license_token, None

        mpd_url = None
        hls_url = None
        has_non_encrypted = False

        for source in sources:
            src_type = source.get("type", "")
            src_url = source.get("src") or source.get("file", "")

            if src_type == "application/dash+xml":
                mpd_url = src_url
            elif src_type == "application/x-mpegURL" or ".m3u8" in src_url:
                # Check if URL is encrypted (contains /encrypted-files or uses -enc- CDN)
                is_encrypted_url = "/encrypted-files" in src_url or "-enc-" in src_url
                if not is_encrypted_url:
                    has_non_encrypted = True
                if not hls_url:
                    hls_url = src_url

        # Only truly encrypted if license token exists AND no non-encrypted HLS available
        is_encrypted = has_license_token and not has_non_encrypted

        return hls_url, is_encrypted, mpd_url

    def download_attachment(self, attachment: "Attachment", download_path: Path, course_slug: str, course_id: str, module_id: str) -> bool:
        try:
            r = self._session.get(attachment.url, stream=True)
            r.raise_for_status()
            with open(download_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        except Exception:
            return False


PlatformFactory.register_platform("Udemy", UdemyPlatform)
