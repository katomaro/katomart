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
)

from src.config.settings_manager import SettingsManager
from src.platforms.base import AuthField, AuthFieldType, PlatformFactory

ValueAccessor = Callable[[], Any]


@dataclass
class AuthInputHandlers:
    accessor: ValueAccessor
    reset: Callable[[], None]


class AuthView(QWidget):
    """First screen: for authentication and platform selection."""

    list_products_requested = Signal(str, dict)

    def __init__(self, settings_manager: SettingsManager, parent: QWidget | None = None) -> None:
        """Initializes the view."""
        super().__init__(parent)
        self._settings_manager = settings_manager
        self._allowed_platforms: set[str] = set()
        self._is_premium_member = False
        self._active_dialogs: list[QMessageBox] = []

        layout = QVBoxLayout(self)

        self.platform_combo = QComboBox()

        self.form_layout = QFormLayout()
        self.form_layout.addRow(QLabel("Plataforma:"), self.platform_combo)

        self.credentials_layout = QFormLayout()

        layout.addLayout(self.form_layout)
        layout.addLayout(self.credentials_layout)

        self.platform_notice_label = QLabel()
        self.platform_notice_label.setWordWrap(True)
        self.platform_notice_label.setStyleSheet("color: #a94442; font-size: 12px;")
        self.platform_notice_label.hide()
        layout.addWidget(self.platform_notice_label)

        self.instructions_title = QLabel("Instruções:")
        self.instructions_title.setStyleSheet("font-weight: 600;")
        self.instructions_label = QLabel()
        self.instructions_label.setWordWrap(True)
        self.instructions_label.setStyleSheet("color: #666666; font-size: 13px;")

        layout.addWidget(self.instructions_title)
        layout.addWidget(self.instructions_label)

        self.list_products_button = QPushButton("Listar Produtos da Conta")
        self.list_products_button.clicked.connect(self._on_list_products)

        layout.addWidget(self.list_products_button)

        self._auth_inputs: dict[str, AuthInputHandlers] = {}

        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)

        self.refresh_membership_state()

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
            self.instructions_label.setText(
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
            self.instructions_label.clear()
            self._update_list_button_state()
            return

        platform_name = self.platform_combo.currentText()
        platform_class = PlatformFactory.get_platform_class(platform_name)
        self._clear_auth_fields()

        if not platform_class:
            self.instructions_label.setText(
                f"Falha ao encontrar instruções para a plataforma '{platform_name}'. Reporte no grupo do Telegram ou em uma Issue."
            )
            self._update_list_button_state()
            return

        self._build_auth_fields(platform_class.all_auth_fields())
        self.instructions_label.setText(platform_class.auth_instructions())
        self._update_list_button_state()

    def _clear_auth_fields(self) -> None:
        """Removes the previous authentication input widgets."""
        while self.credentials_layout.rowCount():
            self.credentials_layout.removeRow(0)
        self._auth_inputs.clear()

    def reset_auth_inputs(self) -> None:
        """Clears the current authentication input values."""
        for handlers in self._auth_inputs.values():
            handlers.reset()

    def _build_auth_fields(self, fields: list[AuthField]) -> None:
        """Creates authentication input widgets based on the selected platform."""
        if not fields:
            no_credentials_label = QLabel("Esta plataforma não requer credenciais adicionais.")
            no_credentials_label.setStyleSheet("color: #666666;")
            self.credentials_layout.addRow(no_credentials_label)
            return

        for field in fields:
            label_text = f"{field.label}:"
            if not field.required:
                label_text = f"{label_text} (opcional)"
            label = QLabel(label_text)
            input_widget, accessor, reset = self._create_input_widget(field)
            self.credentials_layout.addRow(label, input_widget)
            self._auth_inputs[field.name] = AuthInputHandlers(accessor, reset)

    def _create_input_widget(self, field: AuthField) -> tuple[QWidget, ValueAccessor, Callable[[], None]]:
        """Builds the widget for a single authentication field."""
        requires_premium = field.requires_membership and not self._is_premium_member

        if field.field_type is AuthFieldType.PASSWORD:
            line_edit = QLineEdit()
            line_edit.setEchoMode(QLineEdit.EchoMode.Password)
            line_edit.setPlaceholderText(field.placeholder)
            widget: QWidget = line_edit
            accessor = lambda line=line_edit: line.text().strip()
            reset = line_edit.clear
        elif field.field_type is AuthFieldType.MULTILINE:
            text_edit = QTextEdit()
            text_edit.setPlaceholderText(field.placeholder)
            text_edit.setFixedHeight(max(80, text_edit.fontMetrics().lineSpacing() * 4))
            widget = text_edit
            accessor = lambda editor=text_edit: editor.toPlainText().strip()
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
            reset = editor.reset_values
        elif field.field_type is AuthFieldType.CHECKBOX:
            checkbox = QCheckBox()
            widget = checkbox
            accessor = lambda toggle=checkbox: toggle.isChecked()
            reset = lambda toggle=checkbox: toggle.setChecked(False)
        else:
            line_edit = QLineEdit()
            line_edit.setPlaceholderText(field.placeholder)
            widget = line_edit
            accessor = lambda line=line_edit: line.text().strip()
            reset = line_edit.clear

        if requires_premium:
            widget.setEnabled(False)
            widget.setToolTip("Disponível apenas para assinantes ativos.")
            if isinstance(widget, QLineEdit):
                widget.setPlaceholderText("Disponível apenas para assinantes.")
            elif isinstance(widget, QTextEdit):
                widget.setPlaceholderText("Disponível apenas para assinantes.")

            empty_value = False if isinstance(widget, QCheckBox) else ""
            return widget, (lambda: empty_value), (lambda: None)

        return widget, accessor, reset


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


