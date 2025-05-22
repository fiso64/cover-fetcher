# utils/image_fetcher.py
import threading
from typing import Dict, Optional, Tuple
import logging
from utils.config import DEFAULT_REQUESTS_HEADERS
from PySide6.QtCore import (
    QObject, QUrl, Signal, Slot, QMetaObject, Q_ARG, QByteArray, QThread, Qt, QTimer
)
import shiboken6
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

logger = logging.getLogger(__name__)

# Define a type for the callback information bundle
# Stores: (receiver_QObject, success_slot_name_str, error_slot_name_str, original_QUrl_of_request)
CallbackInfo = Tuple[QObject, str, str, QUrl]

class ImageFetcher(QObject):
    cache_primed = Signal(str)  # url (when cache is primed externally)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._cache: Dict[str, bytes] = {}
        # Maps an active QNetworkReply to the information needed for its callback
        self._reply_to_callback_map: Dict[QNetworkReply, CallbackInfo] = {}
        self.nam: Optional[QNetworkAccessManager] = None
        # The QNetworkAccessManager will be created in the `initialize_manager` slot,
        # which should be called after this object is moved to its QThread.

    @Slot()
    def initialize_manager(self):
        """
        Initializes the QNetworkAccessManager. This should be called after
        the ImageFetcher has been moved to its designated QThread,
        so that NAM is created in the correct thread.
        """
        if self.nam is None:
            self.nam = QNetworkAccessManager()
            logger.info("[ImageFetcher] QNetworkAccessManager initialized in its thread.")
        else:
            logger.warning("[ImageFetcher] QNetworkAccessManager already initialized.")

    @Slot(str)
    def get_from_cache(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        return self._cache.get(url)

    @Slot(str, bytes, bool) # url, data, overwrite
    def prime_cache_slot(self, url: str, data: bytes, overwrite: bool = False) -> None:
        if not url or not data:
            return
        # This runs in ImageFetcher's thread, so direct cache access is safe.
        if url not in self._cache or overwrite:
            self._cache[url] = data
            logger.debug(f"[ImageFetcher] Cache primed for URL: {url}")
            self.cache_primed.emit(url)
        else:
            logger.debug(f"[ImageFetcher] Cache already contains URL, not overwriting: {url}")

    @Slot(str, QObject, str, str, int)
    def request_image_data(self, url_str: str,
                           receiver: QObject,
                           success_slot_name: str,
                           error_slot_name: str,
                           timeout_sec: int = 30):
        """
        Asynchronously requests image data.
        Callbacks (success_slot_name, error_slot_name) on the receiver object
        will be invoked with Qt.QueuedConnection.
        Success slot signature: your_slot_name(QUrl original_url, QByteArray image_data)
        Error slot signature:   your_slot_name(QUrl original_url, str error_message)
        """
        if self.nam is None:
            msg = "ImageFetcher QNetworkAccessManager not initialized."
            logger.error(f"[ImageFetcher] {msg} Cannot process request for {url_str}.")
            if shiboken6.isValid(receiver) and receiver.thread() != QThread.currentThread():
                QMetaObject.invokeMethod(receiver, error_slot_name, Qt.QueuedConnection,
                                         Q_ARG(QUrl, QUrl(url_str)), Q_ARG(str, msg))
            else:
                logger.warning(f"[ImageFetcher] Receiver {receiver} invalid or in same thread for init error callback.")
            return

        if not url_str:
            msg = "No URL provided for image request."
            logger.warning(f"[ImageFetcher] {msg}")
            if shiboken6.isValid(receiver):
                QMetaObject.invokeMethod(receiver, error_slot_name, Qt.QueuedConnection,
                                         Q_ARG(QUrl, QUrl()), Q_ARG(str, msg))
            return

        q_url = QUrl(url_str)
        if not q_url.isValid():
            msg = f"Invalid URL for image request: {url_str}"
            logger.error(f"[ImageFetcher] {msg}")
            if shiboken6.isValid(receiver):
                QMetaObject.invokeMethod(receiver, error_slot_name, Qt.QueuedConnection,
                                         Q_ARG(QUrl, q_url), Q_ARG(str, msg))
            return

        cached_data = self._cache.get(url_str) # cached_data is Python bytes
        if cached_data:
            logger.debug(f"[ImageFetcher] Cache hit for URL: {url_str}. Invoking success callback for {receiver.objectName() if receiver.objectName() else receiver}.")
            if shiboken6.isValid(receiver):
                # For cache hit, data is Python bytes, convert to QByteArray for the slot
                QMetaObject.invokeMethod(receiver, success_slot_name, Qt.QueuedConnection,
                                         Q_ARG(QUrl, q_url),
                                         Q_ARG(QByteArray, QByteArray(cached_data)))
            return

        # Simple duplicate active request check for the same receiver, url, and slots.
        for reply_obj, cb_info_tuple in self._reply_to_callback_map.items():
            # cb_info_tuple: (receiver_obj, success_slot_str, error_slot_str, original_qurl_obj)
            if cb_info_tuple[3] == q_url and cb_info_tuple[0] == receiver and \
               cb_info_tuple[1] == success_slot_name and cb_info_tuple[2] == error_slot_name:
                logger.info(f"[ImageFetcher] Request for URL {url_str} by receiver {receiver.objectName() if receiver.objectName() else receiver} with same slots already in progress. Ignoring.")
                return

        logger.info(f"[ImageFetcher] Fetching image data from URL: {url_str} for receiver {receiver.objectName() if receiver.objectName() else receiver}; success_slot: {success_slot_name}, error_slot: {error_slot_name}")
        request = QNetworkRequest(q_url)
        for key, value in DEFAULT_REQUESTS_HEADERS.items():
            request.setRawHeader(key.encode('utf-8'), value.encode('utf-8'))

        if hasattr(request, 'setTransferTimeout'):  # Qt 5.15+
            request.setTransferTimeout(timeout_sec * 1000)
        else: # Fallback QTimer for timeout if setTransferTimeout is not available
            # For simplicity, relying on default NAM timeouts if setTransferTimeout is not available
            # Implementing a QTimer-based timeout per request adds complexity here.
            pass


        reply: QNetworkReply = self.nam.get(request)
        
        # Store callback information mapped to this reply
        callback_info: CallbackInfo = (receiver, success_slot_name, error_slot_name, q_url)
        self._reply_to_callback_map[reply] = callback_info

        reply.finished.connect(self._handle_reply_finished_targeted)

    def _handle_reply_finished_targeted(self):
        reply = self.sender()
        if not isinstance(reply, QNetworkReply):
            logger.error("[ImageFetcher] _handle_reply_finished_targeted called by non-QNetworkReply sender.")
            return

        # Retrieve and remove callback information for this reply
        callback_info = self._reply_to_callback_map.pop(reply, None)

        if not callback_info:
            # This can happen if quit_manager aborted and cleaned up the reply already
            logger.debug(f"[ImageFetcher] Reply finished for {reply.url().toString()}, but no callback info (possibly aborted/cleaned).")
            reply.deleteLater()
            return

        receiver, success_slot_str, error_slot_str, original_qurl = callback_info # slot names are now str

        if not shiboken6.isValid(receiver):
            logger.warning(f"[ImageFetcher] Receiver for {original_qurl.toString()} is no longer valid. Discarding reply.")
            reply.deleteLater()
            return

        if reply.error() == QNetworkReply.NetworkError.NoError:
            image_qbytearray = reply.readAll() # Get QByteArray directly
            if not image_qbytearray.isEmpty():
                # Cache the data; converting to Python bytes for the cache is fine if cache expects bytes
                self._cache[original_qurl.toString()] = image_qbytearray.data() 
                logger.debug(f"[ImageFetcher] Successfully fetched & cached image for URL: {original_qurl.toString()} for {receiver.objectName() if receiver.objectName() else receiver}")
                QMetaObject.invokeMethod(receiver, success_slot_str, Qt.QueuedConnection,
                                         Q_ARG(QUrl, original_qurl),
                                         Q_ARG(QByteArray, image_qbytearray)) # Pass the QByteArray
            else:
                msg = f"No data received from {original_qurl.toString()} despite NoError status."
                logger.warning(f"[ImageFetcher] {msg}")
                QMetaObject.invokeMethod(receiver, error_slot_str, Qt.QueuedConnection,
                                         Q_ARG(QUrl, original_qurl), Q_ARG(str, msg))
        elif reply.error() == QNetworkReply.NetworkError.OperationCanceledError:
            msg = f"Operation timed out or aborted for URL: {original_qurl.toString()}"
            logger.info(f"[ImageFetcher] {msg}") # Changed to INFO as it's a common case for aborts
            QMetaObject.invokeMethod(receiver, error_slot_str, Qt.QueuedConnection,
                                     Q_ARG(QUrl, original_qurl), Q_ARG(str, msg))
        else:
            error_string = reply.errorString()
            msg = f"Network error fetching image from {original_qurl.toString()}: {error_string} (Error code: {reply.error()})"
            logger.error(f"[ImageFetcher] {msg}")
            QMetaObject.invokeMethod(receiver, error_slot_str, Qt.QueuedConnection,
                                     Q_ARG(QUrl, original_qurl), Q_ARG(str, error_string))
        
        reply.deleteLater()

    @Slot()
    def clear_cache_slot(self) -> None:
        # This runs in ImageFetcher's thread.
        self._cache.clear()
        logger.info("[ImageFetcher] Cache cleared.")

    @Slot()
    def quit_manager(self):
        """Prepares the ImageFetcher for shutdown by aborting active replies."""
        logger.info("[ImageFetcher] Quitting manager. Aborting active network replies.")
        
        replies_to_process = list(self._reply_to_callback_map.keys())
        
        if not replies_to_process:
            logger.info("[ImageFetcher] No active replies to abort during quit.")
        else:
            logger.info(f"[ImageFetcher] Attempting to abort {len(replies_to_process)} active replies during quit.")

        for reply in replies_to_process:
            if shiboken6.isValid(reply):
                self._reply_to_callback_map.pop(reply, None) 
                try:
                    reply.finished.disconnect(self._handle_reply_finished_targeted)
                except RuntimeError: 
                    pass
                except TypeError: 
                    pass 
                
                if reply.isRunning(): 
                    logger.debug(f"[ImageFetcher] Aborting reply for URL: {reply.url().toString()} during quit.")
                    reply.abort()
            else: 
                self._reply_to_callback_map.pop(reply, None)
        
        self._reply_to_callback_map.clear()
        
        if self.nam:
            # QNetworkAccessManager handles deletion of its child replies if they weren't manually deleted.
            # No further explicit cleanup of NAM might be needed here if it's parented or deleted later.
            pass
        logger.info("[ImageFetcher] Manager quit preparation complete.")

class ImageFetcherWorker(QObject):
    image_data_ready = Signal(QByteArray)
    error_occurred = Signal(str)

    def __init__(self, image_url: str, image_fetcher: ImageFetcher, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.image_url = image_url
        self.image_fetcher = image_fetcher
        self._is_cancelled = False 

    @Slot()
    def run(self):
        if self._is_cancelled: # Check early if cancelled before even requesting
            # self.error_occurred.emit("Fetch cancelled.") # No request made yet, could just return
            logger.debug(f"[ImageFetcherWorker] Run called on cancelled worker for {self.image_url}")
            return

        try:
            if not self.image_url:
                # Emit error and return, as request cannot be made.
                self.error_occurred.emit("Full image URL is empty.")
                return
            if not self.image_fetcher:
                # Emit error and return.
                self.error_occurred.emit("Image fetcher service not available.")
                return

            logger.debug(f"[ImageFetcherWorker] Requesting image data for {self.image_url} via ImageFetcher.")
            QMetaObject.invokeMethod(
                self.image_fetcher,
                "request_image_data",
                Qt.QueuedConnection,
                Q_ARG(str, self.image_url),
                Q_ARG(QObject, self),
                Q_ARG(str, "on_fetcher_data_ready"),
                Q_ARG(str, "on_fetcher_error"),
                Q_ARG(int, 60)  # Timeout for full image view, e.g., 60 seconds
            )
        except Exception as e: # Catch issues invoking the method, though rare
            logger.error(f"[ImageFetcherWorker] Unexpected error during request_image_data invocation for ({self.image_url}): {e}", exc_info=True)
            if not self._is_cancelled:
                self.error_occurred.emit(f"An unexpected error occurred initiating image fetch: {str(e)}")

    def cancel(self): 
        self._is_cancelled = True

    @Slot(QUrl, QByteArray)
    def on_fetcher_data_ready(self, url: QUrl, image_data_qba: QByteArray):
        if self._is_cancelled:
            logger.debug(f"[ImageFetcherWorker] Data ready for {url.toString()}, but worker is cancelled.")
            return
        if url.toString() != self.image_url:
            logger.warning(f"[ImageFetcherWorker] Data ready for unexpected URL {url.toString()} (expected {self.image_url}). Ignoring.")
            return

        logger.debug(f"[ImageFetcherWorker] Image data received for {self.image_url}.")
        self.image_data_ready.emit(image_data_qba)

    @Slot(QUrl, str)
    def on_fetcher_error(self, url: QUrl, error_message: str):
        if self._is_cancelled:
            logger.debug(f"[ImageFetcherWorker] Error for {url.toString()}, but worker is cancelled: {error_message}")
            return
        if url.toString() != self.image_url:
            logger.warning(f"[ImageFetcherWorker] Error for unexpected URL {url.toString()} (expected {self.image_url}): {error_message}. Ignoring.")
            return

        logger.error(f"[ImageFetcherWorker] ImageFetcher reported error for {self.image_url}: {error_message}")
        self.error_occurred.emit(f"Failed to load image data: {error_message} (URL: {self.image_url})")
        