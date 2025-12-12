import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yt_dlp
from bs4 import BeautifulSoup

from .base import BaseDownloader
from src.config.settings_manager import AppSettings, SettingsManager
from src.utils.retry import build_ytdlp_retry_config


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
        video_assets = [
            a for a in assets 
            if a.get('contentType') == 'application/x-mpegURL' and a.get('url')
        ]

        if not video_assets:
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

    def download_video(self, url: str, session: requests.Session, download_path: Path) -> bool:
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
                logging.error("No suitable video assets found in media assets.")
                return False
            
            m3u8_url = video_asset.get('url')
            if not m3u8_url:
                logging.error("Selected video asset does not have a 'url'.")
                return False

            logging.debug(f"Selected video asset with quality {video_asset.get('height')}p. URL: {m3u8_url}")

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
                pass

            if self.settings.download_subtitles:
                ydl_opts['writesubtitles'] = True
                ydl_opts['subtitleslangs'] = ['all']
                if self.settings.hardcode_subtitles:
                    ydl_opts['embedsubtitles'] = True

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([m3u8_url])

            logging.info(f"Vídeo baixado para {download_path}")
            return True

        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to fetch Hotmart player page: {e}")
            return False
        except Exception as e:
            logging.error(f"An error occurred during Hotmart download: {e}", exc_info=True)
            return False
