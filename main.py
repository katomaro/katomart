import sys
import logging
from pathlib import Path
import importlib
import pkgutil
from datetime import datetime

from PySide6.QtWidgets import QApplication

from src.config.settings_manager import SettingsManager
from src.gui.main_window import MainWindow
import src.platforms

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

    load_platforms()

    app = QApplication(sys.argv)

    settings_path = Path("settings.json")
    settings_manager = SettingsManager(settings_path)

    main_window = MainWindow(settings_manager)
    main_window.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
