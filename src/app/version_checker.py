import logging

import requests
from PySide6.QtCore import QObject, QRunnable, Signal


class VersionCheckSignals(QObject):
    finished = Signal()
    success = Signal(int)
    failure = Signal(str)


class VersionCheckWorker(QRunnable):
    def __init__(self, version_url: str):
        super().__init__()
        self.version_url = version_url
        self.signals = VersionCheckSignals()

    def run(self) -> None:
        try:
            logging.info("Verificando versão mais recente no GitHub...")
            response = requests.get(self.version_url, timeout=5)
            response.raise_for_status()
            data = response.json()
            build_value = data.get("build")
            if build_value is None:
                raise ValueError("Campo 'build' não encontrado no VERSION.json")
            self.signals.success.emit(int(build_value))
        except Exception as exc:  # pragma: no cover - network interaction
            logging.warning("Não foi possível verificar a versão remota: %s", exc)
            self.signals.failure.emit(str(exc))
        finally:
            self.signals.finished.emit()
