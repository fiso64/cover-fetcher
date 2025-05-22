# ui/theme_manager.py
import logging
import pathlib
import sys
import pathlib
import platform as pf
from PySide6.QtWidgets import QApplication
from PySide6 import __version__ as PySide6_VERSION

# for storing qss theme cache
CACHE_DIR = pathlib.Path.home() / ".cache" / "cover_fetcher"

logger = logging.getLogger(__name__)

BASE_CUSTOM_QSS_OVERRIDES = """
        ScrollableImageRow { 
            border: none !important;
            background: transparent !important;
        }
        /* Target the viewport of ScrollableImageRow specifically */
        ScrollableImageRow > QWidget { /* This is the content_widget of ScrollableImageRow */
            border: none !important;
            background: transparent !important; 
        }

        QScrollArea#MainResultsScrollArea {
            border: none !important;
            background: transparent !important;
        }
        /* Target the viewport of MainResultsScrollArea specifically */
        QScrollArea#MainResultsScrollArea > QWidget { /* This is scrollable_results_widget */
            border: none !important;
            background: transparent !important;
        }

        QWidget#ImageFrame {
            background: transparent !important; /* ImageFrame itself should be transparent */
            border: none !important;
            padding: 0px !important;
            margin: 0px !important;
        }
    """

DARK_MODE_ADDITIONAL_QSS = """
        QToolTip {
            background-color: rgb(53, 53, 53); /* Dark gray background for dark themes */
            color: rgb(221, 221, 221);       /* Light gray text for dark themes */
            border: 1px solid rgb(85, 85, 85); /* Slightly lighter border for dark themes */
        }
    """

def resolve_theme(theme_name: str):
    if theme_name == "auto":
        import darkdetect
        theme_name = "dark" if darkdetect.isDark() else "light"
    return theme_name

# --- Theme Caching Helper Functions ---

_THEME_CACHE_FILE_VERSION = "v1" # Increment if cache structure/contents change fundamentally

def _get_cache_path(app_version: str, resolved_theme_name: str) -> pathlib.Path:
    """Generates the file path for the theme cache."""
    pyside_version = PySide6_VERSION
    os_platform = sys.platform
    
    # Sanitize parts of the filename, e.g., replace dots in versions
    app_version_sanitized = app_version.replace(".", "_")
    pyside_version_sanitized = pyside_version.replace(".", "_")
    
    cache_filename = (
        f"theme_cache_{_THEME_CACHE_FILE_VERSION}_{app_version_sanitized}_"
        f"pyside{pyside_version_sanitized}_{os_platform}_{resolved_theme_name}.qss"
    )
    return CACHE_DIR / cache_filename

def _load_qss_from_cache(app_version: str, resolved_theme_name: str) -> str | None:
    """Loads QSS from cache if available and valid."""
    cache_file = _get_cache_path(app_version, resolved_theme_name)
    if cache_file.exists():
        try:
            logger.debug(f"Reading QSS from cache: {cache_file}")
            return cache_file.read_text(encoding='utf-8')
        except Exception as e:
            logger.warning(f"Failed to read QSS cache file {cache_file}: {e}")
            # Attempt to delete corrupted cache file
            try:
                cache_file.unlink(missing_ok=True)
            except OSError as del_e:
                logger.error(f"Failed to delete corrupted cache file {cache_file}: {del_e}")
            return None
    logger.debug(f"QSS cache file not found: {cache_file}")
    return None

def _save_qss_to_cache(app_version: str, resolved_theme_name: str, qss_content: str):
    """Saves QSS content to a cache file."""
    cache_file = _get_cache_path(app_version, resolved_theme_name)
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(qss_content, encoding='utf-8')
        logger.debug(f"Saved QSS to cache: {cache_file}")
    except Exception as e:
        logger.error(f"Failed to save QSS to cache file {cache_file}: {e}")

# --- Main Theme Application Function ---

import time

def apply_app_theme_and_custom_styles(theme_name: str, use_cache=False):
    """
    Applies the specified theme and custom QSS overrides to the application.
    If use_cache=True, uses a caching mechanism to avoid re-generating QSS and importing
    qdarktheme if a valid cache exists.
    """
    start_time = time.time()
    app = QApplication.instance()
    if app is None:
        logger.error("QApplication instance not found during theme application. Cannot set theme.")
        return

    app_version = app.applicationVersion()
    resolved_theme_name = resolve_theme(theme_name)
    logger.info(f"Applying theme: {resolved_theme_name}")

    custom_css_for_setup = BASE_CUSTOM_QSS_OVERRIDES
    if resolved_theme_name == "dark":
        custom_css_for_setup += DARK_MODE_ADDITIONAL_QSS

    if use_cache:
        cached_qss = _load_qss_from_cache(app_version, resolved_theme_name)
        if cached_qss:
            logger.info(f"Applying cached theme QSS for '{resolved_theme_name}'.")
            app.setStyleSheet(cached_qss + custom_css_for_setup)
            duration = time.time() - start_time
            logger.debug(f"Theme application took {duration:.3f} seconds")
            return
        else: logger.info(f"No valid cache found for '{resolved_theme_name}'. Generating and caching QSS.")

    try:
        import qdarktheme # Heavy import, avoided if cache hits
    except ImportError:
        logger.error("qdarktheme module not found. Cannot apply dynamic theme.")
        return
    
    qss = qdarktheme.load_stylesheet(resolved_theme_name)        
    
    if qss:
        app.setStyleSheet(qss + custom_css_for_setup)
        logger.info(f"Theme '{resolved_theme_name}' applied.")
        if (use_cache): 
            _save_qss_to_cache(app_version, resolved_theme_name, qss)
            logger.info(f"Theme '{resolved_theme_name}' QSS cached.")
    else:
        logger.warning(f"Stylesheet was empty after qdarktheme.load_stylesheet for '{resolved_theme_name}'. Cache not saved.")
    
    duration = time.time() - start_time
    logger.debug(f"Theme application took {duration:.3f} seconds")

