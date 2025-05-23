# ui/main_window.py
import threading
import logging
import multiprocessing
import queue
import time
import copy
import os
import re
import sys
import pathlib
import shiboken6
from typing import Dict, List, Optional, Union, Tuple, Any, TYPE_CHECKING

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QScrollArea, QMessageBox,
    QFileDialog, QSizePolicy, QFormLayout, QDialog, QFrame, QMenu
)
from PySide6.QtCore import Qt, QTimer, Signal, Slot, QUrl, QByteArray, QSettings, QObject, QThread, QMetaObject, Q_ARG, QSize, QPoint # Added Q_ARG, QSize
from PySide6.QtGui import QKeySequence, QShortcut, QDesktopServices, QFont, QFontMetrics, QPixmap, QColor, QMouseEvent # Added QPixmap, QColor, QMouseEvent
from PySide6.QtCore import QThread, Signal

from utils.helpers import EFFECTIVE_LOG_FILE_PATH, EFFECTIVE_LOG_LEVEL
from .theme_manager import apply_app_theme_and_custom_styles, apply_theme_tweaks_windows
from .notifications import NotificationManager, DEFAULT_TIMEOUT_MS
from utils.config import get_user_downloads_folder, save_user_config, USER_CONFIG, DEFAULT_CONFIG
from services.image_fetcher import ImageFetcher
from services.models import PotentialImage, ImageResult
from .settings_dialog import SettingsDialog
from .components import (
    ServiceImageSection, ScrollableImageRow,
    DEFAULT_FONT_FAMILY, FONT_SIZE_NORMAL, FONT_SIZE_LARGE, FONT_SIZE_SMALL,
    get_font, ServiceToggleButtonBar, ImageFrame, THUMBNAIL_CORNER_RADIUS, RoundedImageDisplayWidget # Added ImageFrame, THUMBNAIL_CORNER_RADIUS, RoundedImageDisplayWidget
)
from services.worker import (
    worker_process_main,
    # Command Payloads
    CMD_Search,
    CMD_RequestMore,
    CMD_CancelSearch,
    CMD_Shutdown,
    # Event Payloads
    EVT_ServiceAlbumSearchSucceeded,
    EVT_PotentialImageFound,
    EVT_ImageResolved,
    EVT_ServiceBatchSucceeded,
    EVT_ServiceBatchCancelled,
    EVT_ServiceBatchErrored,
    EVT_ServiceError,
    EVT_WorkerReady,
    EVT_AllSearchesConcluded,
    EVT_WorkerShutdownComplete
)

logger = logging.getLogger(__name__)

DOWNLOAD_NOTIFICATION_PREFIX = "download_op_"

EVENT_QUEUE_POLL_INTERVAL_MS = 100

# Helper class for displaying current album art
class _CurrentArtWidget(RoundedImageDisplayWidget):
    openImageRequested = Signal()
    openFolderRequested = Signal()
    setPathRequested = Signal()
    closeWidgetRequested = Signal()

    def __init__(self, render_size: QSize, parent: Optional[QWidget] = None):
        super().__init__(render_size, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.openImageRequested.emit()
        super().mousePressEvent(event)

    def _show_context_menu(self, pos: QPoint):
        menu = QMenu(self)
        
        set_path_action = menu.addAction("Set Image...")
        set_path_action.triggered.connect(self.setPathRequested.emit)
        
        folder_action = menu.addAction("Open Containing Folder")
        folder_action.triggered.connect(self.openFolderRequested.emit)

        menu.addSeparator()

        close_action = menu.addAction("Close Art Display")
        close_action.triggered.connect(self.closeWidgetRequested.emit)
        
        menu.popup(self.mapToGlobal(pos))

class ImageDownloaderWorker(QObject):
    download_completed = Signal(pathlib.Path, bool, str) 

    def __init__(self, image_url: str, path: pathlib.Path, image_fetcher: ImageFetcher, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.image_url = image_url
        self.path = path
        self.image_fetcher = image_fetcher

    @Slot()
    def run(self):
        # Add a cancellation check if you implement a cancel mechanism for ImageDownloaderWorker
        # if self._is_cancelled:
        #     logger.debug(f"[ImageDownloaderWorker] Run called on cancelled worker for {self.image_url}")
        #     return

        try:
            if not self.image_url:
                self.download_completed.emit(self.path, False, "Image URL is empty.")
                return
            if not self.image_fetcher:
                self.download_completed.emit(self.path, False, "Image fetcher service not available for download.")
                return

            logger.info(f"[ImageDownloaderWorker] Requesting image data for download: {self.image_url} via ImageFetcher.")
            QMetaObject.invokeMethod(
                self.image_fetcher,
                "request_image_data",
                Qt.QueuedConnection,
                Q_ARG(str, self.image_url),
                Q_ARG(QObject, self),
                Q_ARG(str, "on_download_data_ready"),
                Q_ARG(str, "on_download_error"),
                Q_ARG(int, 120)  # Timeout for downloads, e.g., 120 seconds
            )
        except Exception as e: # Catch issues invoking the method
            logger.error(f"[ImageDownloaderWorker] Unexpected error during request_image_data invocation for ({self.image_url}): {e}", exc_info=True)
            # if not self._is_cancelled:
            self.download_completed.emit(self.path, False, f"An unexpected error occurred initiating image download: {str(e)}")

            logger.error(f"[ImageDownloaderWorker] Unexpected error during image download process: {e}", exc_info=True)
            self.download_completed.emit(self.path, False, f"An unexpected error occurred: {str(e)}")

    @Slot(QUrl, QByteArray)
    def on_download_data_ready(self, url: QUrl, image_data_qba: QByteArray):
        # Add cancellation check here if implemented
        # if self._is_cancelled: return
        if url.toString() != self.image_url:
            logger.warning(f"[ImageDownloaderWorker] Data ready for unexpected URL {url.toString()} (expected {self.image_url}). Ignoring.")
            return

        try:
            image_bytes = image_data_qba.data()
            if image_bytes:
                with open(self.path, 'wb') as f:
                    f.write(image_bytes)
                logger.info(f"[ImageDownloaderWorker] Image (fetched via ImageFetcher) saved to {self.path}")
                self.download_completed.emit(self.path, True, "")
            else:
                logger.error(f"[ImageDownloaderWorker] ImageFetcher returned no data for {self.image_url} for saving.")
                self.download_completed.emit(self.path, False, f"No image data received for {self.image_url}.")
        except IOError as e:
            logger.error(f"[ImageDownloaderWorker] File error saving image to {self.path}: {e}")
            self.download_completed.emit(self.path, False, f"File system error: {str(e)}")
        except Exception as e:
            logger.error(f"[ImageDownloaderWorker] Unexpected error writing image to {self.path}: {e}", exc_info=True)
            self.download_completed.emit(self.path, False, f"An unexpected error occurred while saving: {str(e)}")

    @Slot(QUrl, str)
    def on_download_error(self, url: QUrl, error_message: str):
        # Add cancellation check here if implemented
        # if self._is_cancelled: return
        if url.toString() != self.image_url:
            logger.warning(f"[ImageDownloaderWorker] Error for unexpected URL {url.toString()} (expected {self.image_url}): {error_message}. Ignoring.")
            return

        logger.error(f"[ImageDownloaderWorker] ImageFetcher failed to retrieve image data for {self.image_url} for saving. Error: {error_message}")
        self.download_completed.emit(self.path, False, f"Failed to download image data for {self.image_url}: {error_message}")

class MainWindow(QMainWindow):
    # pass
    def __init__(self,
                 initial_ui_config_from_cli: Dict,
                 initial_search_payload_for_worker: Optional[CMD_Search] = None, # Updated type hint
                 parent: Optional[QWidget] = None):
        super().__init__(parent)

        self.session_config = USER_CONFIG.copy()
        self.session_config.update(initial_ui_config_from_cli)

        self.initial_search_payload_for_worker = initial_search_payload_for_worker

        self.setWindowTitle("Cover Fetcher")
        self.setGeometry(0, 0, 1050, 850) 
        self.setMinimumSize(300, 300)

        self.event_handlers = {
            EVT_WorkerReady: self._handle_evt_worker_ready,
            EVT_ServiceAlbumSearchSucceeded: self._handle_evt_service_album_search_succeeded,
            EVT_PotentialImageFound: self._handle_evt_potential_image_found,
            EVT_ImageResolved: self._handle_evt_image_resolved,
            EVT_ServiceBatchSucceeded: self._handle_evt_service_batch_completed_successfully,
            EVT_ServiceBatchCancelled: self._handle_evt_service_batch_cancelled,
            EVT_ServiceBatchErrored: self._handle_evt_service_batch_error,
            EVT_AllSearchesConcluded: self._handle_evt_all_searches_concluded,
            EVT_ServiceError: self._handle_evt_service_error,
            EVT_WorkerShutdownComplete: self._handle_evt_worker_shutdown_complete,
        }

        # Calculate max width for service name labels for alignment
        _font_for_calc = get_font(FONT_SIZE_LARGE, weight=QFont.Bold)
        _fm = QFontMetrics(_font_for_calc)
        self.max_service_name_pixel_width = 0
        # Use DEFAULT_CONFIG["services"] as it contains all potential service names
        for service_name, _ in DEFAULT_CONFIG.get("services", []):
            width = _fm.horizontalAdvance(service_name)
            if width > self.max_service_name_pixel_width:
                self.max_service_name_pixel_width = width
        self.max_service_name_pixel_width += 12 # Add a small padding
        
        screen_geo = QApplication.primaryScreen().geometry()
        self.move(screen_geo.center() - self.rect().center())

        self.service_sections: Dict[str, ServiceImageSection] = {}

        self.command_queue: Optional[multiprocessing.Queue] = None
        self.event_queue: Optional[multiprocessing.Queue] = None
        self.worker_process: Optional[multiprocessing.Process] = None
        self.worker_pid: Optional[int] = None

        _services_list = self.session_config.get("services", DEFAULT_CONFIG["services"])
        existing_service_names = {name for name, enabled in _services_list}
        for default_name, default_enabled in DEFAULT_CONFIG["services"]:
            if default_name not in existing_service_names:
                _services_list.append((default_name, default_enabled))

        self.configured_services: List[Tuple[str, bool]] = [tuple(s) for s in _services_list]

        self._initial_auto_search_ui_prepared = False 
        self._pending_worker_shutdown_processes: List[multiprocessing.Process] = []
        self._worker_ready_event = threading.Event() 
        
        # Global Image Fetcher Setup
        self.image_fetcher_thread = QThread(self) # Parent QThread to MainWindow for lifetime management
        self.image_fetcher_thread.setObjectName("GlobalImageFetcherThread")
        self.image_fetcher = ImageFetcher() # Create without parent, will be moved
        self.image_fetcher.moveToThread(self.image_fetcher_thread)

        # Connect signals for initialization and quitting
        self.image_fetcher_thread.started.connect(self.image_fetcher.initialize_manager)
        # self.image_fetcher_thread.finished.connect(self.image_fetcher.quit_manager) # If ImageFetcher needs cleanup before thread stops
        self.image_fetcher_thread.finished.connect(self.image_fetcher.deleteLater) # Ensure ImageFetcher is deleted when thread finishes
        self.image_fetcher_thread.start()
        logger.info("[GUI] Global ImageFetcher thread started.")
        # The ImageFetcher instance is now ready to receive requests via signals/slots on its thread.

        # self.current_download_progress_dialog: Optional[QDialog] = None # Replaced by notification manager
        self.notification_manager = NotificationManager(self)
        self.active_download_notifications: Dict[str, str] = {} # path_str -> notification_id

        self.download_thread: Optional[QThread] = None
        self.current_download_worker: Optional[ImageDownloaderWorker] = None
        self.settings_dialog: Optional[SettingsDialog] = None

        # Dimension filter state
        self.active_min_w_filter: Optional[int] = None
        self.active_min_h_filter: Optional[int] = None

        # Members for current album art display
        self.current_album_art_path: Optional[str] = self.session_config.get("current_album_art_path")
        self.current_art_display_container: Optional[QWidget] = None
        self.current_art_image_widget: Optional[MainWindow._CurrentArtWidget] = None
        self.current_art_dimensions_label: Optional[QLabel] = None
        self.CURRENT_ART_IMAGE_SIZE = QSize(120, 120) # Define image display size
        self._is_initiating_search = False # Guard against re-entrant search calls

        self._setup_ui_frames()

        self.front_only_var_checkbox.setChecked(self.session_config.get("front_only", DEFAULT_CONFIG["front_only"]))

        self._render_service_sections()

        self.is_auto_searching = self.initial_search_payload_for_worker is not None
        if self.is_auto_searching and isinstance(self.initial_search_payload_for_worker, CMD_Search): # Check type
            logger.info("[GUI] CLI auto-search detected. Preparing UI for search.")
            search_payload = self.initial_search_payload_for_worker # This is already a CMD_Search
            payload_artist = search_payload.artist
            payload_album = search_payload.album

            # Ensure batch_size from CLI is honored if present in initial_search_payload_for_worker
            # If initial_search_payload_for_worker.batch_size is None, it will use config/default later.
            # No specific change needed here if initial_search_payload_for_worker is correctly populated by CLI parser.

            services_for_cli_search_tuples: List[Tuple[str, bool]] = search_payload.active_services_config
            services_to_show_searching_for_cli = {name for name, enabled in services_for_cli_search_tuples if enabled}
            for service_name, section in self.service_sections.items():
                if service_name in services_to_show_searching_for_cli:
                    section.set_initial_searching_status()

            if payload_artist: self.artist_entry.setText(payload_artist)
            self.album_entry.setText(payload_album)
            self.load_button.setVisible(False)
            self.cancel_button.setEnabled(True)
            self.cancel_button.setVisible(True)

        self.event_poll_timer = QTimer(self)
        self.event_poll_timer.timeout.connect(self._poll_event_queue)
        self.event_poll_timer.start(EVENT_QUEUE_POLL_INTERVAL_MS)

        # For Enter/Return key presses, we want to cancel ongoing search
        QShortcut(QKeySequence(Qt.Key_Return), self, lambda: self._on_load_images_click(cancel_if_ongoing=True))
        QShortcut(QKeySequence(Qt.Key_Enter), self, lambda: self._on_load_images_click(cancel_if_ongoing=True))
        QShortcut(QKeySequence("Alt+D"), self, lambda: (self.album_entry.setFocus(), self.album_entry.selectAll()))
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._handle_escape_key)
        QShortcut(QKeySequence("Ctrl+P"), self, self._show_settings_dialog) # Added settings shortcut
        QShortcut(QKeySequence("Ctrl+I"), self, self._handle_current_art_set_path) # Shortcut to set current image
        QShortcut(QKeySequence("Ctrl+W"), self, self.close) # Added close shortcut


        logger.info("[GUI] MainWindow initialized. Starting initial worker.")

        if not self._start_worker_process(self.initial_search_payload_for_worker):
            logger.error("[GUI] Failed to start initial worker. UI might be partially unresponsive.")
        else:
            logger.info("[GUI] Initial worker process started successfully. Waiting for worker ready signal.")
        
        # Load and display current album art if path is provided
        if self.current_album_art_path:
            self._load_and_display_current_album_art(set_min_dimensions=False)

        # Initialize dimension filter from session_config (CLI args)
        cli_min_w = self.session_config.get("min_width")
        cli_min_h = self.session_config.get("min_height")
        initial_dims_text = ""
        if cli_min_w is not None and cli_min_h is not None and cli_min_w > 0 and cli_min_h > 0:
            initial_dims_text = f"{cli_min_w}x{cli_min_h}"
            self.active_min_w_filter = int(cli_min_w)
            self.active_min_h_filter = int(cli_min_h)
        elif cli_min_w is not None and cli_min_w > 0:
            initial_dims_text = str(cli_min_w)
            self.active_min_w_filter = int(cli_min_w)
        elif cli_min_h is not None and cli_min_h > 0: # Less common to have only height, but handle
            initial_dims_text = f"x{cli_min_h}"
            self.active_min_h_filter = int(cli_min_h)

        if initial_dims_text:
            self.min_dims_entry.setText(initial_dims_text)
            # _on_min_dimensions_changed will be triggered by setText if text actually changes.
            # If sections are not yet rendered, this call won't do much.
            # We need to ensure filter is applied after sections are rendered.
            # This is handled because _render_service_sections calls update_visual_state
            # which in turn (implicitly via MainWindow telling sections) should consider the filter.
            # Forcing an update after initial render might be safest.
            if self.service_sections: # If sections are somehow rendered before this
                 self._notify_sections_of_filter_change()


        self.album_entry.setFocus()

    def _show_settings_dialog(self):
        if self.settings_dialog is None or not self.settings_dialog.isVisible():
            self.settings_dialog = SettingsDialog(self)
            if sys.platform == "win32":
                apply_theme_tweaks_windows(self.settings_dialog, self.session_config.get("theme", DEFAULT_CONFIG.get("theme", "auto")))
            if self.settings_dialog.exec() == QDialog.Accepted:
                logger.info("[GUI] Settings dialog accepted. Reloading relevant session config from USER_CONFIG.")
                # Store old theme and thumbnail_size before updating session_config to detect change
                old_theme = self.session_config.get("theme", DEFAULT_CONFIG.get("theme", "auto"))
                old_thumbnail_size = self.session_config.get("thumbnail_size", DEFAULT_CONFIG.get("thumbnail_size"))

                self.session_config.update(USER_CONFIG)
                self.front_only_var_checkbox.setChecked(self.session_config.get("front_only", DEFAULT_CONFIG["front_only"]))

                # Apply theme if it has changed
                new_theme = self.session_config.get("theme", DEFAULT_CONFIG.get("theme", "auto"))
                new_thumbnail_size = self.session_config.get("thumbnail_size", DEFAULT_CONFIG.get("thumbnail_size"))
                if old_theme != new_theme:
                    logger.info(f"[GUI] Theme changed from '{old_theme}' to '{new_theme}'. Re-applying theme and styles.")
                    apply_app_theme_and_custom_styles(new_theme)
                    if sys.platform == "win32":
                        apply_theme_tweaks_windows(self, new_theme)

                # Re-render service sections if thumbnail size changed
                if old_thumbnail_size != new_thumbnail_size:
                    logger.info(f"[GUI] Thumbnail size changed from {old_thumbnail_size} to {new_thumbnail_size}. Forcing re-render of service sections.")
                    self._render_service_sections(force_recreate=True) 

            self.settings_dialog = None

    def _cleanup_pending_shutdowns(self):
        still_pending: List[multiprocessing.Process] = []
        for proc in self._pending_worker_shutdown_processes:
            if proc.is_alive():
                still_pending.append(proc)
            else:
                proc.join(timeout=0.1)
                logger.info(f"[GUI] Previously terminated worker process {proc.pid} has been joined.")
        self._pending_worker_shutdown_processes = still_pending

    def _start_worker_process(self, initial_search_payload_for_worker: Optional[CMD_Search] = None) -> bool: # Updated type hint
        self._cleanup_pending_shutdowns()
        if self.worker_process and self.worker_process.is_alive():
            logger.warning(f"[GUI] Attempting to start new worker while worker {self.worker_pid} is alive. Terminating old one.")
            self._terminate_current_worker(graceful_timeout=0.1, force_if_needed=True)
        
        if self.command_queue: self.command_queue.close() 
        if self.event_queue: self.event_queue.close()

        self.command_queue = multiprocessing.Queue()
        self.event_queue = multiprocessing.Queue()
        self._worker_ready_event.clear()

        process_args = (self.command_queue, self.event_queue, initial_search_payload_for_worker, EFFECTIVE_LOG_LEVEL, EFFECTIVE_LOG_FILE_PATH)
        try:
            self.worker_process = multiprocessing.Process(target=worker_process_main, args=process_args, daemon=True)
            self.worker_process.start()
            self.worker_pid = self.worker_process.pid
            logger.info(f"[GUI] New worker process started with PID: {self.worker_pid}")
            return True
        except Exception as e:
            logger.error(f"[GUI] Failed to start new worker process: {e}", exc_info=True)
            QMessageBox.critical(self, "Worker Error", f"Failed to start background services: {e}")
            self.worker_process = None; self.worker_pid = None
            if self.command_queue: self.command_queue.close(); self.command_queue = None
            if self.event_queue: self.event_queue.close(); self.event_queue = None
            return False

    def _terminate_current_worker(self, graceful_timeout: Optional[float] = 1.0, force_if_needed: bool = True):
        if self.worker_process and self.worker_pid is not None:
            current_pid_to_terminate = self.worker_pid
            process_to_terminate = self.worker_process
            self.worker_process = None
            self.worker_pid = None 

            logger.info(f"[GUI] Terminating worker process {current_pid_to_terminate} (graceful_timeout={graceful_timeout}s)")
            if self.command_queue:
                try:
                    self.command_queue.put_nowait(CMD_Shutdown())
                except Exception as e:
                    logger.warning(f"[GUI] Could not send ShutdownCommand to worker {current_pid_to_terminate}: {e}")
            
            self._pending_worker_shutdown_processes.append(process_to_terminate)
            if graceful_timeout is not None and graceful_timeout > 0:
                threading.Thread(target=self._join_or_force_terminate_process,
                                 args=(process_to_terminate, current_pid_to_terminate, graceful_timeout, force_if_needed),
                                 daemon=True).start()
        elif self.worker_process : 
             logger.info(f"[GUI] Worker process object exists but no PID; attempting to join/close.")
             if self.worker_process.is_alive(): self.worker_process.terminate()
             self.worker_process.join(timeout=0.1); self.worker_process = None


    def _join_or_force_terminate_process(self, process: multiprocessing.Process, pid: int, timeout: float, force: bool):
        logger.debug(f"[GUI HelperThread] Attempting to join PID {pid} with timeout {timeout}s")
        process.join(timeout=timeout)
        if process.is_alive():
            if force:
                logger.warning(f"[GUI HelperThread] Worker {pid} did not shut down gracefully. Forcing termination.")
                process.terminate()
                process.join(timeout=1.0) 
                if process.is_alive(): logger.error(f"[GUI HelperThread] Worker {pid} still alive after SIGTERM.")
            else:
                logger.info(f"[GUI HelperThread] Worker {pid} still alive after graceful attempt, not forcing.")
        else:
            logger.info(f"[GUI HelperThread] Worker {pid} terminated and joined successfully.")


    def closeEvent(self, event):
        logger.info("[GUI] Main window closing. Initiating worker shutdown.")
        self.event_poll_timer.stop() 

        if self.worker_process and self.worker_pid is not None:
            self._terminate_current_worker(graceful_timeout=2.0, force_if_needed=True)
        
        final_cleanup_timeout_end = time.time() + 3 
        for proc in self._pending_worker_shutdown_processes:
            if proc.is_alive():
                logger.info(f"[GUI] closeEvent: Force terminating pending worker {proc.pid}")
                proc.terminate() 
                join_timeout = max(0.1, final_cleanup_timeout_end - time.time())
                proc.join(timeout=join_timeout)
                if proc.is_alive(): logger.warning(f"[GUI] closeEvent: Pending worker {proc.pid} did not terminate after force.")
        
        if self.download_thread and self.download_thread.isRunning():
            logger.info("[GUI] closeEvent: Requesting download thread to quit.")
            self.download_thread.quit()
            if not self.download_thread.wait(1000): 
                logger.warning("[GUI] closeEvent: Download thread did not finish in time. It might be terminated.")

        # Shutdown global ImageFetcher thread
        if self.image_fetcher_thread and self.image_fetcher_thread.isRunning():
            logger.info("[GUI] closeEvent: Requesting global ImageFetcher thread to quit.")
            # If ImageFetcher had a quit_manager slot for pre-quit cleanup:
            # QMetaObject.invokeMethod(self.image_fetcher, "quit_manager", Qt.QueuedConnection)
            self.image_fetcher_thread.quit()
            if not self.image_fetcher_thread.wait(2000): # Wait up to 2 seconds
                logger.warning("[GUI] closeEvent: Global ImageFetcher thread did not finish in time. It may be terminated abruptly.")
            else:
                logger.info("[GUI] Global ImageFetcher thread finished.")
        
        logger.info("[GUI] Worker shutdown process complete.")
        super().closeEvent(event)

    def _handle_escape_key(self):
        if self.cancel_button.isVisible() and self.cancel_button.isEnabled():
            logger.info("[GUI] Escape pressed during active search, cancelling search.")
            self._on_cancel_search_click()
        self.close() 


    def _setup_ui_frames(self):
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # --- Top section: Controls on Left, Current Art on Right ---
        top_section_hbox_widget = QWidget()
        top_section_hbox_layout = QHBoxLayout(top_section_hbox_widget)
        top_section_hbox_layout.setContentsMargins(0,0,0,0)
        top_section_hbox_layout.setSpacing(10)

        # --- Left Controls Group (Vertical Box) ---
        left_controls_vbox_widget = QWidget()
        left_controls_vbox_layout = QVBoxLayout(left_controls_vbox_widget)
        left_controls_vbox_layout.setContentsMargins(0,0,0,0)
        left_controls_vbox_layout.setSpacing(10) # Spacing between input_group, service_bar, etc.

        INPUT_WIDGET_HEIGHT = 36
        ACTION_BUTTON_WIDTH = 100
        input_group = QWidget()
        input_layout = QHBoxLayout(input_group)
        input_layout.setContentsMargins(0,0,0,0)
        input_layout.setSpacing(10)

        self.artist_entry = QLineEdit()
        self.artist_entry.setFont(get_font(FONT_SIZE_LARGE, weight=QFont.Bold))
        self.artist_entry.setPlaceholderText("Artist (Optional)")
        self.artist_entry.setFixedHeight(INPUT_WIDGET_HEIGHT)
        input_layout.addWidget(self.artist_entry, 1) 

        self.album_entry = QLineEdit()
        self.album_entry.setFont(get_font(FONT_SIZE_LARGE, weight=QFont.Bold))
        self.album_entry.setPlaceholderText("Album")
        self.album_entry.setFixedHeight(INPUT_WIDGET_HEIGHT)
        input_layout.addWidget(self.album_entry, 1)

        self.min_dims_entry = QLineEdit()
        self.min_dims_entry.setFont(get_font(FONT_SIZE_LARGE)) # Smaller font for this field
        self.min_dims_entry.setPlaceholderText("Min. Width")
        self.min_dims_entry.setFixedHeight(INPUT_WIDGET_HEIGHT)
        self.min_dims_entry.setMaximumWidth(112) # Max width for the dims input
        self.min_dims_entry.textChanged.connect(self._on_min_dimensions_changed)
        input_layout.addWidget(self.min_dims_entry, 0) # No stretch, fixed width
        
        self.load_button = QPushButton("Search")
        self.load_button.setFont(get_font(FONT_SIZE_NORMAL, weight=QFont.Bold))
        self.load_button.setFixedHeight(INPUT_WIDGET_HEIGHT)
        self.load_button.setFixedWidth(ACTION_BUTTON_WIDTH)
        self.load_button.clicked.connect(self._on_load_images_click) 
        input_layout.addWidget(self.load_button, 0)

        self.cancel_button = QPushButton("Cancel") 
        self.cancel_button.setFont(get_font(FONT_SIZE_NORMAL))
        self.cancel_button.setFixedHeight(INPUT_WIDGET_HEIGHT)
        self.cancel_button.setFixedWidth(ACTION_BUTTON_WIDTH)
        self.cancel_button.clicked.connect(self._on_cancel_search_click)
        self.cancel_button.setVisible(False) 
        input_layout.addWidget(self.cancel_button, 0)

        # For returnPressed in input fields, also cancel ongoing search
        self.artist_entry.returnPressed.connect(lambda: self._on_load_images_click(cancel_if_ongoing=True))
        self.album_entry.returnPressed.connect(lambda: self._on_load_images_click(cancel_if_ongoing=True))
        self.min_dims_entry.returnPressed.connect(lambda: self._on_load_images_click(cancel_if_ongoing=True))
        
        left_controls_vbox_layout.addWidget(input_group)


        # Container for ServiceToggleButtonBar and Settings Button
        service_bar_controls_container = QWidget()
        service_bar_controls_layout = QHBoxLayout(service_bar_controls_container)
        service_bar_controls_layout.setContentsMargins(0,0,0,0)
        service_bar_controls_layout.setSpacing(10) # Spacing between bar and button

        self.service_toggle_bar = ServiceToggleButtonBar(self)
        self.service_toggle_bar.populate_services(self.configured_services)
        self.service_toggle_bar.serviceToggled.connect(self._handle_service_toggled_from_bar)
        self.service_toggle_bar.serviceOrderChanged.connect(self._handle_service_order_changed_from_bar)
        service_bar_controls_layout.addWidget(self.service_toggle_bar, 1) # Toggle bar takes up available space

        # Settings Button (Cogwheel)
        self.settings_button = QPushButton("⚙") # Unicode cogwheel
        settings_button_font = get_font(FONT_SIZE_LARGE + 1) # Slightly larger font for the icon
        self.settings_button.setFont(settings_button_font)
        BUTTON_DIAMETER = 28
        self.settings_button.setFixedSize(BUTTON_DIAMETER, BUTTON_DIAMETER)
        self.settings_button.setToolTip("Open Settings (Ctrl+P)")
        self.settings_button.clicked.connect(self._show_settings_dialog)
        self.settings_button.setStyleSheet(f"QPushButton {{ border-radius: {BUTTON_DIAMETER // 2}px; }}")
        self.settings_button.setStyleSheet(
            f"QPushButton {{"
            f"  border-radius: {BUTTON_DIAMETER // 2}px;"
            f"  padding: 1px;"  # Try small padding, or 0px
            f"  border: none;"   # Ensure no border is taking up space
            f"  text-align: center;" # Explicitly center
            f"}}"
        )
        
        service_bar_controls_layout.addWidget(self.settings_button, 0, Qt.AlignTop) # Align to top in case bar wraps

        left_controls_vbox_layout.addWidget(service_bar_controls_container)

        controls_widget = QWidget()
        controls_layout = QHBoxLayout(controls_widget)
        controls_layout.setContentsMargins(0,0,0,0)

        self.front_only_var_checkbox = QCheckBox("Front Covers Only")
        self.front_only_var_checkbox.setFont(get_font(FONT_SIZE_NORMAL))
        self.front_only_var_checkbox.toggled.connect(
            lambda checked: self._update_config_values_and_save("front_only", checked)
        )
        controls_layout.addWidget(self.front_only_var_checkbox)
        controls_layout.addStretch(1)
        
        left_controls_vbox_layout.addWidget(controls_widget)
        left_controls_vbox_layout.addStretch(1) # Pushes left controls to the top

        top_section_hbox_layout.addWidget(left_controls_vbox_widget, 1) # Left controls take available space (stretch = 1)

        # --- Right Side: Current Album Art Display ---
        self.current_art_display_container = QWidget()
        art_display_container_vbox_layout = QVBoxLayout(self.current_art_display_container)
        art_display_container_vbox_layout.setContentsMargins(0,0,0,0)
        art_display_container_vbox_layout.setSpacing(5) # Spacing between image and label
        art_display_container_vbox_layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        self.current_art_image_widget = _CurrentArtWidget(self.CURRENT_ART_IMAGE_SIZE, self) # Use _CurrentArtWidget directly
        # Placeholder for current art - using ImageFrame's static method for consistency
        raw_placeholder = QPixmap(self.CURRENT_ART_IMAGE_SIZE)
        raw_placeholder.fill(QColor(220, 220, 220)) # Slightly different placeholder color
        rounded_placeholder = ImageFrame._create_rounded_pixmap(raw_placeholder, THUMBNAIL_CORNER_RADIUS)
        self.current_art_image_widget.set_display_pixmap(rounded_placeholder)

        art_display_container_vbox_layout.addWidget(self.current_art_image_widget, 0, Qt.AlignHCenter)

        # Connect signals for _CurrentArtWidget here to ensure they are connected only once.
        self.current_art_image_widget.openImageRequested.connect(self._handle_current_art_open_image)
        self.current_art_image_widget.openFolderRequested.connect(self._handle_current_art_open_folder)
        self.current_art_image_widget.setPathRequested.connect(self._handle_current_art_set_path)
        self.current_art_image_widget.closeWidgetRequested.connect(self._handle_current_art_close_widget)

        self.current_art_dimensions_label = QLabel("No Art Loaded")
        self.current_art_dimensions_label.setFont(get_font(FONT_SIZE_SMALL))
        self.current_art_dimensions_label.setAlignment(Qt.AlignCenter)
        art_display_container_vbox_layout.addWidget(self.current_art_dimensions_label, 0, Qt.AlignHCenter)
        
        art_display_container_vbox_layout.addStretch(1) # Pushes art+label to the top of its container

        self.current_art_display_container.setFixedWidth(self.CURRENT_ART_IMAGE_SIZE.width() + 20) # Give some horizontal room
        self.current_art_display_container.setVisible(False) # Initially hidden

        top_section_hbox_layout.addWidget(self.current_art_display_container, 0, Qt.AlignTop) # Art display, no stretch, align top

        main_layout.addWidget(top_section_hbox_widget) # Add the HBox container to the main VBox

        self.results_scroll_area = QScrollArea()
        self.results_scroll_area.setObjectName("MainResultsScrollArea")
        self.results_scroll_area.setWidgetResizable(True)
        self.results_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.results_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.results_scroll_area.setFrameShape(QFrame.Shape.NoFrame) 

        self.scrollable_results_widget = QWidget() 
        self.results_layout = QVBoxLayout(self.scrollable_results_widget) 
        self.results_layout.setContentsMargins(5,5,5,5)
        self.results_layout.setSpacing(8)
        self.results_layout.setAlignment(Qt.AlignTop) 

        self.results_scroll_area.setWidget(self.scrollable_results_widget)
        main_layout.addWidget(self.results_scroll_area, 1) 

    def _render_service_sections(self, force_recreate: bool = False):
        if not self.scrollable_results_widget: return
        
        logger.info(f"[GUI] Rendering service sections. Force recreate: {force_recreate}. Configured services: {self.configured_services}")
        current_thumbnail_size = self.session_config.get("thumbnail_size", DEFAULT_CONFIG.get("thumbnail_size"))
        is_search_active_overall = self.cancel_button.isVisible()

        if force_recreate:
            # --- Force Recreate Logic (e.g., for thumbnail size change) ---
            logger.info(f"[GUI] Forcing recreation of all service sections with thumbnail size: {current_thumbnail_size}")
            
            # 1. Clear all existing section widgets from the layout and delete them
            widgets_to_remove = []
            for i in range(self.results_layout.count()):
                layout_item = self.results_layout.itemAt(i)
                if layout_item and layout_item.widget() and isinstance(layout_item.widget(), ServiceImageSection):
                    widgets_to_remove.append(layout_item.widget())
            
            for widget in widgets_to_remove:
                self.results_layout.removeWidget(widget)
                widget.deleteLater()
                logger.debug(f"[GUI] Removed and deleted old ServiceImageSection: {getattr(widget, 'service_name', 'Unknown')}")

            self.service_sections.clear() # Clear the tracking dictionary

            # 2. Re-create and add sections for currently active services
            active_service_names_in_order = [name for name, enabled in self.configured_services if enabled]
            for service_name in active_service_names_in_order:
                section = ServiceImageSection(
                    parent_widget=self.scrollable_results_widget,
                    service_name=service_name,
                    on_image_double_click_callback=self._on_image_double_click,
                    on_load_more_callback=self._request_load_more_via_queue,
                    image_fetcher=self.image_fetcher,
                    thumbnail_display_dimension=current_thumbnail_size,
                    name_label_min_width=self.max_service_name_pixel_width
                )
                self.results_layout.addWidget(section)
                self.service_sections[service_name] = section
                if is_search_active_overall:
                    section.set_initial_searching_status()
                else:
                    section.update_visual_state()
        else:
            # --- Original Logic (reuse, add, remove, reorder) ---
            active_service_names_in_order = [name for name, enabled in self.configured_services if enabled]
            sections_to_keep_or_create = {}

            for service_name in active_service_names_in_order:
                if service_name in self.service_sections:
                    section = self.service_sections[service_name]
                    # Ensure existing section has correct thumbnail size (it might if not force_recreate)
                    # This path assumes if not force_recreate, thumbnail size hasn't changed.
                    # If it had, force_recreate would be true.
                    sections_to_keep_or_create[service_name] = section
                    if not section.isVisible(): 
                        section.show() # Ensure it's visible if it was hidden previously
                else:
                    logger.debug(f"[GUI] Creating new ServiceImageSection for {service_name} (non-force)")
                    section = ServiceImageSection(
                        parent_widget=self.scrollable_results_widget,
                        service_name=service_name,
                        on_image_double_click_callback=self._on_image_double_click,
                        on_load_more_callback=self._request_load_more_via_queue,
                        image_fetcher=self.image_fetcher,
                        thumbnail_display_dimension=current_thumbnail_size,
                        name_label_min_width=self.max_service_name_pixel_width
                    )
                    sections_to_keep_or_create[service_name] = section
                
                # Update state based on current search status
                if is_search_active_overall:
                     section.set_initial_searching_status()
                else:
                     section.update_visual_state()


            # Remove sections that are no longer active
            current_tracked_section_names = list(self.service_sections.keys())
            for service_name in current_tracked_section_names:
                if service_name not in sections_to_keep_or_create:
                    logger.debug(f"[GUI] Removing ServiceImageSection for {service_name} (non-force, became inactive)")
                    section_to_remove = self.service_sections.pop(service_name)
                    self.results_layout.removeWidget(section_to_remove)
                    section_to_remove.deleteLater()
            
            self.service_sections = sections_to_keep_or_create

            # Reorder widgets in the layout according to active_service_names_in_order
            # Detach all *managed* (ServiceImageSection) widgets currently in the layout
            widgets_in_layout_to_reorder = []
            for i in range(self.results_layout.count()):
                item = self.results_layout.itemAt(i)
                if item and item.widget() and isinstance(item.widget(), ServiceImageSection):
                    widgets_in_layout_to_reorder.append(item.widget())
            
            for widget in widgets_in_layout_to_reorder:
                 self.results_layout.removeWidget(widget) # Detach without deleting

            # Add them back in the correct order
            for service_name in active_service_names_in_order:
                if service_name in self.service_sections:
                    section_widget = self.service_sections[service_name]
                    self.results_layout.addWidget(section_widget) 
                else:
                    logger.warning(f"[GUI] Service {service_name} expected in self.service_sections but not found during re-ordering (non-force).")

            # Final visual update for all sections
            for section in self.service_sections.values():
                if section.isVisible():
                    section.update_visual_state()

        self.scrollable_results_widget.adjustSize()
        self.results_scroll_area.updateGeometry()
        
        # After initial render, ensure sections get current filter if it was set from CLI
        if (self.active_min_w_filter is not None or self.active_min_h_filter is not None) and not force_recreate :
            self._notify_sections_of_filter_change()


    def _parse_min_dimensions_input(self, text: str) -> Tuple[Optional[int], Optional[int], bool]:
        text = text.strip().lower()
        if not text:
            return None, None, True # Valid (no filter)

        match_w_only = re.fullmatch(r"(\d+)", text)
        match_w_x_h = re.fullmatch(r"(\d+)x(\d+)", text)
        match_h_only = re.fullmatch(r"x(\d+)", text)

        min_w, min_h = None, None
        is_valid = False

        if match_w_x_h:
            min_w = int(match_w_x_h.group(1))
            min_h = int(match_w_x_h.group(2))
            if min_w > 0 and min_h > 0: is_valid = True
        elif match_w_only:
            min_w = int(match_w_only.group(1))
            if min_w > 0: is_valid = True
        elif match_h_only:
            min_h = int(match_h_only.group(1))
            if min_h > 0: is_valid = True
        
        if not is_valid: # Reset if parse failed validation
            return None, None, False
            
        return min_w, min_h, True

    def _notify_sections_of_filter_change(self):
        logger.debug(f"[GUI] Notifying service sections of filter change: W={self.active_min_w_filter}, H={self.active_min_h_filter}")
        for section_name, section_ui_obj in self.service_sections.items():
            if shiboken6.isValid(section_ui_obj):
                section_ui_obj.apply_dimension_filter(self.active_min_w_filter, self.active_min_h_filter)

    def _on_min_dimensions_changed(self, text: str):
        min_w, min_h, is_valid = self._parse_min_dimensions_input(text)

        # Visually indicate validity (e.g., border color)
        style = "" # Default style
        if text: # Only apply error style if there's text and it's invalid
            if not is_valid:
                style = "border: 1px solid red;"
            else: # Valid non-empty
                style = "border: 1px solid green;" # Optional: green for valid
        self.min_dims_entry.setStyleSheet(style)


        if is_valid:
            if self.active_min_w_filter != min_w or self.active_min_h_filter != min_h:
                self.active_min_w_filter = min_w
                self.active_min_h_filter = min_h
                logger.info(f"[GUI] Min dimensions filter changed to: W={min_w}, H={min_h}")
                self._notify_sections_of_filter_change()
        # If not valid but text is present, do nothing with the filter itself,
        # the red border indicates error. Previous valid filter remains active.

    def _send_command_to_worker(self, command_obj: Any) -> bool:
        active_command_queue = self.command_queue
        active_worker_pid = self.worker_pid
        if active_command_queue and self.worker_process and self.worker_process.is_alive() and self.worker_pid == active_worker_pid:
            try:
                active_command_queue.put_nowait(command_obj)
                logger.debug(f"[GUI] Sent command object to worker {active_worker_pid}: {type(command_obj).__name__}")
                return True
            except Exception as e:
                logger.error(f"[GUI] Failed to send command object {type(command_obj).__name__} to worker {active_worker_pid}: {e}")
                QMessageBox.critical(self, "Worker Communication Error", f"Could not send command to background service: {e}")
                return False
        else:
            logger.warning(f"[GUI] Cannot send command object {type(command_obj).__name__}: No active worker (PID: {active_worker_pid}) or command queue.")
            return False

    def _handle_service_toggled_from_bar(self, service_name: str, is_enabled: bool):
        logger.info(f"[GUI] Service '{service_name}' toggled to {is_enabled} from bar.")
        updated_config = False
        for i, (s_name, _) in enumerate(self.configured_services):
            if s_name == service_name:
                if self.configured_services[i][1] != is_enabled:
                    self.configured_services[i] = (s_name, is_enabled)
                    updated_config = True
                break
        
        if updated_config:
            self._update_config_values_and_save("services", [list(s) for s in self.configured_services])
            self._render_service_sections() 

    def _handle_service_order_changed_from_bar(self, new_services_config: List[Tuple[str, bool]]):
        logger.info(f"[GUI] Service order or toggle changed from bar: {new_services_config}")
        new_services_tuples = [tuple(s) for s in new_services_config]
        if self.configured_services != new_services_tuples:
            self.configured_services = new_services_tuples
            self._update_config_values_and_save("services", [list(s) for s in self.configured_services])
            self._render_service_sections() 

    def _update_config_values_and_save(self, key: str, value: Any):
        USER_CONFIG[key] = value
        self.session_config[key] = value 
        if save_user_config():
            logger.info(f"GUI: Saved configuration for key '{key}'.")
        else:
            logger.error(f"GUI: Failed to save configuration change for key '{key}'.")
            # Show a warning to the user
            QMessageBox.warning(self, "Configuration Save Error",
                                f"Failed to save your settings for '{key}'.\n\n"
                                f"Please check file permissions or disk space for the configuration file:\n"
                                f"{USER_CONFIG_FILE}\n\n"
                                "Your changes may not persist after closing the application.")

    def _prepare_ui_for_search_start(self):
        logger.debug("[GUI] Preparing UI for search start state (resetting sections, then setting 'Searching...' status).")
        
        for section_ui in self.service_sections.values():
            section_ui.reset_for_new_search()
        
        QApplication.processEvents()

        for section_ui in self.service_sections.values():
            section_ui.set_initial_searching_status()
        
        QApplication.processEvents()

    def _on_load_images_click(self, cancel_if_ongoing: bool = False):
        # This method is the direct slot for UI signals.
        # It schedules the actual search initiation to run after current event processing.
        QTimer.singleShot(0, lambda: self._execute_search_initiation(cancel_if_ongoing=cancel_if_ongoing))

    def _execute_search_initiation(self, cancel_if_ongoing: bool):
        if self._is_initiating_search:
            logger.debug("[GUI] Search initiation already in progress, ignoring redundant trigger.")
            return
        
        self._is_initiating_search = True
        try:
            if self.cancel_button.isVisible(): # A search is currently active or appears to be
                if cancel_if_ongoing:
                    logger.info("[GUI] New search requested while a search is active; proceeding to cancel old and start new.")
                    if self.worker_process and self.worker_pid is not None: # Check if worker is active for cancellation
                        self._send_command_to_worker(CMD_CancelSearch())
                    # Proceed to set up the new search. UI elements (buttons, sections)
                    # will be updated by the subsequent code in this method.
                else: # cancel_if_ongoing is False (e.g. Search button clicked)
                    logger.warning("[GUI] Load Images triggered (e.g. button click), but a search is already in progress. Ignoring.")
                    return # Do not proceed with new search

            current_search_artist = self.artist_entry.text().strip()
            current_search_album = self.album_entry.text().strip()

            if not current_search_album:
                QMessageBox.critical(self, "Input Error", "Album Title is required.")
                return
            
            logger.info(f"[GUI] Load Images: Artist='{current_search_artist}', Album='{current_search_album}'")

            # Restarting the worker on every new search was overkill. Keep it here as a comment, just in case.
            # logger.info(f"[GUI] Load Images: Terminating current worker (PID: {self.worker_pid or 'N/A'}) to start fresh search.")
            # self._terminate_current_worker(graceful_timeout=0.2, force_if_needed=True)
            
            if not self.worker_process or not self.worker_process.is_alive():
                start_success = self._start_worker_process()
                if not start_success:
                    self.load_button.setEnabled(True); self.load_button.setVisible(True)
                    self.cancel_button.setVisible(False)
                    QMessageBox.critical(self, "Error", "Could not start background service for the search.")
                    return
            
            self.load_button.setVisible(False)
            self.cancel_button.setEnabled(True); self.cancel_button.setVisible(True)
            self._prepare_ui_for_search_start()

            logger.info("[GUI] Sending search command to worker.")
            
            current_batch_size = self.session_config.get("batch_size", DEFAULT_CONFIG.get("batch_size"))
            search_command_data = CMD_Search(
                artist=current_search_artist,
                album=current_search_album,
                front_only_setting=self.front_only_var_checkbox.isChecked(),
                active_services_config=list(self.configured_services),
                batch_size=int(current_batch_size) if current_batch_size is not None else None
            )
            
            if not self._send_command_to_worker(search_command_data):
                QMessageBox.critical(self, "Error", "Could not start search. Communication failed.")
                # Revert UI if sending command fails
                self.load_button.setEnabled(True); self.load_button.setVisible(True)
                self.cancel_button.setVisible(False)
            else:
                logger.info(f"[GUI] Search for '{current_search_album}' initiated with worker {self.worker_pid}.")
                self.notification_manager.clear_all_notifications(immediate=True) # Clear previous notifications
                QMetaObject.invokeMethod(self.image_fetcher, "clear_cache_slot", Qt.QueuedConnection)
                logger.info("[GUI] Queued clear_cache_slot for ImageFetcher for the new search.")
        finally:
            self._is_initiating_search = False

    def _on_cancel_search_click(self):
        logger.info("[GUI] Cancel search button clicked.")
        self.load_button.setEnabled(True); self.load_button.setVisible(True)
        self.cancel_button.setVisible(False)

        if self.worker_process and self.worker_pid is not None:
            self._send_command_to_worker(CMD_CancelSearch())

        for section in self.service_sections.values():
            status_text = section.status_label.text()
            if any(s in status_text for s in ["Searching...", "Fetching...", "Displaying...", "Loading...", "available..."]):
                section._current_status_message_key = "Search cancelled."
                section._update_status_label()
                section.set_load_more_status_ui(loading=False, has_more_after_load=section._has_more_actionable)

    def _request_load_more_via_queue(self, service_name: str):
        if service_name not in self.service_sections: return
        section = self.service_sections[service_name]

        logger.info(f"[GUI] Requesting more images for {service_name}")
        
        current_batch_size = self.session_config.get("batch_size", DEFAULT_CONFIG.get("batch_size"))

        request_more_data = CMD_RequestMore(
            service_name=service_name,
        active_services_config=list(self.configured_services),
            batch_size=int(current_batch_size) if current_batch_size is not None else None
        )
        if not self._send_command_to_worker(request_more_data): # request_more_data is already a CMD_RequestMore
            section.set_load_more_status_ui(loading=False, has_more_after_load=section._has_more_actionable)

    def _poll_event_queue(self):
        if not self.isVisible(): return 
        self._cleanup_pending_shutdowns()
        current_event_queue = self.event_queue
        if current_event_queue:
            while True:
                try:
                    event_obj = current_event_queue.get_nowait() # Gets the event object directly
                    self._handle_worker_event(event_obj)
                except queue.Empty:
                    break
                except (EOFError, BrokenPipeError) as e:
                    logger.warning(f"[GUI] Event queue pipe broken for PID (approx) {self.worker_pid}: {e}.")
                    if current_event_queue == self.event_queue:
                        self.event_queue = None 
                        if self.worker_process and not self.worker_process.is_alive():
                             self.worker_pid = None 
                    break 
                except Exception as e:
                    logger.error(f"[GUI] Error processing event queue: {e}", exc_info=True)
                    break

    def _on_image_double_click(self, image_data: Union[PotentialImage, ImageResult], preloaded_bytes_qba: QByteArray):
        if not self.isVisible(): return
        preloaded_bytes = preloaded_bytes_qba.data() if preloaded_bytes_qba else None
        
        full_url = image_data.full_image_url
        logger.info(f"[GUI] Image double-click: Starting save process for {full_url}")

        # --- Filename Generation Logic ---
        base_filename = "Cover"

        filename_template = self.session_config.get("default_filename", DEFAULT_CONFIG.get("default_filename")).strip()

        if filename_template:
            album = image_data.source_candidate
            image_type_raw = "Cover" if image_data.is_front else image_data.original_type or "Cover"
            base_filename = filename_template.replace("{artist}", album.artist_name or "")
            base_filename = base_filename.replace("{album}", album.album_name or "")
            base_filename = base_filename.replace("{type}", image_type_raw)

        # Sanitize filename
        ILLEGAL_FILENAME_CHARS_PATTERN = r'[<>:"/\\|?*\x00-\x1F]'
        REPLACEMENT_CHAR = ' '

        base_filename = re.sub(ILLEGAL_FILENAME_CHARS_PATTERN, REPLACEMENT_CHAR, base_filename)
        base_filename = base_filename.strip("-_. ")
        base_filename = re.sub(r'\s+', ' ', base_filename) # Collapse multiple whitespace to single space
        
        if not base_filename:
            base_filename = "Cover"

        file_ext = pathlib.Path(full_url).suffix.split('?')[0]
        if not file_ext or len(file_ext) > 5 or len(file_ext) < 2 : file_ext = "jpg"

        final_filename_with_ext = f"{base_filename}.{file_ext.strip('.')}"
        logger.info(f"[GUI] Generated filename: {final_filename_with_ext}")

        download_path_str = ""
        if self.session_config.get("no_save_prompt", False):
            output_dir_str = self.session_config.get("default_output_dir", str(pathlib.Path.home() / "Downloads"))
            output_dir = pathlib.Path(output_dir_str).expanduser()
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
                download_path_str = str(output_dir / final_filename_with_ext)
            except OSError as e:
                logger.error(f"[GUI] Error creating output directory {output_dir}: {e}")
                QMessageBox.critical(self, "Save Error", f"Could not create directory:\n{output_dir}\n\n{e}")
                return
            logger.info(f"[GUI] Saving directly to: {download_path_str}")
        else:
            initial_dir_str = self.session_config.get("default_output_dir", str(pathlib.Path.home() / "Downloads"))
            initial_dir = str(pathlib.Path(initial_dir_str).expanduser())
            try:
                pathlib.Path(initial_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                initial_dir = str(pathlib.Path.home())
                logger.warning(f"[GUI] Could not ensure initial_dir {initial_dir_str}, falling back to {initial_dir}")

            file_types = f"{file_ext.upper()[1:]} Image (*{file_ext});;JPEG Image (*.jpg *.jpeg);;PNG Image (*.png);;All files (*.*)"
            
            proposed_path = str(pathlib.Path(initial_dir) / final_filename_with_ext)

            download_path_str, _ = QFileDialog.getSaveFileName(
                self, "Save Album Art As...", proposed_path, file_types
            )
            if not download_path_str:
                logger.info("[GUI] Save dialog cancelled."); return
        
        download_path = pathlib.Path(download_path_str)
        
        # Use NotificationManager instead of modal dialog
        notification_text = f"Downloading to:\n{download_path.name}"
        # Create a unique ID for this download operation, e.g., based on path or URL
        # Using path as string for the dictionary key later
        download_op_key = str(download_path)
        notification_id = f"{DOWNLOAD_NOTIFICATION_PREFIX}{download_op_key}"

        self.active_download_notifications[download_op_key] = notification_id
        
        self.notification_manager.show_notification(
            text=notification_text,
            timeout_ms=None,  # No timeout
            dismissable_by_user=False, # Not dismissable by click
            can_timeout=False, # No auto-timeout
            notification_id=notification_id
        )
        QApplication.processEvents()


        if preloaded_bytes:
            logger.debug(f"[GUI] Queuing prime_cache_slot for ImageFetcher for {full_url} with preloaded bytes before download.")
            QMetaObject.invokeMethod(
                self.image_fetcher, 
                "prime_cache_slot", 
                Qt.QueuedConnection,
                Q_ARG(str, full_url),
                Q_ARG(QByteArray, preloaded_bytes_qba),
                Q_ARG(bool, True) # overwrite = True
            )
        
        self.download_thread = QThread(self) 
        self.current_download_worker = ImageDownloaderWorker(full_url, download_path, self.image_fetcher)
        self.current_download_worker.moveToThread(self.download_thread)

        self.current_download_worker.download_completed.connect(self._handle_download_completed)
        self.download_thread.started.connect(self.current_download_worker.run)
        
        self.current_download_worker.download_completed.connect(self.download_thread.quit)
        self.current_download_worker.download_completed.connect(self._clear_current_download_worker) 
        self.current_download_worker.download_completed.connect(self.current_download_worker.deleteLater)
        
        self.download_thread.finished.connect(self.download_thread.deleteLater)
        self.download_thread.finished.connect(self._clear_download_thread) 
        
        self.download_thread.start()

    def _handle_worker_event(self, event_obj: Any):
        if not self.isVisible(): return

        event_class = type(event_obj)
        event_type_name = event_class.__name__
        
        handler = self.event_handlers.get(event_class)

        if handler:
            # Optional: Log before calling the handler if you want more detailed per-event dispatch logging
            # logger.debug(f"[GUI] Dispatching event {event_type_name} to handler {handler.__name__}")
            logger.info(f"[GUI] Received event object from worker: {event_type_name} - dispatching. Data: {event_obj if len(str(event_obj)) < 100 else str(event_obj)[:100] + '...'}")
            handler(event_obj)
        else:
            logger.warning(f"[GUI] Unknown event object type from worker: {event_type_name}. No handler registered.")

    def _handle_evt_worker_ready(self, event_obj: EVT_WorkerReady):
        self._worker_ready_event.set()
        if self.is_auto_searching and not self._initial_auto_search_ui_prepared:
            logger.info("[GUI] Worker is ready for initial auto-search. Preparing UI sections for search status.")
            self._prepare_ui_for_search_start()
            self._initial_auto_search_ui_prepared = True

    def _handle_evt_service_album_search_succeeded(self, event_obj: EVT_ServiceAlbumSearchSucceeded):
        s_name, num = event_obj.service_name, event_obj.num_candidates
        if s_name in self.service_sections:
            self.service_sections[s_name].handle_service_album_search_succeeded(num)

    def _handle_evt_potential_image_found(self, event_obj: EVT_PotentialImageFound):
        s_name, p_image = event_obj.service_name, event_obj.potential_image
        if s_name in self.service_sections:
            self.service_sections[s_name].handle_potential_image(p_image)

    def _handle_evt_image_resolved(self, event_obj: EVT_ImageResolved):
        s_name, i_result = event_obj.service_name, event_obj.image_result
        if s_name in self.service_sections:
            self.service_sections[s_name].handle_image_resolved(i_result)

    def _handle_evt_service_batch_completed_successfully(self, event_obj: EVT_ServiceBatchSucceeded):
        s_name, has_more = event_obj.service_name, event_obj.has_more
        if s_name in self.service_sections:
            self.service_sections[s_name].handle_batch_processing_complete(has_more, was_cancelled=False, error_message=None)

    def _handle_evt_service_batch_cancelled(self, event_obj: EVT_ServiceBatchCancelled):
        s_name = event_obj.service_name
        if s_name in self.service_sections:
            self.service_sections[s_name].handle_batch_processing_complete(has_more=False, was_cancelled=True, error_message=None)

    def _handle_evt_service_batch_error(self, event_obj: EVT_ServiceBatchErrored):
        s_name, err_msg = event_obj.service_name, event_obj.error_message
        if s_name in self.service_sections:
            self.service_sections[s_name].handle_batch_processing_complete(has_more=False, was_cancelled=False, error_message=err_msg)

    def _handle_evt_all_searches_concluded(self, event_obj: EVT_AllSearchesConcluded):
        self.load_button.setEnabled(True); self.load_button.setVisible(True)
        self.cancel_button.setVisible(False)
        for section in self.service_sections.values():
            current_text = section.status_label.text() # This is the displayed text, not the key
            # Check section's internal status key or counts
            if any(s in section._current_status_message_key for s in ["Searching...", "Fetching...", "Displaying..."]) or \
               section._is_loading_more or section._pending_dims_for_filter_count > 0 :
                
                if section._total_items_received_count == 0:
                    section._current_status_message_key = "No images found."
                elif section._visible_items_count == 0 and section._pending_dims_for_filter_count == 0 :
                     section._current_status_message_key = "No matching images."
                
                section._update_status_label()
                section.set_load_more_status_ui(loading=False, has_more_after_load=section._has_more_actionable)

    def _handle_evt_service_error(self, event_obj: EVT_ServiceError):
        s_name, msg = event_obj.service_name, event_obj.message
        logger.error(f"[GUI] Error from worker service {s_name}: {msg}")
        if s_name in self.service_sections:
            self.service_sections[s_name].handle_service_error(msg)
        elif s_name == "WorkerInitialization" or s_name == "WorkerLoop":
            QMessageBox.critical(self, "Background Service Error", f"A critical error occurred:\n{msg}")

    def _handle_evt_worker_shutdown_complete(self, event_obj: EVT_WorkerShutdownComplete):
        worker_pid_info = getattr(event_obj, 'worker_pid', 'Unknown')
        logger.info(f"[GUI] Worker (PID {worker_pid_info}) confirmed shutdown.")

    def _load_and_display_current_album_art(self, set_min_dimensions: bool = True):
        if not self.current_album_art_path or \
           not self.current_art_image_widget or \
           not self.current_art_dimensions_label or \
           not self.current_art_display_container:
            logger.debug("[GUI] Current album art path or UI elements not available for display.")
            return

        file_path = pathlib.Path(self.current_album_art_path)
        if not file_path.exists() or not file_path.is_file():
            logger.warning(f"[GUI] Current album art file does not exist or is not a file: {file_path}")
            self.current_art_dimensions_label.setText("Art Not Found")
            self.current_art_display_container.setVisible(True) # Show container with error
            return

        pixmap = QPixmap()
        if not pixmap.load(str(file_path)):
            logger.error(f"[GUI] Failed to load current album art from: {file_path}")
            self.current_art_dimensions_label.setText("Load Error")
            self.current_art_display_container.setVisible(True) # Show container with error
            return

        # Scale pixmap to fit display size, maintaining aspect ratio
        scaled_pixmap = pixmap.scaled(self.CURRENT_ART_IMAGE_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        
        # Create rounded version for display
        rounded_display_pixmap = ImageFrame._create_rounded_pixmap(scaled_pixmap, THUMBNAIL_CORNER_RADIUS)
        
        self.current_art_image_widget.set_display_pixmap(rounded_display_pixmap)
        self.current_art_dimensions_label.setText(f"{pixmap.width()}x{pixmap.height()}")

        # Auto-fill min_dims_entry with the new image's dimensions
        if set_min_dimensions and self.min_dims_entry:
            new_dims_text = f"{pixmap.width()}x{pixmap.height()}"
            if self.min_dims_entry.text() != new_dims_text: # Avoid unnecessary signal emissions if same
                self.min_dims_entry.setText(new_dims_text)
                logger.info(f"[GUI] Auto-filled min dimensions to: {new_dims_text}")
            
        # Signals are now connected in _setup_ui_frames to avoid multiple connections.
        
        self.current_art_display_container.setVisible(True)
        logger.info(f"[GUI] Displayed current album art: {file_path}")

    def _handle_current_art_set_path(self):
        # Allow this function to proceed even if the container is not visible,
        # as it might be called via shortcut when no art is initially loaded or widget was closed.
        # The QFileDialog.getOpenFileName itself doesn't need the widget to be visible.
        # _load_and_display_current_album_art will handle making it visible if a new image is chosen.

        image_file_types = "Image files (*.png *.jpg *.jpeg *.bmp *.gif);;All files (*.*)"
        # Use last known directory or default downloads
        initial_dir_str = str(pathlib.Path(self.current_album_art_path).parent) if self.current_album_art_path and pathlib.Path(self.current_album_art_path).exists() else self.session_config.get("default_output_dir", str(get_user_downloads_folder()))
        
        new_path_str, _ = QFileDialog.getOpenFileName(
            self, "Select Album Art Image", initial_dir_str, image_file_types
        )

        if new_path_str:
            logger.info(f"[GUI] New current album art path selected: {new_path_str}")
            self.current_album_art_path = new_path_str
            self._load_and_display_current_album_art() # This will refresh the display and make it visible
        else:
            logger.info("[GUI] Set current album art path cancelled by user.")
            # If it was hidden and user cancelled, ensure it stays hidden or visible based on if a path is still set
            if not self.current_album_art_path:
                 self.current_art_display_container.setVisible(False)
            else: # Path still exists, ensure it's visible
                 self.current_art_display_container.setVisible(True)


    def _handle_current_art_close_widget(self):
        logger.info("[GUI] Closing current album art display.")
        if self.current_art_display_container:
            self.current_art_display_container.setVisible(False)
        
        self.current_album_art_path = None

        # Reset the displayed image to placeholder and text
        if self.current_art_image_widget:
            raw_placeholder = QPixmap(self.CURRENT_ART_IMAGE_SIZE)
            raw_placeholder.fill(QColor(220, 220, 220))
            rounded_placeholder = ImageFrame._create_rounded_pixmap(raw_placeholder, THUMBNAIL_CORNER_RADIUS)
            self.current_art_image_widget.set_display_pixmap(rounded_placeholder)
        if self.current_art_dimensions_label:
            self.current_art_dimensions_label.setText("No Art Loaded")


    def _handle_current_art_open_image(self):
        if self.current_album_art_path:
            logger.info(f"[GUI] Current art clicked: {self.current_album_art_path}. Opening image file.")
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.current_album_art_path))
        else:
            logger.warning("[GUI] Current art clicked to open image, but path is not set.")

    def _handle_current_art_open_folder(self):
        if self.current_album_art_path:
            parent_dir = str(pathlib.Path(self.current_album_art_path).parent)
            logger.info(f"[GUI] Current art context menu: Opening parent folder: {parent_dir}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(parent_dir))
        else:
            logger.warning("[GUI] Current art context menu to open folder, but path is not set.")

    @Slot(pathlib.Path, bool, str)
    def _handle_download_completed(self, path: pathlib.Path, success: bool, error_msg: str = ""):
        logger.debug(f"[GUI] _handle_download_completed called for path: {path}, success: {success}")
        
        download_op_key = str(path)
        notification_id = self.active_download_notifications.pop(download_op_key, None)

        if not self.isVisible():
            if notification_id: # Still try to dismiss it if window closed mid-download
                self.notification_manager.dismiss_notification(notification_id, immediate=True)
            return

        if success:
            if notification_id:
                final_text = f"Saved to:\n{path.name}"
                self.notification_manager.update_notification(notification_id, final_text)
                # Make it dismissable and timed out
                widget = self.notification_manager._notifications.get(notification_id)
                if widget:
                    widget.set_dismissable_by_user(True)
                    widget.set_can_timeout(True)
                    widget.start_timeout(DEFAULT_TIMEOUT_MS * 2) # Longer timeout for success message
            
            if self.session_config.get("exit_on_download", False):
                logger.info("exit_on_download is True - shutting down application")
                # Allow notification to show briefly if possible
                QTimer.singleShot(1000, self.close)
            
        else: # Download failed
            if notification_id:
                final_text = f"Failed to download:\n{path.name}\nError: {error_msg}"
                self.notification_manager.update_notification(notification_id, final_text)
                # Make it dismissable and timed out with a longer duration for error
                widget = self.notification_manager._notifications.get(notification_id)
                if widget:
                    widget.set_dismissable_by_user(True)
                    widget.set_can_timeout(True)
                    widget.start_timeout(DEFAULT_TIMEOUT_MS * 3) # Even longer for errors
            else: # Should not happen if notification was shown
                QMessageBox.critical(self, "Download Failed", f"Failed to download image.\nError: {error_msg}")


    @Slot()
    def _clear_current_download_worker(self):
        self.current_download_worker = None
        logger.debug("[GUI] Cleared current_download_worker reference.")

    @Slot()
    def _clear_download_thread(self):
        if self.download_thread and not self.download_thread.isRunning(): 
            self.download_thread = None
            logger.debug("[GUI] Cleared download_thread reference.")
        elif self.download_thread:
             logger.debug("[GUI] _clear_download_thread called, but thread might still be marked as running or parented elsewhere.")
