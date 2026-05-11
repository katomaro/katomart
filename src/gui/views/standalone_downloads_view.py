from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.config.settings_manager import SettingsManager
from src.utils.html_video_extractor import extract_video_urls


_PREMIUM_TOOLTIP = (
    "Exclusivo para assinantes. Visite katomaro.com/store/katomart para ativar."
)


class StandaloneDownloadsView(QWidget):
    """Tab UI for the standalone-downloads feature."""

    download_requested = Signal(list)  # emits list[str] of URLs

    def __init__(self, settings_manager: SettingsManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings_manager = settings_manager
        self._extracted_urls: list[str] = []

        layout = QVBoxLayout(self)

        info = QLabel(
            "Cole URLs de vídeos (1 por linha) ou, sendo assinante, importe arquivos "
            "HTML salvos da página do curso para extrair os links automaticamente."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        splitter = QSplitter()
        layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("URLs (uma por linha):"))
        self.url_input = QTextEdit()
        self.url_input.setPlaceholderText(
            "https://player-vz-...pandavideo.com/embed/?v=...\n"
            "https://www.youtube.com/watch?v=...\n"
            "https://player.vimeo.com/video/...\n"
        )
        left_layout.addWidget(self.url_input, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel("Importar HTML (assinantes):"))

        self.import_file_button = QPushButton("Adicionar arquivo HTML...")
        self.import_file_button.clicked.connect(self._on_import_file)
        right_layout.addWidget(self.import_file_button)

        self.import_many_button = QPushButton("Adicionar múltiplos HTML / pasta...")
        self.import_many_button.clicked.connect(self._on_import_many)
        right_layout.addWidget(self.import_many_button)

        right_layout.addWidget(QLabel("URLs extraídas do HTML:"))
        self.extracted_list = QListWidget()
        right_layout.addWidget(self.extracted_list, 1)

        self.clear_extracted_button = QPushButton("Limpar lista")
        self.clear_extracted_button.clicked.connect(self._clear_extracted)
        right_layout.addWidget(self.clear_extracted_button)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        action_row = QHBoxLayout()
        self.download_button = QPushButton("Baixar tudo")
        self.download_button.clicked.connect(self._on_download_clicked)
        action_row.addWidget(self.download_button)
        action_row.addStretch()
        layout.addLayout(action_row)

        layout.addWidget(QLabel("Progresso:"))
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        layout.addWidget(QLabel("Log:"))
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        layout.addWidget(self.log_output, 1)

        self.refresh_membership_state()

    # --- public API ---

    def refresh_membership_state(self) -> None:
        """Re-evaluates premium gating; safe to call after a settings change."""
        settings = self._settings_manager.get_settings()
        is_premium = bool(getattr(settings, "has_full_permissions", False))

        for btn in (self.import_file_button, self.import_many_button):
            btn.setEnabled(is_premium)
            btn.setToolTip("" if is_premium else _PREMIUM_TOOLTIP)

        if not is_premium:
            self.import_file_button.setText("Adicionar arquivo HTML (assinantes)")
            self.import_many_button.setText("Adicionar múltiplos HTML / pasta (assinantes)")
        else:
            self.import_file_button.setText("Adicionar arquivo HTML...")
            self.import_many_button.setText("Adicionar múltiplos HTML / pasta...")

    def log_message(self, message: str) -> None:
        self.log_output.append(message)

    def set_progress(self, value: int) -> None:
        self.progress_bar.setValue(value)

    def set_download_active(self, active: bool) -> None:
        self.download_button.setEnabled(not active)
        if active:
            self.download_button.setText("Baixando...")
        else:
            self.download_button.setText("Baixar tudo")


    def _on_import_file(self) -> None:
        if not self._require_premium():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Selecione um arquivo HTML", "", "Arquivos HTML (*.html *.htm);;Todos (*)"
        )
        if path:
            self._ingest_paths([Path(path)])

    def _on_import_many(self) -> None:
        if not self._require_premium():
            return
        dialog = QFileDialog(self, "Selecione arquivos HTML ou uma pasta")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setNameFilter("Arquivos HTML (*.html *.htm)")
        chosen: list[Path] = []
        if dialog.exec():
            chosen.extend(Path(p) for p in dialog.selectedFiles())

        folder = QFileDialog.getExistingDirectory(self, "Ou selecione uma pasta com arquivos HTML")
        if folder:
            folder_path = Path(folder)
            chosen.extend(folder_path.rglob("*.html"))
            chosen.extend(folder_path.rglob("*.htm"))

        if chosen:
            self._ingest_paths(chosen)

    def _ingest_paths(self, paths: list[Path]) -> None:
        settings = self._settings_manager.get_settings()
        blacklist = list(getattr(settings, "embed_domain_blacklist", []) or [])
        added = 0
        scanned = 0
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:
                self.log_message(f"[ERRO] Não consegui ler {path}: {exc}")
                continue
            scanned += 1
            urls = extract_video_urls(text, base_url=None, blacklist=blacklist)
            for url in urls:
                if url in self._extracted_urls:
                    continue
                self._extracted_urls.append(url)
                self.extracted_list.addItem(QListWidgetItem(url))
                added += 1

        self.log_message(
            f"HTML processado: {scanned} arquivo(s), {added} nova(s) URL(s) extraída(s)."
        )

    def _clear_extracted(self) -> None:
        self._extracted_urls.clear()
        self.extracted_list.clear()

    def _on_download_clicked(self) -> None:
        urls = self._collect_urls()
        if not urls:
            QMessageBox.information(
                self, "Nada a baixar", "Cole URLs ou importe um HTML antes de baixar."
            )
            return
        self.log_output.clear()
        self.progress_bar.setValue(0)
        self.log_message(f"Iniciando {len(urls)} download(s)...")
        self.download_requested.emit(urls)

    def _collect_urls(self) -> list[str]:
        text_urls = [
            line.strip()
            for line in self.url_input.toPlainText().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        ordered: list[str] = []
        seen: set[str] = set()
        for url in text_urls + list(self._extracted_urls):
            if url in seen:
                continue
            seen.add(url)
            ordered.append(url)
        return ordered

    def _require_premium(self) -> bool:
        settings = self._settings_manager.get_settings()
        if getattr(settings, "has_full_permissions", False):
            return True
        QMessageBox.information(self, "Recurso de assinante", _PREMIUM_TOOLTIP)
        return False
