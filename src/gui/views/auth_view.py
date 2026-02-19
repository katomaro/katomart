from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFormLayout,
    QComboBox,
    QTextEdit,
    QHBoxLayout,
    QCheckBox,
    QMessageBox,
    QSizePolicy,
)

from src.config.settings_manager import SettingsManager
from src.config.credentials_manager import CredentialsManager
from src.platforms.base import AuthField, AuthFieldType, PlatformFactory

ValueAccessor = Callable[[], Any]
ValueSetter = Callable[[Any], None]


@dataclass
class AuthInputHandlers:
    accessor: ValueAccessor
    setter: ValueSetter
    reset: Callable[[], None]


class AuthView(QWidget):
    """First screen: for authentication and platform selection."""

    list_products_requested = Signal(str, dict)

    def __init__(self, settings_manager: SettingsManager, parent: QWidget | None = None) -> None:
        """Initializes the view."""
        super().__init__(parent)
        self._settings_manager = settings_manager
        self._credentials_manager = CredentialsManager()
        self._allowed_platforms: set[str] = set()
        self._is_premium_member = False
        self._active_dialogs: list[QMessageBox] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # === Platform selector ===
        platform_layout = QHBoxLayout()
        platform_layout.setSpacing(8)
        platform_label = QLabel("Plataforma:")
        platform_label.setStyleSheet("font-weight: 600;")
        self.platform_combo = QComboBox()
        self.platform_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.platform_combo.setMaximumWidth(500)
        platform_layout.addWidget(platform_label)
        platform_layout.addWidget(self.platform_combo)
        platform_layout.addStretch()
        layout.addLayout(platform_layout)

        self.platform_notice_label = QLabel()
        self.platform_notice_label.setWordWrap(True)
        self.platform_notice_label.setStyleSheet("color: #a94442; font-size: 12px; padding: 4px;")
        self.platform_notice_label.hide()
        layout.addWidget(self.platform_notice_label)

        # === Token section (optional, for everyone) ===
        token_title = QLabel("Opção 1: Token de Acesso")
        token_title.setStyleSheet("font-weight: 600; font-size: 12px; color: #888888; margin-top: 8px;")
        layout.addWidget(token_title)

        token_hint = QLabel("Use o token caso não seja assinante. Siga as instruções abaixo para obter o token de acesso.")
        token_hint.setStyleSheet("color: #888888; font-size: 11px; font-style: italic; margin-left: 4px;")
        layout.addWidget(token_hint)

        self.token_form = QFormLayout()
        self.token_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.token_form.setVerticalSpacing(8)
        self.token_form.setContentsMargins(4, 4, 0, 8)
        layout.addLayout(self.token_form)

        # === Credentials section (for subscribers) ===
        self.credentials_title = QLabel("Opção 2: Login Direto (Assinantes)")
        self.credentials_title.setStyleSheet("font-weight: 600; font-size: 12px; color: #27ae60; margin-top: 8px;")
        layout.addWidget(self.credentials_title)

        self.credentials_hint = QLabel("Assinantes podem fazer login diretamente, sem precisar obter o token manualmente.")
        self.credentials_hint.setStyleSheet("color: #27ae60; font-size: 11px; font-style: italic; margin-left: 4px;")
        self.credentials_hint.setWordWrap(True)
        layout.addWidget(self.credentials_hint)

        self.credentials_form = QFormLayout()
        self.credentials_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.credentials_form.setVerticalSpacing(8)
        self.credentials_form.setContentsMargins(4, 4, 0, 4)
        layout.addLayout(self.credentials_form)

        # Save credentials checkboxes
        save_layout = QHBoxLayout()
        save_layout.setContentsMargins(4, 0, 0, 0)
        save_layout.setSpacing(15)
        self.save_email_checkbox = QCheckBox("Salvar email")
        self.save_password_checkbox = QCheckBox("Salvar senha")
        self.save_password_checkbox.toggled.connect(self._on_save_password_toggled)
        save_layout.addWidget(self.save_email_checkbox)
        save_layout.addWidget(self.save_password_checkbox)
        save_layout.addStretch()
        layout.addLayout(save_layout)

        self.clear_credentials_button = QPushButton("Limpar dados salvos")
        self.clear_credentials_button.setFixedWidth(160)
        self.clear_credentials_button.clicked.connect(self._on_clear_credentials)
        layout.addWidget(self.clear_credentials_button)

        # === Extra fields section (platform-specific) ===
        self.extra_title = QLabel("Campos Adicionais")
        self.extra_title.setStyleSheet("font-weight: 600; font-size: 12px; color: #888888; margin-top: 8px;")
        self.extra_title.hide()
        layout.addWidget(self.extra_title)

        self.extra_form = QFormLayout()
        self.extra_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.extra_form.setVerticalSpacing(8)
        self.extra_form.setContentsMargins(4, 4, 0, 8)
        layout.addLayout(self.extra_form)

        # === Instructions section (at bottom, with scroll) ===
        instructions_header = QHBoxLayout()
        instructions_title = QLabel("Instruções da Plataforma")
        instructions_title.setStyleSheet("font-weight: 600; font-size: 12px; color: #3daee9; margin-top: 10px;")
        instructions_header.addWidget(instructions_title)

        scroll_hint = QLabel("(role para ver mais)")
        scroll_hint.setStyleSheet("font-size: 10px; color: #888888; font-style: italic; margin-top: 10px;")
        instructions_header.addWidget(scroll_hint)
        instructions_header.addStretch()
        layout.addLayout(instructions_header)

        self.instructions_text = QTextEdit()
        self.instructions_text.setReadOnly(True)
        self.instructions_text.setStyleSheet("""
            QTextEdit {
                font-size: 12px;
                border: 2px solid #3daee9;
                border-radius: 6px;
                padding: 8px;
                background-color: palette(base);
            }
            QTextEdit QScrollBar:vertical {
                background: palette(base);
                width: 12px;
                border-radius: 6px;
            }
            QTextEdit QScrollBar::handle:vertical {
                background: #3daee9;
                border-radius: 5px;
                min-height: 30px;
            }
            QTextEdit QScrollBar::handle:vertical:hover {
                background: #2d9ed9;
            }
            QTextEdit QScrollBar::add-line:vertical,
            QTextEdit QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        self.instructions_text.setMinimumHeight(100)
        self.instructions_text.setMaximumHeight(140)
        self.instructions_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.instructions_text)

        # Add stretch to push button to bottom
        layout.addStretch()

        self.list_products_button = QPushButton("Listar Produtos da Conta")
        self.list_products_button.setStyleSheet("""
            QPushButton {
                padding: 12px 24px;
                font-weight: 700;
                font-size: 14px;
                background-color: #27ae60;
                color: white;
                border: none;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #2ecc71;
            }
            QPushButton:pressed {
                background-color: #1e8449;
            }
            QPushButton:disabled {
                background-color: #7f8c8d;
                color: #bdc3c7;
            }
        """)
        self.list_products_button.setMinimumHeight(44)
        self.list_products_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.list_products_button.clicked.connect(self._on_list_products)
        layout.addWidget(self.list_products_button)

        self._auth_inputs: dict[str, AuthInputHandlers] = {}

        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)

        self.refresh_membership_state()

    def _on_save_password_toggled(self, checked: bool) -> None:
        if checked:
            QMessageBox.warning(
                self,
                "Aviso de Segurança",
                "Salvar senha não é seguro pois aplicativos terceiros (ex: scripts para backup de terceiros) "
                "podem procurar o arquivo do katomart (e também carteiras de criptomoedas) e os roubar."
            )

    def _on_clear_credentials(self) -> None:
        self._credentials_manager.clear_credentials()
        QMessageBox.information(self, "Sucesso", "Dados salvos limpos com sucesso.")

    def _on_list_products(self) -> None:
        """Emits the signal to request the product list."""
        platform_name = self.platform_combo.currentText()
        credentials = {
            name: handlers.accessor()
            for name, handlers in self._auth_inputs.items()
        }

        if credentials.get("browser_emulation"):
            confirmation_event = threading.Event()
            credentials["manual_auth_confirmation"] = confirmation_event
            self._show_browser_emulation_dialog(confirmation_event)

        if self.save_email_checkbox.isChecked() or self.save_password_checkbox.isChecked():
            email = credentials.get("username") if self.save_email_checkbox.isChecked() else ""
            password = credentials.get("password") if self.save_password_checkbox.isChecked() else ""
            self._credentials_manager.save_credentials(platform_name, email or "", password or "")

        self.list_products_requested.emit(platform_name, credentials)

    def _show_browser_emulation_dialog(self, confirmation_event: threading.Event) -> None:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setWindowTitle("Autenticação manual necessária")
        dialog.setText(
            "Complete a autenticação na plataforma, APÓS o login, clique em ok nesse diálogo"
        )
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.finished.connect(lambda _: confirmation_event.set())
        dialog.finished.connect(lambda _: self._remove_dialog_reference(dialog))

        self._active_dialogs.append(dialog)
        dialog.show()

    def _remove_dialog_reference(self, dialog: QMessageBox) -> None:
        try:
            self._active_dialogs.remove(dialog)
        except ValueError:
            pass

    def refresh_membership_state(self) -> None:
        """Reloads membership info from settings and updates the UI."""
        settings = self._settings_manager.get_settings()
        self._allowed_platforms = set(settings.allowed_platforms or [])
        self._is_premium_member = settings.is_premium_member

        self.save_email_checkbox.setEnabled(self._is_premium_member)
        self.save_password_checkbox.setEnabled(self._is_premium_member)
        self.clear_credentials_button.setEnabled(self._is_premium_member)

        if not self._is_premium_member:
            self.save_email_checkbox.setChecked(False)
            self.save_password_checkbox.setChecked(False)
            self.save_email_checkbox.setToolTip("Disponível apenas para assinantes.")
            self.save_password_checkbox.setToolTip("Disponível apenas para assinantes.")
            # Update credentials appearance for non-subscribers
            self.credentials_title.setStyleSheet("font-weight: 600; font-size: 12px; color: #888888; margin-top: 8px;")
            self.credentials_hint.setText("Disponível apenas para assinantes. Vá em Configurações para ativar sua assinatura.")
            self.credentials_hint.setStyleSheet("color: #888888; font-size: 11px; font-style: italic; margin-left: 4px;")
        else:
            self.save_email_checkbox.setToolTip("")
            self.save_password_checkbox.setToolTip("")
            # Update credentials appearance for subscribers
            self.credentials_title.setStyleSheet("font-weight: 600; font-size: 12px; color: #27ae60; margin-top: 8px;")
            self.credentials_hint.setText("Assinantes podem fazer login diretamente, sem precisar obter o token manualmente.")
            self.credentials_hint.setStyleSheet("color: #27ae60; font-size: 11px; font-style: italic; margin-left: 4px;")

        self._rebuild_platform_combo()

    def _rebuild_platform_combo(self) -> None:
        """Rebuilds the platform list according to the allowed entitlements."""
        previous_selection = self.platform_combo.currentText()
        available = sorted(PlatformFactory.get_platform_names())
        if self._allowed_platforms:
            available = [name for name in available if name in self._allowed_platforms]

        self.platform_combo.blockSignals(True)
        self.platform_combo.clear()
        for name in available:
            self.platform_combo.addItem(name)
        self.platform_combo.blockSignals(False)

        if available:
            if previous_selection in available:
                previous_index = self.platform_combo.findText(previous_selection)
                self.platform_combo.setCurrentIndex(previous_index)
            else:
                self.platform_combo.setCurrentIndex(0)
            self.platform_combo.setEnabled(True)
            self.platform_notice_label.hide()
            self._on_platform_changed(self.platform_combo.currentIndex())
        else:
            self.platform_combo.setEnabled(False)
            self._clear_auth_fields()
            self.instructions_text.setText(
                "Autentique-se na aba Configurações para liberar plataformas."
            )
            self.platform_notice_label.setText(
                "Nenhuma plataforma liberada. Autentique-se na aba Configurações."
            )
            self.platform_notice_label.show()

        self._update_list_button_state()

    def _update_list_button_state(self) -> None:
        has_platform = self.platform_combo.isEnabled() and self.platform_combo.count() > 0
        self.list_products_button.setEnabled(has_platform)

    def _on_platform_changed(self, index: int) -> None:
        """Updates the token instructions text according to the selected platform."""
        if index < 0:
            self._clear_auth_fields()
            self.instructions_text.clear()
            self._update_list_button_state()
            return

        platform_name = self.platform_combo.currentText()
        platform_class = PlatformFactory.get_platform_class(platform_name)
        self._clear_auth_fields()

        if not platform_class:
            self.instructions_text.setText(
                f"Falha ao encontrar instruções para a plataforma '{platform_name}'. Reporte no grupo do Telegram ou em uma Issue."
            )
            self._update_list_button_state()
            return

        self._build_auth_fields(platform_class.all_auth_fields())
        self.instructions_text.setText(platform_class.auth_instructions())
        self._update_list_button_state()

        if self._is_premium_member:
            saved_creds = self._credentials_manager.get_credentials(platform_name)
            if saved_creds:
                email = saved_creds.get("email")
                password = saved_creds.get("password")
                
                self.save_email_checkbox.blockSignals(True)
                self.save_password_checkbox.blockSignals(True)

                if email:
                    if "username" in self._auth_inputs:
                        self._auth_inputs["username"].setter(email)
                    self.save_email_checkbox.setChecked(True)
                else:
                    self.save_email_checkbox.setChecked(False)
                
                if password:
                    if "password" in self._auth_inputs:
                        self._auth_inputs["password"].setter(password)
                    self.save_password_checkbox.setChecked(True)
                else:
                    self.save_password_checkbox.setChecked(False)

                self.save_email_checkbox.blockSignals(False)
                self.save_password_checkbox.blockSignals(False)
            else:
                self.save_email_checkbox.setChecked(False)
                self.save_password_checkbox.setChecked(False)
        else:
            self.save_email_checkbox.setChecked(False)
            self.save_password_checkbox.setChecked(False)

    def _clear_auth_fields(self) -> None:
        """Removes the previous authentication input widgets."""
        # Clear token form
        while self.token_form.rowCount() > 0:
            self.token_form.removeRow(0)
        # Clear credentials form
        while self.credentials_form.rowCount() > 0:
            self.credentials_form.removeRow(0)
        # Clear extra form
        while self.extra_form.rowCount() > 0:
            self.extra_form.removeRow(0)
        self.extra_title.hide()
        self._auth_inputs.clear()

    def reset_auth_inputs(self) -> None:
        """Clears the current authentication input values."""
        for handlers in self._auth_inputs.values():
            handlers.reset()

    def _build_auth_fields(self, fields: list[AuthField]) -> None:
        """Creates authentication input widgets based on the selected platform."""
        if not fields:
            return

        # Define which fields go to which section
        token_field_names = {"token"}
        credential_field_names = {"username", "password", "browser_emulation"}

        has_extra_fields = False

        for field in fields:
            label_text = f"{field.label}:"
            if not field.required:
                label_text = f"{label_text} (opcional)"
            label = QLabel(label_text)
            input_widget, accessor, setter, reset = self._create_input_widget(field)

            # Route to appropriate form
            if field.name in token_field_names:
                self.token_form.addRow(label, input_widget)
            elif field.name in credential_field_names:
                self.credentials_form.addRow(label, input_widget)
            else:
                # Extra platform-specific fields
                self.extra_form.addRow(label, input_widget)
                has_extra_fields = True

            self._auth_inputs[field.name] = AuthInputHandlers(accessor, setter, reset)

        # Show/hide extra fields title
        if has_extra_fields:
            self.extra_title.show()
        else:
            self.extra_title.hide()

    def _create_input_widget(self, field: AuthField) -> tuple[QWidget, ValueAccessor, ValueSetter, Callable[[], None]]:
        """Builds the widget for a single authentication field."""
        requires_premium = field.requires_membership and not self._is_premium_member

        if field.field_type is AuthFieldType.PASSWORD:
            line_edit = QLineEdit()
            line_edit.setEchoMode(QLineEdit.EchoMode.Password)
            line_edit.setPlaceholderText(field.placeholder)
            line_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            line_edit.setMaximumWidth(600)
            widget: QWidget = line_edit
            accessor = lambda line=line_edit: line.text().strip()
            setter = lambda val, line=line_edit: line.setText(str(val))
            reset = line_edit.clear
        elif field.field_type is AuthFieldType.MULTILINE:
            text_edit = QTextEdit()
            text_edit.setPlaceholderText(field.placeholder)
            text_edit.setFixedHeight(max(80, text_edit.fontMetrics().lineSpacing() * 4))
            text_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            text_edit.setMaximumWidth(600)
            widget = text_edit
            accessor = lambda editor=text_edit: editor.toPlainText().strip()
            setter = lambda val, editor=text_edit: editor.setText(str(val))
            reset = text_edit.clear
        elif field.field_type is AuthFieldType.KEY_VALUE_LIST:
            editor = _KeyValueEditor(
                key_label=field.key_label,
                key_placeholder=field.key_placeholder,
                value_label=field.value_label,
                value_placeholder=field.value_placeholder,
            )
            widget = editor
            accessor = editor.get_values
            setter = lambda val: None
            reset = editor.reset_values
        elif field.field_type is AuthFieldType.CHECKBOX:
            checkbox = QCheckBox()
            widget = checkbox
            accessor = lambda toggle=checkbox: toggle.isChecked()
            setter = lambda val, toggle=checkbox: toggle.setChecked(bool(val))
            reset = lambda toggle=checkbox: toggle.setChecked(False)
        else:
            line_edit = QLineEdit()
            line_edit.setPlaceholderText(field.placeholder)
            line_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            line_edit.setMaximumWidth(600)
            widget = line_edit
            accessor = lambda line=line_edit: line.text().strip()
            setter = lambda val, line=line_edit: line.setText(str(val))
            reset = line_edit.clear

        if requires_premium:
            widget.setEnabled(False)
            widget.setToolTip("Disponível apenas para assinantes ativos.")
            if isinstance(widget, QLineEdit):
                widget.setPlaceholderText("Disponível apenas para assinantes.")
            elif isinstance(widget, QTextEdit):
                widget.setPlaceholderText("Disponível apenas para assinantes.")

            empty_value = False if isinstance(widget, QCheckBox) else ""
            return widget, (lambda: empty_value), (lambda val: None), (lambda: None)

        return widget, accessor, setter, reset


class _KeyValueEditor(QWidget):
    """Widget that allows editing a dynamic list of key/value pairs."""

    def __init__(
        self,
        key_label: str,
        key_placeholder: str,
        value_label: str,
        value_placeholder: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._key_label = key_label
        self._key_placeholder = key_placeholder
        self._value_label = value_label
        self._value_placeholder = value_placeholder
        self._rows: list[tuple[QLineEdit, QLineEdit, QWidget]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(6)
        layout.addLayout(self._rows_layout)

        add_button = QPushButton("Adicionar")
        add_button.setFixedWidth(100)
        add_button.clicked.connect(self._add_row)
        layout.addWidget(add_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self._add_row()

    def _add_row(self) -> None:
        """Adds a new key/value row to the editor."""
        row_container = QWidget(self)
        row_layout = QHBoxLayout(row_container)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        key_edit = QLineEdit()
        key_edit.setPlaceholderText(self._key_placeholder)
        value_edit = QLineEdit()
        value_edit.setPlaceholderText(self._value_placeholder)

        key_label_widget = QLabel(self._key_label)
        value_label_widget = QLabel(self._value_label)

        remove_button = QPushButton("Remover")
        remove_button.setFixedWidth(90)

        def _remove() -> None:
            self._remove_row(row_container)

        remove_button.clicked.connect(_remove)

        row_layout.addWidget(key_label_widget)
        row_layout.addWidget(key_edit)
        row_layout.addWidget(value_label_widget)
        row_layout.addWidget(value_edit)
        row_layout.addWidget(remove_button)

        self._rows_layout.addWidget(row_container)
        self._rows.append((key_edit, value_edit, row_container))

    def _remove_row(self, row_widget: QWidget) -> None:
        """Removes the provided row widget from the layout."""
        for index, (_, _, widget) in enumerate(self._rows):
            if widget is row_widget:
                widget.setParent(None)
                del self._rows[index]
                break

        if not self._rows:
            self._add_row()

    def get_values(self) -> dict[str, str]:
        """Returns the edited key/value pairs, ignoring empty keys."""
        values: dict[str, str] = {}
        for key_edit, value_edit, _ in self._rows:
            key = key_edit.text().strip()
            value = value_edit.text().strip()
            if key:
                values[key] = value
        return values

    def reset_values(self) -> None:
        """Clears all rows and leaves a single empty row ready for input."""
        for _, _, widget in list(self._rows):
            widget.setParent(None)
        self._rows.clear()
        self._add_row()


