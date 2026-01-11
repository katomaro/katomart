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
from urllib.parse import urlparse, parse_qs, urljoin, urlencode, urlunparse
from base64 import b64encode, b64decode

import requests
import yt_dlp
from bs4 import BeautifulSoup
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

from .base import BaseDownloader
from src.config.settings_manager import AppSettings, SettingsManager
from src.utils.retry import build_ytdlp_retry_config
from src.utils.filesystem import get_executable_path


class HotmartDownloader(BaseDownloader):
    """
    A downloader for videos hosted on cf-embed.play.hotmart.com.
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)
        self.settings: AppSettings = self.settings_manager.get_settings()

    def _extract_media_assets(self, html_content: str) -> Optional[List[Dict[str, Any]]]:
        """Parses the HTML to extract the mediaAssets data."""
        soup = BeautifulSoup(html_content, 'html.parser')
        script_tag = soup.find('script', id='__NEXT_DATA__')

        if not script_tag:
            logging.error("Could not find the '__NEXT_DATA__' script tag.")
            return None

        try:
            data = json.loads(script_tag.string)
            return data['props']['pageProps']['applicationData']['mediaAssets']
        except (json.JSONDecodeError, KeyError) as e:
            logging.error(f"Failed to parse JSON or find 'mediaAssets' key: {e}")
            return None

    def _select_best_asset(self, assets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Selects the best video asset based on settings."""
        
        # DASH na htm é mais rápido que m3u8 lol
        if self.settings.download_widevine:
            dash_assets = [
                a for a in assets 
                if a.get('contentType') == 'application/dash+xml' and a.get('url')
            ]
            if dash_assets:
                return dash_assets[0]

        video_assets = [
            a for a in assets 
            if a.get('contentType') == 'application/x-mpegURL' and a.get('url')
        ]

        if not video_assets:
            if getattr(self.settings, 'download_podcasts', True):
                audio_assets = [
                    a for a in assets
                    if a.get('contentType', '').startswith('audio/') and a.get('url')
                ]
                if audio_assets:
                    logging.info("Nenhum vídeo encontrado, mas áudio detectado. Selecionando melhor áudio.")
                    best_audio = next((a for a in audio_assets if 'mp4' in a.get('contentType', '')), audio_assets[0])
                    return best_audio
            else:
                logging.info("Nenhum vídeo encontrado e download de podcasts desativado.")
                return None

            logging.error("No HLS video assets found in the media assets list.")
            return None

        unique_assets = []
        seen_heights = set()
        for asset in sorted(video_assets, key=lambda a: int(a.get('height', 0)), reverse=True):
            height = asset.get('height')
            if height not in seen_heights:
                unique_assets.append(asset)
                seen_heights.add(height)
        
        video_assets = unique_assets

        quality_preference = self.settings.video_quality
        logging.debug(f"Available video qualities (heights): {[a.get('height') for a in video_assets]}")
        logging.debug(f"User video quality preference: {quality_preference}")
        
        if quality_preference == "Mais alta":
            return max(video_assets, key=lambda a: int(a.get('height', 0)))
        elif quality_preference == "Mais baixa":
            return min(video_assets, key=lambda a: int(a.get('height', 0)))
        else:
            try:
                target_height = int(quality_preference.replace('p', ''))
            except (ValueError, AttributeError):
                logging.warning(f"Invalid video quality setting: '{quality_preference}'. Defaulting to highest.")
                return max(video_assets, key=lambda a: int(a.get('height', 0)))

            best_match = None
            for asset in sorted(video_assets, key=lambda a: int(a.get('height', 0)), reverse=True):
                asset_height = int(asset.get('height', 0))
                if asset_height <= target_height:
                    best_match = asset
                    break

            return best_match or min(video_assets, key=lambda a: int(a.get('height', 0)))

    def _extract_pssh(self, mpd_content: str) -> Optional[str]:
        """Extracts the PSSH value from the MPD content."""
        try:
            pssh_match = re.search(r'<(?:[a-zA-Z0-9]+:)?pssh[^>]*>(.*?)</(?:[a-zA-Z0-9]+:)?pssh>', mpd_content, re.DOTALL | re.IGNORECASE)
            
            if pssh_match:
                return pssh_match.group(1).strip()

            return None
        except Exception as e:
            logging.error(f"Error extracting PSSH: {e}")
            return None

    def _extract_pssh_from_init(self, mpd_content: str, mpd_url: str, session: requests.Session) -> Optional[str]:
        """
        Fallback: Tenta extrair o PSSH do segmento de inicialização.
        Usa a mesma sessão do download do MPD para garantir consistência de Headers/TLS.
        """
        try:
            logging.info("Tentando extrair PSSH do segmento de inicialização (fallback via String)...")

            if "?" in mpd_url:
                mpd_base_part = mpd_url.split("?")[0]
            else:
                mpd_base_part = mpd_url

            if "/" in mpd_base_part:
                mpd_dir = mpd_base_part.rsplit("/", 1)[0] + "/"
            else:
                mpd_dir = mpd_base_part + "/"
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
            xml_base_url = ""
            if base_url_match:
                xml_base_url = base_url_match.group(1).strip().replace("&amp;", "&")

            if "?" in init_relative:
                init_path_only, init_query = init_relative.split("?", 1)
            else:
                init_path_only, init_query = init_relative, ""

            full_file_url = f"{mpd_dir}{xml_base_url}{init_path_only}"

            urls_to_try = []

            if init_query:
                urls_to_try.append({
                    "url": f"{full_file_url}?{init_query}",
                    "desc": "Token Original (Init)"
                })

            data = None

            for attempt in urls_to_try:
                target_url = attempt["url"]
                logging.debug(f"Tentando {attempt['desc']}: {target_url}")
                
                try:
                    response = session.get(
                        target_url
                    )
                    
                    if response.status_code == 200:
                        data = response.content
                        logging.info(f"Sucesso ao baixar init com {attempt['desc']}")
                        break
                    else:
                        logging.warning(f"Falha {response.status_code} com {attempt['desc']}")
                        
                except Exception as e:
                    logging.error(f"Erro de conexão: {e}")

            if not data:
                return None

            widevine_sys_id = bytes.fromhex("edef8ba979d64acea3c827dcd51d21ed")
            sys_id_pos = data.find(widevine_sys_id)
            
            if sys_id_pos == -1:
                logging.warning("Widevine SystemID não encontrado no arquivo init.")
                return None
            
            box_start = sys_id_pos - 12
            if box_start < 0: return None
            
            if data[box_start+4 : box_start+8] != b'pssh':
                return None
            
            box_size = struct.unpack(">I", data[box_start : box_start+4])[0]
            if box_start + box_size > len(data): return None
                
            pssh_box = data[box_start : box_start + box_size]
            return b64encode(pssh_box).decode('utf-8')

        except Exception as e:
            logging.error(f"Erro no fallback PSSH: {e}", exc_info=True)
            return None

    def _get_license_keys(self, pssh: str, license_url: str, session: requests.Session, membership_code: str) -> Optional[List[str]]:
        """Obtains the decryption keys from the license server."""
        try:
            cdm_path = Path(self.settings.cdm_path)
            wvd_file = next(cdm_path.glob("*.wvd"), None)
            
            if wvd_file:
                device = Device.load(str(wvd_file))
            else:
                logging.error("Arquivo .wvd não encontrado para descriptografia, .bin e .pem serão implementados depois, gere um .wvd.")
                return None

            cdm = Cdm.from_device(device)
            
            pssh_bytes = b64decode(pssh)
            pssh_obj = PSSH(pssh_bytes)
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, pssh_obj)

            headers = {
                "User-Agent": self.settings.user_agent,
                "Accept": "*/*",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                "Content-type": "application/octet-stream",
                "keySystem": "com.widevine.alpha",
                "membership": membership_code,
                "Origin": "https://cf-embed.play.hotmart.com",
                "Referer": "https://cf-embed.play.hotmart.com/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
            }

            logging.debug(f"Enviando challenge para: {license_url}")
            response = session.post(license_url, data=challenge, headers=headers)
            
            if response.status_code != 200:
                logging.error(f"Erro ao obter licença: {response.status_code} - {response.text}")
                cdm.close(session_id)
                return None

            cdm.parse_license(session_id, response.content)
            
            keys = []
            for key in cdm.get_keys(session_id):
                if key.type == 'CONTENT':
                    keys.append(f"{key.kid.hex}:{key.key.hex()}")
            
            cdm.close(session_id)
            return keys

        except Exception as e:
            logging.error(f"Erro durante o processo de obtenção de chaves: {e}", exc_info=True)
            return None

    def download_video(self, url: str, session: requests.Session, download_path: Path, extra_props: Optional[Dict[str, Any]] = None) -> bool:
        """
        Downloads a video from a Hotmart embedded player URL.
        """
        try:
            logging.debug(f"Fetching Hotmart player page: {url}")
            player_response = session.get(url)
            player_response.raise_for_status()

            # with open("debug_hotmart_player.html", "w", encoding="utf-8") as f:
            #     f.write(player_response.text)

            media_assets = self._extract_media_assets(player_response.text)
            if not media_assets:
                logging.error("Could not extract media assets from Hotmart player.")
                return False

            # with open("debug_hotmart_media_assets.json", "w", encoding="utf-8") as f:
            #     json.dump(media_assets, f, indent=2)

            video_asset = self._select_best_asset(media_assets)
            if not video_asset:
                has_audio = any(a.get('contentType', '').startswith('audio/') for a in media_assets)
                download_podcasts = getattr(self.settings, 'download_podcasts', True)
                
                if has_audio and not download_podcasts:
                     logging.info("Download pulado: Apenas áudio disponível e podcasts está desativado.")
                     return True

                logging.error("No suitable video assets found in media assets.")
                return False
            
            video_url = video_asset.get('url')
            if not video_url:
                logging.error("Selected video asset does not have a 'url'.")
                return False

            if '/drm/' in video_url:
                if not self.settings.download_widevine:
                    logging.error("VIDEO CRIPTOGRAFADO, REQUER CDM (download_widevine=False)")
                    return False

                cdm_path = Path(self.settings.cdm_path)
                if not cdm_path.exists() or not cdm_path.is_dir():
                    logging.error(f"Pasta CDM não encontrada ou inválida: {self.settings.cdm_path}")
                    return False

                wvd_file = next(cdm_path.glob("*.wvd"), None)
                if not wvd_file:
                    bin_files = list(cdm_path.glob("*.bin"))
                    pem_files = list(cdm_path.glob("*.pem"))
                    if not (bin_files and pem_files):
                        logging.error("CDM inválida ou não presente (.wvd ou .bin+.pem)")
                        return False
                
                logging.info("Vídeo DRM detectado e CDM encontrada. Iniciando processo de obtenção de chaves...")

                pssh = None
                try:
                    mpd_response = session.get(video_url)
                    if mpd_response.status_code == 200:
                        pssh = self._extract_pssh(mpd_response.text)

                        if pssh:
                            logging.info(f"PSSH extraído do MPD: {pssh}")
                        else:

                            logging.warning("PSSH não encontrado no texto do MPD. Tentando método fallback (Init Segment)...")
                            pssh = self._extract_pssh_from_init(mpd_response.text, video_url, session)

                            if pssh:
                                logging.info(f"PSSH recuperado com sucesso do init segment.")
                            else:
                                logging.error("Falha fatal: PSSH não encontrado nem no MPD nem no Init Segment.")
                    else:
                        logging.warning(f"Falha ao baixar MPD para extração de PSSH: {mpd_response.status_code}")
                except Exception as e:
                    logging.warning(f"Erro ao tentar extrair PSSH: {e}")

                if not pssh:
                    logging.error("PSSH não encontrado. Abortando download criptografado.")
                    return False

                # https://api-player-embed.hotmart.com/v2/drm/{media_code}/license?token={token}&userCode={user_code}&applicationCode={app_code}
                parsed_url = urlparse(url)
                query_params = parse_qs(parsed_url.query)

                path_parts = parsed_url.path.split('/')
                media_code = path_parts[-1] if path_parts else ""

                token = query_params.get('jwtToken', [''])[0] or query_params.get('token', [''])[0]
                user_code = query_params.get('userCode', [''])[0]
                app_code = query_params.get('applicationCode', [''])[0]

                if not (media_code and token and user_code and app_code):
                    logging.error("Não foi possível extrair todos os parâmetros necessários para a URL de licença.")
                    return False

                license_url = f"https://api-player-embed.hotmart.com/v2/drm/{media_code}/license?token={token}&userCode={user_code}&applicationCode={app_code}"
                
                membership_code = extra_props.get("membership_code") if extra_props else ""
                if not membership_code:
                     logging.error("Membership code não fornecido. Necessário para obter a licença.")
                     return False

                keys = self._get_license_keys(pssh, license_url, session, membership_code)
                
                if not keys:
                    logging.error("Falha ao obter chaves de descriptografia.")
                    return False
                
                logging.info(f"Chaves obtidas: {keys}")

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
                            'Origin': 'https://cf-embed.play.hotmart.com',
                            'Referer': 'https://cf-embed.play.hotmart.com/',
                            'Sec-Fetch-Dest': 'empty',
                            'Sec-Fetch-Mode': 'cors',
                            'Sec-Fetch-Site': 'same-site',
                        },
                        'quiet': True,
                        'no_warnings': True,
                        'progress': True,
                        **retry_opts,
                    }
                    
                    ydl_opts['http_headers'].update({header: value for header, value in session.headers.items()})

                    downloaded_files = []
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(video_url, download=True)
                        if 'requested_downloads' in info:
                            for d in info['requested_downloads']:
                                downloaded_files.append(d['filepath'])
                        else:
                            downloaded_files.append(info['filepath'])

                    actual_downloaded_files = []
                    for f in downloaded_files:
                        if os.path.exists(f):
                            actual_downloaded_files.append(f)
                    
                    if not actual_downloaded_files:
                        logging.warning("Arquivos reportados pelo yt-dlp não encontrados. Buscando partes no diretório temporário.")
                        logging.debug(f"Conteúdo do diretório temporário: {os.listdir(temp_dir)}")
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
                        logging.error("mp4decrypt (Bento4) não encontrado. Verifique se o caminho está configurado corretamente ou se está no PATH.")
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
                        logging.error("ffmpeg não encontrado. Verifique se o caminho está configurado corretamente ou se está no PATH.")
                        return False

                    cmd_merge = [ffmpeg_exe, '-y']
                    for dec_file in decrypted_files:
                        cmd_merge.extend(['-i', dec_file])
                    cmd_merge.extend(['-c', 'copy', temp_output_file])

                    logging.info(f"Unindo arquivos...")
                    try:
                        subprocess.run(cmd_merge, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    except subprocess.CalledProcessError as e:
                        logging.error(f"Erro ao unir arquivos: {e.stderr.decode(errors='replace')}")
                        return False

                    final_output_file = str(download_path) + ".mp4"
                    logging.info(f"Movendo arquivo final para {final_output_file}...")
                    shutil.move(temp_output_file, final_output_file)
                
                logging.info(f"Vídeo criptografado baixado e processado para {final_output_file}")
                return True

            logging.debug(f"Selected video asset with quality {video_asset.get('height')}p. URL: {video_url}")

            retry_opts = build_ytdlp_retry_config(self.settings)
            ydl_opts = {
                'outtmpl': str(download_path) + ".%(ext)s",
                'noplaylist': True,
                'http_headers': {header: value for header, value in session.headers.items()},
                'quiet': True,
                'no_warnings': True,
                'progress': True,
                'concurrent_fragment_downloads': max(1, self.settings.max_concurrent_segment_downloads),
                **retry_opts,
            }

            if self.settings.keep_audio_only:
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            else:
                target_height = video_asset.get('height')
                if target_height:
                    ydl_opts['format'] = f"bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]"

            if self.settings.download_subtitles:
                ydl_opts['writesubtitles'] = True
                ydl_opts['subtitleslangs'] = ['all']
                if self.settings.hardcode_subtitles:
                    ydl_opts['embedsubtitles'] = True

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])

            logging.info(f"Vídeo baixado para {download_path}")
            return True

        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to fetch Hotmart player page: {e}")
            return False
        except Exception as e:
            logging.error(f"An error occurred during Hotmart download: {e}", exc_info=True)
            return False
