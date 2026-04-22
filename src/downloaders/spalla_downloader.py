import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp

from src.config.settings_manager import SettingsManager
from src.utils.retry import build_ytdlp_retry_config
from src.utils.filesystem import get_executable_path
from .base import BaseDownloader


SPALLA_UUID_REGEX = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SPALLA_AUTH_URL = "https://beyond.spalla.io/cache/auth/{uuid}"
SPALLA_CONFIG_URL = "https://beyond.spalla.io/cache/config/{uuid}"
SPALLA_PLAYER_ORIGIN = "https://beyond.spalla.io"
SPALLA_PLAYER_REFERER = "https://beyond.spalla.io/player/"
SPALLA_VERSION = "v0.4.2-12"


class SpallaDownloader(BaseDownloader):
    """
    Downloader for Spalla-hosted videos (beyond.spalla.io / plural.cdn.spalla.io).

    Accepts either the iframe URL (https://beyond.spalla.io/player/?video={uuid})
    or any URL carrying a Spalla UUID. Performs the auth/config handshake to
    obtain the JWT-signed HLS playlist and delegates the actual transfer to
    yt-dlp.
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)
        self.settings = self.settings_manager.get_settings()

    def download_video(
        self,
        url: str,
        session: requests.Session,
        download_path: Path,
        extra_props: dict = None,
    ) -> bool:
        extra_props = extra_props or {}
        video_uuid = self._extract_uuid(url, extra_props)
        if not video_uuid:
            logging.error("[Spalla] Não foi possível extrair o UUID do vídeo a partir da URL: %s", url)
            return False

        try:
            playlist_url = self._resolve_playlist(video_uuid, extra_props)
        except Exception as exc:
            logging.error("[Spalla] Falha ao negociar acesso ao vídeo %s: %s", video_uuid, exc)
            return False

        if not playlist_url:
            return False

        return self._download_with_ytdlp(playlist_url, download_path, extra_props)

    def _extract_uuid(self, url: str, extra_props: Dict[str, Any]) -> Optional[str]:
        candidate = extra_props.get("spalla_uuid") or extra_props.get("video_uuid")
        if candidate and SPALLA_UUID_REGEX.fullmatch(str(candidate)):
            return str(candidate)

        parsed = urlparse(url)
        query_video = parse_qs(parsed.query).get("video", [None])[0]
        if query_video and SPALLA_UUID_REGEX.fullmatch(query_video):
            return query_video

        match = SPALLA_UUID_REGEX.search(url)
        return match.group(0) if match else None

    def _resolve_playlist(self, video_uuid: str, extra_props: Dict[str, Any]) -> Optional[str]:
        uid = str(uuid.uuid4())
        origin_referer = extra_props.get("origin_referer") or extra_props.get("site_referer") or "https://beyond.spalla.io/"
        timestamp = int(time.time() * 1000)

        headers = {
            "User-Agent": self.settings.user_agent,
            "Origin": SPALLA_PLAYER_ORIGIN,
            "Referer": SPALLA_PLAYER_REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9",
        }

        local = requests.Session()
        local.headers.update(headers)

        auth_params = {
            "_v": SPALLA_VERSION,
            "video": video_uuid,
            "uid": uid,
            "aid": "null",
            "_t": timestamp,
            "r": origin_referer,
            "i": "1",
            "wv": "0",
        }
        auth_response = local.get(
            SPALLA_AUTH_URL.format(uuid=video_uuid),
            params=auth_params,
            timeout=20,
        )
        auth_response.raise_for_status()
        auth_data = auth_response.json()
        if auth_data.get("status") and auth_data["status"] != "ok":
            logging.warning("[Spalla] Resposta de auth não-ok para %s: %s", video_uuid, auth_data)

        config_params = {
            "_v": SPALLA_VERSION,
            "_t": timestamp + 1,
            "uid": uid,
            "aid": "null",
            "dpr": "1",
        }
        config_response = local.get(
            SPALLA_CONFIG_URL.format(uuid=video_uuid),
            params=config_params,
            timeout=20,
        )
        config_response.raise_for_status()
        config = config_response.json()

        jwt_token = config.get("lm_live_jwt_token")
        if not jwt_token:
            logging.error("[Spalla] Config para %s sem lm_live_jwt_token.", video_uuid)
            return None

        cdn_url = self._pick_cdn(config.get("lm_live_cdns") or [])
        playlist_url = (
            f"{cdn_url}{video_uuid}/playlist.m3u8?sjwt={jwt_token}&uid={uid}&magica=sim"
        )
        logging.info("[Spalla] Playlist negociada para %s via %s", video_uuid, cdn_url)
        return playlist_url

    @staticmethod
    def _pick_cdn(cdns: List[Dict[str, Any]]) -> str:
        usable = [cdn for cdn in cdns if cdn.get("URL")]
        if not usable:
            return "https://plural.cdn.spalla.io/vod/"
        preferred_name = None
        for cdn in usable:
            if cdn.get("Nome") == "PluralVOD":
                preferred_name = cdn
                break
        chosen = preferred_name or min(usable, key=lambda c: c.get("Peso", 255))
        return chosen["URL"]

    def _download_with_ytdlp(
        self,
        playlist_url: str,
        download_path: Path,
        extra_props: Dict[str, Any],
    ) -> bool:
        retry_opts = build_ytdlp_retry_config(self.settings)
        output_template = self.build_ytdlp_output_template(download_path, self.settings)
        ffmpeg_exe = get_executable_path("ffmpeg", getattr(self.settings, "ffmpeg_path", None))

        http_headers = {
            "User-Agent": self.settings.user_agent,
            "Origin": SPALLA_PLAYER_ORIGIN,
            "Referer": SPALLA_PLAYER_REFERER,
        }

        ydl_opts = {
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress": True,
            "http_headers": http_headers,
            "concurrent_fragment_downloads": max(1, self.settings.max_concurrent_segment_downloads),
            **retry_opts,
        }
        ydl_opts.update(self.build_quality_opts(self.settings))

        if ffmpeg_exe:
            ydl_opts["ffmpeg_location"] = str(Path(ffmpeg_exe).parent)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([playlist_url])
            return True
        except Exception as exc:
            logging.error("[Spalla] yt-dlp falhou ao baixar %s: %s", playlist_url, exc)
            return False
