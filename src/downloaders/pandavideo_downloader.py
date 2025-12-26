import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests
import yt_dlp

from src.config.settings_manager import SettingsManager
from src.utils.retry import build_ytdlp_retry_config
from src.utils.filesystem import get_executable_path
from .base import BaseDownloader


class PandaVideoDownloader(BaseDownloader):
    """
    Downloader for PandaVideo hosted content.
    Handles converting player URLs to playlist URLs, fetching metadata,
    and downloading the best stream based on settings.
    """

    def __init__(self, settings_manager: SettingsManager):
        super().__init__(settings_manager)
        self.settings = self.settings_manager.get_settings()

    def _ensure_playlist_url(self, url: str) -> str:
        """
        Converts a PandaVideo player/embed URL to the backing playlist.m3u8 URL.
        Example:
            Input: https://player-vz-....tv.pandavideo.com.br/embed/?v=UUID
            Output: https://b-vz-....tv.pandavideo.com.br/UUID/playlist.m3u8
        """
        parsed = urlparse(url)

        if "playlist.m3u8" in parsed.path:
            return urlunparse(parsed._replace(query=""))

        if "embed" in parsed.path:
            query_params = parse_qs(parsed.query)
            video_id = query_params.get("v", [None])[0]
            if video_id:
                new_netloc = parsed.netloc.replace("player", "b", 1)
                new_path = f"/{video_id}/playlist.m3u8"
                return urlunparse((parsed.scheme, new_netloc, new_path, "", "", ""))
        
        return url

    def _fetch_metadata(self, playlist_url: str, session: requests.Session) -> Dict[str, Any]:
        """
        Fetches the master playlist with ?get_qualities=1 to retrieve
        metadata like DRM status, duration, and security levels.
        """
        meta_url = f"{playlist_url}?get_qualities=1"
        logging.debug(f"Fetching PandaVideo metadata from: {meta_url}")
        metadata = {}

        try:
            response = session.get(meta_url)
            response.raise_for_status()
            content = response.text

            #EXTINF:0,duration:432|drm:false|block_download:false|percent_ts:0|security_type:regular|security_level:medium
            for line in content.splitlines():
                if line.startswith("#EXTINF:"):
                    info_part = line.split(":", 1)[1]
                    if "," in info_part:
                        _, properties = info_part.split(",", 1)
                        props = properties.split("|")
                        for prop in props:
                            if ":" in prop:
                                key, val = prop.split(":", 1)
                                metadata[key.strip()] = val.strip()
            
            logging.debug(f"PandaVideo Metadata: {metadata}")
            return metadata

        except Exception as e:
            logging.error(f"Failed to fetch PandaVideo metadata: {e}")
            return {}

    def _fetch_streams(self, playlist_url: str, session: requests.Session) -> List[Dict[str, Any]]:
        """
        Fetches the standard playlist to get the correct relative URLs for streams.
        """
        logging.debug(f"Fetching PandaVideo streams from: {playlist_url}")

        try:
            response = session.get(playlist_url)
            response.raise_for_status()
            return self._parse_m3u8_qualities(response.text, playlist_url)
        except Exception as e:
            logging.error(f"Failed to fetch/parse PandaVideo streams: {e}")
            return []

    def _parse_m3u8_qualities(self, content: str, base_url: str) -> List[Dict[str, Any]]:
        """
        Parses the standard M3U8 content to extract stream info, resolving
        relative URLs against the base_url.
        """
        streams = []
        lines = content.splitlines()
        
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                res_match = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)

                height = int(res_match.group(2)) if res_match else 0
                width = int(res_match.group(1)) if res_match else 0
                bandwidth = int(bw_match.group(1)) if bw_match else 0

                if i + 1 < len(lines):
                    stream_uri = lines[i+1].strip()
                    if stream_uri and not stream_uri.startswith("#"):
                        stream_url = urljoin(base_url, stream_uri)
                        
                        streams.append({
                            'height': height,
                            'width': width,
                            'bandwidth': bandwidth,
                            'url': stream_url
                        })
        return streams

    def _select_best_stream(self, streams: List[Dict[str, Any]]) -> Optional[str]:
        """Selects the best stream URL based on user settings."""
        if not streams:
            return None

        video_streams = [s for s in streams if s.get('height', 0) > 0]
        if not video_streams:
            video_streams = streams

        sorted_streams = sorted(video_streams, key=lambda s: s['height'], reverse=True)
        
        quality_preference = self.settings.video_quality
        
        if quality_preference == "Mais alta":
            selected = sorted_streams[0]
        elif quality_preference == "Mais baixa":
            selected = sorted_streams[-1]
        else:
            try:
                target_height = int(quality_preference.replace('p', ''))
                best_match = None
                for stream in sorted_streams:
                    if stream['height'] <= target_height:
                        best_match = stream
                        break
                selected = best_match or sorted_streams[-1]
            except (ValueError, AttributeError):
                selected = sorted_streams[0]

        logging.info(f"Selected PandaVideo quality: {selected.get('height')}p (Bandwidth: {selected.get('bandwidth')})")
        return selected.get('url')

    def download_video(self, url: str, session: requests.Session, download_path: Path) -> bool:
        """
        Orchestrates the download process for PandaVideo using a fresh session
        that mimics the player's internal request headers.
        """
        parsed_input = urlparse(url)
        origin = f"{parsed_input.scheme}://{parsed_input.netloc}"
        referer = f"{origin}/"
        local_session = requests.Session()
        local_session.headers.update({
            'User-Agent': self.settings.user_agent,
            'Accept': '*/*',
            'Accept-Language': 'pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3',
            'Referer': referer,
            'Origin': origin,
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site',
            'Priority': 'u=4',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache',
        })

        try:
            master_url = self._ensure_playlist_url(url)

            metadata = self._fetch_metadata(master_url, local_session)
            
            if metadata.get("drm") == "true":
                logging.warning(f"PandaVideo acusou o uso do Widevine, entre em contato com o autor pois isso é complicado painho. Metadata: {metadata}")
                return False

            streams = self._fetch_streams(master_url, local_session)

            target_url = self._select_best_stream(streams)
            if not target_url:
                logging.warning("Could not identify streams from playlist. Falling back to master URL.")
                target_url = master_url

            retry_opts = build_ytdlp_retry_config(self.settings)
            
            def _has_extension(p: Path) -> bool:
                return bool(p.suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,5}", p.suffix))

            output_template = str(download_path)
            if not _has_extension(download_path):
                output_template += ".%(ext)s"

            ffmpeg_exe = get_executable_path("ffmpeg", getattr(self.settings, "ffmpeg_path", None))
            
            ydl_opts = {
                'outtmpl': output_template,
                'noplaylist': True,
                'http_headers': {k: v for k, v in local_session.headers.items()},
                'quiet': True,
                'no_warnings': True,
                'progress': True,
                'concurrent_fragment_downloads': max(1, self.settings.max_concurrent_segment_downloads),
                **retry_opts,
            }
            
            if ffmpeg_exe:
                ydl_opts['ffmpeg_location'] = str(Path(ffmpeg_exe).parent)

            if self.settings.keep_audio_only:
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
            logging.info(f"Downloading PandaVideo from: {target_url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target_url])

            return True

        except Exception as e:
            logging.error(f"PandaVideo download failed: {e}", exc_info=True)
            return False
