import logging
import yt_dlp
from pathlib import Path
from urllib.parse import urlparse
import requests

from .base import BaseDownloader
from src.config.settings_manager import SettingsManager
from src.utils.retry import build_ytdlp_retry_config

logger = logging.getLogger(__name__)

# Current SafeVideo player host. SafeVideo migrated the embed player from
# `player2.safevideo.com` to `player.safevideo.com` (seen 2026-06). The
# playlist (`*-videozz.k8s.eduzz.com`) and segment (`cdn.safevideo.com`)
# hosts validate Origin/Referer against the live player, so these must match
# whatever player page the embed actually loads.
DEFAULT_PLAYER_HOST = "player.safevideo.com"
WATCH_API_URL = "https://api.safevideo.com/player/watch"


class SafeVideoDownloader(BaseDownloader):
    """
    Downloader específico para SafeVideo (Eduzz).
    Resolve a URL do vídeo via API interna antes de baixar.

    Fluxo (2026-06):
      embed `https://player.safevideo.com/<JWT>`
        -> GET api.safevideo.com/player/watch?token=<JWT>  (resolve playlist)
        -> master m3u8  `playlist-videozz.k8s.eduzz.com/?t=...`
        -> sub  m3u8    `subplaylist-videozz.k8s.eduzz.com/?t=...`
        -> segmentos     `cdn.safevideo.com/<key>/<res>_NNN.ts` (CloudFront)

    O host de armazenamento dos segmentos migrou de Cloudflare R2 para
    CloudFront (`cdn.safevideo.com`), mas como as URLs vêm embutidas no m3u8
    retornado pelo servidor, o yt-dlp as segue automaticamente — nenhuma
    URL de segmento é fixada aqui.
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)

    def _resolve_player_host(self, url: str) -> str:
        """
        Deriva o host do player a partir da URL do embed para casar o
        Origin/Referer com a página que realmente carrega o vídeo.

        Aceita tanto o embed antigo (`player2.safevideo.com`) quanto o novo
        (`player.safevideo.com`); cai no host atual por padrão.
        """
        try:
            host = urlparse(url).netloc
        except Exception:
            host = ""
        if host.endswith("safevideo.com") and "player" in host:
            return host
        return DEFAULT_PLAYER_HOST

    def download_video(self, url: str, session: requests.Session, download_path: Path, extra_props: dict = None) -> bool:
        try:
            logger.info(f"Processando SafeVideo (API): {url}")

            settings = self.settings_manager.get_settings()
            token = url.split('/')[-1].split('?')[0]

            player_host = self._resolve_player_host(url)
            origin = f"https://{player_host}"
            referer = f"{origin}/"

            target_url = url

            if not token or len(token) < 20:
                logger.warning(f"Não foi possível extrair token válido da URL: {url}. Tentando download direto.")
            else:
                api_headers = {
                    "User-Agent": settings.user_agent,
                    "Referer": referer,
                    "Origin": origin,
                    "Accept": "application/json, text/plain, */*",
                }

                try:
                    logger.debug(f"Consultando API SafeVideo: {WATCH_API_URL}")
                    resp = requests.get(WATCH_API_URL, params={"token": token}, headers=api_headers)
                    resp.raise_for_status()
                    data = resp.json()

                    playlist = data.get("playlist")
                    if playlist:
                        target_url = playlist
                        logger.info(f"Playlist resolvida: {target_url}")
                    else:
                        logger.warning("Campo 'playlist' não encontrado na resposta da API. Tentando download direto.")

                except Exception as e:
                    logger.error(f"Erro ao resolver playlist via API: {e}")

            dl_headers = {
                "User-Agent": settings.user_agent,
                "Referer": referer,
                "Origin": origin,
            }

            retry_opts = build_ytdlp_retry_config(settings)

            ydl_opts = {
                'outtmpl': self.build_ytdlp_output_template(download_path, settings),
                'http_headers': dl_headers,
                'quiet': True,
                'no_warnings': True,
                'nocheckcertificate': True,
                **retry_opts,
            }

            ydl_opts.update(self.build_quality_opts(settings))
            ydl_opts.update(self.build_js_runtime_opts(settings))

            if settings.ffmpeg_path:
                ydl_opts['ffmpeg_location'] = settings.ffmpeg_path

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target_url])

            return True

        except Exception as e:
            logger.error(f"Erro no SafeVideoDownloader: {e}")
            return False
