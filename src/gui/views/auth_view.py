from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QFormLayout, QComboBox
)

from src.platforms.base import PlatformFactory


class AuthView(QWidget):
    """First screen: for authentication and platform selection."""
    list_products_requested = Signal(str, dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initializes the view."""
        super().__init__(parent)

        layout = QVBoxLayout(self)
        form_layout = QFormLayout()

        self.platform_combo = QComboBox()
        self.platform_combo.addItems(PlatformFactory.get_platform_names())
        layout.addWidget(self.platform_combo)

        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText("Token de Acesso...")
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_instructions = QLabel()
        self.token_instructions.setWordWrap(True)
        self.token_instructions.setStyleSheet("color: #666666; font-size: 13px;")

        self.list_products_button = QPushButton("Listar Produtos da Conta")
        self.list_products_button.clicked.connect(self._on_list_products)

        form_layout.addRow(QLabel("Plataforma:"), self.platform_combo)
        form_layout.addRow(QLabel("Token de Acesso:"), self.token_input)
        form_layout.addRow(QLabel("Instruções:"), self.token_instructions)
        layout.addLayout(form_layout)
        layout.addWidget(self.token_instructions)
        layout.addWidget(self.list_products_button)

        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)
        self._on_platform_changed(self.platform_combo.currentIndex())

    def _on_list_products(self) -> None:
        """Emits the signal to request the product list."""
        platform_name = self.platform_combo.currentText()
        credentials = {"token": self.token_input.text()}
        self.list_products_requested.emit(platform_name, credentials)

    def _on_platform_changed(self, index: int) -> None:
        """Updates the token instructions text according to the selected platform."""
        platform_name = self.platform_combo.currentText()

        guidance = {
            "Hotmart": (
                "Como obter o token da Hotmart?:\n"
                "1) Abra o seu navegador e vá para https://consumer.hotmart.com.\n"
                "2) Abra as Ferramentas de Desenvolvedor (F12) → aba Rede (também pode ser chamada de Requisições ou Network).\n"
                "3) Faça o login normalmente sem fechar essa aba aguarde aparecer a lista de produtos da conta.\n"
                "4) Use a lupa para procurar a URL \"https://api-hub.cb.hotmart.com/club-drive-api/rest/v1/\".\n"
                "5) Clique nessa requisição que tenha o indicativo GET e vá para a aba Headers (Cabeçalhos), em requisição lá em baixo.\n"
                "6) Copie o valor do cabeçalho 'Authorization' — ele se parece com 'Bearer <token>'.\n"
                "   Cole apenas a parte do token aqui."
            ),
        }

        text = guidance.get(platform_name,
            (
                f"Falha ao encontrar instruções para a plataforma '{platform_name}'. Reporte no grupo do Telegram ou em uma Issue."
            )
        )
        self.token_instructions.setText(text)
