import sys
import pathlib
import logging
import sys
import os

DEFAULT_LOG_LEVEL = logging.DEBUG # Temporary default
logger = logging.getLogger(__name__)

def setup_logging():
    log_file_path = None
    try:
        argv_list = sys.argv[1:]
        if "--log-file" in argv_list:
            idx = argv_list.index("--log-file")
            if idx + 1 < len(argv_list):
                potential_path = argv_list[idx+1]
                # Basic check: ensure it's not another flag
                if not potential_path.startswith("-"):
                    log_file_path = potential_path
                else:
                    print(f"WARNING: --log-file provided but the next argument '{potential_path}' looks like another flag. Ignoring --log-file.", file=sys.stderr)
            else:
                print(f"WARNING: --log-file provided without a path. Ignoring.", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: Error during --log-file pre-parsing: {e}. Defaulting to console logging.", file=sys.stderr)

    # BasicConfig sets up a StreamHandler to stderr by default.
    # We create a basic formatter first, to be used by both handlers.
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Configure the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(DEFAULT_LOG_LEVEL)
    root_logger.handlers.clear() # Clear any existing handlers (e.g., from basicConfig if called elsewhere)

    # Console Handler (always add)
    console_handler = logging.StreamHandler(sys.stderr) # Log to stderr
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(DEFAULT_LOG_LEVEL) # Console can have its own level if desired
    root_logger.addHandler(console_handler)

    if log_file_path:
        try:
            # Ensure the directory for the log file exists
            abs_log_file_path = os.path.abspath(log_file_path)
            log_dir = os.path.dirname(abs_log_file_path)
            if log_dir: # if log_file_path is just a filename, log_dir will be empty string, path will be CWD
                os.makedirs(log_dir, exist_ok=True)

            file_handler = logging.FileHandler(abs_log_file_path, mode='a', encoding='utf-8')
            file_handler.setFormatter(log_formatter)
            file_handler.setLevel(DEFAULT_LOG_LEVEL) # File can have its own level
            root_logger.addHandler(file_handler)
            # Cannot use logger.info here yet if this is the very first log message
            print(f"INFO: Logging to file: {abs_log_file_path}", file=sys.stderr)
        except Exception as e:
            # If file logging setup fails, console logging will still work.
            # Print an error directly to stderr.
            print(f"ERROR: Error setting up log file {log_file_path}: {e}. Logging to console only.", file=sys.stderr)
            # If root_logger is already partially configured, an error message can be logged.
            # However, logger instance from getLogger(__name__) might not be fully set up here.
            # logging.error(f"Failed to set up file logging to {log_file_path}", exc_info=True) # This might be too early

    # Set levels for other verbose loggers
    musicbrainzngs_logger = logging.getLogger('musicbrainzngs')
    musicbrainzngs_logger.setLevel(logging.WARNING)
    urllib_logger = logging.getLogger('urllib3')
    urllib_logger.setLevel(logging.WARNING)
    pil_logger = logging.getLogger('PIL')
    pil_logger.setLevel(logging.WARNING)
    qt_logger = logging.getLogger('PySide6')
    qt_logger.setLevel(logging.INFO)

def get_bundle_dir() -> pathlib.Path:
    """
    Returns the base directory for the application.
    For bundled apps, it's the executable's dir or _MEIPASS.
    For development, it's the project root (assumed to be parent of 'utils' and 'assets').
    """
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            return pathlib.Path(sys._MEIPASS) # one-file
        else:
            return pathlib.Path(sys.executable).parent # standalone
    else:
        # Development mode:
        # __file__ in utils/helpers.py -> project_root/utils/helpers.py
        # .parent -> project_root/utils
        # .parent.parent -> project_root
        return pathlib.Path(__file__).resolve().parent.parent