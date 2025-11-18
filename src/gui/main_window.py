import json
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QMainWindow, QWidget, QStackedWidget, QTabWidget, QMessageBox
import logging

from src.config.settings_manager import SettingsManager
from src.gui.views.auth_view import AuthView
from src.gui.views.course_selection_view import CourseSelectionView
from src.gui.views.module_selection_view import ModuleSelectionView
from src.gui.views.progress_view import ProgressView
from src.gui.views.settings_view import SettingsView
from src.platforms.base import PlatformFactory, BasePlatform
from src.app.workers import FetchCoursesWorker, FetchModulesWorker, DownloadWorker

class MainWindow(QMainWindow):
    """Main application window that holds all UI views."""

    def __init__(self, settings_manager: SettingsManager, parent: QWidget | None = None) -> None:
        """Initializes the main window."""
        super().__init__(parent)
        self.setWindowTitle("Katomart! Visite o Repositório em https://github.com/katomaro/katomart")
        self.setMinimumSize(600, 400)

        self._settings_manager = settings_manager
        self._platform: BasePlatform | None = None
        self._selected_courses: list = []
        self._thread_pool = QThreadPool()

        self._stacked_widget = QStackedWidget()
        self.setCentralWidget(self._stacked_widget)

        self._setup_views()
        self._connect_signals()
        QTimer.singleShot(0, self._show_subscription_prompt)

    def _setup_views(self) -> None:
        """Creates and adds all views to the stacked widget."""
        self.auth_tab_widget = QTabWidget()
        self.auth_view = AuthView(self._settings_manager)
        self.settings_view = SettingsView(self._settings_manager)
        self.auth_tab_widget.addTab(self.auth_view, "Authenticação")
        self.auth_tab_widget.addTab(self.settings_view, "Configurações")

        self.course_selection_view = CourseSelectionView()
        self.module_selection_view = ModuleSelectionView()
        self.progress_view = ProgressView()

        self._stacked_widget.addWidget(self.auth_tab_widget)
        self._stacked_widget.addWidget(self.course_selection_view)
        self._stacked_widget.addWidget(self.module_selection_view)
        self._stacked_widget.addWidget(self.progress_view)

    def _show_subscription_prompt(self) -> None:
        """Displays a pop-up encouraging the monthly subscription."""
        message = (
            "Simplifique o uso do Katomart e desbloqueie recursos extras com uma assinatura "
            "mensal de apenas R$5.\n\nVisite https://katomaro.com e conhe\u00e7a os benefícios."
        )
        QMessageBox.information(self, "Assinatura Premium", message)

    def _connect_signals(self) -> None:
        """Connects signals between views and workers."""
        self.auth_view.list_products_requested.connect(self._fetch_courses)
        self.course_selection_view.courses_selected.connect(self._fetch_modules)
        self.module_selection_view.download_requested.connect(self._start_download)
        self.settings_view.membership_updated.connect(self.auth_view.refresh_membership_state)

    def _handle_worker_error(self, error_tuple: tuple) -> None:
        """Logs errors from worker threads."""
        exctype, value, tb = error_tuple
        logging.error(f"An error occurred in a worker thread: {value}", exc_info=(exctype, value, tb))

    def _fetch_courses(self, platform_name: str, credentials: dict) -> None:
        """Starts a worker to fetch courses."""
        self._platform = PlatformFactory.create_platform(platform_name, self._settings_manager)
        if not self._platform:
            logging.error(f"Platform '{platform_name}' not found.")
            return

        self.auth_view.list_products_button.setEnabled(False)
        self.auth_view.list_products_button.setText("Authenticando e buscando cursos...")
        
        worker = FetchCoursesWorker(self._platform, credentials)
        worker.signals.result.connect(self._on_courses_fetched)
        worker.signals.error.connect(self._handle_worker_error)
        worker.signals.finished.connect(lambda: self.auth_view.list_products_button.setEnabled(True))
        worker.signals.finished.connect(lambda: self.auth_view.list_products_button.setText("Listar Produtos da Conta"))
        self._thread_pool.start(worker)

    def _on_courses_fetched(self, courses_json: str) -> None:
        """Handles the result from the FetchCoursesWorker."""
        courses = json.loads(courses_json)
        logging.info(f"Courses fetched: {len(courses)} items")
        self.course_selection_view.update_courses(courses)
        self._stacked_widget.setCurrentWidget(self.course_selection_view)

    def _fetch_modules(self, courses: list) -> None:
        """Starts a worker to fetch modules for selected courses."""
        if not self._platform:
            return

        self._selected_courses = courses
        self.course_selection_view.next_button.setEnabled(False)
        worker = FetchModulesWorker(self._platform, courses)
        worker.signals.result.connect(self._on_modules_fetched)
        worker.signals.error.connect(self._handle_worker_error)
        worker.signals.finished.connect(lambda: self.course_selection_view.next_button.setEnabled(True))
        self._thread_pool.start(worker)

    def _on_modules_fetched(self, content_json: str) -> None:
        """Handles the result from the FetchModulesWorker."""
        content = json.loads(content_json)
        logging.info(f"Module content fetched for {len(content)} course(s)")
        self.module_selection_view.update_modules(content, self._selected_courses)
        self._stacked_widget.setCurrentWidget(self.module_selection_view)

    def _start_download(self, selection_json: str) -> None:
        """Starts a worker to download the selected content."""
        if not self._platform:
            return

        selection = json.loads(selection_json)
            
        self._stacked_widget.setCurrentWidget(self.progress_view)
        self.progress_view.log_message("Iniciando download...")
        
        download_dir = self._settings_manager.get_settings().download_path
        worker = DownloadWorker(self._platform, selection, download_dir, self._settings_manager)
        worker.signals.progress.connect(self.progress_view.set_progress)
        worker.signals.result.connect(self.progress_view.log_message)
        worker.signals.error.connect(self._handle_worker_error)
        worker.signals.finished.connect(lambda: self.progress_view.log_message("Worker finished."))
        self._thread_pool.start(worker)
