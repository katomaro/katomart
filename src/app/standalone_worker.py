from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import requests
from PySide6.QtCore import QObject, QRunnable, Signal
from urllib.parse import urlparse

from src.config.settings_manager import SettingsManager
from src.downloaders.factory import DownloaderFactory
from src.utils.filesystem import sanitize_path_component, truncate_filename_preserve_ext


class StandaloneWorkerSignals(QObject):
    progress = Signal(int)
    log = Signal(str)
    error = Signal(tuple)
    finished = Signal()


class StandaloneDownloadWorker(QRunnable):
    """Downloads each URL to ``settings.download_path / 'standalone'``."""

    def __init__(self, urls: List[str], settings_manager: SettingsManager) -> None:
        super().__init__()
        self.urls = [u for u in urls if u]
        self.settings_manager = settings_manager
        self.signals = StandaloneWorkerSignals()

    def run(self) -> None:
        total = len(self.urls)
        if total == 0:
            self.signals.log.emit("Nenhuma URL para baixar.")
            self.signals.finished.emit()
            return

        try:
            settings = self.settings_manager.get_settings()
            base_dir = Path(settings.download_path) / "standalone"
            base_dir.mkdir(parents=True, exist_ok=True)

            max_name = getattr(settings, "max_file_name_length", 30)

            for index, url in enumerate(self.urls, start=1):
                self.signals.log.emit(f"[{index}/{total}] Baixando: {url}")
                try:
                    host = (urlparse(url).netloc or "video").lower()
                    if host.startswith("www."):
                        host = host[4:]
                    stem = sanitize_path_component(f"{index:03d}_{host}")
                    stem = truncate_filename_preserve_ext(stem, max_name)
                    output_path = base_dir / stem

                    downloader = DownloaderFactory.get_downloader(
                        url, self.settings_manager, extra_props={}
                    )
                    session = requests.Session()
                    ok = downloader.download_video(url, session, output_path, extra_props={})
                    if ok:
                        self.signals.log.emit(f"  - OK: {output_path.resolve()}")
                    else:
                        self.signals.log.emit(f"  - [FALHOU] Downloader retornou False para {url}")
                except Exception as exc:
                    logging.error("Falha no download avulso de %s: %s", url, exc, exc_info=True)
                    self.signals.log.emit(f"  - [ERRO] {exc!r}")
                finally:
                    self.signals.progress.emit(int(index * 100 / total))
        except Exception as exc:
            logging.error("Erro no worker de downloads avulsos: %s", exc, exc_info=True)
            self.signals.error.emit((type(exc), exc, str(exc)))
        finally:
            self.signals.finished.emit()
