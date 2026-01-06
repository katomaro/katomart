import logging
import yt_dlp
from pathlib import Path
import requests
import json

from .base import BaseDownloader
from src.config.settings_manager import SettingsManager
from src.utils.retry import build_ytdlp_retry_config

logger = logging.getLogger(__name__)

class SafeVideoDownloader(BaseDownloader):
    """
    Downloader específico para SafeVideo (Eduzz).
    Resolve a URL do vídeo via API interna antes de baixar.
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)

    def download_video(self, url: str, session: requests.Session, download_path: Path, extra_props: dict = None) -> bool:
        try:
            logger.info(f"Processando SafeVideo (API): {url}")

            settings = self.settings_manager.get_settings()
            token = url.split('/')[-1].split('?')[0]

            target_url = url

            if not token or len(token) < 20: 
                logger.warning(f"Não foi possível extrair token válido da URL: {url}. Tentando download direto.")
            else:
                api_url = "https://api.safevideo.com/player/watch"
                params = {"token": token}

                api_headers = {
                    "User-Agent": settings.user_agent,
                    "Referer": "https://player2.safevideo.com/",
                    "Origin": "https://player2.safevideo.com",
                    "Accept": "application/json, text/plain, */*"
                }
                
                try:
                    logger.debug(f"Consultando API SafeVideo: {api_url}")
                    resp = requests.get(api_url, params=params, headers=api_headers)
                    resp.raise_for_status()
                    data = resp.json()
                    
                    if 'playlist' in data and data['playlist']:
                        target_url = data['playlist']
                        logger.info(f"Playlist resolvida: {target_url}")
                    else:
                        logger.warning("Campo 'playlist' não encontrado na resposta da API. Tentando download direto.")
                        
                except Exception as e:
                    logger.error(f"Erro ao resolver playlist via API: {e}")

            dl_headers = {
                "User-Agent": settings.user_agent,
                "Referer": "https://player2.safevideo.com/",
                "Origin": "https://player2.safevideo.com",
            }

            retry_opts = build_ytdlp_retry_config(settings)
            
            ydl_opts = {
                'format': 'best',
                'outtmpl': f"{str(download_path)}.%(ext)s",
                'http_headers': dl_headers,
                'quiet': True,
                'no_warnings': True,
                'nocheckcertificate': True,
                **retry_opts,
            }

            if settings.ffmpeg_path:
                ydl_opts['ffmpeg_location'] = settings.ffmpeg_path

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target_url])
            
            return True

        except Exception as e:
            logger.error(f"Erro no SafeVideoDownloader: {e}")
            return False
