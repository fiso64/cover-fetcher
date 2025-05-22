# ui/components.py
import sys
import uuid # For generating unique request_ids
import pathlib
import shiboken6
import logging
import sys
from typing import List, Callable, Optional, Dict, Union, Any, Tuple, TYPE_CHECKING

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QScrollArea, QSizePolicy, QFrame, QDialog, QMenu, QMessageBox, QGroupBox,
    QCheckBox, QLayout, QLayoutItem, QStyle
)
from PySide6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QFont, QAction, QDesktopServices, QPainterPath,
    QPaintEvent,QWheelEvent, QMouseEvent, QDrag,QDragEnterEvent,QDragMoveEvent,QDragLeaveEvent, QDropEvent, QMovie,
    QKeySequence, QShortcut
)
from PySide6.QtCore import (
    Qt, QSize, Signal, QTimer, QMetaObject, QUrl, QByteArray, Slot, Q_ARG,
    QObject, QThread, QEvent, QMimeData, QPoint, QRect, QPropertyAnimation, QEasingCurve
)

from utils.helpers import get_bundle_dir
from services.models import PotentialImage, ImageResult, AlbumCandidate
from services.image_fetcher import ImageFetcher, ImageFetcherWorker
from .image_viewer_window import ImageViewerWindow

IMAGE_SPACING = 20
THUMBNAIL_CORNER_RADIUS = 6

DEFAULT_FONT_FAMILY = "Arial"
FONT_SIZE_NORMAL = 9
FONT_SIZE_LARGE = 10
FONT_SIZE_SMALL = 8

logger = logging.getLogger(__name__)

def get_font(size=FONT_SIZE_NORMAL, weight: QFont.Weight = QFont.Weight.Normal, italic=False):
    font = QFont(DEFAULT_FONT_FAMILY, size)
    font.setWeight(weight)
    font.setItalic(italic)
    return font


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, hspacing=-1, vspacing=-1):
        super().__init__(parent)
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)
        else:
            # Default margins if no parent to derive from (e.g. QLayout.setContentsMargins)
            # This path might not be hit if always parented.
            self.setContentsMargins(margin, margin, margin, margin)

        self._hspacing = hspacing
        self._vspacing = vspacing
        self._items: List[QLayoutItem] = []

    def __del__(self):
        if not shiboken6.isValid(self):
            self._items.clear()
            return
        item = self.takeAt(0)
        while item:
            del item 
            item = self.takeAt(0)

    def addItem(self, item: QLayoutItem):
        self._items.append(item)
        self.invalidate()

    def horizontalSpacing(self) -> int:
        if self._hspacing >= 0:
            return self._hspacing
        else:
            return self.smartSpacing(QStyle.PixelMetric.PM_LayoutHorizontalSpacing)

    def verticalSpacing(self) -> int:
        if self._vspacing >= 0:
            return self._vspacing
        else:
            return self.smartSpacing(QStyle.PixelMetric.PM_LayoutVerticalSpacing)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:
        if 0 <= index < len(self._items):
            # Only call invalidate if the C++ object still exists.
            if shiboken6.isValid(self):
                self.invalidate()
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientations(Qt.Orientation(0)) # Not expanding

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        height = self._doLayout(QRect(0, 0, width, 0), True)
        return height

    def setGeometry(self, rect: QRect):
        super().setGeometry(rect)
        self._doLayout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size
    
    def setSpacing(self, spacing: int) -> None:
        """Sets both horizontal and vertical spacing."""
        self._hspacing = spacing
        self._vspacing = spacing
        self.invalidate()

    def _doLayout(self, rect: QRect, testOnly: bool) -> int:
        margins = self.contentsMargins()
        effectiveRect = rect.adjusted(+margins.left(), +margins.top(), -margins.right(), -margins.bottom())
        x = effectiveRect.x()
        y = effectiveRect.y()
        lineHeight = 0

        for item in self._items:
            widget = item.widget()
            h_space = self.horizontalSpacing()
            if h_space == -1 and widget: # Check widget exists
                h_space = widget.style().layoutSpacing(QSizePolicy.ControlType.PushButton, QSizePolicy.ControlType.PushButton, Qt.Horizontal)
            elif h_space == -1: # Fallback if no widget (e.g. custom QLayoutItem)
                h_space = 0

            v_space = self.verticalSpacing()
            if v_space == -1 and widget: # Check widget exists
                v_space = widget.style().layoutSpacing(QSizePolicy.ControlType.PushButton, QSizePolicy.ControlType.PushButton, Qt.Vertical)
            elif v_space == -1:
                v_space = 0
            
            item_size_hint = item.sizeHint()
            nextX = x + item_size_hint.width() + h_space
            
            # Wrap condition: if the item (even if it's the first on the line) exceeds available width
            if (nextX - h_space > effectiveRect.right() and x > effectiveRect.x()) or \
               (item_size_hint.width() > effectiveRect.width() and x == effectiveRect.x()): # First item wider than rect
                if x > effectiveRect.x(): # Only wrap if not the first item on a new line already
                    x = effectiveRect.x()
                    y = y + lineHeight + v_space
                    nextX = x + item_size_hint.width() + h_space
                lineHeight = 0


            if not testOnly:
                item.setGeometry(QRect(QPoint(x, y), item_size_hint))

            x = nextX
            lineHeight = max(lineHeight, item_size_hint.height())

        return y + lineHeight - effectiveRect.y() # Total height used without bottom margin yet
        # The original C++ example adds margins at the end of heightForWidth calculation based on this return.
        # For heightForWidth, the final height should include top/bottom margins.
        # Let's adjust: heightForWidth should return (y + lineHeight - effectiveRect.y()) + margins.top() + margins.bottom()
        # However, _doLayout returns height used *within* effectiveRect.
        # So, if rect height is 0 (testOnly for heightForWidth), this is correct.
        # The caller (heightForWidth) should handle adding margins if needed, but usually it's about content height.

    def smartSpacing(self, pm: QStyle.PixelMetric) -> int:
        parent = self.parent()
        if not parent:
            return -1
        
        if isinstance(parent, QWidget): # Check if parent is QWidget
            return parent.style().pixelMetric(pm, None, parent)
        elif isinstance(parent, QLayout): # Check if parent is QLayout
            return parent.spacing()
        return -1


class RoundedImageDisplayWidget(QWidget):
    def __init__(self, thumbnail_render_size: QSize, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._pixmap_to_draw: Optional[QPixmap] = None
        self._current_render_size = thumbnail_render_size
        self.setFixedSize(self._current_render_size)
        self.setAutoFillBackground(False) # Crucial: this widget won't fill its own background

    def set_display_pixmap(self, pixmap: Optional[QPixmap]):
        # Expects pixmap to be already rounded and scaled to THUMBNAIL_MAX_SIZE
        self._pixmap_to_draw = pixmap
        self.update() # Request a repaint

    def paintEvent(self, event: QPaintEvent):
        if self._pixmap_to_draw and not self._pixmap_to_draw.isNull():
            painter = QPainter(self)
            # The pixmap is already rounded and scaled, just draw it.
            painter.drawPixmap(0, 0, self._pixmap_to_draw)
            # No need to call super().paintEvent(event) if we've handled all painting.
        # If no pixmap, it will be transparent due to setAutoFillBackground(False)
        # and the parent ImageFrame's QSS making it transparent.

    def sizeHint(self) -> QSize: # Good practice, though setFixedSize is used
        return self._current_render_size
    
    def update_render_size(self, new_size: QSize):
        self._current_render_size = new_size
        self.setFixedSize(self._current_render_size)
        # Repaint might be needed if content needs rescaling based on new size,
        # but set_display_pixmap already takes care of providing a pre-scaled pixmap.
        self.update()


class ImageFrame(QWidget):
    @staticmethod
    def _create_rounded_pixmap(source_pixmap: QPixmap, radius: int) -> QPixmap:
        if source_pixmap.isNull():
            return QPixmap()
        # Work with QImage for robust alpha channel handling
        source_image = source_pixmap.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
        img_size = source_image.size()

        # Create the target QImage with an alpha channel, filled transparent
        result_image = QImage(img_size, QImage.Format_ARGB32_Premultiplied)
        result_image.fill(Qt.transparent)

        # Painter to draw on the result_image
        painter = QPainter(result_image)
        painter.setRenderHint(QPainter.Antialiasing, True) # Enable antialiasing for the path
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True) # For drawing the source image

        # Define the rounded rectangle path
        path = QPainterPath()
        path.addRoundedRect(result_image.rect(), radius, radius)

        # Set the clip path: only areas within this path will be painted on
        painter.setClipPath(path)

        # Draw the source image onto the result_image.
        # Regions outside the clip path in result_image will remain transparent.
        painter.drawImage(0, 0, source_image)
        painter.end()

        return QPixmap.fromImage(result_image)

    doubleClicked = Signal(object, QByteArray) # PotentialImage or ImageResult, Optional[bytes]
    # Signal for when the full image viewer (single click) is done loading/closed
    viewerOperationCompleted = Signal()


    def __init__(self, parent_widget: QWidget,
                 initial_image_data: Union[PotentialImage, ImageResult],
                 on_double_click_callback: Callable[[Union[PotentialImage, ImageResult], Optional[bytes]], None], # Kept as callback for now
                 image_fetcher: ImageFetcher,
                 thumbnail_display_dimension: int): # New parameter for square thumbnail side length
        super().__init__(parent_widget)
        self.thumbnail_render_size = QSize(thumbnail_display_dimension, thumbnail_display_dimension)
        self.setAutoFillBackground(False) # <<< Make ImageFrame itself not fill its background
        self.setObjectName("ImageFrame") # For QSS styling
        self.setCursor(Qt.PointingHandCursor)

        self.image_fetcher = image_fetcher # This is the global, thread-safe ImageFetcher
        self.initial_data = initial_image_data
        self.resolved_image_result: Optional[ImageResult] = None
        if isinstance(initial_image_data, ImageResult):
            self.resolved_image_result = initial_image_data

        # Connect the doubleClicked signal to the passed callback
        self.doubleClicked.connect(lambda data, qbyte_arr: on_double_click_callback(data, qbyte_arr.data() if qbyte_arr else None))

        self.q_pixmap: Optional[QPixmap] = None
        # self.current_request_id is no longer needed
        self.tried_full_image_as_thumb = False
        self.full_image_bytes_if_displayed: Optional[bytes] = None # Stored as raw bytes
        self.current_thumb_url_attempted: Optional[str] = None # URL for the current_request_id
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.timeout.connect(self._perform_single_click_action)
        self._menu_active_flag = False # Helps differentiate clicks if menu is open
        self._dismissing_own_menu = False # Flag to track if current click is dismissing our own menu

        self.active_viewer: Optional[ImageViewerWindow] = None
        self.viewerOperationCompleted.connect(self._clear_active_viewer)

        # Placeholder related members will be initialized in _setup_ui
        self.rounded_placeholder_pixmap_for_display: Optional[QPixmap] = None
        self.current_displayed_pixmap_is_placeholder: bool = True
        self._is_waiting_for_resolution_for_filter: bool = False
        self._has_ever_attempted_thumbnail_load: bool = False


        self._setup_ui()
        self._update_display_text()
        self._update_tooltip() # Add tooltip

        # The ImageFetcher will call our specific slots directly, so no global signal connections here.

        if not self.resolved_image_result:
            self.load_thumbnail_from_url(self.initial_data.thumbnail_url)
        else:
            self.load_thumbnail_from_url(self.resolved_image_result.thumbnail_url,
                                         getattr(self.resolved_image_result, 'thumbnail_data', None))

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0) # Padding is on the frame itself
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter) # Align content to top and center horizontally

        self.image_widget = RoundedImageDisplayWidget(self.thumbnail_render_size, self) # NEW

        # Create and set the rounded placeholder
        raw_placeholder_pixmap = QPixmap(self.thumbnail_render_size) # Use dynamic size
        raw_placeholder_pixmap.fill(QColor(230, 230, 230))
        self.rounded_placeholder_pixmap_for_display = ImageFrame._create_rounded_pixmap(raw_placeholder_pixmap, THUMBNAIL_CORNER_RADIUS)
        
        self.image_widget.set_display_pixmap(self.rounded_placeholder_pixmap_for_display) # NEW
        
        layout.addWidget(self.image_widget, 0, Qt.AlignCenter) # NEW, ensure centered

        self.overlay_label = QLabel("Loading...", self.image_widget) # NEW: Child of image_widget
        self.overlay_label.setObjectName("OverlayLabel") # For QSS
        self.overlay_label.setFont(get_font(FONT_SIZE_SMALL, italic=True))
        self.overlay_label.setAlignment(Qt.AlignCenter)
        self.overlay_label.adjustSize()
        self.overlay_label.move(
            (self.image_widget.width() - self.overlay_label.width()) // 2,
            (self.image_widget.height() - self.overlay_label.height()) // 2
        )
        self.overlay_label.setVisible(True)


        self.label_dimensions = QLabel("Dimensions: ...")
        self.label_dimensions.setFont(get_font(FONT_SIZE_SMALL))
        self.label_dimensions.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.label_dimensions, 0, Qt.AlignCenter)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_right_click)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._menu_active_flag: # If OUR menu is active and user left-clicks on US
                 self._dismissing_own_menu = True # This click sequence is intended to dismiss our menu
                 # We don't accept or return early, let Qt handle menu dismissal naturally.
                 # The flag _dismissing_own_menu will prevent single-click in mouseReleaseEvent.
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._dismissing_own_menu:
                self._dismissing_own_menu = False # Reset flag, action was menu dismissal
            elif not self._menu_active_flag: # If no menu was involved or it was already handled
                if self.rect().contains(event.pos()):
                    # Start timer for single click. If double click happens, timer will be cancelled.
                    self._single_click_timer.start(QApplication.doubleClickInterval() / 2)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._single_click_timer.stop() # Cancel single click action
            data_to_pass = self.resolved_image_result if self.resolved_image_result else self.initial_data
            qbyte_array_data = QByteArray(self.full_image_bytes_if_displayed) if self.full_image_bytes_if_displayed else QByteArray()
            self.doubleClicked.emit(data_to_pass, qbyte_array_data)
        super().mouseDoubleClickEvent(event)

    def _clear_active_viewer(self):
        # This slot is called when the ImageViewerWindow signals it's done
        # (via self.viewerOperationCompleted.emit()).
        if self.active_viewer:
            logger.debug(f"Clearing reference to active viewer: {self.active_viewer}")
            self.active_viewer = None
        else:
            # This might happen if _perform_single_click_action clears it due to immediate re-click
            logger.debug("Attempted to clear active viewer, but no active viewer was (or is currently) set.")

    def _create_viewer_loading_dialog(self) -> Optional[QDialog]:
        return create_modal_progress_dialog(
            parent_window=self.window(), 
            title="Loading Image",
            message="Loading image, please wait...",
            is_resizable=False
        )

    def _perform_single_click_action(self):
        data_to_pass = self.resolved_image_result if self.resolved_image_result else self.initial_data

        if not data_to_pass or not data_to_pass.full_image_url:
            logger.info("Single click: No image data or full_image_url to display.")
            # Ensure any existing viewer is closed if this action effectively means "nothing to show"
            if self.active_viewer:
                self.active_viewer.close() # This will trigger its on_done_callback path
            else: # If no viewer, emit directly
                self.viewerOperationCompleted.emit()
            return

        # If there's an existing viewer from *this* ImageFrame, close it first.
        # This prevents multiple viewers from the same image frame appearing.
        if self.active_viewer:
            logger.info(f"Closing previously active viewer for this ImageFrame: {self.active_viewer}")
            # Calling close() will trigger its closeEvent, which eventually calls on_done_callback,
            # leading to _clear_active_viewer.
            # It's important that _clear_active_viewer doesn't try to operate on a stale reference
            # if close() somehow runs synchronously and clears it before we reassign.
            # So, we can clear our reference here *after* calling close.
            # The WA_DeleteOnClose on the viewer handles Qt-side cleanup.
            current_viewer_to_close = self.active_viewer
            self.active_viewer = None # Clear our primary reference
            current_viewer_to_close.close()


        logger.info(f"Single click detected for image: {data_to_pass.full_image_url}")

        # Create and store the new viewer instance in self.active_viewer
        self.active_viewer = ImageViewerWindow(
            self.window(), # This argument is no longer used for parenting in ImageViewerWindow
            data_to_pass,
            self.image_fetcher,
            on_done_callback=lambda: self.viewerOperationCompleted.emit(), 
            on_download_callback=lambda img_data, preloaded: self.doubleClicked.emit(img_data, QByteArray(preloaded) if preloaded else QByteArray()),
            on_open_in_browser_callback=lambda url: QDesktopServices.openUrl(QUrl(url))
        )
        self.active_viewer.show()


    def _on_right_click(self, pos):
        self._menu_active_flag = True
        self._dismissing_own_menu = False # Reset this flag each time a new menu is shown

        menu = QMenu(self)

        view_action = menu.addAction("View")
        view_action.triggered.connect(self._perform_single_click_action) # Calls the same viewer logic
        
        download_action = menu.addAction("Download")
        download_action.triggered.connect(self._on_download_click)

        open_browser_action = menu.addAction("Open In Browser")
        open_browser_action.triggered.connect(self._on_open_in_browser_click)
        
        menu.popup(self.mapToGlobal(pos))
        menu.aboutToHide.connect(self._handle_menu_about_to_hide) # Connect to new helper

    def _handle_menu_about_to_hide(self): # New helper method
        self._menu_active_flag = False
        self._dismissing_own_menu = False # Also reset this if menu is hidden by other means (e.g. Esc)

    def _on_download_click(self):
        data_to_pass = self.resolved_image_result if self.resolved_image_result else self.initial_data
        qbyte_array_data = QByteArray(self.full_image_bytes_if_displayed) if self.full_image_bytes_if_displayed else QByteArray()
        self.doubleClicked.emit(data_to_pass, qbyte_array_data)

    def _on_open_in_browser_click(self):
        url_to_open = self.initial_data.full_image_url
        if self.resolved_image_result:
            url_to_open = self.resolved_image_result.full_image_url
        if url_to_open:
            QDesktopServices.openUrl(QUrl(url_to_open))

    def _update_display_text(self):
        if not self.label_dimensions: return 

        display_data = self.resolved_image_result if self.resolved_image_result else self.initial_data
        type_text = ""
        if hasattr(display_data, 'original_type') and display_data.original_type:
            type_text = f" ({display_data.original_type})"

        if self.resolved_image_result:
            w = self.resolved_image_result.full_width
            h = self.resolved_image_result.full_height
            if w is not None and h is not None and w > 0 and h > 0:
                dimensions_text = f"{w}x{h}{type_text}"
            else:
                dimensions_text = f"Size: N/A{type_text}" 
            self.label_dimensions.setText(dimensions_text)
        else:
            pi_type_text = ""
            if hasattr(self.initial_data, 'original_type') and self.initial_data.original_type:
                 pi_type_text = f" ({self.initial_data.original_type})"
            self.label_dimensions.setText(f"Size: Pending{pi_type_text}")

    def update_with_resolved_details(self, image_result: ImageResult):
        self.resolved_image_result = image_result
        self._update_display_text()

        has_new_thumb_data = hasattr(image_result, 'thumbnail_data') and image_result.thumbnail_data

        if has_new_thumb_data:
            self.load_thumbnail_from_url(image_result.thumbnail_url, image_result.thumbnail_data)
        elif not self.q_pixmap: 
            self.load_thumbnail_from_url(image_result.thumbnail_url, None)
        
        self._update_tooltip() # Update tooltip with potentially new album/artist info

    def load_thumbnail_from_url(self, thumb_url: str, thumb_data: Optional[bytes] = None):
        # self.current_request_id and self.current_thumb_url_attempted are no longer needed here.

        if self.image_widget and self.image_widget.isVisible():
            if self.rounded_placeholder_pixmap_for_display:
                self.image_widget.set_display_pixmap(self.rounded_placeholder_pixmap_for_display)
            self.current_displayed_pixmap_is_placeholder = True

        if self.overlay_label and self.image_widget.isVisible():
            self.overlay_label.setText("Loading...")
            self.overlay_label.setVisible(True)
            self.overlay_label.adjustSize()
            self.overlay_label.move(
                (self.image_widget.width() - self.overlay_label.width()) // 2,
                (self.image_widget.height() - self.overlay_label.height()) // 2
            )

        if thumb_data:
            # If data is already provided (e.g., from resolved details), display it directly.
            # Pre-supplied thumb_data is assumed to be actual thumb, not full image attempt.
            QTimer.singleShot(0, lambda d=thumb_data, u=thumb_url: self._display_thumbnail_data(
                image_data=d,
                original_url_str=u or "pre_supplied_unknown_url",
                # store_if_full should be False for pre-supplied thumbnail_data
                store_if_full=False
            ))
        elif thumb_url:
            logger.debug(f"[ImageFrame] Requesting thumbnail for {thumb_url} via ImageFetcher.")
            QMetaObject.invokeMethod(
                self.image_fetcher,
                "request_image_data", # Slot name in ImageFetcher
                Qt.QueuedConnection,
                Q_ARG(str, thumb_url),
                Q_ARG(QObject, self),
                Q_ARG(str, "on_thumbnail_loaded"), # Success slot BASE NAME
                Q_ARG(str, "on_thumbnail_error"),   # Error slot BASE NAME
                Q_ARG(int, 30)  # timeout_sec
            )
        else:
            # No URL and no data
            if self.isVisible():
                 self._set_overlay_text_on_main_thread("No Thumb")

    @Slot(QUrl, QByteArray)
    def on_thumbnail_loaded(self, q_url: QUrl, image_data_qba: QByteArray):
        if not self.isVisible():
            return

        url_str = q_url.toString()
        logger.debug(f"[ImageFrame] Thumbnail data loaded for URL: {url_str}")

        image_data = image_data_qba.data()

        # Determine if this loaded image was an attempt to use the full image as a thumbnail
        is_known_full_image_url = False
        if self.resolved_image_result and url_str == self.resolved_image_result.full_image_url:
            is_known_full_image_url = True
        elif not self.resolved_image_result and self.initial_data and url_str == self.initial_data.full_image_url:
            is_known_full_image_url = True
        
        store_this_as_full_image_data = self.tried_full_image_as_thumb and is_known_full_image_url

        self._display_thumbnail_data(image_data, url_str, store_this_as_full_image_data)

    @Slot(QUrl, str)
    def on_thumbnail_error(self, q_url: QUrl, error_message: str):
        if not self.isVisible():
            return

        url_str = q_url.toString()
        logger.warning(f"[ImageFrame] Error fetching thumbnail from ImageFetcher for URL: {url_str}. Error: {error_message}")
        self._handle_thumb_load_error_ui("Thumb Err", url_str, error_message)

    def _display_thumbnail_data(self, image_data: bytes, original_url_str: str, store_if_full: bool = False):
        # request_id check is no longer needed here as callbacks are targeted.
        if not self.isVisible():
            return
        try:
            raw_pixmap = QPixmap()
            if not raw_pixmap.loadFromData(image_data):
                raise ValueError("QPixmap.loadFromData failed.")

            # Scale the pixmap
            scaled_pixmap = raw_pixmap.scaled(self.thumbnail_render_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

            if store_if_full:
                self.full_image_bytes_if_displayed = image_data
                logger.debug(f"[ImageFrame] Stored full image bytes for {original_url_str} as it was used for thumbnail.")
            else:
                # If we are not storing this as the full image,
                # and a full image wasn't already stored (from trying full as thumb), clear it.
                # This logic might need refinement: if full_image_bytes_if_displayed was set by a full image attempt,
                # and now a *different actual thumbnail* is loaded, full_image_bytes_if_displayed should be cleared.
                if not store_if_full: # If current display is NOT the full image
                     self.full_image_bytes_if_displayed = None


            self._update_image_on_ui(scaled_pixmap)

        except Exception as e:
            logger.warning(f"[ImageFrame] Error processing thumbnail data (from {original_url_str}): {e}")
            if self.isVisible():
                 # Pass the original_url_str for fallback logic, and a generic error for display
                 QTimer.singleShot(0, lambda: self._handle_thumb_load_error_ui("Bad Thumb", original_url_str, f"Processing error: {e}"))

    def _handle_thumb_load_error_ui(self, overlay_message: str, failed_url_str: str, error_from_fetcher: Optional[str] = None):
        if not self.isVisible():
            return

        logger.warning(f"[ImageFrame] Thumbnail load error for URL: {failed_url_str}. Overlay: '{overlay_message}'. Fetcher error: '{error_from_fetcher or 'N/A'}'")
        self.full_image_bytes_if_displayed = None # Clear any previously stored full image if thumb fails

        full_img_url_to_try = None
        if self.resolved_image_result:
            full_img_url_to_try = self.resolved_image_result.full_image_url
        elif isinstance(self.initial_data, (PotentialImage, ImageResult)): # Ensure initial_data is checked
            full_img_url_to_try = self.initial_data.full_image_url

        if not self.tried_full_image_as_thumb and \
            full_img_url_to_try and \
            full_img_url_to_try != failed_url_str: # Compare with the string form of failed_url
            logger.info(f"[ImageFrame] Thumbnail for '{failed_url_str}' failed. Attempting fallback to full image: '{full_img_url_to_try}'")
            self.tried_full_image_as_thumb = True # Mark that we are now trying the full image
            if self.overlay_label and self.overlay_label.isVisible():
                self.overlay_label.setText("Retrying...") # Or "Loading Full..."
                self.overlay_label.setVisible(True)
            # This will call ImageFetcher again for the full_img_url_to_try
            self.load_thumbnail_from_url(full_img_url_to_try, None)
        else:
            # Fallback failed or no fallback possible
            if self.image_widget and self.image_widget.isVisible():
                self._set_overlay_text_on_main_thread(overlay_message) # Display the provided overlay_message like "Thumb Err"


    def _update_image_on_ui(self, pixmap: QPixmap):
        if not self.isVisible() or not self.image_widget or not self.image_widget.isVisible(): return

        # The request_id check is no longer needed due to targeted callbacks
        self.q_pixmap = pixmap
        self.current_displayed_pixmap_is_placeholder = False

        rounded_display_pixmap = ImageFrame._create_rounded_pixmap(self.q_pixmap, THUMBNAIL_CORNER_RADIUS)
        self.image_widget.set_display_pixmap(rounded_display_pixmap)
        if self.overlay_label: self.overlay_label.setVisible(False)

    def _set_overlay_text_on_main_thread(self, text:str):
        if self.isVisible():
            QTimer.singleShot(0, lambda t=text: self._set_overlay_text(t))

    def _set_overlay_text(self, text: str):
        if not self.isVisible() or not self.image_widget or not self.overlay_label: return # NEW

        if not self.current_displayed_pixmap_is_placeholder or self.overlay_label.text() != text:
            if self.rounded_placeholder_pixmap_for_display: 
                self.image_widget.set_display_pixmap(self.rounded_placeholder_pixmap_for_display) # NEW
            self.q_pixmap = None 
            self.current_displayed_pixmap_is_placeholder = True

        self.overlay_label.setText(text)
        self.overlay_label.adjustSize() 
        self.overlay_label.move(
            (self.image_widget.width() - self.overlay_label.width()) // 2, # NEW
            (self.image_widget.height() - self.overlay_label.height()) // 2 # NEW
        )
        self.overlay_label.setVisible(True)

    def _update_tooltip(self):
        album_name_str = "Unknown Album"
        artist_name_str = "Unknown Artist"

        data_for_tooltip = self.resolved_image_result if self.resolved_image_result else self.initial_data

        if isinstance(data_for_tooltip, ImageResult):
            # ImageResult has properties that correctly fetch from its source_candidate
            album_name_str = data_for_tooltip.album_name if data_for_tooltip.album_name else "Unknown Album"
            artist_name_str = data_for_tooltip.artist_name if data_for_tooltip.artist_name else "Unknown Artist"
        elif isinstance(data_for_tooltip, PotentialImage):
            if data_for_tooltip.source_candidate:
                album_name_str = data_for_tooltip.source_candidate.album_name if data_for_tooltip.source_candidate.album_name else "Unknown Album"
                artist_name_str = data_for_tooltip.source_candidate.artist_name if data_for_tooltip.source_candidate.artist_name else "Unknown Artist"
        
        self.setToolTip(f"{album_name_str}\n{artist_name_str}")

    @Slot()
    def ensure_thumbnail_loaded(self):
        # This method is called when the frame should definitely try to load its image,
        # e.g., it became visible after being hidden due to a filter, or needs a reload.
        # The caller (e.g., ServiceImageSection) is responsible for calling this method
        # only when the frame is intended to be visible.

        # If the image is already successfully loaded and displayed (not a placeholder), do nothing.
        if self.q_pixmap and not self.current_displayed_pixmap_is_placeholder:
            logger.debug(f"[ImageFrame {self.initial_data.identifier_for_logging() if hasattr(self.initial_data, 'identifier_for_logging') else 'Unknown'}] Ensure_thumbnail_loaded: Image already present and displayed. Doing nothing.")
            # Ensure overlay is consistently hidden if image is present.
            if self.overlay_label and self.overlay_label.isVisible():
                 self.overlay_label.setVisible(False)
            return
        
        # At this point, a load or reload is necessary because the frame either has no pixmap
        # or is displaying a placeholder.
        logger.debug(f"[ImageFrame {self.initial_data.identifier_for_logging() if hasattr(self.initial_data, 'identifier_for_logging') else 'Unknown'}] Ensuring thumbnail load (image content is missing/placeholder).")

        # Get the necessary URL and data
        target_url: Optional[str] = None
        target_data: Optional[bytes] = None
        data_source = self.resolved_image_result if self.resolved_image_result else self.initial_data
        
        if data_source:
            target_url = data_source.thumbnail_url
            if isinstance(data_source, ImageResult): # ImageResult might have pre-loaded thumb data
                target_data = getattr(data_source, 'thumbnail_data', None)
        
        if target_url or target_data:
            # Call the main public loading function. It will handle all internal state
            # like _is_waiting_for_resolution_for_filter and _has_ever_attempted_thumbnail_load.
            self.load_thumbnail_from_url(target_url, target_data)
        else:
            logger.warning(f"[ImageFrame {self.initial_data.identifier_for_logging() if hasattr(self.initial_data, 'identifier_for_logging') else 'Unknown'}] Ensure_thumbnail_loaded: No URL/data found to load.")
            # Set overlay directly as load_thumbnail_from_url won't be called.
            self._set_overlay_text_on_main_thread("No Thumb")


    def set_overlay_status(self, status_key: str):
        # status_key: "pending_resolution_for_filter", "filtered_out", "clear"
        logger.debug(f"[ImageFrame {self.initial_data.identifier_for_logging() if hasattr(self.initial_data, 'identifier_for_logging') else 'Unknown'}] Setting overlay status: {status_key}")
        
        if status_key == "pending_resolution_for_filter":
            self._is_waiting_for_resolution_for_filter = True
            self._has_ever_attempted_thumbnail_load = False # Allow future load via ensure_thumbnail_loaded
            if self.isVisible():
                self._set_overlay_text_on_main_thread("Resolving dims...")
                if self.image_widget and self.rounded_placeholder_pixmap_for_display:
                    self.image_widget.set_display_pixmap(self.rounded_placeholder_pixmap_for_display)
                self.q_pixmap = None
                self.current_displayed_pixmap_is_placeholder = True
        
        elif status_key == "filtered_out":
            self._is_waiting_for_resolution_for_filter = False
            # If it was previously loaded, q_pixmap would exist. We want to show placeholder if filtered.
            if self.image_widget and self.rounded_placeholder_pixmap_for_display:
                self.image_widget.set_display_pixmap(self.rounded_placeholder_pixmap_for_display)
            self.q_pixmap = None # Clear loaded image
            self.current_displayed_pixmap_is_placeholder = True
            if self.isVisible(): # Usually hidden, but if shown, reflect status
                 self._set_overlay_text_on_main_thread("Filtered")
        
        elif status_key == "clear":
            self._is_waiting_for_resolution_for_filter = False
            # If clearing and it's visible with a placeholder, and no image loaded,
            # and a load was never attempted, reset to "Loading..."
            if self.isVisible() and self.current_displayed_pixmap_is_placeholder and \
               not self.q_pixmap and not self._has_ever_attempted_thumbnail_load:
                self._set_overlay_text_on_main_thread("Loading...")


    # Rename original load_thumbnail_from_url to _actually_load_thumbnail
    def _actually_load_thumbnail(self, thumb_url: Optional[str], thumb_data: Optional[bytes] = None):
        # ... (original content of load_thumbnail_from_url method starts here)
        # self.current_request_id and self.current_thumb_url_attempted are no longer needed here.

        if self.image_widget and self.image_widget.isVisible():
            if self.rounded_placeholder_pixmap_for_display:
                self.image_widget.set_display_pixmap(self.rounded_placeholder_pixmap_for_display)
            self.current_displayed_pixmap_is_placeholder = True

        if self.overlay_label and self.image_widget.isVisible():
            # If _is_waiting_for_resolution_for_filter was true, overlay might be "Resolving dims..."
            # Otherwise, "Loading..." is fine.
            if not self._is_waiting_for_resolution_for_filter: # Check this flag one last time
                 self.overlay_label.setText("Loading...")
            # else: keep "Resolving dims..." if it was set by set_overlay_status
            
            self.overlay_label.setVisible(True)
            self.overlay_label.adjustSize()
            self.overlay_label.move(
                (self.image_widget.width() - self.overlay_label.width()) // 2,
                (self.image_widget.height() - self.overlay_label.height()) // 2
            )

        if thumb_data:
            # ... (rest of the original method continues)
            QTimer.singleShot(0, lambda d=thumb_data, u=thumb_url: self._display_thumbnail_data(
                image_data=d,
                original_url_str=u or "pre_supplied_unknown_url",
                store_if_full=False
            ))
        elif thumb_url:
            logger.debug(f"[ImageFrame] Requesting thumbnail for {thumb_url} via ImageFetcher.")
            QMetaObject.invokeMethod(
                self.image_fetcher,
                "request_image_data", 
                Qt.QueuedConnection,
                Q_ARG(str, thumb_url),
                Q_ARG(QObject, self),
                Q_ARG(str, "on_thumbnail_loaded"), 
                Q_ARG(str, "on_thumbnail_error"),  
                Q_ARG(int, 30) 
            )
        else:
            if self.isVisible():
                 self._set_overlay_text_on_main_thread("No Thumb")


    # New load_thumbnail_from_url that checks the waiting state first
    def load_thumbnail_from_url(self, thumb_url: Optional[str], thumb_data: Optional[bytes] = None):
        # This is the primary entry point now for ServiceImageSection to request a thumb load.
        if self._is_waiting_for_resolution_for_filter and self.isVisible():
            logger.debug(f"[ImageFrame {self.initial_data.identifier_for_logging() if hasattr(self.initial_data, 'identifier_for_logging') else 'Unknown'}] Load_thumbnail_from_url called while waiting for filter resolution. Setting overlay.")
            self.set_overlay_status("pending_resolution_for_filter") # Re-affirm overlay
            return # Don't proceed with actual load if waiting for dims for filter

        # If not waiting, or if it's not visible (ServiceImageSection will call ensure_thumbnail_loaded when it becomes visible)
        self._has_ever_attempted_thumbnail_load = True # Mark that a load attempt is now happening.
        self._is_waiting_for_resolution_for_filter = False # Crucial: clear this if we are proceeding.
        
        self._actually_load_thumbnail(thumb_url, thumb_data)


class ScrollableImageRow(QScrollArea):
    def __init__(self, parent_widget: QWidget, image_fetcher: ImageFetcher, thumbnail_display_dimension: int): # Added thumbnail_display_dimension
        super().__init__(parent_widget)
        self.image_fetcher = image_fetcher
        self.thumbnail_display_dimension = thumbnail_display_dimension # Store for creating ImageFrames

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff) 
        self.setFrameShape(QFrame.Shape.NoFrame) 

        self.content_widget = QWidget(self) 
        self.content_layout = QHBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(IMAGE_SPACING)
        self.content_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop) 

        self.setWidget(self.content_widget)

        self.image_frames_map: Dict[Any, ImageFrame] = {}
        
        self.horizontalScrollBar().installEventFilter(self)
        self.horizontalScrollBar().sliderPressed.connect(self._handle_scrollbar_pressed)
        self.horizontalScrollBar().sliderReleased.connect(self._handle_scrollbar_released)
        self._user_is_scrolling: bool = False
        self.scroll_animation: Optional[QPropertyAnimation] = None

        self.auto_scroll_debounce_timer = QTimer(self)
        self.auto_scroll_debounce_timer.setSingleShot(True)
        self.auto_scroll_debounce_timer.timeout.connect(self._perform_actual_smooth_scroll)
        self.auto_scroll_debounce_timer_interval = 150 # milliseconds

        # Minimum height includes thumbnail, label space, and some padding
        # Assuming label_dimensions height is approx 15-20px, and spacing 5px in ImageFrame layout
        # ImageFrame vertical structure: image_widget (thumb_dim) + spacing (5) + label_dimensions (e.g. 15) + frame padding (implicit)
        # ScrollableImageRow padding/margins (content_layout has 0,0,0,0)
        # Let's set min height based on thumbnail_display_dimension + reasonable space for text below it.
        # For example, thumbnail_display_dimension + 30 (for text) + 10 (for overall padding in row).
        self.setMinimumHeight(self.thumbnail_display_dimension + 30 + 10)


    def add_potential_image(self, p_image: PotentialImage,
                            on_double_click_callback: Callable[[Union[PotentialImage, ImageResult], Optional[bytes]], None],
                            is_load_more_context: bool = False):
        if p_image.identifier in self.image_frames_map: return

        frame = ImageFrame(self.content_widget, p_image, on_double_click_callback, self.image_fetcher, self.thumbnail_display_dimension)
        self.content_layout.addWidget(frame)
        self.image_frames_map[p_image.identifier] = frame
        if is_load_more_context and self.isVisible(): # Only auto-scroll if it's a "load more" action and the row is visible
            self.auto_scroll_debounce_timer.start(self.auto_scroll_debounce_timer_interval)

    def update_image_frame(self, image_result: ImageResult):
        frame_id = image_result.source_potential_image_identifier
        if frame_id in self.image_frames_map:
            frame = self.image_frames_map[frame_id]
            frame.update_with_resolved_details(image_result)

    def clear_images(self):
        while self.content_layout.count():
            child = self.content_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.image_frames_map.clear()
        self.horizontalScrollBar().setValue(0)

    def eventFilter(self, watched_object: QObject, event: QEvent) -> bool:
        if watched_object == self.horizontalScrollBar() and event.type() == QEvent.Type.Wheel:
            wheel_event = QWheelEvent(event)  

            delta_y = wheel_event.angleDelta().y()
            delta_x = wheel_event.angleDelta().x()
            is_shift_pressed = bool(wheel_event.modifiers() & Qt.ShiftModifier)
            is_purely_horizontal_gesture = (delta_y == 0 and delta_x != 0)

            if is_shift_pressed or is_purely_horizontal_gesture:
                return False  
            elif delta_y != 0:
                main_window = self.window()
                if main_window and hasattr(main_window, 'results_scroll_area'):
                    parent_scroll_area = main_window.results_scroll_area
                    if parent_scroll_area:
                        new_event = QWheelEvent(
                            parent_scroll_area.viewport().mapFromGlobal(wheel_event.globalPosition()), 
                            wheel_event.globalPosition(),    
                            wheel_event.pixelDelta(),
                            wheel_event.angleDelta(),        
                            Qt.MouseButton.NoButton,         
                            wheel_event.modifiers() & ~Qt.ShiftModifier, 
                            wheel_event.phase(),
                            wheel_event.inverted(),
                            wheel_event.source() if hasattr(wheel_event, "source") else Qt.MouseEventSource.Unknown 
                        )
                        QApplication.postEvent(parent_scroll_area.viewport(), new_event)
                
                return True 
            
            return False 

        return super().eventFilter(watched_object, event)

    def wheelEvent(self, event: QWheelEvent):
        delta_y = event.angleDelta().y()
        delta_x = event.angleDelta().x()
        is_shift_pressed = bool(event.modifiers() & Qt.ShiftModifier)
        is_purely_horizontal_gesture = (delta_y == 0 and delta_x != 0)
        
        intended_horizontal_scroll = (is_shift_pressed and (delta_y != 0 or delta_x !=0 )) or \
                                     is_purely_horizontal_gesture

        if intended_horizontal_scroll and self.horizontalScrollBar().isVisible():
            effective_delta = delta_x if is_purely_horizontal_gesture and delta_x != 0 else delta_y
            
            current_val = self.horizontalScrollBar().value()
            num_notches = effective_delta / 120.0 
            base_pixel_scroll_per_notch = self.horizontalScrollBar().singleStep() * 10
            scroll_change = -int(num_notches * base_pixel_scroll_per_notch)

            if scroll_change == 0 and effective_delta != 0:
                if effective_delta > 0: 
                    scroll_change = -base_pixel_scroll_per_notch 
                else: 
                    scroll_change = base_pixel_scroll_per_notch
            
            self.horizontalScrollBar().setValue(current_val + scroll_change)
            event.accept()
        elif delta_y != 0: 
            event.ignore() 
        else:
            super().wheelEvent(event)

    def _handle_scrollbar_pressed(self):
        # logger.debug("[ScrollableImageRow] Scrollbar pressed by user.")
        self._user_is_scrolling = True
        
        # Stop the debounce timer if it's active
        if self.auto_scroll_debounce_timer.isActive():
            # logger.debug("[ScrollableImageRow] Stopping debounce timer due to user scrollbar press.")
            self.auto_scroll_debounce_timer.stop()

        if self.scroll_animation and shiboken6.isValid(self.scroll_animation) and \
           self.scroll_animation.state() == QPropertyAnimation.State.Running:
            # logger.debug("[ScrollableImageRow] Stopping active scroll animation due to user interaction.")
            self.scroll_animation.stop() # Will be deleted by policy
            self.scroll_animation = None # Clear Python reference

    def _handle_scrollbar_released(self):
        logger.debug("[ScrollableImageRow] Scrollbar released by user.")
        self._user_is_scrolling = False

    def _perform_actual_smooth_scroll(self):
        if self._user_is_scrolling:
            # logger.debug("[ScrollableImageRow] Debounce: User is actively scrolling, scroll skipped.")
            return
        if not self.isVisible():
            # logger.debug("[ScrollableImageRow] Debounce: Row not visible, scroll skipped.")
            return

        scrollbar = self.horizontalScrollBar()
        target_value = scrollbar.maximum()
        current_value = scrollbar.value()

        if current_value == target_value:
            # logger.debug(f"[ScrollableImageRow] Debounce: Already at target scroll value {target_value}, scroll skipped.")
            # Cleanup any lingering (but stopped) animation reference if C++ object still valid
            if self.scroll_animation and shiboken6.isValid(self.scroll_animation) and \
               self.scroll_animation.state() != QPropertyAnimation.State.Running:
                self.scroll_animation.stop() # Ensure it's fully stopped for deletion policy
                self.scroll_animation = None
            return

        # If an animation is currently running
        if self.scroll_animation and shiboken6.isValid(self.scroll_animation) and \
           self.scroll_animation.state() == QPropertyAnimation.State.Running:
            if self.scroll_animation.endValue() == target_value:
                # logger.debug(f"[ScrollableImageRow] Debounce: Animation already running to correct target {target_value}.")
                return # Already animating to the correct, up-to-date target
            else:
                # logger.debug(f"[ScrollableImageRow] Debounce: Animation running to old target {self.scroll_animation.endValue()}, stopping to restart for new target {target_value}.")
                self.scroll_animation.stop() # Will be deleted by policy
                self.scroll_animation = None # Clear Python reference
        elif self.scroll_animation and shiboken6.isValid(self.scroll_animation):
            # Animation exists but is not running (e.g. paused/stopped previously, C++ object might be valid)
            # logger.debug("[ScrollableImageRow] Debounce: Existing animation was not running, ensuring it's stopped for cleanup.")
            self.scroll_animation.stop() # Ensure it's stopped for deletion policy
            self.scroll_animation = None


        # Create and start a new animation
        # logger.debug(f"[ScrollableImageRow] Debounce: Starting scroll animation from {current_value} to {target_value}")
        new_animation = QPropertyAnimation(scrollbar, b"value", self)
        new_animation.setDuration(300) 
        new_animation.setStartValue(current_value) # Start from current actual position
        new_animation.setEndValue(target_value)
        new_animation.setEasingCurve(QEasingCurve.InOutQuad)
        
        self.scroll_animation = new_animation # Store the new animation
        self.scroll_animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


class ServiceButton(QPushButton):
    def __init__(self, service_name: str, parent: Optional[QWidget] = None):
        super().__init__(service_name, parent)
        self.service_name = service_name
        self.setCheckable(True)
        self.setFont(get_font(FONT_SIZE_NORMAL))
        self.setStyleSheet("""
            QPushButton { 
                padding: 5px 8px; 
                border-radius: 4px; 
            }
            QPushButton:checked { 
                background-color: #4a69bd; /* Active service color */
                color: white; 
                border: 1px solid #3a599d; /* Border for active state */
            }
            QPushButton:!checked { 
                background-color: transparent; 
                color: #b0b0b0; /* Light gray text for inactive state */
                border: 1px solid #5A5A5A; /* Border for inactive state */
            }
            QPushButton:!checked:hover { /* Subtle hover for inactive state */
                background-color: rgba(255, 255, 255, 0.05); /* Very faint highlight */
                border: 1px solid #787878; /* Slightly lighter border on hover */
            }
        """)
        self._drag_start_position: Optional[QPoint] = None

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._drag_start_position = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if not (event.buttons() & Qt.LeftButton):
            return
        if not self._drag_start_position:
            return
        if (event.pos() - self._drag_start_position).manhattanLength() < QApplication.startDragDistance():
            return

        drag = QDrag(self)
        mime_data = QMimeData()
        mime_data.setText(self.service_name) 
        drag.setMimeData(mime_data)

        pixmap = QPixmap(self.size())
        self.render(pixmap)
        drag.setPixmap(pixmap)
        drag.setHotSpot(event.pos())

        logger.debug(f"ServiceButton: Starting drag for {self.service_name}")
        drag.exec(Qt.MoveAction) # Removed if condition as we don't do anything with result here
        self._drag_start_position = None


class ServiceToggleButtonBar(QWidget):
    serviceToggled = Signal(str, bool)
    serviceOrderChanged = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._buttons: List[ServiceButton] = []
        
        self._layout = FlowLayout(self, margin=0, hspacing=5, vspacing=5) # Use FlowLayout
        self.setLayout(self._layout) # Explicitly set the layout for the widget

        self._drag_indicator: Optional[QFrame] = None

    def populate_services(self, services_config: List[Tuple[str, bool]]):
        self._clear_buttons()
        for service_name, is_enabled in services_config:
            button = ServiceButton(service_name, self)
            button.setChecked(is_enabled)
            button.toggled.connect(lambda checked, s=service_name: self._on_button_toggled(s, checked))
            self._layout.addWidget(button) # FlowLayout's addWidget
            self._buttons.append(button)
        # No addStretch(1) for FlowLayout in this manner
        self._layout.invalidate() # Trigger a re-layout

    def _clear_buttons(self):
        # Remove items from layout and delete widgets
        while self._layout.count() > 0:
            layout_item = self._layout.takeAt(0)
            if layout_item:
                widget = layout_item.widget()
                if widget:
                    widget.deleteLater()
                del layout_item # Delete the QLayoutItem itself
        self._buttons.clear()

    def _on_button_toggled(self, service_name: str, checked: bool):
        logger.debug(f"ServiceToggleButtonBar: '{service_name}' toggled to {checked}")
        self.serviceToggled.emit(service_name, checked)
        self.serviceOrderChanged.emit(self.get_current_config())

    def get_current_config(self) -> List[Tuple[str, bool]]:
        return [(btn.service_name, btn.isChecked()) for btn in self._buttons]

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasText():
            source_button = event.source()
            if isinstance(source_button, ServiceButton) and source_button in self._buttons:
                event.acceptProposedAction()
                self._show_drag_indicator(event.position().toPoint()) # Use QPoint
                logger.debug("ServiceToggleButtonBar: dragEnterEvent accepted")
                return
        event.ignore()
        logger.debug("ServiceToggleButtonBar: dragEnterEvent ignored")

    def dragMoveEvent(self, event: QDragMoveEvent):
        if event.mimeData().hasText():
            event.acceptProposedAction()
            self._show_drag_indicator(event.position().toPoint()) # Use QPoint
            return
        event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent):
        self._hide_drag_indicator()
        event.accept()

    def dropEvent(self, event: QDropEvent):
        self._hide_drag_indicator()
        if event.mimeData().hasText():
            source_service_name = event.mimeData().text()
            source_button_widget = event.source()

            if not isinstance(source_button_widget, ServiceButton) or source_button_widget.service_name != source_service_name:
                logger.warning("ServiceToggleButtonBar: Drop source mismatch or not a ServiceButton.")
                event.ignore()
                return

            # Use event.position().toPoint() for QPoint
            target_index = self._get_drop_index(event.position().toPoint())
            
            source_button_index = -1
            for i, btn in enumerate(self._buttons):
                if btn.service_name == source_service_name:
                    source_button_index = i
                    break
            
            if source_button_index == -1:
                logger.error(f"ServiceToggleButtonBar: Could not find source button {source_service_name} in internal list.")
                event.ignore()
                return

            logger.debug(f"ServiceToggleButtonBar: Drop event for '{source_service_name}' from index {source_button_index} to target index {target_index}")

            # Simplified move logic: if source is already at target, or (source is at target-1 and target_index > source_button_index)
            # This means if source_button is dragged to its own position or right after itself before adjustment
            if source_button_index == target_index or (source_button_index == target_index - 1 and target_index > source_button_index) :
                 logger.debug("ServiceToggleButtonBar: No change in position (dropped on self or immediately after self).")
                 event.acceptProposedAction()
                 return

            moved_button = self._buttons.pop(source_button_index)
            
            # Adjust target_index if source_button was before it in the list
            # No, this is already handled by how target_index is derived.
            # If target_index was calculated based on the list *before* removal, it might need adjustment.
            # But self._get_drop_index calculates based on current visual layout.
            # The pop operation changes indices. If target_index was > source_button_index, it should be decremented.
            # Let's re-evaluate: target_index is the insertion point in the *current* visual layout.
            # After popping `moved_button`, if `target_index` was greater than `source_button_index`,
            # the effective insertion slot shifts left by one.
            
            actual_insertion_index = target_index
            if source_button_index < target_index:
                actual_insertion_index -=1
            
            # Clamp actual_insertion_index to valid range for insert
            actual_insertion_index = max(0, min(len(self._buttons), actual_insertion_index))
            self._buttons.insert(actual_insertion_index, moved_button)


            # Clear all items from layout (widgets are still in self._buttons)
            while self._layout.count() > 0:
                item = self._layout.takeAt(0)
                # We don't delete the widget here, it's managed by self._buttons
                # and will be re-added. QLayoutItem itself is deleted by takeAt if not returned.
                # The FlowLayout __del__ and takeAt will handle QLayoutItem memory.
                # For safety, if takeAt returns an item, it should be deleted if not used.
                if item: del item


            # Re-add all buttons in the new order
            for btn in self._buttons:
                self._layout.addWidget(btn)
            
            self._layout.invalidate() # Crucial for FlowLayout to re-calculate

            event.acceptProposedAction()
            self.serviceOrderChanged.emit(self.get_current_config())
            logger.info(f"ServiceToggleButtonBar: Service order changed. New order: {self.get_current_config()}")
        else:
            event.ignore()

    def _get_drop_index(self, drop_pos: QPoint) -> int:
        """Calculates the target linear index in self._buttons for a drop at drop_pos."""
        if not self._buttons:
            return 0

        h_spacing = self._layout.horizontalSpacing()
        v_spacing = self._layout.verticalSpacing()

        target_idx = len(self._buttons) # Default to inserting at the end

        for i, button in enumerate(self._buttons):
            btn_geom = button.geometry() # Geometry relative to self (ServiceToggleButtonBar)

            # Check if drop_pos is on the same "visual row" as the button
            # A simple check: if y is between top of button and bottom of button (approx)
            if drop_pos.y() >= btn_geom.top() - v_spacing / 2 and \
               drop_pos.y() <= btn_geom.bottom() + v_spacing / 2:
                # If drop_pos is to the left of the button's horizontal center
                if drop_pos.x() < btn_geom.center().x():
                    target_idx = i
                    return target_idx
                # If to the right, the insertion is after this button (i.e., before button i+1)
                # So, by default, target_idx will become i+1 effectively if loop continues or ends
            
            # If drop_pos is clearly above this button's row and we haven't found a closer spot
            elif drop_pos.y() < btn_geom.top() - v_spacing / 2:
                target_idx = i
                return target_idx
        
        # If loop completes, target_idx is len(self._buttons), meaning append.
        return target_idx

    def _show_drag_indicator(self, drag_pos: QPoint): # Changed x_pos to drag_pos: QPoint
        if not self._drag_indicator:
            self._drag_indicator = QFrame(self)
            self._drag_indicator.setFrameShape(QFrame.Shape.VLine)
            self._drag_indicator.setFrameShadow(QFrame.Shadow.Sunken)
            # A more visible indicator style
            self._drag_indicator.setStyleSheet("QFrame { background-color: #0078D7; border: none; }") 
            self._drag_indicator.setFixedWidth(2)
        
        insertion_idx = self._get_drop_index(drag_pos)
        
        h_spacing = self._layout.horizontalSpacing()
        indicator_y = 0
        indicator_height = self.fontMetrics().height() + 10 # Default height

        if not self._buttons: # No buttons, indicator at top-left
            indicator_x = self.contentsMargins().left() + h_spacing // 2
            indicator_y = self.contentsMargins().top()
        elif insertion_idx == len(self._buttons): # Insert at the end
            last_button = self._buttons[-1]
            btn_geom = last_button.geometry()
            indicator_x = btn_geom.right() + h_spacing // 2
            indicator_y = btn_geom.top()
            indicator_height = btn_geom.height()
        else: # Insert before _buttons[insertion_idx]
            button_to_insert_before = self._buttons[insertion_idx]
            btn_geom = button_to_insert_before.geometry()
            indicator_x = btn_geom.left() - h_spacing // 2
            indicator_y = btn_geom.top()
            indicator_height = btn_geom.height()

        # Ensure indicator_x is within bounds
        indicator_x = max(self.contentsMargins().left(), indicator_x)
        
        self._drag_indicator.setGeometry(indicator_x, indicator_y, self._drag_indicator.width(), indicator_height)
        self._drag_indicator.raise_()
        self._drag_indicator.show()

    def _hide_drag_indicator(self):
        if self._drag_indicator:
            self._drag_indicator.hide()


class ServiceImageSection(QGroupBox):
    imageDoubleClicked = Signal(object, QByteArray)
    loadMoreRequested = Signal(str)

    def __init__(self, parent_widget: QWidget, service_name: str,
                 on_image_double_click_callback: Callable[[Union[PotentialImage, ImageResult], Optional[bytes]], None],
                 on_load_more_callback: Callable[[str], None],
                 image_fetcher: ImageFetcher,
                 thumbnail_display_dimension: int, # New parameter
                 name_label_min_width: int = 0
                 ):
        super().__init__("", parent_widget)
        self.setObjectName("ServiceImageSection")
        self.thumbnail_display_dimension = thumbnail_display_dimension # Store for ScrollableImageRow

        self._name_label_min_width = name_label_min_width
        self.service_name = service_name
        self.imageDoubleClicked.connect(on_image_double_click_callback)
        self.loadMoreRequested.connect(on_load_more_callback)

        self.image_fetcher = image_fetcher

        self.spinner_label = QLabel(self)
        # Construct path to assets folder relative to this file's location
        assets_dir = get_bundle_dir() / "assets"
        spinner_gif_path = assets_dir / "spinner.gif"
        self.spinner_movie = QMovie(str(spinner_gif_path), QByteArray(), self)
        if not self.spinner_movie.isValid():
            logger.error(f"Could not load or invalid spinner GIF: {spinner_gif_path}")
            # Fallback or hide: For now, we'll just log. The label won't show if movie is invalid / not started.
        else:
            self.spinner_movie.setSpeed(500) # Speed up to 200%
        self.spinner_movie.setScaledSize(QSize(16, 16)) # Adjust size as needed
        self.spinner_label.setMovie(self.spinner_movie)
        self.spinner_label.setVisible(False)


        self._collapsed = True 
        self._has_more_actionable = False
        self._is_loading_more = False
        self._is_user_initiated_load_more: bool = False 
        
        # Dimension filter state (mirrors MainWindow's active filter)
        self._filter_min_w: Optional[int] = None
        self._filter_min_h: Optional[int] = None
        
        # Image counts for status reporting
        self._total_items_received_count = 0 
        self._visible_items_count = 0
        self._pending_dims_for_filter_count = 0
        self._filtered_out_by_dims_count = 0
        
        self._current_status_message_key: str = ""

        self._setup_ui()
        self.update_visual_state() 
        self._apply_collapsed_state()

    def _setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(5, 5, 5, 5) 
        self.main_layout.setSpacing(10) 

        self.header_widget = QWidget(self)
        header_layout = QHBoxLayout(self.header_widget)
        header_layout.setContentsMargins(0,0,0,0)
        header_layout.setSpacing(5)

        self.service_name_label = QLabel(self.service_name, self)
        self.service_name_label.setFont(get_font(FONT_SIZE_LARGE, weight=QFont.Bold))
        if self._name_label_min_width > 0:
            self.service_name_label.setFixedWidth(self._name_label_min_width) # Changed to setFixedWidth
        header_layout.addWidget(self.service_name_label)

        header_layout.addWidget(self.spinner_label) # Spinner now added before status label

        self.status_label = QLabel(self)
        self.status_label.setFont(get_font(FONT_SIZE_SMALL, italic=True))
        header_layout.addWidget(self.status_label, 1) # Status label still stretches, pushing subsequent items right

        self.load_more_button = QPushButton("Load More", self)
        self.load_more_button.clicked.connect(self._handle_load_more_click)
        self.load_more_button.setFixedHeight(24) # Match toggle_button height
        self.load_more_button.setVisible(False) 
        header_layout.addWidget(self.load_more_button)

        self.toggle_button = QPushButton("▸", self) 
        self.toggle_button.setObjectName("SmallButton")
        self.toggle_button.setFixedSize(24,24) 
        self.toggle_button.clicked.connect(self.toggle_collapse)
        header_layout.addWidget(self.toggle_button)

        self.main_layout.addWidget(self.header_widget)

        # Configure header interaction and cursors
        self.header_widget.installEventFilter(self)
        self.header_widget.setCursor(Qt.PointingHandCursor)
        self.toggle_button.setCursor(Qt.ArrowCursor) # Override for the button
        self.load_more_button.setCursor(Qt.ArrowCursor) # Override for the button

        self.image_row = ScrollableImageRow(self, self.image_fetcher, self.thumbnail_display_dimension)
        self.image_row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed) 
        self.main_layout.addWidget(self.image_row)
        self.image_row.setVisible(not self._collapsed)

    def _handle_load_more_click(self):
        if self._has_more_actionable and not self._is_loading_more: # Removed self._is_enabled check
            self._is_user_initiated_load_more = True # Set flag before requesting more
            self.set_load_more_status_ui(loading=True, has_more_after_load=self._has_more_actionable)
            self.loadMoreRequested.emit(self.service_name)

    # update_visual_state is called by MainWindow._render_service_sections
    # We need to ensure it also applies the current filter state from MainWindow if this section is re-enabled.
    # This is now handled by MainWindow explicitly calling apply_dimension_filter
    # when _render_service_sections happens or when a filter changes.
    # So, update_visual_state itself doesn't need to re-fetch the filter,
    # but its _update_status_label call will use the locally stored filter counts.
    def update_visual_state(self): 
        if self.status_label:
            self.status_label.setStyleSheet("") 
            if self._current_status_message_key == "Disabled":
                 self._current_status_message_key = ""

        self.toggle_button.setEnabled(True)
        self.image_row.setVisible(not self._collapsed)
        self.set_load_more_status_ui(self._is_loading_more, self._has_more_actionable)

        self._update_toggle_button_text()
        self._update_status_label() # This will also call _update_spinner_visibility implicitly

    def apply_dimension_filter(self, min_w: Optional[int], min_h: Optional[int]):
        """Called by MainWindow when the global dimension filter changes."""
        filter_changed = (self._filter_min_w != min_w) or \
                         (self._filter_min_h != min_h)
        
        if filter_changed:
            logger.debug(f"ServiceSection '{self.service_name}': Applying dimension filter W>={min_w}, H>={min_h}")
            self._filter_min_w = min_w
            self._filter_min_h = min_h
            self._update_all_frames_visibility_and_load_state()
        # else: No change in filter for this section, do nothing.

    def _evaluate_frame_for_filter(self, frame: ImageFrame) -> Tuple[bool, str]:
        """
        Determines if a frame should be visible based on current dimension filter.
        Returns: (should_be_visible, reason_string)
        reason_string: "ok", "pending_dims_for_filter", "filtered_by_dims"
        """
        has_active_filter = self._filter_min_w is not None or \
                            self._filter_min_h is not None

        if not has_active_filter:
            return True, "ok" # No filter active, frame should be visible by default

        # Filter is active, check frame's dimensions
        if frame.resolved_image_result is None:
            return False, "pending_dims_for_filter" # Dimensions not yet known
        
        w = frame.resolved_image_result.full_width
        h = frame.resolved_image_result.full_height

        # If resolved dimensions are invalid or zero, treat as pending for filtering purposes
        if w is None or w <= 0 or h is None or h <= 0:
            return False, "pending_dims_for_filter" 

        # Dimensions are known and valid, check against filter
        passes_w_filter = True
        if self._filter_min_w is not None:
            passes_w_filter = (w >= self._filter_min_w)
        
        passes_h_filter = True
        if self._filter_min_h is not None:
            passes_h_filter = (h >= self._filter_min_h)

        if passes_w_filter and passes_h_filter:
            return True, "ok" # Meets filter criteria
        else:
            return False, "filtered_by_dims" # Known dimensions, but filtered out

    def _update_all_frames_visibility_and_load_state(self):
        if not self.image_row or not hasattr(self.image_row, 'image_frames_map') or not self.isVisible():
            return

        logger.debug(f"ServiceSection '{self.service_name}': Updating all frames visibility and load state.")
        
        current_visible_count = 0
        current_pending_dims_count = 0
        current_filtered_out_count = 0
        
        # Ensure all ImageFrame widgets are in the layout before toggling visibility
        # This simplifies logic, FlowLayout will handle reflow.
        # This loop also ensures that any frame in the map has its widget in the layout.
        for frame_id, frame in list(self.image_row.image_frames_map.items()): # Use list for safe modification if needed
            if not shiboken6.isValid(frame): # ImageFrame is a QWidget
                logger.warning(f"ServiceSection '{self.service_name}': Invalid frame for id {frame_id}. Removing from map.")
                if frame_id in self.image_row.image_frames_map: # Check again before pop
                    del self.image_row.image_frames_map[frame_id]
                # If widget was in layout, it will be cleaned up by Qt or layout.
                continue
            
            # Ensure widget is in the layout. If it's already there, addWidget does nothing.
            # This is important if frames were previously kept out of layout when pending.
            # With FlowLayout, it's usually better to add all and hide/show.
            if frame.parentWidget() != self.image_row.content_widget: # Check if it needs to be added
                 self.image_row.content_layout.addWidget(frame)


        all_frames_in_map = list(self.image_row.image_frames_map.values())
        for frame in all_frames_in_map:
            if not shiboken6.isValid(frame): # ImageFrame is a QWidget
                continue # Already handled, but good to be safe

            should_show, reason = self._evaluate_frame_for_filter(frame)
            
            current_is_visible = frame.isVisible() # Corrected here, frame is the widget
            if current_is_visible != should_show:
                frame.setVisible(should_show)

            if should_show:
                current_visible_count += 1
                frame.ensure_thumbnail_loaded() 
            else: # Not visible
                if reason == "pending_dims_for_filter":
                    current_pending_dims_count += 1
                    frame.set_overlay_status("pending_resolution_for_filter")
                elif reason == "filtered_by_dims":
                    current_filtered_out_count += 1
                    frame.set_overlay_status("filtered_out")
        
        self._visible_items_count = current_visible_count
        self._pending_dims_for_filter_count = current_pending_dims_count
        self._filtered_out_by_dims_count = current_filtered_out_count
        
        self._update_status_label() 

        # Collapse/expand logic based on visibility
        if self._total_items_received_count > 0:
            is_actively_loading = any(s in self._current_status_message_key for s in ["Searching...", "Fetching...", "Loading..."]) or self._is_loading_more
            
            if self._visible_items_count == 0 and not self._collapsed and not is_actively_loading:
                logger.debug(f"ServiceSection '{self.service_name}': Collapsing (no visible items, not actively loading).")
                self.collapse()
            elif self._visible_items_count > 0 and self._collapsed:
                logger.debug(f"ServiceSection '{self.service_name}': Expanding (items became visible).")
                self.expand()
        
        if self.image_row and hasattr(self.image_row, 'content_layout') and shiboken6.isValid(self.image_row.content_layout):
            self.image_row.content_layout.invalidate() # Trigger re-layout

    def _update_toggle_button_text(self):
        if not self.toggle_button: return
        prefix = "▸" if self._collapsed else "▾"
        self.toggle_button.setText(prefix)

    def _update_status_label(self):
        if not self.status_label: return

        text_to_display = self._current_status_message_key
        
        # Check for error conditions to apply red color
        is_error = text_to_display.startswith("Error")

        if is_error:
            self.status_label.setStyleSheet("color: red;")
        else:
            self.status_label.setStyleSheet("") # Reset to default color

        # New status text logic incorporating filter counts
        status_parts = []
        if text_to_display: # Primary status message (Searching..., Error..., Album not found, etc.)
            status_parts.append(text_to_display)

        # Count details, but not for certain primary statuses
        no_count_primary_statuses = ["Disabled", "Album not found", "Error", "No images found."] # Simplified
        can_show_counts = True
        for s in no_count_primary_statuses:
            if s in text_to_display:
                can_show_counts = False
                break
        
        # Also, don't show detailed counts if it's a very early "Searching..." state and no items yet.
        if "Searching..." in text_to_display and self._total_items_received_count == 0:
            can_show_counts = False

        if can_show_counts:
            if self._total_items_received_count > 0: # Only show counts if items were received
                count_detail_parts = []
                if self._visible_items_count > 0 or (not text_to_display and self._total_items_received_count > 0): # Show "X visible" if any are visible or if it's the main status
                    count_detail_parts.append(f"{self._visible_items_count} visible")
                
                has_active_filter = self._filter_min_w is not None or self._filter_min_h is not None
                if has_active_filter: # Only show pending/filtered if a filter is actually on
                    if self._pending_dims_for_filter_count > 0:
                        count_detail_parts.append(f"{self._pending_dims_for_filter_count} pending")
                    if self._filtered_out_by_dims_count > 0:
                        count_detail_parts.append(f"{self._filtered_out_by_dims_count} filtered")
                
                if count_detail_parts:
                    status_parts.append(f"({', '.join(count_detail_parts)})")
            # elif not text_to_display and self._total_items_received_count == 0 and not is_error and not self._is_loading_more and \
            #      not any(s in self._current_status_message_key for s in ["Searching...", "Fetching..."]):
            #       # If no specific status, no items, not error, not loading -> imply "Images available (0 visible)" or similar
            #       if not (self._filter_min_w or self._filter_min_h): # If no filter
            #         status_parts.append("(0 visible)") # Default if no text_to_display
                  # If there is a filter, the (0 pending, 0 filtered) might be too noisy if no items.
                  # Let it be blank or handled by primary status key.

        final_text = " ".join(status_parts).strip()
        
        # Default to "Images available" if primary status is empty and items exist
        if not text_to_display and self._total_items_received_count > 0 and not is_error:
            if final_text: # if counts were added
                final_text = f"Images available {final_text}"
            else: # no counts added, but items exist
                final_text = f"Images available ({self._visible_items_count} visible)"
        # elif not final_text and not is_error and not self._is_loading_more and \
            #  not any(s in self._current_status_message_key for s in ["Searching...", "Fetching..."]):
            # Truly empty state, no errors, no loading.
            # if self._total_items_received_count == 0 : # And no items received
                #  final_text = "Status OK" # Or some neutral placeholder
            # else: if items received, previous block should have handled it.

        self.status_label.setText(final_text)
        self._update_spinner_visibility()

    def toggle_collapse(self):
        # Removed: if not self._is_enabled: return
        self._collapsed = not self._collapsed
        self._apply_collapsed_state()

    def _apply_collapsed_state(self):
        self._update_toggle_button_text()
        self.image_row.setVisible(not self._collapsed) 
        if self.parentWidget(): 
            QTimer.singleShot(0, lambda: self.parentWidget().updateGeometry() if self.parentWidget() else None)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.header_widget:
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.LeftButton:
                # event.pos() is relative to self.header_widget
                child_widget_under_cursor = self.header_widget.childAt(event.pos())

                # If the click was on an interactive button within the header, let the button handle it.
                if child_widget_under_cursor is self.toggle_button or \
                   child_widget_under_cursor is self.load_more_button:
                    return False # Pass event to the button

                # Otherwise, the click was on other parts of the header (labels, spinner, or background)
                self.toggle_collapse()
                return True # Event handled by the header itself
            
            # For other event types on header_widget, or other mouse buttons, default processing
            return False 
        
        # For other watched objects, default processing
        return super().eventFilter(watched, event)

    def _show_spinner(self):
        if self.spinner_movie.isValid() and not self.spinner_label.isVisible():
            self.spinner_label.setVisible(True)
            self.spinner_movie.start()

    def _hide_spinner(self):
        if self.spinner_label.isVisible():
            self.spinner_movie.stop()
            self.spinner_label.setVisible(False)

    def _update_spinner_visibility(self):
        if not self.spinner_movie.isValid():
            self._hide_spinner() # Ensure it's hidden if movie is bad
            return

        is_searching = "Searching..." in self._current_status_message_key
        is_fetching = "Fetching images..." in self._current_status_message_key
        is_displaying = "Displaying images..." in self._current_status_message_key
        
        # Check if "Load More" implies spinner already via status_label, if not add explicit check for self._is_loading_more
        should_be_spinning = self._is_loading_more or is_searching or is_fetching or is_displaying

        if should_be_spinning:
            self._show_spinner()
        else:
            self._hide_spinner()

    def collapse(self):
        if not self._collapsed:
            self._collapsed = True
            self._apply_collapsed_state()

    def expand(self):
        if self._collapsed: 
            self._collapsed = False
            self._apply_collapsed_state()

    def set_load_more_status_ui(self, loading: bool, has_more_after_load: bool):
        if not self.load_more_button: return

        self._is_loading_more = loading
        self._has_more_actionable = has_more_after_load if not loading else self._has_more_actionable

        if loading:
            self.load_more_button.setText("Loading...")
            self.load_more_button.setEnabled(False)
            self.load_more_button.setVisible(True)
        else:
            self.load_more_button.setText("Load More")
            # Use _total_items_received_count to determine if any items have been processed by this section.
            # Or _visible_items_count if "Load More" should only be active if something is currently shown.
            # Let's use _total_items_received_count as it indicates potential for more, even if current are filtered.
            can_load_more = self._has_more_actionable and self._total_items_received_count > 0
            self.load_more_button.setEnabled(can_load_more)
            self.load_more_button.setVisible(can_load_more)
        self._update_spinner_visibility() # Update spinner based on loading state

    def reset_for_new_search(self):
        if not self.isVisible(): return
        self._current_status_message_key = "" 
        
        self._total_items_received_count = 0
        self._visible_items_count = 0
        self._pending_dims_for_filter_count = 0
        self._filtered_out_by_dims_count = 0
        
        self.image_row.clear_images()
        self._has_more_actionable = False
        self._is_loading_more = False
        self._is_user_initiated_load_more = False # Reset flag for new search
        self.set_load_more_status_ui(loading=False, has_more_after_load=False) # This will call _update_spinner_visibility
        if not self._collapsed: self.collapse()
        self._update_status_label() # This will also call _update_spinner_visibility

    def set_initial_searching_status(self):
        if self._current_status_message_key not in ["Disabled"] and not self._current_status_message_key.startswith("Error:"):
            self._current_status_message_key = "Searching..."
            self._update_status_label()

    def handle_service_album_search_succeeded(self, num_candidates_found: Optional[int]):
        if not self.isVisible(): return 
        if num_candidates_found > 0:
            msg = f"Found {num_candidates_found} candidate{'s' if num_candidates_found != 1 else ''}." if num_candidates_found is not None else "Album found."
            self._current_status_message_key = f"{msg} Fetching images..."
        else:
            self._current_status_message_key = "Album not found."
            self._total_items_received_count = 0 # No items to receive if album not found
            # Reset other counts too, as there will be no images.
            self._visible_items_count = 0
            self._pending_dims_for_filter_count = 0
            self._filtered_out_by_dims_count = 0
            self.set_load_more_status_ui(loading=False, has_more_after_load=False)
        self._update_status_label()

    def handle_potential_image(self, p_image: PotentialImage):
        if not self.isVisible() or not self.image_row: return 
        
        # Create frame and add to map. Also add its widget to the layout immediately.
        # Visibility will be controlled based on filter.
        frame = self.image_row.image_frames_map.get(p_image.identifier)
        if not frame: # Should not happen if add_potential_image handles it, but good check.
            def internal_double_click_handler(image_data_obj, image_qbyte_array): 
                self.imageDoubleClicked.emit(image_data_obj, image_qbyte_array) 
            
            # This call to add_potential_image adds the frame to the map AND its widget to the layout.
            self.image_row.add_potential_image(p_image, internal_double_click_handler, self._is_user_initiated_load_more)
            frame = self.image_row.image_frames_map.get(p_image.identifier) # Get the created frame
            if not frame: # Still not found, something is wrong.
                logger.error(f"ServiceSection '{self.service_name}': Frame not found after add_potential_image for {p_image.identifier_for_logging()}.")
                return
        
        self._total_items_received_count += 1

        # Evaluate this new frame against the current filter
        should_show, reason = self._evaluate_frame_for_filter(frame)
        frame.setVisible(should_show)

        if should_show:
            self._visible_items_count +=1 # Increment here, _update_all_frames will re-calculate if filter changes
            frame.ensure_thumbnail_loaded() # Triggers thumbnail load if needed and visible
        else:
            if reason == "pending_dims_for_filter":
                self._pending_dims_for_filter_count += 1
                frame.set_overlay_status("pending_resolution_for_filter")
            elif reason == "filtered_by_dims": # Should not happen for PotentialImage as dims are unknown
                self._filtered_out_by_dims_count +=1 
                frame.set_overlay_status("filtered_out")


        if "Fetching images..." in self._current_status_message_key or \
           self._current_status_message_key == "" or \
           "Images available" in self._current_status_message_key or \
           self._current_status_message_key == "Displaying images...": # Default message progression
            if self._total_items_received_count > 0: # Only switch to "Displaying" if we actually have items
                self._current_status_message_key = "Displaying images..."

        self._update_status_label() # This will use new counts
        if self._visible_items_count > 0 and self._collapsed: # Check against visible items
            self.expand()

    def handle_image_resolved(self, i_result: ImageResult):
        if not self.isVisible() or not self.image_row: return 
        
        frame_id = i_result.source_potential_image_identifier
        frame = self.image_row.image_frames_map.get(frame_id)

        if not frame:
            logger.warning(f"ServiceSection '{self.service_name}': Resolved image for unknown frame ID {frame_id}. PotentialImage might not have been handled.")
            return
        if not shiboken6.isValid(frame): # ImageFrame is a QWidget
            logger.warning(f"ServiceSection '{self.service_name}': Resolved image for invalid frame ID {frame_id}.")
            return

        # Store old state for count adjustment
        was_pending_filter = frame._is_waiting_for_resolution_for_filter # Access ImageFrame internal state

        frame.update_with_resolved_details(i_result) # This updates the frame's internal resolved_image_result

        # Re-evaluate visibility now that dimensions are known
        should_show, reason = self._evaluate_frame_for_filter(frame)
        
        current_is_visible = frame.isVisible()
        if current_is_visible != should_show:
            frame.setVisible(should_show)

        # Adjust counts based on state change after resolution
        if was_pending_filter and self._pending_dims_for_filter_count > 0:
            self._pending_dims_for_filter_count -= 1 # No longer pending resolution

        if should_show:
            if not current_is_visible : # If it just became visible
                 self._visible_items_count += 1
            frame.ensure_thumbnail_loaded()
        else: # Not visible after resolution
            if current_is_visible: # If it was visible but now filtered out by new dims
                if self._visible_items_count > 0: self._visible_items_count -=1
            
            if reason == "filtered_by_dims":
                self._filtered_out_by_dims_count += 1
                frame.set_overlay_status("filtered_out")
            # If reason is "pending_dims_for_filter" again (e.g. resolved dims were invalid),
            # it means it's still pending, count was decremented and will be re-added if still pending by _update_all_frames..
            # Simpler: _update_all_frames_visibility_and_load_state will reconcile all counts.
            # For now, let's rely on _update_status_label to use the latest counts.
            # A full recount via _update_all_frames_visibility_and_load_state() might be more robust here too.
            # For now, incremental updates and _update_status_label.

        self._update_status_label() # Update texts based on new counts.
        if self._visible_items_count > 0 and self._collapsed:
            self.expand()
        elif self._visible_items_count == 0 and not self._collapsed and self._total_items_received_count > 0 and \
             not any(s in self._current_status_message_key for s in ["Searching...", "Fetching...", "Loading..."]):
            self.collapse()


    def handle_batch_processing_complete(self, has_more: bool, was_cancelled: bool, error_message: Optional[str]):
        if not self.isVisible(): return
        
        self._has_more_actionable = has_more if not (was_cancelled or error_message) else False

        if error_message:
            self._current_status_message_key = f"{error_message}"
        elif was_cancelled:
            # Use _total_items_received_count
            self._current_status_message_key = "Batch cancelled." if self._total_items_received_count > 0 else "Search cancelled."
        elif self._total_items_received_count == 0 and not error_message : # If no items and no specific error
             self._current_status_message_key = "No images found."
        elif not error_message and not was_cancelled: # Batch completed successfully
            self._current_status_message_key = "" # Clears to "Images available (...)" or similar via _update_status_label

        self._is_user_initiated_load_more = False 
        
        # If there are still items pending resolution for filter, the batch isn't fully "complete" from UI perspective
        # However, the backend batch is done. We should reflect this.
        # If has_more is true, but all current items are filtered or pending, Load More might still be valid.
        
        # Recalculate counts and update UI fully, as some "pending_dims_for_filter" might now be considered final
        # if the service won't provide more info.
        # However, the current design implies ImageFetcher resolves all, so "pending" means waiting on ImageFetcher.
        # For now, just update the load more button and status label.
        if self._pending_dims_for_filter_count == 0 : # If nothing is pending filter resolution
             self.set_load_more_status_ui(loading=False, has_more_after_load=self._has_more_actionable)
        else: # If items are still pending, "Load More" might be confusing.
              # For now, let has_more_actionable control it.
             self.set_load_more_status_ui(loading=False, has_more_after_load=self._has_more_actionable)
        
        self._update_status_label()


    def handle_service_error(self, message: str):
        pass # do not show non-batch errors in the UI