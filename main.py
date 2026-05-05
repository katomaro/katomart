import sys
import io
import logging
import subprocess
from pathlib import Path
import importlib
import pkgutil
from datetime import datetime
import os

from PySide6.QtWidgets import QApplication

from src.config.settings_manager import SettingsManager
from src.gui.main_window import MainWindow
import src.platforms


def install_playwright_browsers():
    """Installs Playwright Chromium browser using the bundled driver directly."""
    logging.info("Installing Playwright browsers...")
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env

        node_exe, cli_js = compute_driver_executable()
        env = get_driver_env()

        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": env,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        result = subprocess.run([node_exe, cli_js, "install", "chromium"], **kwargs)

        if result.returncode == 0:
            logging.info("Playwright browsers installed successfully.")
        else:
            logging.warning(
                f"Playwright browser install exited with code {result.returncode}."
            )
    except Exception as e:
        logging.error(f"Failed to install Playwright browsers: {e}")

def load_platforms() -> None:
    """Dynamically imports all modules in the 'platforms' package."""
    logging.info("Loading platforms...")
    platform_pkg = src.platforms
    for _, name, _ in pkgutil.iter_modules(platform_pkg.__path__, platform_pkg.__name__ + "."):
        if "base" not in name:
            importlib.import_module(name)
            logging.debug(f"Successfully loaded platform module: {name}")

def main() -> None:
    """Initializes and runs the application."""

    # In frozen (PyInstaller) mode, set CWD to the exe's directory so that
    # relative paths (logs/, settings.json, credentials.json, downloads/) resolve correctly.
    if getattr(sys, 'frozen', False):
        os.chdir(Path(sys.executable).parent)

    # Force Playwright to use the shared global browser cache (%LOCALAPPDATA%/ms-playwright)
    # instead of .local-browsers relative to the bundled driver package.
    # This must be set before any Playwright import or subprocess call.
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ms-playwright"
        )

    # Ensure stdout/stderr exist — in --windowed mode they are None,
    # which crashes logging and any code that writes to them.
    _has_console = sys.stdout is not None
    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()

    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = logs_dir / f"{timestamp}.log"

    # Set up logging — only add a console handler when a real console is present.
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    if _has_console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    # Ensure Playwright browsers are installed
    install_playwright_browsers()

    load_platforms()

    app = QApplication(sys.argv)

    settings_path = Path("settings.json")
    settings_manager = SettingsManager(settings_path)

    main_window = MainWindow(settings_manager)
    main_window.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
