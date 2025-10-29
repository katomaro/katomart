from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QLineEdit, QSpinBox, 
    QCheckBox, QPushButton, QComboBox
)
from src.config.settings_manager import SettingsManager, AppSettings

class SettingsView(QWidget):
    """A widget to display and edit application settings."""

    def __init__(self, settings_manager: SettingsManager, parent: QWidget | None = None) -> None:
        """Initializes the SettingsView."""
        super().__init__(parent)
        self._settings_manager = settings_manager

        layout = QVBoxLayout(self)
        
        self._setup_ui()
        
        self.load_settings()

        save_button = QPushButton("Salvar Configurações")
        save_button.clicked.connect(self.save_settings)
        
        layout.addLayout(self._form_layout)
        layout.addStretch()
        layout.addWidget(save_button)
        self.setLayout(layout)

    def _setup_ui(self) -> None:
        """Creates and arranges the UI widgets for settings."""
        self._form_layout = QFormLayout()
        
        self.download_path_edit = QLineEdit()
        
        self.video_quality_combo = QComboBox()
        self.video_qualities = ["Mais alta", "1080p", "720p", "480p", "Mais baixa"]
        self.video_quality_combo.addItems(self.video_qualities)

        self.max_concurrent_downloads_spin = QSpinBox()
        self.max_concurrent_downloads_spin.setRange(1, 16)
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
        self.subtitle_languages = {"Português": "pt", "English": "en", "Español": "es", "Français": "fr", "Deutsch": "de"}
        for name, code in self.subtitle_languages.items():
            self.subtitle_lang_combo.addItem(name, userData=code)

        self.hardcode_subtitles_check = QCheckBox("Incorporar Legendas no Vídeo")
        self.download_embedded_check = QCheckBox("Baixar Vídeos na Descrição (recomendado)")

        self.audio_lang_combo = QComboBox()
        self.audio_languages = {
            "Português (Brasil)": "pt-BR", "English": "en", "Español": "es"
        }
        for name, code in self.audio_languages.items():
            self.audio_lang_combo.addItem(name, userData=code)

        self.keep_audio_only_check = QCheckBox("Manter Apenas Áudio")
        
        self._form_layout.addRow("Caminho para Download:", self.download_path_edit)
        self._form_layout.addRow("Qualidade do Vídeo:", self.video_quality_combo)
        self._form_layout.addRow("Máximo de Downloads Concorrentes:", self.max_concurrent_downloads_spin)
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

    def load_settings(self) -> None:
        """Loads settings from the manager and populates the UI."""
        settings = self._settings_manager.get_settings()
        self.download_path_edit.setText(settings.download_path)
        
        index = self.video_quality_combo.findText(settings.video_quality)
        if index != -1:
            self.video_quality_combo.setCurrentIndex(index)

        self.max_concurrent_downloads_spin.setValue(settings.max_concurrent_segment_downloads)
        self.timeout_spin.setValue(settings.timeout_seconds)
        self.course_name_max_spin.setValue(getattr(settings, 'max_course_name_length', 40))
        self.module_name_max_spin.setValue(getattr(settings, 'max_module_name_length', 60))
        self.lesson_name_max_spin.setValue(getattr(settings, 'max_lesson_name_length', 60))
        self.file_name_max_spin.setValue(getattr(settings, 'max_file_name_length', 30))
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
            user_agent=current_settings.user_agent,
            run_ffmpeg=current_settings.run_ffmpeg,
            ffmpeg_args=current_settings.ffmpeg_args
            ,
            max_course_name_length=self.course_name_max_spin.value(),
            max_module_name_length=self.module_name_max_spin.value(),
            max_lesson_name_length=self.lesson_name_max_spin.value(),
            max_file_name_length=self.file_name_max_spin.value()
        )
        self._settings_manager.save_settings(updated_settings)
