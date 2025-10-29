from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QProgressBar, QLabel

class ProgressView(QWidget):
    """shows download progress via logging and a progress bar"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("Download Progress:"))
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)

        layout.addWidget(self.progress_bar)
        layout.addWidget(QLabel("Log:"))
        layout.addWidget(self.log_output)

    def log_message(self, message: str) -> None:
        """Appends a message to the log output."""
        self.log_output.append(message)

    def set_progress(self, value: int) -> None:
        """Sets the progress bar's value."""
        self.progress_bar.setValue(value)
