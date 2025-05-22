# main.py
import time
start_time = time.time()

# IMPORTANT: utils.config performs initial config loading at import time.
# Logging should be set up BEFORE utils.config is imported if we want to see its loading logs.
import logging
DEFAULT_LOG_LEVEL = logging.DEBUG # Temporary default
logger = logging.getLogger(__name__)
def setup_logging():
    logging.basicConfig(level=DEFAULT_LOG_LEVEL, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    musicbrainzngs_logger = logging.getLogger('musicbrainzngs')
    musicbrainzngs_logger.setLevel(logging.WARNING)
    urllib_logger = logging.getLogger('urllib3')
    urllib_logger.setLevel(logging.WARNING)
    pil_logger = logging.getLogger('PIL')
    pil_logger.setLevel(logging.WARNING)
    qt_logger = logging.getLogger('PySide6')
    qt_logger.setLevel(logging.INFO)
setup_logging()

from PySide6.QtWidgets import QApplication, QMessageBox
import logging.config
import sys
import multiprocessing
import traceback # For formatting exceptions
from typing import Optional, Tuple, Any, TYPE_CHECKING

from utils.config import USER_CONFIG, DEFAULT_CONFIG, USER_CONFIG_DIR, USER_CONFIG_FILE, save_user_config, get_initial_config_loading_errors
from ui.theme_manager import apply_app_theme_and_custom_styles, resolve_theme
from ui.main_window import MainWindow
from utils.config import USER_CONFIG, DEFAULT_CONFIG, USER_CONFIG_DIR, USER_CONFIG_FILE, save_user_config
from cli import process_cli_arguments

if TYPE_CHECKING:
    from services.worker import CMD_Search

# --- Global Exception Handler ---
def handle_global_exception(exc_type, exc_value, exc_traceback):
    """
    Handles unhandled exceptions, logs them, and shows an error dialog.
    """
    # Format the traceback
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    error_message_long = "".join(tb_lines)
    
    logger.critical(f"Unhandled exception caught by global handler:\n{error_message_long}")

    dialog_title = "Unhandled Application Error"
    dialog_message_short = (
        f"An unexpected error occurred: {exc_value}\n\n"
        "Please report this issue if it persists.\n"
        "Details have been logged."
    )

    # the original excepthook outputs the traceback in color
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

    app_instance = QApplication.instance()
    if app_instance:
        error_dialog = QMessageBox(None)
        error_dialog.setIcon(QMessageBox.Critical)
        error_dialog.setWindowTitle(dialog_title)
        error_dialog.setText(dialog_message_short)
        error_dialog.setDetailedText(error_message_long)
        error_dialog.setStandardButtons(QMessageBox.Ok)
        error_dialog.setDefaultButton(QMessageBox.Ok)
        error_dialog.exec()


def main():
    elapsed = time.time() - start_time
    logger.debug(f"Initial imports completed in {elapsed:.3f} seconds")
    logger.debug("Application starting...")
    multiprocessing.freeze_support()
    # _initialize_configs_and_globals() is called at module level in utils.config
    # This populates global USER_CONFIG from file initially.

    # Check for configuration loading errors after app object might be created for QMessageBox
    # We will show this message after QApplication is initialized.
    initial_config_errors = get_initial_config_loading_errors()

    # Process CLI arguments using the dedicated function
    # USER_CONFIG and DEFAULT_CONFIG are globally available from utils.config
    initial_ui_config, perform_auto_search, initial_search_payload_for_worker = \
        process_cli_arguments(USER_CONFIG, DEFAULT_CONFIG)

    # Set the global exception handler
    sys.excepthook = handle_global_exception
    logger.info("Global exception handler set.")

    initial_theme = initial_ui_config.get("theme", DEFAULT_CONFIG.get("theme", "auto")) 
    needs_dark_mode = resolve_theme(initial_theme) == "dark"

    start_args = sys.argv if not needs_dark_mode else sys.argv + ['-platform', 'windows:darkmode=2']

    app = QApplication(start_args)
    app.setApplicationVersion("0.1")

    # Show configuration load errors now that QApplication exists
    if initial_config_errors:
        error_str = "\n\n".join(initial_config_errors)
        QMessageBox.warning(None, "Configuration Load Warning",
                            f"There were issues loading configuration files:\n\n{error_str}\n\n"
                            "The application will use default settings where necessary. "
                            "You can review or correct settings in the Settings dialog or by editing the config file(s). "
                            "Corrupted files may have been backed up with a '.corrupted' extension.")

    if needs_dark_mode: app.setStyle('Fusion') # needed to make the title bar dark

    apply_app_theme_and_custom_styles(initial_theme, use_cache=True)

    logger.info("Showing main window")
    main_window = MainWindow(initial_ui_config_from_cli=initial_ui_config,
                             initial_search_payload_for_worker=initial_search_payload_for_worker)
    main_window.show()

    logger.info("Starting PySide6 event loop...")
    exit_code = app.exec()

    logger.info(f"Application finished with exit code {exit_code}.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
