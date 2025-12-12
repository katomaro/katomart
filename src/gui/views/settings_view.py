from dataclasses import replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QCheckBox,
    QPushButton,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QTextEdit,
)

from src.app.membership_service import MembershipService
from src.config.settings_manager import AppSettings, SettingsManager
from urllib.parse import urlparse


class SettingsView(QWidget):
    """A widget to display and edit application settings."""

    membership_updated = Signal()

    def __init__(self, settings_manager: SettingsManager, parent: QWidget | None = None) -> None:
        """Initializes the SettingsView."""
        super().__init__(parent)
        self._settings_manager = settings_manager

        layout = QVBoxLayout(self)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)

        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)

        membership_box, general_group, paid_group = self._setup_ui()

        scroll_layout.addWidget(membership_box)
        scroll_layout.addWidget(general_group)
        scroll_layout.addWidget(paid_group)
        scroll_layout.addStretch()

        scroll_area.setWidget(scroll_content)

        self.load_settings()

        save_button = QPushButton("Salvar Configurações")
        save_button.clicked.connect(self.save_settings)

        layout.addWidget(scroll_area)
        layout.addWidget(save_button)
        self.setLayout(layout)

    def _setup_ui(self) -> tuple[QGroupBox, QGroupBox, QGroupBox]:
        """Creates and arranges the UI widgets for settings."""
        self._form_layout = QFormLayout()
        self._paid_form_layout = QFormLayout()

        self._membership_group = QGroupBox("Autenticação do Software")
        membership_layout = QFormLayout()

        self.membership_email_edit = QLineEdit()
        self.membership_password_edit = QLineEdit()
        self.membership_password_edit.setEchoMode(QLineEdit.EchoMode.Password)

        self.membership_status_label = QLabel()
        self.membership_status_label.setStyleSheet("font-weight: 600;")
        self.membership_allowed_label = QLabel()
        self.membership_allowed_label.setWordWrap(True)
        self.membership_allowed_label.setStyleSheet("color: #555555; font-size: 12px;")

        button_row = QHBoxLayout()
        self.membership_login_button = QPushButton("Entrar")
        self.membership_login_button.clicked.connect(self._authenticate_membership)
        self.membership_logout_button = QPushButton("Sair")
        self.membership_logout_button.clicked.connect(self._clear_membership)
        button_row.addWidget(self.membership_login_button)
        button_row.addWidget(self.membership_logout_button)
        button_row.addStretch()

        membership_layout.addRow("E-mail:", self.membership_email_edit)
        membership_layout.addRow("Senha:", self.membership_password_edit)
        membership_layout.addRow(button_row)
        membership_layout.addRow("Status:", self.membership_status_label)
        membership_layout.addRow("Plataformas liberadas:", self.membership_allowed_label)

        self._membership_group.setLayout(membership_layout)

        self.download_path_edit = QLineEdit()

        self.video_quality_combo = QComboBox()
        self.video_qualities = ["Mais alta", "1080p", "720p", "480p", "Mais baixa"]
        self.video_quality_combo.addItems(self.video_qualities)

        self.course_name_max_spin = QSpinBox()
        self.course_name_max_spin.setRange(10, 200)
        self.module_name_max_spin = QSpinBox()
        self.module_name_max_spin.setRange(10, 300)
        self.lesson_name_max_spin = QSpinBox()
        self.lesson_name_max_spin.setRange(10, 300)
        self.file_name_max_spin = QSpinBox()
        self.file_name_max_spin.setRange(5, 200)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 300)
        self.download_subtitles_check = QCheckBox("Baixar Legendas")

        self.subtitle_lang_combo = QComboBox()
        self.subtitle_languages = {
            "Português": "pt",
            "Português (Brasil)": "pt-BR",
            "English": "en",
            "Español": "es",
            "Français": "fr",
            "Deutsch": "de",
            "Italiano": "it",
            "Nederlands": "nl",
            "Polski": "pl",
            "Svenska": "sv",
            "Norsk": "nb",
            "Dansk": "da",
            "Suomi": "fi",
            "Русский": "ru",
            "Українська": "uk",
            "العربية": "ar",
            "עברית": "he",
            "हिन्दी": "hi",
            "বাংলা": "bn",
            "اردو": "ur",
            "Türkçe": "tr",
            "Română": "ro",
            "Čeština": "cs",
            "Magyar": "hu",
            "Ελληνικά": "el",
            "Bahasa Indonesia": "id",
            "Bahasa Melayu": "ms",
            "Tiếng Việt": "vi",
            "ภาษาไทย": "th",
            "中文 (简体)": "zh-CN",
            "中文 (繁體)": "zh-TW",
            "日本語": "ja",
            "한국어": "ko",
        }
        for name, code in self.subtitle_languages.items():
            self.subtitle_lang_combo.addItem(name, userData=code)

        self.hardcode_subtitles_check = QCheckBox("Incorporar Legendas no Vídeo")
        self.download_embedded_check = QCheckBox("Baixar Vídeos na Descrição (recomendado)")

        self.audio_lang_combo = QComboBox()
        self.audio_languages = {
            "Português": "pt",
            "Português (Brasil)": "pt-BR",
            "English": "en",
            "Español": "es",
            "Français": "fr",
            "Deutsch": "de",
            "Italiano": "it",
            "Nederlands": "nl",
            "Polski": "pl",
            "Svenska": "sv",
            "Norsk": "nb",
            "Dansk": "da",
            "Suomi": "fi",
            "Русский": "ru",
            "Українська": "uk",
            "العربية": "ar",
            "עברית": "he",
            "हिन्दी": "hi",
            "বাংলা": "bn",
            "اردو": "ur",
            "Türkçe": "tr",
            "Română": "ro",
            "Čeština": "cs",
            "Magyar": "hu",
            "Ελληνικά": "el",
            "Bahasa Indonesia": "id",
            "Bahasa Melayu": "ms",
            "Tiếng Việt": "vi",
            "ภาษาไทย": "th",
            "中文 (简体)": "zh-CN",
            "中文 (繁體)": "zh-TW",
            "日本語": "ja",
            "한국어": "ko",
        }
        for name, code in self.audio_languages.items():
            self.audio_lang_combo.addItem(name, userData=code)

        self.keep_audio_only_check = QCheckBox("Manter Apenas Áudio")
        self._form_layout.addRow("Caminho para Download:", self.download_path_edit)
        self._form_layout.addRow("Qualidade do Vídeo:", self.video_quality_combo)
        self._form_layout.addRow("Tamanho máximo do nome do Curso:", self.course_name_max_spin)
        self._form_layout.addRow("Tamanho máximo do nome do Módulo:", self.module_name_max_spin)
        self._form_layout.addRow("Tamanho máximo do nome da Aula:", self.lesson_name_max_spin)
        self._form_layout.addRow("Tamanho máximo do nome do Arquivo:", self.file_name_max_spin)
        self._form_layout.addRow("Timeout de Requisição (s):", self.timeout_spin)
        self._form_layout.addRow(self.download_subtitles_check)
        self._form_layout.addRow("Idioma das Legendas:", self.subtitle_lang_combo)
        self._form_layout.addRow(self.hardcode_subtitles_check)
        self._form_layout.addRow(self.download_embedded_check)
        self._form_layout.addRow("Idioma do Áudio (Em caso de múltiplos áudios):", self.audio_lang_combo)
        self._form_layout.addRow(self.keep_audio_only_check)

        general_group = QGroupBox("Configurações Gerais")
        general_group.setLayout(self._form_layout)

        self.paid_status_label = QLabel()
        self.paid_status_label.setStyleSheet("color: #a94442; font-size: 12px;")

        self.user_agent_edit = QLineEdit()

        self.max_concurrent_downloads_spin = QSpinBox()
        self.max_concurrent_downloads_spin.setRange(1, 16)

        self.retry_attempts_spin = QSpinBox()
        self.retry_attempts_spin.setRange(0, 10)

        self.retry_delay_spin = QSpinBox()
        self.retry_delay_spin.setRange(0, 600)
        self.create_resume_summary_check = QCheckBox("Criar JSON de Resumo")

        self.download_widevine_check = QCheckBox("Baixar Widevine")

        self.cdm_path_edit = QLineEdit()

        self.use_http_proxy_check = QCheckBox("Usar Proxy HTTP")

        self.proxy_address_edit = QLineEdit()
        self.proxy_username_edit = QLineEdit()
        self.proxy_password_edit = QLineEdit()
        self.proxy_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_port_spin = QSpinBox()
        self.proxy_port_spin.setRange(0, 65535)

        self.use_whisper_transcription_check = QCheckBox(
            "Usar Whisper para transcrever os vídeos baixados (requer ffmpeg)"
        )
        self.whisper_model_combo = QComboBox()
        self.whisper_models = [
            "tiny",
            "base",
            "small",
            "medium",
            "large",
            "large-v2",
            "large-v3",
        ]
        self.whisper_model_combo.addItems(self.whisper_models)

        self.whisper_language_combo = QComboBox()
        self.whisper_languages = {
            "Detectar automaticamente": "auto",
            "English": "en",
            "Español": "es",
            "Português": "pt",
            "Português (Brasil)": "pt-BR",
            "Français": "fr",
            "Deutsch": "de",
            "Italiano": "it",
            "العربية": "ar",
            "Русский": "ru",
            "हिन्दी": "hi",
            "বাংলা": "bn",
            "اردو": "ur",
            "Türkçe": "tr",
            "Bahasa Indonesia": "id",
            "Bahasa Melayu": "ms",
            "日本語": "ja",
            "한국어": "ko",
            "中文 (简体)": "zh-CN",
            "中文 (繁體)": "zh-TW",
        }
        for name, code in self.whisper_languages.items():
            self.whisper_language_combo.addItem(name, userData=code)

        self.whisper_output_format_combo = QComboBox()
        self.whisper_output_formats = {
            "Texto (.txt)": "txt",
            "Subrip (.srt)": "srt",
            "WebVTT (.vtt)": "vtt",
            "JSON (.json)": "json",
        }
        for name, code in self.whisper_output_formats.items():
            self.whisper_output_format_combo.addItem(name, userData=code)

        self.use_http_proxy_check.toggled.connect(self._update_proxy_fields_state)
        self._update_proxy_fields_state(False)
        self.use_whisper_transcription_check.toggled.connect(
            self._update_whisper_fields_state
        )
        self._update_whisper_fields_state(False)

        self._paid_form_layout.addRow("User Agent:", self.user_agent_edit)
        self._paid_form_layout.addRow(
            "Máximo de Downloads Concorrentes:", self.max_concurrent_downloads_spin
        )
        self._paid_form_layout.addRow(
            "Quantidade de Retentativas de download:", self.retry_attempts_spin
        )
        self._paid_form_layout.addRow("Delay para Retentativas (s):", self.retry_delay_spin)
        self._paid_form_layout.addRow(self.create_resume_summary_check)
        self._paid_form_layout.addRow(self.download_widevine_check)
        self._paid_form_layout.addRow("Caminho da CDM:", self.cdm_path_edit)
        self._paid_form_layout.addRow(self.use_http_proxy_check)
        self._paid_form_layout.addRow("Endereço do Proxy:", self.proxy_address_edit)
        self._paid_form_layout.addRow("Nome de usuário do Proxy:", self.proxy_username_edit)
        self._paid_form_layout.addRow("Senha do Proxy:", self.proxy_password_edit)
        self._paid_form_layout.addRow("Porta do Proxy:", self.proxy_port_spin)
        self._paid_form_layout.addRow(self.use_whisper_transcription_check)
        self._paid_form_layout.addRow("Modelo do Whisper:", self.whisper_model_combo)
        self._paid_form_layout.addRow("Idioma do Whisper:", self.whisper_language_combo)
        self._paid_form_layout.addRow(
            "Formato da Transcrição:", self.whisper_output_format_combo
        )
        self.embed_blacklist_edit = QTextEdit()
        self.embed_blacklist_edit.setPlaceholderText("example.com\ndocs.example.com\n...")
        self.embed_blacklist_edit.setFixedHeight(100)
        self.embed_blacklist_edit.setToolTip(
            "Insira um domínio por linha. Use apenas o hostname (ex: example.com ou docs.example.com).\n"
            "Não inclua 'http(s)://' nem caminhos. Subdomínios também serão verificados (ex: 'docs.example.com' influenciará 'sub.docs.example.com')."
        )
        self._paid_form_layout.addRow("Domínios Ignorados para Embeds (um por linha):", self.embed_blacklist_edit)
        self._paid_form_layout.addRow(self.paid_status_label)

        paid_group = QGroupBox("Configurações Pagas")
        paid_group.setLayout(self._paid_form_layout)

        return self._membership_group, general_group, paid_group

    def load_settings(self) -> None:
        """Loads settings from the manager and populates the UI."""
        settings = self._settings_manager.get_settings()
        self.download_path_edit.setText(settings.download_path)

        index = self.video_quality_combo.findText(settings.video_quality)
        if index != -1:
            self.video_quality_combo.setCurrentIndex(index)

        self.timeout_spin.setValue(settings.timeout_seconds)
        self.course_name_max_spin.setValue(getattr(settings, "max_course_name_length", 40))
        self.module_name_max_spin.setValue(getattr(settings, "max_module_name_length", 60))
        self.lesson_name_max_spin.setValue(getattr(settings, "max_lesson_name_length", 60))
        self.file_name_max_spin.setValue(getattr(settings, "max_file_name_length", 30))
        self.download_subtitles_check.setChecked(settings.download_subtitles)

        index = self.subtitle_lang_combo.findData(settings.subtitle_language)
        if index != -1:
            self.subtitle_lang_combo.setCurrentIndex(index)

        self.hardcode_subtitles_check.setChecked(settings.hardcode_subtitles)

        index = self.audio_lang_combo.findData(settings.audio_language)
        if index != -1:
            self.audio_lang_combo.setCurrentIndex(index)

        self.keep_audio_only_check.setChecked(settings.keep_audio_only)
        self.download_embedded_check.setChecked(settings.download_embedded_videos)

        self.membership_email_edit.setText(settings.membership_email)
        self.membership_password_edit.clear()
        self.membership_status_label.setText(
            "Assinante" if settings.is_premium_member else "Gratuito"
        )
        allowed_text = ", ".join(settings.allowed_platforms) if settings.allowed_platforms else "Nenhuma"
        self.membership_allowed_label.setText(allowed_text)
        self.membership_logout_button.setEnabled(
            bool(settings.membership_token) or settings.has_full_permissions
        )

        self.user_agent_edit.setText(settings.user_agent)
        self.max_concurrent_downloads_spin.setValue(settings.max_concurrent_segment_downloads)
        self.retry_attempts_spin.setValue(settings.download_retry_attempts)
        self.retry_delay_spin.setValue(settings.download_retry_delay_seconds)
        self.create_resume_summary_check.setChecked(
            getattr(settings, "create_resume_summary", False)
        )
        self.download_widevine_check.setChecked(settings.download_widevine)
        self.cdm_path_edit.setText(settings.cdm_path)
        self.use_http_proxy_check.setChecked(settings.use_http_proxy)
        self.proxy_address_edit.setText(settings.proxy_address)
        self.proxy_username_edit.setText(settings.proxy_username)
        self.proxy_password_edit.setText(settings.proxy_password)
        self.proxy_port_spin.setValue(settings.proxy_port)
        self._update_proxy_fields_state(settings.use_http_proxy)
        self.use_whisper_transcription_check.setChecked(settings.use_whisper_transcription)

        model_index = self.whisper_model_combo.findText(settings.whisper_model)
        if model_index != -1:
            self.whisper_model_combo.setCurrentIndex(model_index)

        language_index = self.whisper_language_combo.findData(settings.whisper_language)
        if language_index != -1:
            self.whisper_language_combo.setCurrentIndex(language_index)

        output_format_index = self.whisper_output_format_combo.findData(
            settings.whisper_output_format
        )
        if output_format_index != -1:
            self.whisper_output_format_combo.setCurrentIndex(output_format_index)

        self._update_whisper_fields_state(settings.use_whisper_transcription)
        self._update_paid_settings_state(settings)
        try:
            normalized = []
            for entry in (settings.embed_domain_blacklist or []):
                try:
                    p = urlparse(entry.strip())
                    host = (p.netloc or p.path or "").lower().strip()
                except Exception:
                    host = entry.strip().lower()
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    normalized.append(host)
            self.embed_blacklist_edit.setPlainText("\n".join(normalized))
        except Exception:
            self.embed_blacklist_edit.setPlainText("")

    def save_settings(self) -> None:
        """Saves the current UI settings back to the file."""
        current_settings = self._settings_manager.get_settings()
        updated_settings = AppSettings(
            download_path=self.download_path_edit.text(),
            video_quality=self.video_quality_combo.currentText(),
            max_concurrent_segment_downloads=self.max_concurrent_downloads_spin.value(),
            timeout_seconds=self.timeout_spin.value(),
            download_subtitles=self.download_subtitles_check.isChecked(),
            subtitle_language=self.subtitle_lang_combo.currentData(),
            hardcode_subtitles=self.hardcode_subtitles_check.isChecked(),
            audio_language=self.audio_lang_combo.currentData(),
            keep_audio_only=self.keep_audio_only_check.isChecked(),
            user_agent=self.user_agent_edit.text().strip(),
            download_retry_attempts=self.retry_attempts_spin.value(),
            download_retry_delay_seconds=self.retry_delay_spin.value(),
            download_widevine=self.download_widevine_check.isChecked(),
            cdm_path=self.cdm_path_edit.text().strip(),
            use_http_proxy=self.use_http_proxy_check.isChecked(),
            proxy_address=self.proxy_address_edit.text().strip(),
            proxy_username=self.proxy_username_edit.text().strip(),
            proxy_password=self.proxy_password_edit.text(),
            proxy_port=self.proxy_port_spin.value(),
            create_resume_summary=self.create_resume_summary_check.isChecked(),
            use_whisper_transcription=self.use_whisper_transcription_check.isChecked(),
            whisper_model=self.whisper_model_combo.currentText(),
            whisper_language=self.whisper_language_combo.currentData(),
            whisper_output_format=self.whisper_output_format_combo.currentData(),
            run_ffmpeg=current_settings.run_ffmpeg,
            ffmpeg_args=current_settings.ffmpeg_args,
            max_course_name_length=self.course_name_max_spin.value(),
            max_module_name_length=self.module_name_max_spin.value(),
            max_lesson_name_length=self.lesson_name_max_spin.value(),
            max_file_name_length=self.file_name_max_spin.value(),
            membership_email=current_settings.membership_email,
            membership_token=current_settings.membership_token,
            allowed_platforms=list(current_settings.allowed_platforms),
            is_premium_member=current_settings.is_premium_member,
            download_embedded_videos=self.download_embedded_check.isChecked(),
            embed_domain_blacklist=[
                (lambda s: (lambda h: h[4:] if h.startswith('www.') else h)(
                    (urlparse(s.strip()).netloc or urlparse(s.strip()).path or '').lower().strip()
                ))(line)
                for line in self.embed_blacklist_edit.toPlainText().splitlines()
                if line.strip()
            ],
            permissions=list(current_settings.permissions),
            has_full_permissions=current_settings.has_full_permissions,
        )
        self._settings_manager.save_settings(updated_settings)

    def _authenticate_membership(self) -> None:
        """Authenticates the app user and stores the returned entitlements."""
        email = self.membership_email_edit.text().strip()
        password = self.membership_password_edit.text().strip()

        if not email or not password:
            QMessageBox.warning(self, "Dados incompletos", "Informe e-mail e senha para autenticar.")
            return

        settings = self._settings_manager.get_settings(include_premium=True)
        service = MembershipService(timeout=settings.timeout_seconds)

        try:
            membership_info = service.authenticate(email, password)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Falha na autenticação", str(exc))
            return

        updated_settings = replace(
            settings,
            membership_email=email,
            membership_token=membership_info.token,
            allowed_platforms=membership_info.allowed_platforms,
            is_premium_member=membership_info.is_premium,
            permissions=membership_info.permissions,
            has_full_permissions=membership_info.is_premium,
        )
        self._settings_manager.save_settings(updated_settings)
        self.load_settings()
        self.membership_password_edit.clear()
        self.membership_updated.emit()

    def _clear_membership(self) -> None:
        """Clears membership data from the settings."""
        settings = self._settings_manager.get_settings()
        if not settings.membership_token and not settings.has_full_permissions:
            self.membership_password_edit.clear()
            return

        updated_settings = replace(
            settings,
            membership_token="",
            allowed_platforms=[],
            is_premium_member=False,
            permissions=[],
            has_full_permissions=False,
        )
        self._settings_manager.save_settings(updated_settings)
        self.load_settings()
        self.membership_password_edit.clear()
        self.membership_updated.emit()

    def _update_paid_settings_state(self, settings: AppSettings) -> None:
        """Enables or disables paid settings based on permissions."""
        if settings.has_full_permissions:
            self.paid_status_label.setText("Permissão katomart.FULL detectada. Opções liberadas.")
            self.paid_status_label.setStyleSheet("color: #3c763d; font-weight: 600;")
        else:
            self.paid_status_label.setText(
                "Faça login para liberar configurações pagas como concorrência de segmentos."
            )
            self.paid_status_label.setStyleSheet("color: #a94442; font-size: 12px;")

        self._paid_form_layout.parentWidget().setEnabled(settings.has_full_permissions)

    def _update_proxy_fields_state(self, enabled: bool | None = None) -> None:
        """Enables or disables proxy detail inputs."""
        if enabled is None:
            enabled = self.use_http_proxy_check.isChecked()

        for widget in (
            self.proxy_address_edit,
            self.proxy_username_edit,
            self.proxy_password_edit,
            self.proxy_port_spin,
        ):
            widget.setEnabled(bool(enabled))

    def _update_whisper_fields_state(self, enabled: bool | None = None) -> None:
        """Enables or disables Whisper-related inputs."""
        if enabled is None:
            enabled = self.use_whisper_transcription_check.isChecked()

        for widget in (
            self.whisper_model_combo,
            self.whisper_language_combo,
            self.whisper_output_format_combo,
        ):
            widget.setEnabled(bool(enabled))
