import sys
import logging
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
    """Installs Playwright browsers."""
    logging.info("Installing Playwright browsers...")
    try:
        original_argv = sys.argv.copy()

        sys.argv = ["playwright", "install", "chromium"]
        if sys.stdout is None:
            sys.stdout = open(os.devnull, 'w')
        if sys.stderr is None:
            sys.stderr = open(os.devnull, 'w')

        from playwright.__main__ import main as playwright_main
        try:
            playwright_main()
        except SystemExit:
            pass

        logging.info("Playwright browsers installed successfully.")

        sys.argv = original_argv
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
    
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = logs_dir / f"{timestamp}.log"

    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    logging.getLogger().addHandler(file_handler)

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
