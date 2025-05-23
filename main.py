# main.py
import time
start_time = time.time()
# Logging should be set up BEFORE utils.config is imported if we want to see its loading logs.
from utils.helpers import setup_logging
setup_logging()

from PySide6.QtWidgets import QApplication, QMessageBox
import logging.config
import sys
import multiprocessing
import traceback # For formatting exceptions
from typing import Optional, Tuple, Any, TYPE_CHECKING

from utils.config import USER_CONFIG, DEFAULT_CONFIG, USER_CONFIG_DIR, USER_CONFIG_FILE, save_user_config, get_initial_config_loading_errors
from ui.theme_manager import apply_app_theme_and_custom_styles, resolve_theme, apply_theme_tweaks_windows
from ui.main_window import MainWindow
from utils.config import USER_CONFIG, DEFAULT_CONFIG, USER_CONFIG_DIR, USER_CONFIG_FILE, save_user_config
from cli import process_cli_arguments

if TYPE_CHECKING:
    from services.worker import CMD_Search

logger = logging.getLogger(__name__)

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

    is_console_mode = False

    if sys.stdout and sys.stdout.isatty():
        is_console_mode = True
        logger.info("Console mode detected (stdout is a TTY).")
    else:
        logger.info("GUI mode detected (stdout is not a TTY).")

    # Process CLI arguments using the dedicated function
    # USER_CONFIG and DEFAULT_CONFIG are globally available from utils.config
    initial_ui_config, perform_auto_search, initial_search_payload_for_worker, \
    cli_error_message, help_arg_definitions = \
        process_cli_arguments(USER_CONFIG, DEFAULT_CONFIG, is_console_mode)

    # Handle CLI parsing results: error, help, or proceed
    if cli_error_message:
        if not is_console_mode:
            # Need QApplication to show QMessageBox
            # Minimal app setup for error dialog
            pre_app = QApplication.instance()
            if not pre_app: pre_app = QApplication(sys.argv)
            QMessageBox.critical(None, "Argument Error", cli_error_message)
        # In console mode, error was already printed and exited by CustomArgumentParser
        sys.exit(1)

    if help_arg_definitions:
        if not is_console_mode:
            # Need QApplication for HelpDialog
            pre_app = QApplication.instance()
            if not pre_app: pre_app = QApplication(sys.argv)
            initial_theme = USER_CONFIG.get("theme", DEFAULT_CONFIG.get("theme", "auto")) 
            apply_app_theme_and_custom_styles(initial_theme, use_cache=True)
            # Import HelpDialog locally to avoid circular dependencies or premature Qt imports
            from ui.help_dialog import HelpDialog 
            dialog = HelpDialog(help_arg_definitions)
            dialog.exec()
        # In console mode, help was already printed and exited by CustomArgumentParser
        sys.exit(0)

    # If initial_ui_config is None here, it means CLI processing signaled an exit handled above.
    # This check is mostly a safeguard; previous blocks should have sys.exit().
    if initial_ui_config is None:
        logger.error("initial_ui_config is None after CLI processing without error/help signal. Exiting.")
        sys.exit(1)


    # Check for configuration loading errors after app object might be created for QMessageBox
    # We will show this message after QApplication is initialized.
    initial_config_errors = get_initial_config_loading_errors()

    # Set the global exception handler
    sys.excepthook = handle_global_exception
    logger.info("Global exception handler set.")

    app = QApplication(sys.argv)
    app.setApplicationVersion("0.1")

    # Show configuration load errors now that QApplication exists
    if initial_config_errors:
        error_str = "\n\n".join(initial_config_errors)
        QMessageBox.warning(None, "Configuration Load Warning",
                            f"There were issues loading configuration files:\n\n{error_str}\n\n"
                            "The application will use default settings where necessary. "
                            "You can review or correct settings in the Settings dialog or by editing the config file(s). "
                            "Corrupted files may have been backed up with a '.corrupted' extension.")

    initial_theme = initial_ui_config.get("theme", DEFAULT_CONFIG.get("theme", "auto")) 
    initial_theme = resolve_theme(initial_theme)
    apply_app_theme_and_custom_styles(initial_theme, use_cache=True)

    logger.info("Showing main window")
    main_window = MainWindow(initial_ui_config_from_cli=initial_ui_config,
                             initial_search_payload_for_worker=initial_search_payload_for_worker)

    if sys.platform == "win32":
        apply_theme_tweaks_windows(main_window, initial_theme)

    main_window.show()

    logger.info("Starting PySide6 event loop...")
    exit_code = app.exec()

    logger.info(f"Application finished with exit code {exit_code}.")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
