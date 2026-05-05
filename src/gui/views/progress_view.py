from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
    QProgressBar, QLabel, QPushButton,
)
from PySide6.QtCore import Signal


class ProgressView(QWidget):
    """shows download progress via logging, a progress bar, and control buttons"""

    pause_requested = Signal()
    resume_requested = Signal()
    save_progress_requested = Signal()
    cancel_requested = Signal()
    reauth_requested = Signal()
    retry_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        # --- Status counters ---
        counters_layout = QHBoxLayout()
        self._success_count = 0
        self._partial_count = 0
        self._error_count = 0
        self._skipped_count = 0

        self.success_label = QLabel("Sucesso: 0")
        self.success_label.setStyleSheet("color: green; font-weight: bold; padding: 2px 6px;")
        self.partial_label = QLabel("Parcial: 0")
        self.partial_label.setStyleSheet("color: orange; font-weight: bold; padding: 2px 6px;")
        self.error_label = QLabel("Erro: 0")
        self.error_label.setStyleSheet("color: red; font-weight: bold; padding: 2px 6px;")
        self.skipped_label = QLabel("Puladas: 0")
        self.skipped_label.setStyleSheet("color: gray; font-weight: bold; padding: 2px 6px;")

        counters_layout.addWidget(self.success_label)
        counters_layout.addWidget(self.partial_label)
        counters_layout.addWidget(self.error_label)
        counters_layout.addWidget(self.skipped_label)
        counters_layout.addStretch()
        layout.addLayout(counters_layout)

        # --- Progress bar ---
        layout.addWidget(QLabel("Progresso geral de download:"))
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # --- Control buttons ---
        buttons_layout = QHBoxLayout()
        self.pause_button = QPushButton("Pausar")
        self.save_button = QPushButton("Salvar Progresso")
        self.reauth_button = QPushButton("Reautenticar")
        self.cancel_button = QPushButton("Cancelar")
        self.retry_button = QPushButton("Tentar Novamente (Erros/Parciais)")
        self.retry_button.setEnabled(False)

        self.pause_button.clicked.connect(self._on_pause_clicked)
        self.save_button.clicked.connect(self.save_progress_requested.emit)
        self.reauth_button.clicked.connect(self.reauth_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.retry_button.clicked.connect(self.retry_requested.emit)

        buttons_layout.addWidget(self.pause_button)
        buttons_layout.addWidget(self.save_button)
        buttons_layout.addWidget(self.reauth_button)
        buttons_layout.addWidget(self.cancel_button)
        buttons_layout.addWidget(self.retry_button)
        layout.addLayout(buttons_layout)

        # --- Log area ---
        layout.addWidget(QLabel("Log:"))
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        layout.addWidget(self.log_output)

        self._paused = False
        self._download_active = False
        self._is_premium = False

    # --- Public helpers ---

    def set_premium(self, is_premium: bool) -> None:
        """Update premium state and button availability."""
        self._is_premium = is_premium
        if is_premium:
            self.save_button.setText("Salvar Progresso")
            self.retry_button.setText("Tentar Novamente (Erros/Parciais)")
            self.save_button.setEnabled(True)
            self.retry_button.setEnabled(False)
        else:
            self.save_button.setText("Salvar Progresso (assinantes)")
            self.save_button.setEnabled(False)
            self.retry_button.setText("Tentar Novamente (Erros/Parciais) (assinantes)")
            self.retry_button.setEnabled(False)

    def log_message(self, message: str) -> None:
        """Appends a message to the log output."""
        self.log_output.append(message)

    def set_progress(self, value: int) -> None:
        """Sets the progress bar's value."""
        self.progress_bar.setValue(value)

    def update_lesson_status(self, status: str) -> None:
        """Update the real-time counters for a completed lesson."""
        if status == "success":
            self._success_count += 1
            self.success_label.setText(f"Sucesso: {self._success_count}")
        elif status == "partial":
            self._partial_count += 1
            self.partial_label.setText(f"Parcial: {self._partial_count}")
        elif status == "error":
            self._error_count += 1
            self.error_label.setText(f"Erro: {self._error_count}")
        elif status == "skipped":
            self._skipped_count += 1
            self.skipped_label.setText(f"Puladas: {self._skipped_count}")

    def set_download_active(self, active: bool) -> None:
        """Toggle button states based on whether a download is running."""
        self._download_active = active
        self.pause_button.setEnabled(active)
        self.save_button.setEnabled(active and self._is_premium)
        self.reauth_button.setEnabled(active)
        if active:
            self._paused = False
            self.pause_button.setText("Pausar")
            self.cancel_button.setText("Cancelar")
            self.retry_button.setEnabled(False)
        else:
            self.cancel_button.setText("Voltar ao Inicio")

    def reset(self) -> None:
        """Reset all state for a new download session."""
        self._success_count = 0
        self._partial_count = 0
        self._error_count = 0
        self._skipped_count = 0
        self.success_label.setText("Sucesso: 0")
        self.partial_label.setText("Parcial: 0")
        self.error_label.setText("Erro: 0")
        self.skipped_label.setText("Puladas: 0")
        self.progress_bar.setValue(0)
        self.log_output.clear()
        self._paused = False
        self.retry_button.setEnabled(False)

    # --- Private slots ---

    def _on_pause_clicked(self) -> None:
        if self._paused:
            self._paused = False
            self.pause_button.setText("Pausar")
            self.resume_requested.emit()
        else:
            self._paused = True
            self.pause_button.setText("Retomar")
            self.pause_requested.emit()
