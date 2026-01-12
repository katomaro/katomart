import json
import logging
from pathlib import Path
from typing import Any
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QMainWindow, QWidget, QStackedWidget, QTabWidget, QMessageBox

from src.config.settings_manager import SettingsManager
from src.config.version import BUILD_NUMBER, VERSION_FILE_URL
from src.gui.views.auth_view import AuthView
from src.gui.views.course_selection_view import CourseSelectionView
from src.gui.views.module_selection_view import ModuleSelectionView
from src.gui.views.progress_view import ProgressView
from src.gui.views.settings_view import SettingsView
from src.platforms.base import PlatformFactory, BasePlatform
from src.app.workers import FetchCoursesWorker, FetchModulesWorker, DownloadWorker
from src.app.version_checker import VersionCheckWorker
from src.utils.resume_manager import ResumeManager

class MainWindow(QMainWindow):
    """Main application window that holds all UI views."""

    def __init__(self, settings_manager: SettingsManager, parent: QWidget | None = None) -> None:
        """Initializes the main window."""
        super().__init__(parent)
        self.setWindowTitle("Katomart! Visite o Repositório em github.com/katomaro/katomart ou assine em katomaro.com/store/katomart")
        self.setMinimumSize(650, 450)
        self.resize(750, 650)

        self._settings_manager = settings_manager
        self._platform: BasePlatform | None = None
        self._platform_name: str | None = None
        self._selected_courses: list = []
        self._resume_state: dict | None = None
        self._thread_pool = QThreadPool()

        self._stacked_widget = QStackedWidget()
        self.setCentralWidget(self._stacked_widget)

        self._setup_views()
        self._connect_signals()
        QTimer.singleShot(0, self._show_subscription_prompt)
        QTimer.singleShot(0, self._start_version_check)

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
        settings = self._settings_manager.get_settings()
        if settings.membership_email:
            return

        message = (
            "Manter um Software desse nível \u00e9 um trabalho extensivo e custoso, que exige muito tempo e estudo, além de acessos legítimos em plataformas. "
            "Com uma assinatura mensal de apenas R$9.90, você pode ajudar a manter o Katomart! ativo e em constante melhoria, além de desbloquear downloads mais rápido e funções extras poderosas.\n\n"
            "Visite https://katomaro.com/store/katomart e conhe\u00e7a os benefícios. Uma janela no seu navegador foi aberta para você verificar."
        )
        QMessageBox.information(self, "Suporte o Katomart!", message)

    def _connect_signals(self) -> None:
        """Connects signals between views and workers."""
        self.auth_view.list_products_requested.connect(self._fetch_courses)
        self.course_selection_view.courses_selected.connect(self._fetch_modules)
        self.module_selection_view.download_requested.connect(self._start_download)
        self.settings_view.membership_updated.connect(self.auth_view.refresh_membership_state)
        self.course_selection_view.search_requested.connect(self._search_courses)

    def _start_version_check(self) -> None:
        """Starts an asynchronous check for the latest build on GitHub."""
        worker = VersionCheckWorker(VERSION_FILE_URL)
        worker.signals.success.connect(self._handle_remote_build)
        worker.signals.failure.connect(self._handle_version_check_failure)
        self._thread_pool.start(worker)

    def _handle_remote_build(self, remote_build: int) -> None:
        """Compares remote and local builds and informs the user if outdated."""
        if remote_build != BUILD_NUMBER:
            message = (
                "Uma nova atualização está disponível no GitHub. "
                "Baixe a versão mais recente em https://github.com/katomaro/katomart."
            )
            QMessageBox.information(self, "Atualização disponível", message)

    def _handle_version_check_failure(self, error_message: str) -> None:
        """Notifies the user when the version check could not be completed."""
        logging.warning("Falha ao verificar atualização: %s", error_message)
        message = (
            "Não foi possível verificar atualizações porque o GitHub pode estar inacessível. "
            "As atualizações automáticas não puderam ser verificadas; verifique manualmente o repositório em "
            "https://github.com/katomaro/katomart."
        )
        QMessageBox.warning(self, "Verificação de atualização indisponível", message)

    def _handle_worker_error(self, error_tuple: tuple) -> None:
        """Logs errors from worker threads and notifies the user."""
        exctype, value, tb = error_tuple

        if isinstance(tb, str):
            logging.error(
                "An error occurred in a worker thread: %s\n%s", value, tb
            )
        else:
            logging.error(
                f"An error occurred in a worker thread: {value}",
                exc_info=(exctype, value, tb),
            )

        message = str(value) or "Ocorreu um erro inesperado."
        if exctype is ValueError and self._stacked_widget.currentWidget() is self.auth_tab_widget:
            self.auth_view.reset_auth_inputs()
            QMessageBox.warning(
                self,
                "Falha na autenticação",
                f"{message}\n\nVerifique as credenciais e tente novamente.",
            )
            return

        QMessageBox.critical(self, "Erro na execução", f"Ocorreu um erro: {message}")

    def _search_courses(self, query: str) -> None:
        """Searches for courses on the platform."""
        if self._platform and self._platform.credentials:
             self._fetch_courses(self._platform_name, self._platform.credentials, query=query)
        else:
             QMessageBox.warning(self, "Erro", "Não foi possível pesquisar: Credenciais não encontradas.")

    def _fetch_courses(self, platform_name: str, credentials: dict, query: str | None = None) -> None:
        """Starts a worker to fetch courses."""
        self._resume_state = None
        self._platform_name = platform_name

        self._platform = PlatformFactory.create_platform(platform_name, self._settings_manager)
        if not self._platform:
            logging.error(f"Platform '{platform_name}' not found.")
            return

        self.auth_view.list_products_button.setEnabled(False)
        self.auth_view.list_products_button.setText("Authenticando e buscando cursos..." if not query else "Pesquisando...")
        
        worker = FetchCoursesWorker(self._platform, credentials, query=query)
        worker.signals.result.connect(self._on_courses_fetched)
        worker.signals.error.connect(self._handle_worker_error)
        worker.signals.finished.connect(lambda: self.auth_view.list_products_button.setEnabled(True))
        worker.signals.finished.connect(lambda: self.auth_view.list_products_button.setText("Listar Produtos da Conta"))
        self._thread_pool.start(worker)

    def _on_courses_fetched(self, courses_json: str) -> None:
        """Handles the result from the FetchCoursesWorker."""
        courses = json.loads(courses_json)
        logging.info(f"Courses fetched: {len(courses)} items")

        settings = self._settings_manager.get_settings()
        if getattr(settings, "create_resume_summary", False):
            resume_manager = ResumeManager(Path(settings.download_path))
            saved_state = resume_manager.load_state(self._platform_name or "")
            if saved_state and not resume_manager.is_complete(saved_state):
                reply = QMessageBox.question(
                    self,
                    "Retomar download",
                    (
                        "Foi encontrado um resumo de download anterior para esta plataforma. "
                        "Deseja retomar a sessão inacabada?"
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._resume_state = saved_state

        if self._resume_state:
            selection = self._resume_state.get("selection")
            if selection:
                self._selected_courses = self._resume_state.get("selected_courses", [])
                self._stacked_widget.setCurrentWidget(self.progress_view)
                self.progress_view.log_message("Retomando download pendente...")
                self._start_download(json.dumps(selection), resume_state=self._resume_state)
                self._resume_state = None
                return
            self._resume_state = None

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

    def _start_download(self, selection_json: str, resume_state: dict | None = None) -> None:
        """Starts a worker to download the selected content."""
        if not self._platform:
            return

        selection = json.loads(selection_json)
            
        self._stacked_widget.setCurrentWidget(self.progress_view)
        if resume_state:
            self.progress_view.log_message("Retomando downloads da sessão anterior...")
        else:
            self.progress_view.log_message("Iniciando download...")

        download_dir = self._settings_manager.get_settings().download_path
        worker = DownloadWorker(
            self._platform,
            selection,
            download_dir,
            self._settings_manager,
            self._platform_name or "",
            self._selected_courses,
            resume_state,
        )
        worker.signals.progress.connect(self.progress_view.set_progress)
        worker.signals.result.connect(self.progress_view.log_message)
        worker.signals.error.connect(self._handle_worker_error)
        worker.signals.request_auth_confirmation.connect(self._handle_auth_confirmation_request)
        worker.signals.finished.connect(lambda: self.progress_view.log_message("Worker finished."))
        self._thread_pool.start(worker)

    def _handle_auth_confirmation_request(self, confirmation_event: Any) -> None:
        """Handles a request from the worker to confirm manual authentication."""
        QMessageBox.information(
            self,
            "Re-autenticação Necessária",
            "A sessão expirou e o sistema está tentando re-autenticar.\n"
            "Uma janela do navegador foi aberta (ou será aberta).\n"
            "Por favor, realize o login/captcha manualmente no navegador e clique em OK aqui quando terminar."
        )
        if confirmation_event:
            confirmation_event.set()
