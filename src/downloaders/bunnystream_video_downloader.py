from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from base64 import b64decode
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
import yt_dlp
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

from src.config.settings_manager import AppSettings, SettingsManager
from src.utils.filesystem import get_executable_path
from src.utils.retry import build_ytdlp_retry_config

from .base import BaseDownloader


IFRAME_ORIGIN = "https://iframe.mediadelivery.net"
IFRAME_REFERER = "https://iframe.mediadelivery.net/"
LICENSE_URL_TEMPLATE = "https://video.bunnycdn.com/WidevineLicense/{library_id}/{video_guid}"
WIDEVINE_KEYFORMAT = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"

EMBED_PATH_RE = re.compile(
    r"/embed/(?P<library_id>\d+)/(?P<video_guid>[0-9a-fA-F-]{36})"
)
PLAYLIST_URL_RE = re.compile(
    r"https://vz-[0-9a-fA-F-]+\.b-cdn\.net/[0-9a-fA-F-]{36}/playlist\.m3u8"
)
EXT_X_KEY_RE = re.compile(
    r'#EXT-X-KEY:[^\n]*KEYFORMAT="' + re.escape(WIDEVINE_KEYFORMAT) + r'"[^\n]*',
    re.IGNORECASE,
)
PSSH_DATA_URI_RE = re.compile(r'URI="data:text/plain;base64,([^"]+)"')
M3U8_STREAM_INF_RE = re.compile(r"^(?!#)(.+\.m3u8)\s*$", re.MULTILINE)
M3U8_MEDIA_URI_RE = re.compile(r'#EXT-X-MEDIA:[^\n]*URI="([^"]+\.m3u8)"', re.IGNORECASE)


class BunnyStreamDownloader(BaseDownloader):
    """Downloader for Bunny.net Stream videos embedded via iframe.mediadelivery.net."""

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)
        self.settings: AppSettings = self.settings_manager.get_settings()

    @staticmethod
    def _parse_embed_ids(url: str) -> Optional[Dict[str, str]]:
        match = EMBED_PATH_RE.search(urlparse(url).path)
        if not match:
            return None
        return match.groupdict()

    def _new_bunny_session(self) -> requests.Session:
        """Creates a fresh session so the platform's Bearer JWT never leaks to Bunny."""
        sess = requests.Session()
        sess.headers.update(
            {
                "User-Agent": self.settings.user_agent,
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )
        return sess

    def _fetch_embed_html(
        self, embed_url: str, bunny_session: requests.Session, parent_referer: str
    ) -> Optional[str]:
        """Fetches the iframe HTML, using the parent site as Referer (matches browser)."""
        try:
            resp = bunny_session.get(
                embed_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": parent_referer,
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            logging.error("Bunny: falha ao abrir embed %s: %s", embed_url, exc)
            return None

    @staticmethod
    def _extract_playlist_url(embed_html: str) -> Optional[str]:
        match = PLAYLIST_URL_RE.search(embed_html)
        return match.group(0) if match else None

    def _fetch_m3u8(self, url: str, bunny_session: requests.Session) -> Optional[str]:
        try:
            resp = bunny_session.get(
                url,
                headers={
                    "Accept": "*/*",
                    "Origin": IFRAME_ORIGIN,
                    "Referer": IFRAME_REFERER,
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            logging.error("Bunny: falha ao baixar %s: %s", url, exc)
            return None

    @staticmethod
    def _collect_variants(playlist_url: str, master_body: str) -> List[str]:
        base_dir = playlist_url.rsplit("/", 1)[0] + "/"
        candidates: List[str] = []
        seen: set = set()

        def _add(ref: str) -> None:
            absolute = ref if ref.startswith("http") else base_dir + ref
            if absolute not in seen:
                seen.add(absolute)
                candidates.append(absolute)

        for ref in M3U8_STREAM_INF_RE.findall(master_body):
            _add(ref.strip())
        for ref in M3U8_MEDIA_URI_RE.findall(master_body):
            _add(ref.strip())
        return candidates

    def _extract_pssh(
        self, playlist_url: str, bunny_session: requests.Session
    ) -> Optional[str]:
        """Descends from the master playlist into variants to find the Widevine PSSH."""
        master = self._fetch_m3u8(playlist_url, bunny_session)
        if not master:
            return None

        key_match = EXT_X_KEY_RE.search(master)
        if key_match:
            pssh_match = PSSH_DATA_URI_RE.search(key_match.group(0))
            if pssh_match:
                return pssh_match.group(1)

        for variant_url in self._collect_variants(playlist_url, master):
            body = self._fetch_m3u8(variant_url, bunny_session)
            if not body:
                continue
            key_match = EXT_X_KEY_RE.search(body)
            if not key_match:
                continue
            pssh_match = PSSH_DATA_URI_RE.search(key_match.group(0))
            if pssh_match:
                return pssh_match.group(1)

        return None

    def _get_license_keys(
        self,
        pssh: str,
        library_id: str,
        video_guid: str,
        bunny_session: requests.Session,
    ) -> Optional[List[str]]:
        try:
            cdm_path = Path(self.settings.cdm_path)
            wvd_file = next(cdm_path.glob("*.wvd"), None)
            if not wvd_file:
                logging.error(
                    "Bunny: arquivo .wvd não encontrado em %s. Gere um CDM antes de continuar.",
                    self.settings.cdm_path,
                )
                return None

            device = Device.load(str(wvd_file))
            cdm = Cdm.from_device(device)

            pssh_obj = PSSH(b64decode(pssh))
            session_id = cdm.open()
            try:
                challenge = cdm.get_license_challenge(session_id, pssh_obj)

                license_url = LICENSE_URL_TEMPLATE.format(
                    library_id=library_id, video_guid=video_guid
                )
                response = bunny_session.post(
                    license_url,
                    data=challenge,
                    headers={
                        "Accept": "*/*",
                        "Content-Type": "application/octet-stream",
                        "Origin": IFRAME_ORIGIN,
                        "Referer": IFRAME_REFERER,
                    },
                    timeout=30,
                )
                if response.status_code != 200:
                    logging.error(
                        "Bunny: servidor de licença retornou %s: %s",
                        response.status_code,
                        response.text[:200],
                    )
                    return None

                cdm.parse_license(session_id, response.content)
                keys = [
                    f"{k.kid.hex}:{k.key.hex()}"
                    for k in cdm.get_keys(session_id)
                    if k.type == "CONTENT"
                ]
                return keys or None
            finally:
                cdm.close(session_id)
        except Exception as exc:
            logging.error("Bunny: erro ao obter chaves Widevine: %s", exc, exc_info=True)
            return None

    def _download_encrypted(self, playlist_url: str, temp_path: Path) -> List[str]:
        retry_opts = build_ytdlp_retry_config(self.settings)
        ydl_opts: Dict[str, Any] = {
            "format": "bestvideo+bestaudio/best",
            "allow_unplayable_formats": True,
            # Why: bestvideo+bestaudio downloads two HLS variants in parallel.
            # Bunny serves audio in fmp4 too, so both ends up with ext=mp4 and
            # collide on the same path → Windows file lock. format_id keeps
            # them distinct without dragging in the long video GUID via %(id)s.
            "outtmpl": str(temp_path / "video.encrypted.f%(format_id)s.%(ext)s"),
            "keepvideo": True,
            "http_headers": {
                "User-Agent": self.settings.user_agent,
                "Accept": "*/*",
                "Origin": IFRAME_ORIGIN,
                "Referer": IFRAME_REFERER,
            },
            "quiet": True,
            "no_warnings": True,
            "concurrent_fragment_downloads": max(
                1, self.settings.max_concurrent_segment_downloads
            ),
            **retry_opts,
            **self.build_js_runtime_opts(self.settings),
        }

        downloaded: List[str] = []
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(playlist_url, download=True)
            for entry in info.get("requested_downloads") or []:
                downloaded.append(entry["filepath"])
            if not downloaded and info.get("filepath"):
                downloaded.append(info["filepath"])

        if not downloaded:
            for name in os.listdir(temp_path):
                full = temp_path / name
                if full.is_file() and ".encrypted." in name and not name.endswith(".part"):
                    downloaded.append(str(full))

        return downloaded

    def _decrypt_and_merge(
        self,
        encrypted_files: List[str],
        keys: List[str],
        download_path: Path,
        temp_path: Path,
    ) -> bool:
        mp4decrypt = get_executable_path("mp4decrypt", getattr(self.settings, "bento4_path", None))
        if not mp4decrypt:
            logging.error("Bunny: mp4decrypt (Bento4) não encontrado.")
            return False

        ffmpeg = get_executable_path("ffmpeg", getattr(self.settings, "ffmpeg_path", None))
        if not ffmpeg:
            logging.error("Bunny: ffmpeg não encontrado.")
            return False

        subprocess_kwargs: Dict[str, Any] = {
            "check": True,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            subprocess_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        decrypted: List[str] = []
        for enc_file in encrypted_files:
            base, ext = os.path.splitext(enc_file)
            dec_file = f"{base}.decrypted{ext}"
            cmd = [mp4decrypt]
            for key in keys:
                cmd.extend(["--key", key])
            cmd.extend([enc_file, dec_file])
            try:
                subprocess.run(cmd, **subprocess_kwargs)
                decrypted.append(dec_file)
            except subprocess.CalledProcessError as exc:
                logging.error(
                    "Bunny: mp4decrypt falhou em %s: %s",
                    os.path.basename(enc_file),
                    exc.stderr.decode(errors="replace"),
                )
                return False

        merged = str(temp_path / "video.mp4")
        merge_cmd = [ffmpeg, "-y"]
        for dec in decrypted:
            merge_cmd.extend(["-i", dec])
        merge_cmd.extend(["-c", "copy", merged])
        try:
            subprocess.run(merge_cmd, **subprocess_kwargs)
        except subprocess.CalledProcessError as exc:
            logging.error("Bunny: ffmpeg merge falhou: %s", exc.stderr.decode(errors="replace"))
            return False

        final_path = str(download_path) + ".mp4"
        shutil.move(merged, final_path)
        return True

    def _download_plain(self, playlist_url: str, download_path: Path) -> bool:
        retry_opts = build_ytdlp_retry_config(self.settings)
        ydl_opts: Dict[str, Any] = {
            "outtmpl": self.build_ytdlp_output_template(download_path, self.settings),
            "noplaylist": True,
            "http_headers": {
                "User-Agent": self.settings.user_agent,
                "Accept": "*/*",
                "Origin": IFRAME_ORIGIN,
                "Referer": IFRAME_REFERER,
            },
            "quiet": True,
            "no_warnings": True,
            "concurrent_fragment_downloads": max(
                1, self.settings.max_concurrent_segment_downloads
            ),
            **retry_opts,
            **self.build_quality_opts(self.settings),
            **self.build_js_runtime_opts(self.settings),
        }

        if self.settings.download_subtitles:
            ydl_opts["writesubtitles"] = True
            ydl_opts["subtitleslangs"] = ["all"]
            if self.settings.hardcode_subtitles:
                ydl_opts["embedsubtitles"] = True

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([playlist_url])
        return True

    def download_video(
        self,
        url: str,
        session: requests.Session,
        download_path: Path,
        extra_props: Optional[Dict[str, Any]] = None,
    ) -> bool:
        extra_props = extra_props or {}

        ids = self._parse_embed_ids(url)
        if not ids:
            logging.error("Bunny: URL de embed inválida: %s", url)
            return False

        parent_referer = extra_props.get("parent_referer") or extra_props.get("referer")
        bunny_session = self._new_bunny_session()

        embed_html = self._fetch_embed_html(url, bunny_session, parent_referer)
        if not embed_html:
            return False

        playlist_url = self._extract_playlist_url(embed_html)
        if not playlist_url:
            logging.error("Bunny: playlist.m3u8 não localizada no HTML do embed.")
            return False

        enable_drm = bool(extra_props.get("enable_drm"))

        if not enable_drm:
            try:
                return self._download_plain(playlist_url, download_path)
            except Exception as exc:
                logging.error("Bunny: download direto falhou: %s", exc, exc_info=True)
                return False

        if not self.settings.download_widevine:
            logging.error(
                "Bunny: vídeo protegido por DRM, porém a configuração download_widevine está desativada."
            )
            return False

        cdm_path = Path(self.settings.cdm_path)
        if not cdm_path.exists() or not cdm_path.is_dir():
            logging.error("Bunny: pasta CDM inválida: %s", self.settings.cdm_path)
            return False

        pssh = self._extract_pssh(playlist_url, bunny_session)
        if not pssh:
            logging.error("Bunny: PSSH Widevine não encontrado no HLS.")
            return False

        keys = self._get_license_keys(pssh, ids["library_id"], ids["video_guid"], bunny_session)
        if not keys:
            return False

        logging.info("Bunny: chaves Widevine obtidas (%d).", len(keys))

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            encrypted = self._download_encrypted(playlist_url, temp_path)
            if not encrypted:
                logging.error("Bunny: nenhum arquivo criptografado baixado.")
                return False
            return self._decrypt_and_merge(encrypted, keys, download_path, temp_path)
