# services/base_retriever.py
import abc
import inspect
import logging
import threading # Added
import requests
from typing import List, Optional, Tuple, Any, Dict, Type
from services.models import PotentialImage, ImageResult, AlbumCandidate

from utils.config import DEFAULT_REQUESTS_HEADERS


logger = logging.getLogger(__name__)


# --- Custom Exceptions ---

class RetrieverError(Exception):
    """Base class for retriever-specific errors."""
    def __init__(self, message, original_exception=None):
        super().__init__(message)
        self.original_exception = original_exception

class RetrieverInputError(ValueError, RetrieverError):
    """Error for invalid input to a retriever method."""
    pass # original_exception is not typically needed here, but can be added if desired

class RetrieverNetworkError(RetrieverError):
    """Error related to network issues during an API call."""
    def __init__(self, message, original_exception=None, url=None):
        super().__init__(message, original_exception)
        self.url = url

class RetrieverAPIError(RetrieverError):
    """Error reported by the external API (e.g., HTTP 4xx/5xx)."""
    def __init__(self, message, status_code=None, url=None, response_text=None, original_exception=None):
        super().__init__(message, original_exception)
        self.status_code = status_code
        self.url = url
        self.response_text = response_text

    @classmethod
    def from_http_error(cls, http_error: requests.exceptions.HTTPError, custom_message: Optional[str] = None):
        """
        Creates a RetrieverAPIError instance from a requests.exceptions.HTTPError.
        """
        status_code = http_error.response.status_code if http_error.response is not None else None
        url_from_request = http_error.request.url if http_error.request is not None else None
        response_text = http_error.response.text if http_error.response is not None else None

        message = custom_message or f"API request to {url_from_request} failed with status {status_code}"
        
        return cls(
            message=message,
            status_code=status_code,
            url=url_from_request,
            response_text=response_text, # Consider truncating if it can be very long
            original_exception=http_error
        )

class RetrieverDataError(RetrieverError):
    """Error parsing or interpreting data from an API response,
    or if essential data is missing from an otherwise successful response."""
    def __init__(self, message, original_exception=None, url=None):
        super().__init__(message, original_exception)
        self.url = url

# --- Abstract Class ---

class AbstractImageRetriever(abc.ABC):
    """
    Abstract base class for all image retriever services.

    This class defines the common interface that all concrete image retriever
    implementations (e.g., for Bandcamp, Discogs, Last.fm) must adhere to.
    It also provides a registry for auto-discovery of retriever subclasses.

    Subclasses must define a `service_name` class attribute, which is used
    for registration and identification. They must be also added as an import
    in retrievers.__init__.py.
    """
    _registry: Dict[str, Type['AbstractImageRetriever']] = {}

    def __init_subclass__(cls, **kwargs):
        """
        Registers concrete subclasses of AbstractImageRetriever in a central registry.
        This method is called automatically when a class inherits from
        AbstractImageRetriever. If the subclass has a `service_name` attribute
        and is not abstract itself, it will be added to the `_registry`.
        """
        super().__init_subclass__(**kwargs)
        service_name_from_class_attr = getattr(cls, 'service_name', None)

        if service_name_from_class_attr and not inspect.isabstract(cls):
            if service_name_from_class_attr in AbstractImageRetriever._registry:
                existing_class = AbstractImageRetriever._registry[service_name_from_class_attr]
                if existing_class is not cls:
                    logger.warning(
                        f"Service name '{service_name_from_class_attr}' already registered by {existing_class.__name__}. "
                        f"Overwriting with {cls.__name__}"
                    )
            AbstractImageRetriever._registry[service_name_from_class_attr] = cls
            logger.debug(f"Registered retriever: {service_name_from_class_attr} -> {cls.__name__}")
        elif not service_name_from_class_attr and not inspect.isabstract(cls) and cls is not AbstractImageRetriever:
             logger.warning(
                f"Concrete retriever {cls.__name__} does not define a 'service_name' "
                "class attribute and will not be auto-registered."
            )

    def __init__(self):
        cls = self.__class__
        service_name_from_class = getattr(cls, 'service_name', None)

        if not service_name_from_class:
            raise ValueError(
                f"Retriever class {cls.__name__} must define a 'service_name' class attribute."
            )
        self.service_name = service_name_from_class
        self.scraper_instance: Optional[Any] = None
        self._cloudscraper_module: Optional[Any] = None
        self._attempted_scraper_init: bool = False


    @classmethod
    def get_retriever_class(cls, service_name: str) -> Optional[Type['AbstractImageRetriever']]:
        """
        Retrieves a registered retriever class by its service name.

        Args:
            service_name (str): The name of the service whose retriever class is to be fetched.

        Returns:
            Optional[Type[AbstractImageRetriever]]: The class of the retriever if found,
                                                    otherwise None.
        """
        return cls._registry.get(service_name)

    @abc.abstractmethod
    def search_album_candidates(self, artist: str, album: str, 
                                cancel_event: Optional[threading.Event] = None) -> List[AlbumCandidate]:
        """
        Searches the service for album candidates matching the given artist and album.

        This method should be implemented by concrete retriever subclasses.
        It is designed to be a **fast operation**, primarily focused on querying the
        service's search endpoint and returning a list of potential matches
        (AlbumCandidate objects) without performing extensive processing or
        fetching detailed information for each candidate. The goal is to quickly
        provide a list of search results.

        Args:
            artist (str): The name of the artist to search for.
            album (str): The name of the album to search for.
            cancel_event (Optional[threading.Event]): An event that can be set to signal
                                                     cancellation of the operation.
                                                     Implementations should check this event
                                                     periodically.

        Returns:
            List[AlbumCandidate]: A list of AlbumCandidate objects representing
                                  potential matches. Returns an empty list if no
                                  matches are found after a successful query.

        Raises:
            RetrieverInputError: If essential input like artist/album is invalid for searching.
            RetrieverNetworkError: If a network issue occurs (e.g., timeout, DNS failure).
            RetrieverAPIError: If the service API returns an error (e.g., 4xx, 5xx status codes).
            RetrieverDataError: If the API response is malformed or essential data is missing.
            RetrieverError: For other retriever-specific operational errors.
        """
        pass

    @abc.abstractmethod
    def list_potential_images(self, candidate: AlbumCandidate, 
                              cancel_event: Optional[threading.Event] = None) -> List[PotentialImage]:
        """
        Lists potential images associated with a given AlbumCandidate.

        This method is called after `search_album_candidates` has identified a
        promising album. It should retrieve a list of image URLs (both thumbnail
        and full-size) for the specified album candidate. It typically involves
        making another request to the service, possibly to a specific album page
        or an image API endpoint related to the candidate.
        Retrievers should do their best to obtain the highest quality images available.

        Args:
            candidate (AlbumCandidate): The album candidate for which to list images.
            cancel_event (Optional[threading.Event]): An event for signalling cancellation.

        Returns:
            List[PotentialImage]: A list of PotentialImage objects. Returns an empty
                                  list if no images are found for the candidate after a
                                  successful query.

        Raises:
            RetrieverInputError: If the provided AlbumCandidate is invalid or missing key info.
            RetrieverNetworkError: If a network issue occurs.
            RetrieverAPIError: If the service API returns an error for this candidate.
            RetrieverDataError: If the API response for image listing is malformed.
            RetrieverError: For other retriever-specific operational errors.
        """
        pass

    @abc.abstractmethod
    def resolve_image_details(self, potential_image: PotentialImage, 
                              cancel_event: Optional[threading.Event] = None) -> Optional[ImageResult]:
        """
        Resolves the full details of a PotentialImage, such as dimensions.

        This method takes a PotentialImage (which primarily contains URLs) and
        fetches more detailed information about the full-size image, most
        importantly its dimensions (width and height). This might involve
        downloading a portion of the image file or querying specific metadata.

        Args:
            potential_image (PotentialImage): The potential image whose details are to be resolved.
            cancel_event (Optional[threading.Event]): An event for signalling cancellation.

        Returns:
            Optional[ImageResult]: An ImageResult object containing the resolved details
                                   if successful. Returns None if details cannot be
                                   determined (e.g., image format not recognized by Pillow,
                                   or a non-critical issue specific to image parsing)
                                   after a successful fetch.

        Raises:
            RetrieverInputError: If the PotentialImage is invalid.
            RetrieverNetworkError: If a network issue occurs while fetching image data/metadata.
            RetrieverAPIError: If the image hosting service returns an error (e.g., 404 for image URL).
            RetrieverDataError: If image metadata from an API (if used) is malformed.
            RetrieverError: For other retriever-specific operational errors.
        """
        pass

    def _try_init_scraper(self) -> bool:
        """
        Tries to import and initialize the cloudscraper instance if not already done.
        This method attempts initialization only once.

        Returns:
            bool: True if scraper_instance is now available, False otherwise.
        """
        if self.scraper_instance: # Already initialized and ready
            return True
        
        if self._attempted_scraper_init: # If true, means we tried once and self.scraper_instance is still None.
            logger.debug(f"[{self.service_name}] Previous attempt to initialize scraper failed or module not found. Not retrying.")
            return False

        self._attempted_scraper_init = True # Mark that we are now attempting the full initialization.

        try:
            # Local import to avoid top-level dependency if not used
            import cloudscraper as cs 
            self._cloudscraper_module = cs
            logger.info(f"[{self.service_name}] Successfully imported cloudscraper module.")
        except ImportError:
            logger.warning(f"[{self.service_name}] cloudscraper module not found. Scraper fallback will be unavailable.")
            return False # Cannot initialize if module is not found

        try:
            self.scraper_instance = self._cloudscraper_module.create_scraper()
            logger.info(f"[{self.service_name}] Cloudscraper instance created successfully.")
            return True
        except Exception as e:
            logger.error(f"[{self.service_name}] Failed to create cloudscraper instance: {e}", exc_info=True)
            self.scraper_instance = None # Ensure it's None if creation failed
            return False

    def _execute_http_get( # Renamed from _perform_http_get_request
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: int = 15, # Increased default timeout slightly, can be overridden
        cancel_event: Optional[threading.Event] = None,
        request_context: str = "HTTP GET request",
        scraper_instance: Optional[Any] = None # For Cloudflare-protected sites like Bandcamp
    ) -> Optional[requests.Response]:
        """
        Performs an HTTP GET request and returns the raw response object.

        Handles common network errors, API errors (HTTP status codes), and cancellation.
        Allows for using a custom scraper instance (e.g., cloudscraper).

        Args:
            url (str): The URL to request.
            params (Optional[Dict[str, Any]]): Query parameters.
            extra_headers (Optional[Dict[str, str]]): Additional headers.
            timeout (int): Request timeout in seconds.
            cancel_event (Optional[threading.Event]): Event for cancellation.
            request_context (str): Context for logging/error messages.
            scraper_instance (Optional[Any]): A requests-compatible scraper instance.
                                              If None, `requests.get` is used.

        Returns:
            Optional[requests.Response]: The response object if successful.
                                         None if cancelled before an exception.
        Raises:
            RetrieverNetworkError: For network issues.
            RetrieverAPIError: For HTTP 4xx/5xx errors.
            RetrieverError: For other unexpected errors.
        """
        if self._check_cancelled(cancel_event, f"before making {request_context} to {url}"):
            return None

        current_headers = DEFAULT_REQUESTS_HEADERS.copy()
        if extra_headers:
            current_headers.update(extra_headers)

        requester = scraper_instance if scraper_instance else requests
        
        logger.debug(f"[{self.service_name}] Making {request_context} to {url} with params: {params} using {'scraper' if scraper_instance else 'requests'}")
        response_obj = None
        try:
            response_obj = requester.get(url, params=params, headers=current_headers, timeout=timeout, allow_redirects=True)
            response_obj.raise_for_status()  # Raises HTTPError for 4xx/5xx
            return response_obj
        except requests.exceptions.Timeout as e_timeout:
            if self._check_cancelled(cancel_event, f"in Timeout handler for {request_context} to {url}"):
                return None
            err_msg = f"Timeout during {request_context} to {url}"
            logger.warning(f"[{self.service_name}] {err_msg}: {e_timeout}")
            raise RetrieverNetworkError(err_msg, original_exception=e_timeout, url=url) from e_timeout
        except requests.exceptions.HTTPError as e_http:
            if self._check_cancelled(cancel_event, f"in HTTPError handler for {request_context} to {url}"):
                return None
            err_msg = f"HTTP error during {request_context} to {url}"
            logger.warning(f"[{self.service_name}] {err_msg} - Status: {e_http.response.status_code if e_http.response else 'Unknown'}")
            raise RetrieverAPIError.from_http_error(e_http, custom_message=err_msg) from e_http
        except requests.exceptions.ConnectionError as e_conn: # Covers DNS, Refused, etc.
            if self._check_cancelled(cancel_event, f"in ConnectionError handler for {request_context} to {url}"):
                return None
            err_msg = f"Connection error during {request_context} to {url}"
            logger.warning(f"[{self.service_name}] {err_msg}: {e_conn}")
            raise RetrieverNetworkError(err_msg, original_exception=e_conn, url=url) from e_conn
        except requests.exceptions.RequestException as e_req: # Base class for other requests exceptions
            if self._check_cancelled(cancel_event, f"in RequestException handler for {request_context} to {url}"):
                return None
            err_msg = f"Generic request exception during {request_context} to {url}"
            logger.error(f"[{self.service_name}] {err_msg}: {e_req}")
            raise RetrieverNetworkError(err_msg, original_exception=e_req, url=url) from e_req
        except Exception as e_gen: # Catch-all, e.g. cloudscraper specific errors not inheriting from RequestException
            if self._check_cancelled(cancel_event, f"in generic exception handler for {request_context} to {url}"):
                 return None
            # Check if it's a cloudscraper ChallengeError and provide a more specific message
            if scraper_instance and type(e_gen).__name__ == 'CloudflareChallengeError': # Avoid direct import of cloudscraper here
                 err_msg = f"Cloudflare challenge failed for {request_context} to {url}"
                 logger.warning(f"[{self.service_name}] {err_msg}: {e_gen}")
                 raise RetrieverAPIError(err_msg, status_code=503, url=url, original_exception=e_gen) from e_gen # Treat as service unavailable

            err_msg = f"Unexpected error during {request_context} to {url}"
            logger.error(f"[{self.service_name}] {err_msg}: {e_gen}", exc_info=True)
            raise RetrieverError(err_msg, original_exception=e_gen) from e_gen

    def _perform_http_get_request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: int = 15,
        cancel_event: Optional[threading.Event] = None,
        request_context: str = "HTTP GET request",
        expect_html_cloudflare: bool = False # Hint for scraper fallback
    ) -> Optional[requests.Response]:
        """
        Performs an HTTP GET request with optional Cloudscraper fallback for HTML pages.
        """
        # Initial attempt: use existing scraper if available, otherwise use basic requests
        current_scraper_to_use = self.scraper_instance
        
        try:
            logger.debug(f"[{self.service_name}] Attempting {request_context} to {url} (Initial scraper: {'yes' if current_scraper_to_use else 'no'})")
            return self._execute_http_get(
                url, params, extra_headers, timeout, cancel_event, request_context,
                scraper_instance=current_scraper_to_use
            )
        except RetrieverAPIError as e:
            # Condition to try scraper fallback:
            # 1. expect_html_cloudflare is True.
            # 2. The error is a common Cloudflare block status code (403, 503).
            # 3. No scraper was used in the initial attempt (current_scraper_to_use was None).
            if expect_html_cloudflare and e.status_code in [403, 503] and current_scraper_to_use is None:
                logger.warning(
                    f"[{self.service_name}] {request_context} to {url} failed with status {e.status_code}. "
                    f"Attempting Cloudscraper fallback."
                )
                if self._try_init_scraper() and self.scraper_instance:
                    logger.info(f"[{self.service_name}] Retrying {request_context} to {url} with Cloudscraper.")
                    try:
                        # Retry with the now-initialized scraper
                        return self._execute_http_get(
                            url, params, extra_headers, timeout, cancel_event, request_context,
                            scraper_instance=self.scraper_instance
                        )
                    except Exception as e_retry:
                        logger.warning(f"[{self.service_name}] Cloudscraper fallback for {request_context} to {url} also failed: {type(e_retry).__name__} - {e_retry}")
                        raise e_retry # Propagate the error from the scraper attempt
                else:
                    logger.warning(
                        f"[{self.service_name}] Cloudscraper initialization failed or module not available. "
                        f"Propagating original error for {request_context} to {url}."
                    )
                    raise e # Propagate original error
            else:
                # Not a CF-suggestive error, or scraper was already tried, or not expecting CF.
                raise e
        except RetrieverDataError as e_data:
            # Placeholder: If a RetrieverDataError could also indicate a Cloudflare page,
            # similar logic could be added here, possibly checking e_data.url or some content hint.
            # For now, primarily relying on RetrieverAPIError for automatic fallback.
            # If expect_html_cloudflare and current_scraper_to_use is None and "some specific content":
            #    ... try scraper ...
            raise e_data


    def _make_generic_json_request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: int = 10, # Specific timeout for JSON APIs, can be shorter
        cancel_event: Optional[threading.Event] = None,
        request_context: str = "JSON API request" 
    ) -> Optional[Any]: # Returns parsed JSON data, or None if cancelled before exception
        """
        Makes a GET request expecting a JSON response, using _perform_http_get_request.

        Handles JSON decoding and related errors.

        Args:
            url (str): The URL to request.
            params (Optional[Dict[str, Any]]): Query parameters.
            extra_headers (Optional[Dict[str, str]]): Additional headers.
            timeout (int): Request timeout in seconds.
            cancel_event (Optional[threading.Event]): Event for cancellation.
            request_context (str): Context for logging/error messages.

        Returns:
            Optional[Any]: Parsed JSON response or None if cancelled.
        Raises:
            RetrieverNetworkError, RetrieverAPIError: Propagated from _perform_http_get_request.
            RetrieverDataError: If JSON decoding fails or response is not JSON when expected.
            RetrieverError: For other unexpected errors.
        """
        # _check_cancelled for "before making request" is handled by _perform_http_get_request
        
        # Ensure "Accept: application/json" is typically set for JSON APIs,
        # though DEFAULT_REQUESTS_HEADERS might already cover common needs.
        # If specific JSON APIs need it explicitly, it can be added here or in extra_headers.
        # merged_headers = DEFAULT_REQUESTS_HEADERS.copy()
        # merged_headers.update({'Accept': 'application/json'}) # Example
        # if extra_headers:
        #     merged_headers.update(extra_headers)

        response_obj = self._perform_http_get_request(
            url=url,
            params=params,
            extra_headers=extra_headers, 
            timeout=timeout,
            cancel_event=cancel_event,
            request_context=request_context,
            expect_html_cloudflare=False # JSON APIs typically don't need scraper fallback
        )

        if response_obj is None: # Cancelled during _perform_http_get_request before an exception
            return None

        try:
            return response_obj.json()
        except requests.exceptions.JSONDecodeError as e_json:
            if self._check_cancelled(cancel_event, f"in JSONDecodeError handler for {request_context} to {url}"):
                return None # Technically, cancellation check here is redundant if _perform_http_get_request handled it.
            
            response_text_snippet = response_obj.text[:500] if hasattr(response_obj, 'text') else "N/A"
            err_msg = f"Failed to decode JSON response from {request_context} to {url}"
            logger.error(f"[{self.service_name}] {err_msg}: {e_json}. Response text snippet: {response_text_snippet}")
            # Pass the original exception to RetrieverDataError
            raise RetrieverDataError(err_msg, original_exception=e_json, url=url) from e_json
        except Exception as e_gen: # Catch-all for other unexpected errors during JSON processing
             if self._check_cancelled(cancel_event, f"in generic exception handler for JSON processing {request_context} to {url}"):
                 return None
             err_msg = f"Unexpected error processing JSON response from {request_context} to {url}"
             logger.error(f"[{self.service_name}] {err_msg}: {e_gen}", exc_info=True)
             raise RetrieverError(err_msg, original_exception=e_gen) from e_gen

    def _check_cancelled(self, cancel_event: Optional[threading.Event], context: str = "") -> bool:
        """
        Helper method to check if a cancellation event has been set.

        If the event is set, a debug log message is emitted.

        Args:
            cancel_event (Optional[threading.Event]): The cancellation event to check.
            context (str): A string describing the context of the cancellation check,
                           used for logging.

        Returns:
            bool: True if the operation should be cancelled, False otherwise.
        """
        if cancel_event and cancel_event.is_set():
            logger.debug(f"[{self.service_name}] Operation cancelled: {context}")
            return True
        return False

    def get_image_dimensions(self, image_url: str, extra_headers: Optional[dict] = None,
                             cancel_event: Optional[threading.Event] = None) -> Tuple[Optional[int], Optional[int]]:
        """
        Retrieves the dimensions (width, height) of an image from its URL.

        This method attempts to download just enough of the image to determine
        its dimensions using the PIL/Pillow library, without downloading the
        entire file if possible. It handles request errors, timeouts, and
        cancellation.

        **Note for implementers of derived classes:** If the specific API for your
        service provides a more direct or efficient way to obtain image dimensions
        (e.g., as part of metadata in an API response, or through specific image
        info endpoints), you should override this method in your subclass to
        leverage that more efficient approach.

        Args:
            image_url (str): The URL of the image.
            extra_headers (Optional[dict]): Additional HTTP headers to include in the request.
            cancel_event (Optional[threading.Event]): An event for signalling cancellation.

        Returns:
            Tuple[Optional[int], Optional[int]]: A tuple containing (width, height)
                                                 if successful, otherwise (None, None).
        """
        from PIL import Image
        from io import BytesIO
        
        def _check_cancelled_local(context: str = ""): # Renamed to avoid conflict with self._check_cancelled
            if cancel_event and cancel_event.is_set():
                logger.debug(f"[{self.service_name}] Image dimension check for {image_url} cancelled: {context}")
                return True
            return False

        if _check_cancelled_local("before request"):
            return None, None
        
        response_obj = None
        try:
            current_headers = DEFAULT_REQUESTS_HEADERS.copy()
            if extra_headers:
                current_headers.update(extra_headers)

            # Use a timeout to prevent indefinite blocking, allowing cancel checks
            response_obj = requests.get(image_url, stream=True, headers=current_headers, timeout=10)
            response_obj.raise_for_status()

            if _check_cancelled_local("after request headers, before content"):
                return None, None

            image_data = BytesIO()
            chunk_size = 1024 # Smaller chunk for more frequent cancel checks
            bytes_read = 0
            # Max bytes to read for just getting dimensions, to avoid downloading huge files entirely
            max_bytes_to_read_for_dims = 128 * 1024 # Increased slightly

            for chunk in response_obj.iter_content(chunk_size=chunk_size):
                if not chunk: 
                    break # Should not happen with successful status if content-length > 0

                if _check_cancelled_local("during chunk iteration"):
                    return None, None
                
                image_data.write(chunk)
                bytes_read += len(chunk)
                
                # Try to open image after a certain amount of data is read
                # This threshold can be adjusted. 2048 is often enough for headers.
                if bytes_read > 2048: 
                    try:
                        image_data.seek(0)
                        img = Image.open(image_data)
                        # Once opened, we have dimensions, no need to read further for this method's purpose
                        return img.size
                    except Exception: # PIL.UnidentifiedImageError or other PIL issues
                        # If it's not enough data yet, continue reading up to max_bytes_to_read_for_dims
                        if bytes_read >= max_bytes_to_read_for_dims:
                            logger.warning(f"Could not determine dimensions for {image_url} after reading {bytes_read} bytes (max: {max_bytes_to_read_for_dims}). Image might be incomplete or corrupt at this point.")
                            return None, None
                        # else, continue loop to get more data
            
            # If loop finished (e.g., small image fully read before 2048 bytes, or read up to max_bytes)
            if bytes_read > 0:
                try:
                    image_data.seek(0)
                    img = Image.open(image_data)
                    return img.size
                except Exception as e_pil:
                    logger.warning(f"Final attempt to get dimensions for {image_url} (read {bytes_read} bytes) failed: {e_pil}")
                    return None, None
            else:
                logger.warning(f"No data received for image dimension check (0 bytes read): {image_url}")
                return None, None

        except requests.exceptions.Timeout as e_timeout:
            if _check_cancelled_local("in Timeout handler"): return None, None
            logger.warning(f"Request timed out for image dimension check: {image_url}")
            raise RetrieverNetworkError(f"Timeout accessing {image_url}", original_exception=e_timeout, url=image_url) from e_timeout
        except requests.exceptions.HTTPError as e_http: # Should be caught if raise_for_status() is used
            if _check_cancelled_local("in HTTPError handler"): return None, None
            # The logger warning can still be useful for immediate context
            logger.warning(f"HTTP error {e_http.response.status_code if e_http.response else 'Unknown'} for image dimension check {image_url}: {e_http}")
            raise RetrieverAPIError.from_http_error(e_http, custom_message=f"Failed image dimension check for {image_url}") from e_http
        except requests.exceptions.RequestException as e_req: # Catches other network errors like ConnectionError
            if _check_cancelled_local("in RequestException handler"): return None, None
            logger.warning(f"Request failed for image dimension check {image_url}: {e_req}")
            raise RetrieverNetworkError(f"Request failed for {image_url}: {e_req}", original_exception=e_req, url=image_url) from e_req
        except Exception as e_gen: # Catch-all for unexpected errors, like PIL errors if not handled above
            if _check_cancelled_local("in generic Exception handler"): return None, None
            logger.error(f"Generic error during image dimension check for {image_url}: {e_gen}", exc_info=True)
            # This could be a more generic RetrieverError or re-raised if it's critical
            # For now, returning None, None for non-network/API PIL issues or truly unexpected things.
            # If this path is hit by something other than a PIL issue, it might need more specific handling.
            return None, None
        finally:
            if response_obj: 
                response_obj.close()