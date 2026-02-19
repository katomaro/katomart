import json
import logging
import re
import subprocess
import os
import shutil
import tempfile
import struct

from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlencode
from base64 import b64encode, b64decode

import yt_dlp
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

from .base import BaseDownloader
from src.config.settings_manager import AppSettings, SettingsManager
from src.utils.retry import build_ytdlp_retry_config
from src.utils.filesystem import get_executable_path


class UdemyDownloader(BaseDownloader):

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)
        self.settings: AppSettings = self.settings_manager.get_settings()

    def _extract_pssh(self, mpd_content: str) -> Optional[str]:
        try:
            pssh_match = re.search(
                r'<(?:[a-zA-Z0-9]+:)?pssh[^>]*>(.*?)</(?:[a-zA-Z0-9]+:)?pssh>',
                mpd_content,
                re.DOTALL | re.IGNORECASE
            )
            if pssh_match:
                return pssh_match.group(1).strip()
            return None
        except Exception as e:
            logging.error(f"Error extracting PSSH: {e}")
            return None

    def _extract_pssh_from_init(self, mpd_content: str, mpd_url: str, session) -> Optional[str]:
        try:
            logging.info("Tentando extrair PSSH do segmento de inicialização...")

            mpd_base_part = mpd_url.split("?")[0] if "?" in mpd_url else mpd_url
            mpd_dir = mpd_base_part.rsplit("/", 1)[0] + "/" if "/" in mpd_base_part else mpd_base_part + "/"

            init_match = re.search(r'initialization="([^"]+)"', mpd_content)
            if not init_match:
                logging.warning("Atributo initialization não encontrado no MPD.")
                return None

            init_relative = init_match.group(1).replace("&amp;", "&")

            if '$RepresentationID$' in init_relative:
                rep_match = re.search(r'<Representation[^>]+id="([^"]+)"', mpd_content)
                if rep_match:
                    rep_id = rep_match.group(1)
                    init_relative = init_relative.replace('$RepresentationID$', rep_id)

            base_url_match = re.search(r'<BaseURL>(.*?)</BaseURL>', mpd_content)
            xml_base_url = base_url_match.group(1).strip().replace("&amp;", "&") if base_url_match else ""

            init_path_only = init_relative.split("?")[0] if "?" in init_relative else init_relative
            init_query = init_relative.split("?")[1] if "?" in init_relative else ""

            full_file_url = f"{mpd_dir}{xml_base_url}{init_path_only}"
            if init_query:
                full_file_url = f"{full_file_url}?{init_query}"

            response = session.get(full_file_url)
            if response.status_code != 200:
                logging.warning(f"Falha ao baixar init segment: {response.status_code}")
                return None

            data = response.content
            widevine_sys_id = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
            sys_id_pos = data.find(widevine_sys_id)

            if sys_id_pos == -1:
                logging.warning("Widevine SystemID não encontrado no arquivo init.")
                return None

            box_start = sys_id_pos - 12
            if box_start < 0:
                return None

            if data[box_start + 4: box_start + 8] != b'pssh':
                return None

            box_size = struct.unpack(">I", data[box_start: box_start + 4])[0]
            if box_start + box_size > len(data):
                return None

            pssh_box = data[box_start: box_start + box_size]
            return b64encode(pssh_box).decode('utf-8')

        except Exception as e:
            logging.error(f"Erro no fallback PSSH: {e}", exc_info=True)
            return None

    def _get_license_keys(self, pssh: str, license_url: str, session) -> Optional[List[str]]:
        try:
            cdm_path = Path(self.settings.cdm_path)
            wvd_file = next(cdm_path.glob("*.wvd"), None)

            if wvd_file:
                device = Device.load(str(wvd_file))
            else:
                logging.error("Arquivo .wvd não encontrado para descriptografia.")
                return None

            cdm = Cdm.from_device(device)

            pssh_bytes = b64decode(pssh)
            pssh_obj = PSSH(pssh_bytes)
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, pssh_obj)

            headers = {}
            if hasattr(session, 'headers'):
                for header, value in session.headers.items():
                    header_key = header
                    if header.lower() == 'cookie':
                        header_key = 'Cookie'
                    elif header.lower() == 'user-agent':
                        header_key = 'User-Agent'
                    headers[header_key] = value

            headers.update({
                "Accept": "*/*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Content-Type": "application/octet-stream",
                "Origin": "https://www.udemy.com",
                "Referer": "https://www.udemy.com/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            })

            logging.debug(f"Enviando challenge para: {license_url}")
            response = session.post(license_url, data=challenge, headers=headers)

            if response.status_code != 200:
                logging.error(f"Erro ao obter licença: {response.status_code} - {response.text}")
                cdm.close(session_id)
                return None

            cdm.parse_license(session_id, response.content)

            keys = []
            for key in cdm.get_keys(session_id):
                key_type = str(key.type).upper()
                if key_type == 'CONTENT' or 'CONTENT' in key_type:
                    keys.append(f"{key.kid.hex}:{key.key.hex()}")

            cdm.close(session_id)
            return keys

        except Exception as e:
            logging.error(f"Erro durante o processo de obtenção de chaves: {e}", exc_info=True)
            return None

    def _refresh_license_token(self, session, course_id: str, lecture_id: str) -> Optional[str]:
        """Fetch a fresh media_license_token from Udemy API."""
        try:
            url = f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/lectures/{lecture_id}"
            params = {
                "fields[lecture]": "asset",
                "fields[asset]": "media_license_token",
            }
            resp = session.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("asset", {}).get("media_license_token")
                if token:
                    logging.debug(f"Token de licença renovado para lecture {lecture_id}")
                    return token
            logging.warning(f"Falha ao renovar token: {resp.status_code}")
            return None
        except Exception as e:
            logging.warning(f"Erro ao renovar token de licença: {e}")
            return None

    def download_video(self, url: str, session, download_path: Path, extra_props: Optional[Dict[str, Any]] = None) -> bool:
        try:
            extra_props = extra_props or {}
            is_encrypted = extra_props.get("is_encrypted", False)
            media_license_token = extra_props.get("media_license_token")
            mpd_url = extra_props.get("mpd_url")
            hls_url = extra_props.get("hls_url") or url

            logging.debug(f"Udemy download: encrypted={is_encrypted}, has_token={bool(media_license_token)}, widevine_enabled={self.settings.download_widevine}")

            if is_encrypted and media_license_token:
                if not self.settings.download_widevine:
                    logging.error(
                        "VÍDEO CRIPTOGRAFADO (WIDEVINE) DETECTADO! "
                        "Para baixar este vídeo, ative 'Baixar Widevine' nas configurações e configure o caminho da CDM (.wvd)."
                    )
                    return False
                return self._download_drm_video(mpd_url or url, session, download_path, media_license_token, extra_props)
            else:
                return self._download_regular_video(hls_url, session, download_path, extra_props)

        except Exception as e:
            logging.error(f"Erro no download Udemy: {e}", exc_info=True)
            return False

    def _download_drm_video(self, mpd_url: str, session, download_path: Path, license_token: str, extra_props: Dict[str, Any]) -> bool:
        cdm_path = Path(self.settings.cdm_path)
        if not cdm_path.exists() or not cdm_path.is_dir():
            logging.error(f"Pasta CDM não encontrada ou inválida: {self.settings.cdm_path}")
            return False

        wvd_file = next(cdm_path.glob("*.wvd"), None)
        if not wvd_file:
            logging.error("CDM inválida ou não presente (.wvd)")
            return False

        logging.info("Vídeo DRM detectado. Iniciando processo de obtenção de chaves...")

        pssh = None
        try:
            mpd_response = session.get(mpd_url)
            if mpd_response.status_code == 200:
                pssh = self._extract_pssh(mpd_response.text)
                if pssh:
                    logging.info(f"PSSH extraído do MPD")
                else:
                    logging.warning("PSSH não encontrado no MPD. Tentando fallback...")
                    pssh = self._extract_pssh_from_init(mpd_response.text, mpd_url, session)
            else:
                logging.warning(f"Falha ao baixar MPD: {mpd_response.status_code}")
        except Exception as e:
            logging.warning(f"Erro ao extrair PSSH: {e}")

        if not pssh:
            logging.error("PSSH não encontrado. Abortando download criptografado.")
            return False

        # Refresh the license token to avoid expiration
        course_id = extra_props.get("course_id")
        lecture_id = extra_props.get("lecture_id")
        if course_id and lecture_id:
            fresh_token = self._refresh_license_token(session, course_id, lecture_id)
            if fresh_token:
                license_token = fresh_token
            else:
                logging.warning("Usando token original (renovação falhou)")

        license_url = f"https://www.udemy.com/media-license-server/validate-auth-token?drm_type=widevine&auth_token={license_token}"

        keys = self._get_license_keys(pssh, license_url, session)
        if not keys:
            logging.error("Falha ao obter chaves de descriptografia.")
            return False

        logging.info(f"Chaves obtidas: {len(keys)} key(s)")

        retry_opts = build_ytdlp_retry_config(self.settings)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            temp_base_name = "video"

            ydl_opts = {
                'format': 'bestvideo+bestaudio',
                'allow_unplayable_formats': True,
                'outtmpl': str(temp_path / f"{temp_base_name}.encrypted.%(ext)s"),
                'keepvideo': True,
                'http_headers': {
                    'Origin': 'https://www.udemy.com',
                    'Referer': 'https://www.udemy.com/',
                    'Sec-Fetch-Dest': 'empty',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Site': 'same-origin',
                },
                'quiet': True,
                'no_warnings': True,
                'progress': True,
                **retry_opts,
            }

            for header, value in session.headers.items():
                header_key = header
                if header.lower() == 'cookie':
                    header_key = 'Cookie'
                elif header.lower() == 'user-agent':
                    header_key = 'User-Agent'
                ydl_opts['http_headers'][header_key] = value

            downloaded_files = []
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(mpd_url, download=True)
                if 'requested_downloads' in info:
                    for d in info['requested_downloads']:
                        if 'filepath' in d:
                            downloaded_files.append(d['filepath'])
                elif 'filepath' in info:
                    downloaded_files.append(info['filepath'])

            actual_downloaded_files = []
            for f in downloaded_files:
                if os.path.exists(f):
                    actual_downloaded_files.append(f)

            if not actual_downloaded_files:
                for file_name in os.listdir(temp_dir):
                    full_path = os.path.join(temp_dir, file_name)
                    if os.path.isfile(full_path) and ".encrypted." in file_name and not file_name.endswith(".part"):
                        actual_downloaded_files.append(full_path)

            if not actual_downloaded_files:
                logging.error("Nenhum arquivo criptografado encontrado para processar.")
                return False

            downloaded_files = actual_downloaded_files
            logging.info(f"Arquivos para descriptografia: {[os.path.basename(f) for f in downloaded_files]}")

            decrypted_files = []

            mp4decrypt_exe = get_executable_path("mp4decrypt", getattr(self.settings, "bento4_path", None))
            if not mp4decrypt_exe:
                logging.error("mp4decrypt (Bento4) não encontrado.")
                return False

            for enc_file in downloaded_files:
                base, ext = os.path.splitext(enc_file)
                dec_file = f"{base}.decrypted{ext}"

                cmd = [mp4decrypt_exe]
                for key in keys:
                    cmd.extend(['--key', key])
                cmd.extend([enc_file, dec_file])

                logging.info(f"Descriptografando {os.path.basename(enc_file)}...")
                try:
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    decrypted_files.append(dec_file)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Erro na descriptografia: {e.stderr.decode(errors='replace')}")
                    return False

            temp_output_file = str(temp_path / f"{temp_base_name}.mp4")

            ffmpeg_exe = get_executable_path("ffmpeg", getattr(self.settings, "ffmpeg_path", None))
            if not ffmpeg_exe:
                logging.error("ffmpeg não encontrado.")
                return False

            cmd_merge = [ffmpeg_exe, '-y']
            for dec_file in decrypted_files:
                cmd_merge.extend(['-i', dec_file])
            cmd_merge.extend(['-c', 'copy', temp_output_file])

            logging.info("Unindo arquivos...")
            try:
                subprocess.run(cmd_merge, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError as e:
                logging.error(f"Erro ao unir arquivos: {e.stderr.decode(errors='replace')}")
                return False

            final_output_file = str(download_path) + ".mp4"
            logging.info(f"Movendo arquivo final para {final_output_file}...")
            shutil.move(temp_output_file, final_output_file)

        logging.info(f"Vídeo DRM baixado e processado com sucesso")
        return True

    def _download_regular_video(self, video_url: str, session, download_path: Path, extra_props: Dict[str, Any]) -> bool:
        retry_opts = build_ytdlp_retry_config(self.settings)

        http_headers = {}
        for header, value in session.headers.items():
            header_key = header
            if header.lower() == 'cookie':
                header_key = 'Cookie'
            elif header.lower() == 'user-agent':
                header_key = 'User-Agent'
            http_headers[header_key] = value

        ydl_opts = {
            'outtmpl': str(download_path) + ".%(ext)s",
            'noplaylist': True,
            'http_headers': http_headers,
            'quiet': True,
            'no_warnings': True,
            'progress': True,
            'concurrent_fragment_downloads': max(1, self.settings.max_concurrent_segment_downloads),
            **retry_opts,
        }

        # Use quality from user settings
        quality = self.settings.video_quality
        if quality and quality not in ("highest", "best"):
            try:
                target_height = int(str(quality).replace("p", "").strip())
                ydl_opts['format'] = f"bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]"
            except ValueError:
                pass

        if self.settings.keep_audio_only:
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]

        if self.settings.download_subtitles:
            ydl_opts['writesubtitles'] = True
            ydl_opts['subtitleslangs'] = ['all']
            if self.settings.hardcode_subtitles:
                ydl_opts['embedsubtitles'] = True

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        logging.info(f"Vídeo baixado para {download_path}")
        return True
