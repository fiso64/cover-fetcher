# ui/notifications.py
import uuid
from typing import List, Optional, Dict, Callable

from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, QRect, QPoint, QSize, Signal, QObject, QEvent
)
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QFont, QFontMetrics, QPainterPath, QMouseEvent
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QApplication, QFrame

import logging

logger = logging.getLogger(__name__)

# --- Notification Appearance ---
NOTIFICATION_WIDTH = 350
NOTIFICATION_MIN_HEIGHT = 50
NOTIFICATION_PADDING = 10 # Inner padding for text
NOTIFICATION_CORNER_RADIUS = 8
NOTIFICATION_SPACING = 10 # Vertical spacing between notifications
NOTIFICATION_MARGIN_BOTTOM = 20 # From window bottom
NOTIFICATION_MARGIN_HORIZONTAL = 20 # From window sides (if not centered, for max width scenarios)

# --- Notification Animation ---
ANIMATION_DURATION_MS = 300
FADE_EASING_CURVE = QEasingCurve.InOutQuad
MOVE_EASING_CURVE = QEasingCurve.OutQuad

# --- Notification Behavior ---
DEFAULT_TIMEOUT_MS = 2000


class NotificationWidget(QFrame):
    """
    A single notification message widget.
    Manages its own appearance, fade-in/out animations, and optional auto-timeout.
    """
    dismiss_requested = Signal(str) # Emits its own ID when it wants to be dismissed (e.g., timeout)
    clicked = Signal(str) # Emits its own ID when clicked

    def __init__(self, notification_id: str, text: str, parent: QWidget):
        super().__init__(parent)
        self.notification_id = notification_id
        self._text = text
        self.parent_widget = parent # To calculate position relative to main window

        self.setFixedWidth(NOTIFICATION_WIDTH)
        self.setObjectName("NotificationWidget") # For QSS styling if needed
        self.setStyleSheet(f"""
            NotificationWidget {{
                background-color: rgba(30, 30, 30, 0.85); /* Darker, slightly more opaque */
                border-radius: {NOTIFICATION_CORNER_RADIUS}px;
                border: 1px solid rgba(255, 255, 255, 0.1); /* Subtle border */
            }}
        """)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(NOTIFICATION_PADDING, NOTIFICATION_PADDING,
                                        NOTIFICATION_PADDING, NOTIFICATION_PADDING)

        self.text_label = QLabel(text)
        self.text_label.setWordWrap(True)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text_label.setStyleSheet("background-color: transparent; color: white; border: none;")
        font = QFont()
        font.setPointSize(10)
        self.text_label.setFont(font)
        self._layout.addWidget(self.text_label)

        self.adjustSize() # Calculate initial height based on text

        self.opacity_animation = QPropertyAnimation(self, b"windowOpacity", self)
        self.opacity_animation.setDuration(ANIMATION_DURATION_MS)
        self.opacity_animation.setEasingCurve(FADE_EASING_CURVE)
        self._on_fully_hidden_connected = False # Flag to track connection state

        self.move_animation = QPropertyAnimation(self, b"pos", self)
        self.move_animation.setDuration(ANIMATION_DURATION_MS)
        self.move_animation.setEasingCurve(MOVE_EASING_CURVE)

        self._timeout_timer: Optional[QTimer] = None
        self._can_timeout: bool = True
        self._dismissable_by_user: bool = True
        self.is_hovered: bool = False


    def text(self) -> str:
        return self._text

    def setText(self, text: str):
        self._text = text
        self.text_label.setText(text)
        # Current height before adjust
        old_height = self.height()
        self.adjustSize() # Re-calculate height if text changes
        # Emit a signal if height changed, so manager can reposition
        if old_height != self.height():
            # This requires a new signal, or manager handles it via _recalculate_positions_and_apply
            # For now, manager will call _recalculate_positions_and_apply after update_notification
            pass


    def set_can_timeout(self, can_timeout: bool):
        self._can_timeout = can_timeout
        if not can_timeout and self._timeout_timer and self._timeout_timer.isActive():
            self._timeout_timer.stop()

    def set_dismissable_by_user(self, dismissable: bool):
        self._dismissable_by_user = dismissable

    def show_animated(self, target_pos: QPoint):
        self.move(target_pos.x(), target_pos.y() + self.height() // 3) # Start slightly lower for "pop up"
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_() # Ensure it's on top

        self.opacity_animation.setStartValue(0.0)
        self.opacity_animation.setEndValue(1.0)
        self.opacity_animation.start()

        self.move_animation.setStartValue(self.pos())
        self.move_animation.setEndValue(target_pos)
        self.move_animation.start()

    def hide_animated(self):
        # Ensure hide animation doesn't restart if already hiding
        if self.opacity_animation.state() == QPropertyAnimation.State.Running and \
           self.opacity_animation.endValue() == 0.0:
            return

        current_opacity = self.windowOpacity()

        # Stop any currently running opacity animation.
        if self.opacity_animation.state() == QPropertyAnimation.State.Running:
            self.opacity_animation.stop() # This prevents its 'finished' signal.
            # If _on_fully_hidden was connected for the animation we just stopped,
            # it's now "orphaned" unless we disconnect it.
            if self._on_fully_hidden_connected:
                try:
                    self.opacity_animation.finished.disconnect(self._on_fully_hidden)
                except RuntimeError: 
                    # This might happen if the flag is somehow out of sync,
                    # but the primary goal is to ensure the flag is reset.
                    pass 
                self._on_fully_hidden_connected = False # Mark as disconnected

        self.opacity_animation.setStartValue(current_opacity)
        self.opacity_animation.setEndValue(0.0)
        
        # Connect _on_fully_hidden for the new animation, only if not already connected.
        # Given the logic above (stop and conditional disconnect), it should be safe to connect.
        # However, to be absolutely sure and to align with the flag's purpose:
        if not self._on_fully_hidden_connected:
            self.opacity_animation.finished.connect(self._on_fully_hidden)
            self._on_fully_hidden_connected = True
        
        self.opacity_animation.start()

        if self._timeout_timer:
            self._timeout_timer.stop()

    def _on_fully_hidden(self):
        if self._on_fully_hidden_connected: # Check flag before attempting disconnect
            try:
                self.opacity_animation.finished.disconnect(self._on_fully_hidden)
            except RuntimeError:
                # This should ideally not happen if the flag is accurate.
                logger.warning(f"NotificationWidget '{self.notification_id}': _on_fully_hidden disconnect failed despite flag.")
            self._on_fully_hidden_connected = False # Update flag
        
        self.hide()
        self.dismiss_requested.emit(self.notification_id) # Notify manager it's ready for removal

    def move_to_animated(self, new_pos: QPoint):
        if self.pos() == new_pos:
            return
        
        if self.move_animation.state() == QPropertyAnimation.State.Running:
            self.move_animation.stop() # Stop current move to start a new one

        self.move_animation.setStartValue(self.pos())
        self.move_animation.setEndValue(new_pos)
        self.move_animation.start()

    def start_timeout(self, timeout_ms: int):
        if not self._can_timeout:
            return

        if self._timeout_timer is None:
            self._timeout_timer = QTimer(self)
            self._timeout_timer.setSingleShot(True)
            self._timeout_timer.timeout.connect(self._handle_timeout)
        self._timeout_timer.start(timeout_ms)

    def stop_timeout(self):
        if self._timeout_timer and self._timeout_timer.isActive():
            self._timeout_timer.stop()

    def _handle_timeout(self):
        if not self.is_hovered: # Don't timeout if mouse is over it
             self.hide_animated()
        else: # If hovered, restart timer for a short duration when mouse leaves
            pass # Timeout will be restarted on leaveEvent if still active

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._dismissable_by_user:
                self.hide_animated()
            self.clicked.emit(self.notification_id)
        super().mousePressEvent(event)

    def enterEvent(self, event: QEvent):
        self.is_hovered = True
        if self._timeout_timer and self._timeout_timer.isActive():
            self._timeout_timer.stop() # Pause timeout while hovered
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent):
        self.is_hovered = False
        if self._timeout_timer and self._can_timeout:
             # Resume with a fixed portion of default timeout to prevent immediate dismissal
            self._timeout_timer.start(max(DEFAULT_TIMEOUT_MS // 2, 1000))
        super().leaveEvent(event)

    def sizeHint(self) -> QSize:
        # Ensure label exists for font metrics
        if not hasattr(self, 'text_label') or not self.text_label:
             return QSize(NOTIFICATION_WIDTH, NOTIFICATION_MIN_HEIGHT)

        fm = QFontMetrics(self.text_label.font())
        # Calculate bounding rect for the text within the allowed width
        text_bounding_rect = fm.boundingRect(QRect(0, 0, NOTIFICATION_WIDTH - 2 * NOTIFICATION_PADDING, 10000), # Large height for calc
                                    Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._text)
        
        content_height = text_bounding_rect.height()
        total_height = content_height + 2 * NOTIFICATION_PADDING
        return QSize(NOTIFICATION_WIDTH, max(NOTIFICATION_MIN_HEIGHT, total_height))

    def adjustSize(self):
        new_height = self.sizeHint().height()
        if self.height() != new_height:
            self.setFixedHeight(new_height)
        super().adjustSize()


class NotificationManager(QObject):
    """
    Manages the display, positioning, and lifecycle of multiple NotificationWidgets
    within a parent window.
    """

    def __init__(self, parent_window: QWidget):
        super().__init__(parent_window)
        self.parent_window = parent_window
        self._notifications: Dict[str, NotificationWidget] = {} # Store by ID
        self._notification_order: List[str] = [] # Maintain display order (bottom is end of list)

        if self.parent_window:
            self.parent_window.installEventFilter(self)

    def show_notification(self,
                          text: str,
                          timeout_ms: Optional[int] = DEFAULT_TIMEOUT_MS,
                          dismissable_by_user: bool = True,
                          can_timeout: bool = True,
                          notification_id: Optional[str] = None) -> str:
        
        generated_id = notification_id if notification_id is not None else str(uuid.uuid4())

        if generated_id in self._notifications:
            widget = self._notifications[generated_id]
            widget.setText(text) # Update text
            widget.set_can_timeout(can_timeout)
            widget.set_dismissable_by_user(dismissable_by_user)

            # Bring to front of order if it was already there but re-shown
            if generated_id in self._notification_order:
                self._notification_order.remove(generated_id)
            self._notification_order.append(generated_id)


            if can_timeout and timeout_ms is not None:
                 widget.start_timeout(timeout_ms)
            else:
                widget.stop_timeout()
            
            self._recalculate_positions_and_apply(is_update_for_id=generated_id)
            if widget.isHidden(): # If it was hidden, animate it back
                # Find its correct position first without showing
                target_pos = self._calculate_target_pos_for_widget(widget)
                if target_pos:
                    widget.show_animated(target_pos)

            return generated_id

        logger.debug(f"Manager: Showing new notification '{generated_id}': {text[:30]}...")
        notification = NotificationWidget(generated_id, text, self.parent_window)
        notification.dismiss_requested.connect(self._handle_widget_dismiss_request)

        notification.set_can_timeout(can_timeout)
        notification.set_dismissable_by_user(dismissable_by_user)

        self._notifications[generated_id] = notification
        self._notification_order.append(generated_id) 

        self._recalculate_positions_and_apply(is_new_notification_id=generated_id)

        if can_timeout and timeout_ms is not None:
            notification.start_timeout(timeout_ms)

        return generated_id

    def update_notification(self, notification_id: str, new_text: str):
        if notification_id in self._notifications:
            logger.debug(f"Manager: Updating notification '{notification_id}': {new_text[:30]}...")
            widget = self._notifications[notification_id]
            widget.setText(new_text) # This calls adjustSize in widget
            # Manager needs to react to potential height change
            self._recalculate_positions_and_apply(is_update_for_id=notification_id)
        else:
            logger.warning(f"Manager: Attempted to update non-existent notification ID: {notification_id}")

    def dismiss_notification(self, notification_id: str, immediate: bool = False):
        if notification_id in self._notifications:
            logger.debug(f"Manager: Dismissing notification '{notification_id}' (immediate: {immediate})")
            widget = self._notifications[notification_id]
            if immediate:
                widget.hide() 
                self._handle_widget_dismiss_request(notification_id) 
            else:
                widget.hide_animated() 
        else:
            logger.warning(f"Manager: Attempted to dismiss non-existent notification ID: {notification_id}")

    def _handle_widget_dismiss_request(self, notification_id: str):
        logger.debug(f"Manager: Handling dismiss request for '{notification_id}'")
        widget_removed = False
        if notification_id in self._notifications:
            widget = self._notifications.pop(notification_id)
            widget.deleteLater() 
            widget_removed = True
        
        if notification_id in self._notification_order:
            self._notification_order.remove(notification_id)
            widget_removed = True # Even if pop failed, remove from order

        if widget_removed:
            self._recalculate_positions_and_apply()
        else:
            logger.warning(f"Manager: Received dismiss request for already removed/unknown ID: {notification_id}")

    def _calculate_target_pos_for_widget(self, widget: NotificationWidget, current_y_offset: int) -> Optional[QPoint]:
        if not self.parent_window: return None
        parent_width = self.parent_window.width()
        
        # Target Y is current_y_offset (which is bottom edge for this widget) minus its height
        target_y = current_y_offset - widget.height()
        target_x = (parent_width - widget.width()) // 2
        return QPoint(target_x, target_y)

    def _recalculate_positions_and_apply(self, is_new_notification_id: Optional[str] = None, is_update_for_id: Optional[str] = None):
        if not self.parent_window:
            return

        current_y_bottom_edge = self.parent_window.height() - NOTIFICATION_MARGIN_BOTTOM

        # Iterate through _notification_order which is bottom-to-top
        # The last item in _notification_order is the visually lowest (most recent or brought to front)
        for notification_id in reversed(self._notification_order):
            widget = self._notifications.get(notification_id)
            if not widget:
                continue

            target_pos = self._calculate_target_pos_for_widget(widget, current_y_bottom_edge)
            if not target_pos: continue

            if notification_id == is_new_notification_id and widget.isHidden():
                widget.show_animated(target_pos)
            elif widget.isVisible(): # For existing or updated visible widgets
                widget.move_to_animated(target_pos)
            
            current_y_bottom_edge = target_pos.y() - NOTIFICATION_SPACING


    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj == self.parent_window:
            if event.type() == QEvent.Type.Resize:
                # Parent window resized, recalculate positions after a short delay for layout to settle
                QTimer.singleShot(50, lambda: self._recalculate_positions_and_apply() if self.parent_window else None)

            elif event.type() == QEvent.Type.MouseButtonPress:
                if not self.parent_window: return super().eventFilter(obj, event)
                
                # Check if the click is outside any dismissable notification
                # Convert click position to parent window's coordinate system
                click_pos_in_parent = self.parent_window.mapFromGlobal(event.globalPosition().toPoint())

                clicked_on_any_notification = False
                for notification_id in self._notification_order:
                    widget = self._notifications.get(notification_id)
                    if widget and widget.isVisible() and widget.geometry().contains(click_pos_in_parent):
                        clicked_on_any_notification = True
                        break
                
                if not clicked_on_any_notification:
                    ids_to_check = list(self._notification_order) # Iterate on a copy
                    for nid in ids_to_check:
                        widget_to_check = self._notifications.get(nid)
                        if widget_to_check and widget_to_check._dismissable_by_user and widget_to_check.isVisible():
                            widget_to_check.hide_animated() # Let widget handle its dismissal process
        
        return super().eventFilter(obj, event)

    def clear_all_notifications(self, immediate: bool = True):
        ids_to_dismiss = list(self._notification_order) 
        for nid in ids_to_dismiss:
            self.dismiss_notification(nid, immediate=immediate)