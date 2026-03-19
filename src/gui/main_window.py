import json
import logging
from pathlib import Path
from typing import Any
from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QMainWindow, QWidget, QStackedWidget, QTabWidget, QMessageBox, QPushButton, QInputDialog, QLineEdit

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
        self._current_worker: DownloadWorker | None = None
        self._retry_selection_json: str | None = None
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
        self.progress_view.pause_requested.connect(self._on_pause_download)
        self.progress_view.resume_requested.connect(self._on_resume_download)
        self.progress_view.save_progress_requested.connect(self._on_save_progress)
        self.progress_view.cancel_requested.connect(self._on_cancel_download)
        self.progress_view.reauth_requested.connect(self._on_reauth_requested)
        self.progress_view.retry_requested.connect(self._on_retry_requested)

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
        """Searches for courses on the platform using the existing authenticated session."""
        if not self._platform or not self._platform.credentials:
            QMessageBox.warning(self, "Erro", "Não foi possível pesquisar: Credenciais não encontradas.")
            return

        self.course_selection_view.search_button.setEnabled(False)
        self.course_selection_view.search_button.setText("Pesquisando...")

        worker = FetchCoursesWorker(self._platform, self._platform.credentials, query=query)
        worker.signals.result.connect(self._on_search_results)
        worker.signals.error.connect(self._handle_worker_error)
        worker.signals.finished.connect(lambda: self.course_selection_view.search_button.setEnabled(True))
        worker.signals.finished.connect(lambda: self.course_selection_view.search_button.setText("Pesquisar"))
        self._thread_pool.start(worker)

    def _on_search_results(self, courses_json: str) -> None:
        """Handles search results without changing views."""
        courses = json.loads(courses_json)
        logging.info(f"Search results: {len(courses)} items")
        self.course_selection_view.update_courses(courses)

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

        platform_class = PlatformFactory.get_platform_class(self._platform_name or "")
        requires_search = getattr(platform_class, "requires_search", lambda: False)()
        self.course_selection_view.set_requires_search(requires_search)

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

        self.progress_view.reset()
        self._stacked_widget.setCurrentWidget(self.progress_view)
        self.progress_view.set_download_active(True)
        if resume_state:
            self.progress_view.log_message("Retomando downloads da sessao anterior...")
        else:
            self.progress_view.log_message("Iniciando download...")

        settings = self._settings_manager.get_settings()
        self.progress_view.set_premium(settings.has_full_permissions)
        download_dir = settings.download_path
        worker = DownloadWorker(
            self._platform,
            selection,
            download_dir,
            self._settings_manager,
            self._platform_name or "",
            self._selected_courses,
            resume_state,
        )
        if self._current_worker:
            self._disconnect_worker(self._current_worker)
        self._current_worker = worker
        worker.signals.progress.connect(self.progress_view.set_progress)
        worker.signals.result.connect(self.progress_view.log_message)
        worker.signals.error.connect(self._handle_worker_error)
        worker.signals.request_auth_confirmation.connect(self._handle_auth_confirmation_request)
        worker.signals.lesson_status.connect(self.progress_view.update_lesson_status)
        worker.signals.auto_paused.connect(self._on_auto_paused)
        worker.signals.retry_selection.connect(self._on_retry_selection_received)
        worker.signals.finished.connect(self._on_download_finished)
        self._thread_pool.start(worker)

    def _handle_auth_confirmation_request(self, confirmation_event: Any) -> None:
        """Handles a request from the worker to confirm manual authentication."""
        QMessageBox.information(
            self,
            "Re-autenticacao Necessaria",
            "A sessao expirou e o sistema esta tentando re-autenticar.\n"
            "Uma janela do navegador foi aberta (ou sera aberta).\n"
            "Por favor, realize o login/captcha manualmente no navegador e clique em OK aqui quando terminar."
        )
        if confirmation_event:
            confirmation_event.set()

    def _disconnect_worker(self, worker: DownloadWorker) -> None:
        """Disconnect all signals from a worker to prevent stale callbacks."""
        for sig, slot in [
            (worker.signals.progress, self.progress_view.set_progress),
            (worker.signals.result, self.progress_view.log_message),
            (worker.signals.error, self._handle_worker_error),
            (worker.signals.lesson_status, self.progress_view.update_lesson_status),
            (worker.signals.auto_paused, self._on_auto_paused),
            (worker.signals.retry_selection, self._on_retry_selection_received),
            (worker.signals.finished, self._on_download_finished),
            (worker.signals.request_auth_confirmation, self._handle_auth_confirmation_request),
        ]:
            try:
                sig.disconnect(slot)
            except RuntimeError:
                pass

    def _on_download_finished(self) -> None:
        """Called when the download worker finishes (success or cancel)."""
        self._current_worker = None
        self.progress_view.set_download_active(False)
        self.progress_view.log_message("Worker finalizado.")

    def _on_auto_paused(self) -> None:
        """Called when the worker auto-pauses due to error/partial thresholds."""
        self.progress_view._paused = True
        self.progress_view.pause_button.setText("Retomar")

    def _on_pause_download(self) -> None:
        if self._current_worker:
            self._current_worker.pause()
            self.progress_view.log_message("Download pausado.")

    def _on_resume_download(self) -> None:
        if self._current_worker:
            self._current_worker.resume()
            self.progress_view.log_message("Download retomado.")

    def _on_save_progress(self) -> None:
        if self._current_worker:
            self._current_worker.request_save_progress()

    def _on_cancel_download(self) -> None:
        if self._current_worker:
            reply = QMessageBox.question(
                self,
                "Cancelar Download",
                "Deseja cancelar o download?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                worker = self._current_worker
                self._current_worker = None
                self._disconnect_worker(worker)
                worker.cancel()
                self._stacked_widget.setCurrentWidget(self.auth_tab_widget)
        else:
            self._stacked_widget.setCurrentWidget(self.auth_tab_widget)

    def _on_reauth_requested(self) -> None:
        """Re-authenticate on the platform. If token-only, ask for a new token."""
        if not self._current_worker or not self._platform:
            return

        was_paused = not self._current_worker._pause_event.is_set()
        if not was_paused:
            self._current_worker.pause()
            self.progress_view.log_message("Download pausado para reautenticacao...")

        creds = self._platform.credentials
        is_token_only = bool(creds.get("token")) and not creds.get("username")

        if is_token_only:
            new_token, ok = QInputDialog.getText(
                self,
                "Novo Token",
                "Digite o novo token de acesso:",
                QLineEdit.EchoMode.Normal,
                creds.get("token", ""),
            )
            if not ok or not new_token.strip():
                self.progress_view.log_message("Reautenticacao cancelada pelo usuario.")
                if not was_paused:
                    self._current_worker.resume()
                return
            creds["token"] = new_token.strip()

        try:
            self._platform.authenticate(creds)
            self.progress_view.log_message("Reautenticacao realizada com sucesso.")
        except Exception as e:
            logging.error(f"Falha na reautenticacao: {e}", exc_info=True)
            QMessageBox.critical(
                self, "Erro na reautenticacao", f"Falha ao reautenticar: {e}"
            )

        if not was_paused:
            self._current_worker.resume()

    def _on_retry_selection_received(self, selection_json: str) -> None:
        """Stores the retry selection and enables the retry button."""
        self._retry_selection_json = selection_json
        if not self.progress_view._is_premium:
            return
        selection = json.loads(selection_json)
        has_retryable = any(
            any(
                lesson.get("download", False)
                for module in course.get("modules", [])
                for lesson in module.get("lessons", [])
            )
            for course in selection.get("courses", [])
        )
        self.progress_view.retry_button.setEnabled(has_retryable)

    def _on_retry_requested(self) -> None:
        """Starts a new download with only the failed/partial lessons."""
        if not self._retry_selection_json:
            return
        self._start_download(self._retry_selection_json)
