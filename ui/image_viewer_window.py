# ui/image_viewer_window.py
import logging
import pathlib
from typing import Callable, Optional, Union

from PySide6.QtCore import QByteArray, QEvent, QPoint, QRect, QSize, QTimer, Qt, Slot, QThread
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QLabel, QLayout, QMainWindow,
                             QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                             QVBoxLayout, QWidget)

from utils.helpers import get_bundle_dir
from services.image_fetcher import ImageFetcher, ImageFetcherWorker
from services.models import PotentialImage, ImageResult

logger = logging.getLogger(__name__)


class ImageViewerWindow(QDialog):
    def __init__(self, parent_widget: QWidget, image_data: Union[PotentialImage, ImageResult],
                 image_fetcher: ImageFetcher,
                 on_done_callback: Optional[Callable[[], None]] = None,
                 on_download_callback: Optional[Callable[[Union[PotentialImage, ImageResult], Optional[bytes]], None]] = None,
                 on_open_in_browser_callback: Optional[Callable[[str], None]] = None):
        # Initialize as a parentless dialog to make it a true top-level window
        super().__init__(None) 
        # Set window flags to behave as a standard window, not an auxiliary dialog
        # Qt.Window implies it's a top-level window and will get standard window decorations.
        # It also makes it non-modal with respect to other application windows.
        self.setWindowFlags(Qt.Window)

        self.image_fetcher = image_fetcher
        self.image_data = image_data
        self.raw_image_bytes: Optional[bytes] = None # Stores the raw bytes of the full image
        self.q_pixmap: Optional[QPixmap] = None

        self.on_done_callback = on_done_callback
        self.on_download_callback = on_download_callback
        self.on_open_in_browser_callback = on_open_in_browser_callback

        self.fetch_thread: Optional[QThread] = None
        self.fetch_worker: Optional[ImageFetcherWorker] = None

        title_text = f"Image Viewer - {pathlib.Path(image_data.full_image_url).name}" if image_data.full_image_url else "Image Viewer"
        self.setWindowTitle(title_text)
        # self.setStyleSheet() 

        # self.setWindowModality(Qt.NonModal) # This is now redundant due to Qt.Window on a parentless widget

        self._setup_ui()

        self.setAttribute(Qt.WA_DeleteOnClose)

        # Add Ctrl+S shortcut for saving
        save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        save_shortcut.activated.connect(self._on_download_click)
        # Make sure the shortcut is only active when the window has focus
        save_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        
        # Add Ctrl+W shortcut to close
        close_shortcut = QShortcut(QKeySequence("Ctrl+W"), self)
        close_shortcut.activated.connect(self.close)
        close_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)


        if not self.image_data.full_image_url:
            logger.warning("FullImageViewer: No full_image_url provided.")
            QTimer.singleShot(0, lambda: self._handle_fetch_error("No image URL available."))
            return

        logger.debug(f"FullImageViewer: Preparing to fetch image from URL: {self.image_data.full_image_url}")
        self.image_label.setText("Loading image...")
        self.image_label.setAlignment(Qt.AlignCenter)

        QTimer.singleShot(50, self._start_fetch_image_qthread) 

    def _setup_ui(self):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0,0,0,0)
        self.layout.setSpacing(0) 

        self.image_label = QLabel(self)
        self.image_label.setAutoFillBackground(False) 
        self.image_label.setStyleSheet("background-color: transparent; border: none; padding: 0px;") 
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.layout.addWidget(self.image_label)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_right_click)

        self.resize(200, 150)

    def _start_fetch_image_qthread(self):
        if not self.isVisible(): return 

        self.fetch_thread = QThread(self) 
        self.fetch_worker = ImageFetcherWorker(self.image_data.full_image_url, self.image_fetcher)
        self.fetch_worker.moveToThread(self.fetch_thread)

        self.fetch_worker.image_data_ready.connect(self._on_image_data_ready)
        self.fetch_worker.error_occurred.connect(self._on_fetch_error)

        self.fetch_thread.started.connect(self.fetch_worker.run)
        self.fetch_worker.image_data_ready.connect(self.fetch_thread.quit)
        self.fetch_worker.error_occurred.connect(self.fetch_thread.quit)
        
        self.fetch_thread.finished.connect(self.fetch_worker.deleteLater)
        self.fetch_thread.finished.connect(self.fetch_thread.deleteLater)
        self.fetch_thread.finished.connect(self._clear_fetch_references) 

        self.fetch_thread.start()
        logger.debug(f"FullImageViewer: QThread started for {self.image_data.full_image_url}")

    @Slot(QByteArray)
    def _on_image_data_ready(self, image_qbytearray: QByteArray):
        if not self.isVisible():
            logger.debug("FullImageViewer: Image data ready, but window no longer visible.")
            if self.on_done_callback: self.on_done_callback()
            return
        
        image_bytes_data = image_qbytearray.data()
        logger.debug(f"FullImageViewer: Image data received ({len(image_bytes_data)} bytes) for {self.image_data.full_image_url}")
        self.process_and_display_image(image_bytes_data) 

    @Slot(str)
    def _on_fetch_error(self, error_message: str):
        if not self.isVisible():
            logger.debug(f"FullImageViewer: Fetch error '{error_message}', but window no longer visible.")
            if self.on_done_callback: self.on_done_callback() 
            return

        logger.error(f"FullImageViewer: Error fetching/processing image: {error_message}")
        self.show_error(error_message) 

    @Slot()
    def _clear_fetch_references(self):
        logger.debug("FullImageViewer: Clearing fetch thread and worker references.")
        self.fetch_worker = None
        self.fetch_thread = None 

    def process_and_display_image(self, image_bytes_data: bytes):
        if not self.isVisible(): return
        
        self.raw_image_bytes = image_bytes_data # Store raw bytes
        
        try:
            self.q_pixmap = QPixmap()
            if not self.q_pixmap.loadFromData(self.raw_image_bytes):
                logger.error(f"FullImageViewer: QPixmap.loadFromData failed for {self.image_data.full_image_url}")
                self.show_error("Invalid or corrupted image data (QPixmap load failed).")
                return
            if self.q_pixmap.isNull():
                logger.error(f"FullImageViewer: QPixmap is null after loading for {self.image_data.full_image_url}")
                self.show_error("Invalid or corrupted image data (QPixmap is null).")
                return
        except Exception as e: # Should be less common now, but keep for unexpected issues
            logger.error(f"Unexpected error loading QPixmap: {e}", exc_info=True)
            self.show_error("An unexpected error occurred while loading the image.")
            return

        self.image_label.setText("") 
        self.display_image_resized_to_fit_screen()
        
        # Do NOT call on_done_callback here. 
        # It should only be called when the window is actually closing (handled in closeEvent).
        # if self.on_done_callback: 
        #     self.on_done_callback()

    def _get_scaled_pixmap_for_display(self, target_logical_size: QSize) -> Optional[QPixmap]:
        if not self.q_pixmap or self.q_pixmap.isNull():
            logger.debug("_get_scaled_pixmap_for_display: self.q_pixmap is null or invalid.")
            return None
        
        if target_logical_size.width() <= 0 or target_logical_size.height() <= 0:
            logger.debug(f"_get_scaled_pixmap_for_display: Target logical size is invalid {target_logical_size}, returning empty pixmap.")
            return QPixmap() # Return an empty (null) pixmap

        dpr = self.devicePixelRatioF()
        if dpr <= 0: # Should not happen with valid screens
            logger.warning(f"Invalid devicePixelRatioF: {dpr}. Defaulting to 1.0.")
            dpr = 1.0
            
        physical_target_width = round(target_logical_size.width() * dpr)
        physical_target_height = round(target_logical_size.height() * dpr)
        
        if target_logical_size.width() > 0 and physical_target_width <= 0:
            physical_target_width = 1
        if target_logical_size.height() > 0 and physical_target_height <= 0:
            physical_target_height = 1

        if physical_target_width <= 0 or physical_target_height <= 0:
            logger.warning(
                f"Cannot scale to non-positive physical size: {physical_target_width}x{physical_target_height} "
                f"from logical size {target_logical_size} with DPR {dpr}. Returning empty pixmap."
            )
            return QPixmap()

        physical_target_qsize = QSize(physical_target_width, physical_target_height)

        original_image = self.q_pixmap.toImage()
        if original_image.isNull():
            logger.error("Failed to convert QPixmap to QImage for scaling. Falling back to QPixmap.scaled.")
            return self.q_pixmap.scaled(target_logical_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        scaled_image = original_image.scaled(physical_target_qsize, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if scaled_image.isNull():
            logger.error("QImage.scaled resulted in a null image. Falling back to QPixmap.scaled.")
            return self.q_pixmap.scaled(target_logical_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        final_pixmap = QPixmap.fromImage(scaled_image)
        if final_pixmap.isNull():
            logger.error("QPixmap.fromImage(scaled_image) resulted in a null pixmap. No pixmap to display.")
            return QPixmap() # Return an empty pixmap
            
        final_pixmap.setDevicePixelRatio(dpr)
        
        return final_pixmap

    def display_image_resized_to_fit_screen(self):
        if not self.isVisible() or not self.q_pixmap: return

        screen_geometry = QApplication.primaryScreen().availableGeometry()
        max_win_w = screen_geometry.width()
        max_win_h = screen_geometry.height()

        img_w = self.q_pixmap.width()
        img_h = self.q_pixmap.height()

        if img_w == 0 or img_h == 0:
            self.show_error("Image has zero dimensions.") 
            return

        new_size = QSize(img_w, img_h)
        new_size.scale(int(max_win_w), int(max_win_h), Qt.KeepAspectRatio)

        self.resize(new_size)
        # Use the helper method to get a high-quality scaled pixmap
        # Pass new_size (the target logical size for the window/label)
        scaled_pixmap = self._get_scaled_pixmap_for_display(new_size)
        if scaled_pixmap and not scaled_pixmap.isNull():
            self.image_label.setPixmap(scaled_pixmap)
        else:
            # Fallback or error handling if scaled_pixmap is None or null
            logger.warning("display_image_resized_to_fit_screen: Could not generate scaled pixmap.")
            # Optionally clear the label or show a placeholder
            self.image_label.setPixmap(QPixmap())


        self.move(screen_geometry.center() - self.rect().center())

        self.raise_() 
        self.activateWindow() 
        QTimer.singleShot(0, lambda: self.setFocus(Qt.OtherFocusReason) if self.isVisible() else None)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.q_pixmap and not self.q_pixmap.isNull() and self.image_label.isVisible():
            # Use the helper method to get a high-quality scaled pixmap
            # self.image_label.size() gives the current logical size of the label
            scaled_pixmap = self._get_scaled_pixmap_for_display(self.image_label.size())
            if scaled_pixmap and not scaled_pixmap.isNull():
                self.image_label.setPixmap(scaled_pixmap)
            else:
                # Fallback or error handling if scaled_pixmap is None or null
                logger.warning("resizeEvent: Could not generate scaled pixmap.")
                # Optionally clear the label or show a placeholder
                self.image_label.setPixmap(QPixmap())


    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        logger.debug("FullImageViewer.closeEvent called.")
        if self.fetch_thread and self.fetch_thread.isRunning():
            logger.info("FullImageViewer closing: Requesting fetch thread to quit.")
            if self.fetch_worker:
                self.fetch_worker.cancel() 
            self.fetch_thread.quit()
            if not self.fetch_thread.wait(100): 
                logger.warning("FullImageViewer: Fetch thread did not finish quickly on close. May be terminated.")

        if self.on_done_callback:
            self.on_done_callback() 
        super().closeEvent(event)


    def show_error(self, message: str):
        if not self.isVisible(): return
        QMessageBox.critical(self, "Error", message)
        self.close() 

    def _on_right_click(self, pos):
        menu = QMenu(self)
        if self.on_download_callback:
            download_action = menu.addAction("Download")
            download_action.triggered.connect(self._on_download_click)
        if self.on_open_in_browser_callback and self.image_data and self.image_data.full_image_url:
            open_browser_action = menu.addAction("Open In Browser")
            open_browser_action.triggered.connect(self._on_open_in_browser_click)

        if menu.actions():
            menu.popup(self.mapToGlobal(pos))

    def _on_download_click(self):
        if self.image_data and self.on_download_callback:
            # Use the stored raw image bytes directly for preloading.
            # This ensures the preloaded data is exactly what was fetched.
            preloaded_bytes = self.raw_image_bytes
            
            self.on_download_callback(self.image_data, preloaded_bytes)
            self.close() 

    def _on_open_in_browser_click(self):
        if self.image_data and self.image_data.full_image_url and self.on_open_in_browser_callback:
            self.on_open_in_browser_callback(self.image_data.full_image_url)
