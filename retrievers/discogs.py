# services/discogs.py
import discogs_client # Ensure this is present
from discogs_client.exceptions import HTTPError as DiscogsHTTPError # Alias for clarity
import logging
from typing import List, Optional, Tuple, Dict, Any
import threading

from .base_retriever import (
    AbstractImageRetriever, AlbumCandidate, PotentialImage, ImageResult,
    RetrieverError, RetrieverNetworkError, RetrieverAPIError, RetrieverDataError, RetrieverInputError
)
from utils.config import USER_CONFIG

logger = logging.getLogger(__name__)

# Max items to analyze from Discogs search results when finding candidates
MAX_SEARCH_ITEMS_TO_ANALYZE = 20

class DiscogsRetriever(AbstractImageRetriever):
    service_name = "Discogs"

    def __init__(self):
        super().__init__()
        self.client = None
        # No search-specific state stored in the instance anymore.

        discogs_token = USER_CONFIG.get("discogs_token")
        self.has_token = bool(discogs_token)
        if self.has_token:
            try:
                self.client = discogs_client.Client(
                    f"{USER_CONFIG.get('musicbrainz_app_name', 'GenericArtBot')}/{USER_CONFIG.get('musicbrainz_app_version', '0.1')}",
                    user_token=discogs_token
                )
            except Exception as e:
                # Not raising RetrieverError here as it's during construction,
                # but logging is important. The client will remain None.
                logger.error(f"[{self.service_name}] Failed to initialize Discogs client during __init__: {e}", exc_info=True)
        else:
            logger.warning(f"[{self.service_name}] Discogs token not available. Discogs retriever will be non-functional.")

    def search_album_candidates(self, artist: str, album: str, 
                                cancel_event: Optional[threading.Event] = None) -> List[AlbumCandidate]:
        if not self.has_token:
            msg = "Discogs client requires a personal access token."
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverError(msg) 
        if not self.client:
            msg = "Discogs client is not initialized. Cannot search."
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverError(msg) 
        
        if self._check_cancelled(cancel_event, "before starting search_album_candidates"):
            return []

        if not album and not artist: # Or more stringent check if one is always required
            msg = "Both artist and album search terms are empty for Discogs search."
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverInputError(msg)

        logger.info(f"[{self.service_name}] Searching for Discogs candidates: '{album}' by '{artist}'")
        
        results_iterable = None
        search_attempts_failed = False
        try:
            search_params = {'release_title': album, 'type': 'release', 'sort': 'score', 'per_page': MAX_SEARCH_ITEMS_TO_ANALYZE}
            if artist:
                search_params['artist'] = artist
            
            if self._check_cancelled(cancel_event, "before primary Discogs search API call"):
                return []
            logger.debug(f"[{self.service_name}] Primary Discogs search with params: {search_params}")
            primary_results = self.client.search(**search_params) # API call
            results_iterable = primary_results
            if self._check_cancelled(cancel_event, "after primary Discogs search API call"):
                return []

            if not results_iterable or results_iterable.count == 0:
                logger.info(f"[{self.service_name}] Primary Discogs search yielded no results for '{album}' by '{artist}'. Trying fallback.")
                fallback_query = f"{artist} - {album}" if artist else album # Ensure fallback_query is not empty
                if not fallback_query.strip():
                    logger.info(f"[{self.service_name}] Fallback query is empty, skipping fallback search.")
                    return [] # No valid terms to search with

                if self._check_cancelled(cancel_event, "before fallback Discogs search API call"):
                    return []
                logger.debug(f"[{self.service_name}] Fallback Discogs search with query: '{fallback_query}'")
                fallback_results = self.client.search(title=fallback_query, type='release', sort='score', per_page=MAX_SEARCH_ITEMS_TO_ANALYZE) # API call
                if self._check_cancelled(cancel_event, "after fallback Discogs search API call"):
                    return []
                if fallback_results and fallback_results.count > 0:
                    results_iterable = fallback_results
                    logger.info(f"[{self.service_name}] Fallback search yielded {fallback_results.count} results.")
                else:
                    logger.info(f"[{self.service_name}] No Discogs search results after primary and fallback attempts for '{album}' by '{artist}'.")
                    return [] # Genuine "no results" after all attempts
        except requests.exceptions.JSONDecodeError as e_json:
            if self._check_cancelled(cancel_event, f"in JSONDecodeError handler for Discogs API request"):
                return []            
            err_msg = f"Failed to decode JSON response from Discogs API while searching for '{album}' by '{artist}'"
            logger.error(f"[{self.service_name}] {err_msg}: {e_json}.")
            error_url = "Discogs API search endpoint" 
            raise RetrieverDataError(err_msg, original_exception=e_json, url=error_url) from e_json
        except DiscogsHTTPError as e_search:
            if self._check_cancelled(cancel_event, "in Discogs search HTTPError handler"):
                return [] # Propagate cancellation
            # DiscogsHTTPError has status_code and url attributes
            status = e_search.status_code
            # The discogs-client doesn't easily expose the full URL of the failed request in the exception object.
            # We'll use a generic message.
            error_url = "Discogs API search endpoint" 
            custom_msg = f"Discogs API error during search for '{album}' by '{artist}'"
            logger.error(f"[{self.service_name}] {custom_msg}: Status {status}, Error: {e_search}")
            search_attempts_failed = True
            if 400 <= status < 600:
                 raise RetrieverAPIError(custom_msg, status_code=status, url=error_url, original_exception=e_search) from e_search
            else: # Other HTTP errors (less common)
                 raise RetrieverNetworkError(custom_msg, original_exception=e_search, url=error_url) from e_search
        except Exception as e_search_unexpected: # Catch broader exceptions (e.g., network issues not caught as DiscogsHTTPError)
            if self._check_cancelled(cancel_event, "in Discogs search unexpected error handler"):
                return []
            custom_msg = f"Unexpected error during Discogs search for '{album}' by '{artist}'"
            logger.error(f"[{self.service_name}] {custom_msg}: {e_search_unexpected}", exc_info=True)
            search_attempts_failed = True
            # Treat as a generic retriever error, could be network or other library issue.
            raise RetrieverError(custom_msg, original_exception=e_search_unexpected) from e_search_unexpected

        if search_attempts_failed: # Should have been raised, but as a fallback
            return []
        if not results_iterable: # If all paths led to no results_iterable (e.g. empty fallback query)
            logger.info(f"[{self.service_name}] No Discogs results iterable available after search attempts for '{album}' by '{artist}'.")
            return []

        norm_album_query = album.lower().strip()
        norm_artist_query = artist.lower().strip() if artist else ""
        
        temp_exact_candidates: List[AlbumCandidate] = []
        temp_other_candidates: List[AlbumCandidate] = []
        
        stubs_scanned = 0
        processed_ids = set()

        try:
            for i, stub_obj in enumerate(results_iterable): # Iterate up to per_page limit implicitly
                if self._check_cancelled(cancel_event, f"in search results loop (item {i})"):
                    break
                if i >= MAX_SEARCH_ITEMS_TO_ANALYZE: 
                    logger.debug(f"[{self.service_name}] Reached MAX_SEARCH_ITEMS_TO_ANALYZE ({MAX_SEARCH_ITEMS_TO_ANALYZE}) for candidate analysis.")
                    break
                stubs_scanned += 1
                
                stub_id = getattr(stub_obj, 'id', None)
                if not stub_id or stub_id in processed_ids:
                    continue
                processed_ids.add(stub_id)

                stub_type_name = stub_obj.__class__.__name__ 

                try:
                    if self._check_cancelled(cancel_event, f"before accessing data for stub ID {stub_id}"):
                        break
                    
                    # Accessing stub_obj.data or specific attributes can trigger API calls or lazy loading
                    # for some stub types, wrap these accesses.
                    if not hasattr(stub_obj, 'data') or not stub_obj.data:
                        logger.warning(f"[{self.service_name}] Stub (ID: {stub_id}, Type: {stub_type_name}) has no 'data' attribute or it's empty. Skipping.")
                        continue

                    raw_title_from_data = stub_obj.data.get('title', "")
                    if not raw_title_from_data:
                        # This is a data quality issue from Discogs for this item
                        logger.warning(f"[{self.service_name}] Stub (ID: {stub_id}, Type: {stub_type_name}) missing 'title' in data. Skipping.")
                        continue

                    parts = raw_title_from_data.split(' - ', 1)
                    parsed_stub_artist_str = ""
                    parsed_stub_album_str = ""
                    
                    display_artist_name = None 
                    display_album_name = None

                    if len(parts) == 2:
                        parsed_stub_artist_str = parts[0].strip()
                        parsed_stub_album_str = parts[1].strip()
                        display_artist_name = parsed_stub_artist_str
                        display_album_name = parsed_stub_album_str
                    elif len(parts) == 1: 
                        parsed_stub_album_str = parts[0].strip()
                        display_album_name = parsed_stub_album_str
                    else: 
                        logger.warning(f"[{self.service_name}] Stub (ID: {stub_id}) title '{raw_title_from_data}' was empty after checks or split oddly. Using full title as album.")
                        parsed_stub_album_str = raw_title_from_data.strip()
                        display_album_name = raw_title_from_data.strip()

                    match_artist_name_lower = parsed_stub_artist_str.lower()
                    match_album_name_lower = parsed_stub_album_str.lower()

                    is_album_match = (norm_album_query == match_album_name_lower)
                    
                    is_artist_match = False
                    if norm_artist_query: 
                        is_artist_match = norm_artist_query == match_artist_name_lower
                    else: 
                        is_artist_match = True


                    candidate = AlbumCandidate(
                        identifier={'id': stub_id, 'type': stub_type_name, 'title_for_log': raw_title_from_data},
                        album_name=display_album_name,
                        artist_name=display_artist_name, 
                        source_service=self.service_name,
                        extra_data={
                            'discogs_stub_title': raw_title_from_data,
                            'year': stub_obj.data.get('year'),
                            'country': stub_obj.data.get('country')
                        }
                    )

                    if is_album_match and (norm_artist_query and is_artist_match):
                        temp_exact_candidates.append(candidate)
                    elif is_album_match and (not norm_artist_query and is_artist_match): 
                        temp_exact_candidates.append(candidate) 
                    else:
                        temp_other_candidates.append(candidate)

                except discogs_client.exceptions.HTTPError as e_stub_access:
                    if not self._check_cancelled(cancel_event, f"in stub HTTPError handler for ID {stub_id}"):
                        logger.warning(f"[{self.service_name}] HTTPError accessing data for stub (ID: {stub_id}): {e_stub_access}. Skipping.")
                except AttributeError as e_attr:
                    if not self._check_cancelled(cancel_event, f"in stub AttributeError handler for ID {stub_id}"):
                         logger.warning(f"[{self.service_name}] AttributeError accessing data for stub (ID: {stub_id}): {e_attr}. Skipping.")
                except Exception as e_unexpected_stub:
                    if not self._check_cancelled(cancel_event, f"in stub unexpected error handler for ID {stub_id}"):
                        logger.error(f"[{self.service_name}] Unexpected error processing stub (ID: {stub_id}): {e_unexpected_stub}. Skipping.", exc_info=True)
        
        except discogs_client.exceptions.HTTPError as e_page_fetch:
            if not self._check_cancelled(cancel_event, "in results iteration HTTPError handler"):
                logger.error(f"[{self.service_name}] HTTPError iterating Discogs search result pages: {e_page_fetch}. Proceeding with collected.")
        except Exception as e_iter_general:
            if not self._check_cancelled(cancel_event, "in results iteration general error handler"):
                logger.error(f"[{self.service_name}] General error iterating Discogs search results: {e_iter_general}. Proceeding with collected.", exc_info=True)

        all_candidates = temp_exact_candidates + temp_other_candidates
        logger.info(f"[{self.service_name}] Candidate collection: {len(temp_exact_candidates)} exact-ish, {len(temp_other_candidates)} other (from {stubs_scanned} scanned). Total: {len(all_candidates)}")
        return all_candidates

    def list_potential_images(self, candidate: AlbumCandidate, 
                              cancel_event: Optional[threading.Event] = None) -> List[PotentialImage]:
        if not self.client:
            msg = "Discogs client is not initialized. Cannot list potential images."
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverError(msg)

        if not isinstance(candidate.identifier, dict) or \
           'id' not in candidate.identifier or \
           'type' not in candidate.identifier:
            msg = f"Invalid candidate identifier for Discogs list_potential_images: {candidate.identifier}"
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverInputError(msg)

        if self._check_cancelled(cancel_event, "before starting list_potential_images"):
            return []

        discogs_id = candidate.identifier['id']
        item_type = candidate.identifier['type']
        item_title_for_log = candidate.identifier.get('title_for_log', f"{candidate.artist_name} - {candidate.album_name}")

        logger.info(f"[{self.service_name}] Listing potential images for Discogs candidate ID {discogs_id} (Type: {item_type}, Title: '{item_title_for_log}')")

        release_obj_for_images = None
        try:
            if self._check_cancelled(cancel_event, f"before fetching Discogs item ID {discogs_id}"):
                return []
            
            release_obj_for_images_local = None # Use a local var to avoid confusion with outer scope if any
            if item_type == 'Master':
                master = self.client.master(discogs_id) # API call
                if self._check_cancelled(cancel_event, f"after fetching Master ID {discogs_id}, before accessing main_release"):
                    return []
                if master and hasattr(master, 'main_release') and master.main_release:
                    release_obj_for_images_local = master.main_release 
                    logger.debug(f"[{self.service_name}] Accessed main_release (ID: {getattr(release_obj_for_images_local, 'id', 'N/A')}) for Master ID {discogs_id}.")
                else:
                    logger.info(f"[{self.service_name}] Master ID {discogs_id} ('{item_title_for_log}') has no main_release or it's invalid. No images to list.")
                    return [] # Valid "no images for this master's main release"
            elif item_type == 'Release':
                release_obj_for_images_local = self.client.release(discogs_id) # API call
                if self._check_cancelled(cancel_event, f"after fetching Release ID {discogs_id}"):
                    return []
                logger.debug(f"[{self.service_name}] Fetched Release ID {discogs_id} ('{item_title_for_log}').")
            else:
                # This case should ideally be caught by input validation if item_type is restricted.
                # If item_type can be arbitrary, then logging and returning empty is one way.
                # Raising RetrieverInputError might be better if item_type is from a known set.
                msg = f"Unknown item type '{item_type}' for Discogs ID {discogs_id}."
                logger.warning(f"[{self.service_name}] {msg}")
                raise RetrieverInputError(msg) # Or return [] if this is a possible valid state

            if self._check_cancelled(cancel_event, f"before accessing .images for Discogs item {discogs_id}"):
                 return []

            if not release_obj_for_images_local or not hasattr(release_obj_for_images_local, 'images') or not release_obj_for_images_local.images:
                rel_id_log = getattr(release_obj_for_images_local, 'id', 'N/A') if release_obj_for_images_local else 'N/A'
                logger.info(f"[{self.service_name}] Release (ID: {rel_id_log}, from candidate ID: {discogs_id}, Title: '{item_title_for_log}') has no images attribute or .images is empty.")
                return [] # Valid "no images found" for this release.

            potential_images: List[PotentialImage] = []
            # Ensure images is actually a list before sorting, though discogs-client usually provides it.
            images_data_list = release_obj_for_images_local.images
            if not isinstance(images_data_list, list):
                msg = f"Discogs images data for release ID {getattr(release_obj_for_images_local, 'id', 'N/A')} is not a list as expected. Got {type(images_data_list)}."
                logger.error(f"[{self.service_name}] {msg}")
                raise RetrieverDataError(msg)


            sorted_discogs_images_data = sorted(
                images_data_list, 
                key=lambda img_data: (
                    isinstance(img_data, dict) and img_data.get('type') == 'primary', 
                    isinstance(img_data, dict) and img_data.get('width', 0) * img_data.get('height', 0)
                ),
                reverse=True
            )
            
            for img_data in sorted_discogs_images_data:
                if self._check_cancelled(cancel_event, f"in image data loop for Discogs item {discogs_id}"):
                    break
                
                if not isinstance(img_data, dict): # Defensive check
                    logger.warning(f"[{self.service_name}] Encountered non-dict item in images list for {discogs_id}. Skipping item: {type(img_data)}")
                    continue

                full_url = img_data.get('uri')
                thumb_url = img_data.get('uri150')
                
                if not full_url or not thumb_url:
                    logger.debug(f"[{self.service_name}] Image data missing 'uri' or 'uri150' for release {getattr(release_obj_for_images_local, 'id', 'N/A')}. Skipping.")
                    continue

                extra_img_data: Dict[str, Any] = {}
                width, height = img_data.get('width'), img_data.get('height')
                if width is not None and height is not None:
                    try:
                        extra_img_data['width'] = int(width)
                        extra_img_data['height'] = int(height)
                    except ValueError: 
                        logger.debug(f"[{self.service_name}] Could not parse width/height for image {full_url}: w={width}, h={height}")
                
                extra_img_data['discogs_image_type'] = img_data.get('type') # 'primary' or 'secondary'
                
                potential_images.append(PotentialImage(
                    identifier=full_url,
                    thumbnail_url=thumb_url,
                    full_image_url=full_url,
                    source_candidate=candidate,
                    original_type=img_data.get('type'), # Store Discogs type directly
                    extra_data=extra_img_data,
                    is_front=(isinstance(img_data.get('type'), str) and img_data.get('type').lower() == 'primary')
                ))
            
            logger.info(f"[{self.service_name}] Found {len(potential_images)} potential images for Discogs candidate ID {discogs_id}.")
            return potential_images

        except DiscogsHTTPError as e_resolve:
            if self._check_cancelled(cancel_event, f"in list PIs HTTPError for ID {discogs_id}"):
                return [] # Propagate cancellation
            status = e_resolve.status_code
            # The discogs-client doesn't easily expose the full URL of the failed request here either.
            error_url = f"Discogs API item endpoint for ID {discogs_id} (Type: {item_type})"
            custom_msg = f"Discogs API error resolving item ID {discogs_id} (Type: {item_type}, Title: '{item_title_for_log}')"
            logger.warning(f"[{self.service_name}] {custom_msg}: Status {status}, Error: {e_resolve}")
            if 400 <= status < 600:
                 raise RetrieverAPIError(custom_msg, status_code=status, url=error_url, original_exception=e_resolve) from e_resolve
            else: # Other HTTP errors (e.g. connection related if client doesn't map to specific Python error)
                 raise RetrieverNetworkError(custom_msg, original_exception=e_resolve, url=error_url) from e_resolve
        except Exception as e_resolve_unexpected: # Other errors from discogs-client (e.g., parsing issues, unexpected disconnects)
            if self._check_cancelled(cancel_event, f"in list PIs unexpected error for ID {discogs_id}"):
                return []
            custom_msg = f"Unexpected error resolving Discogs item ID {discogs_id} (Type: {item_type}, Title: '{item_title_for_log}')"
            logger.error(f"[{self.service_name}] {custom_msg}: {e_resolve_unexpected}", exc_info=True)
            raise RetrieverError(custom_msg, original_exception=e_resolve_unexpected) from e_resolve_unexpected

    def resolve_image_details(self, potential_image: PotentialImage, 
                              cancel_event: Optional[threading.Event] = None) -> Optional[ImageResult]:
        if self._check_cancelled(cancel_event, "before starting resolve_image_details"):
            return None

        logger.debug(f"[{self.service_name}] Resolving details for Discogs image: {potential_image.full_image_url}")
        width, height = None, None

        if 'width' in potential_image.extra_data and 'height' in potential_image.extra_data:
            width = potential_image.extra_data.get('width')
            height = potential_image.extra_data.get('height')
            if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0) :
                width, height = None, None 
            else:
                 logger.debug(f"[{self.service_name}] Using pre-fetched dimensions from Discogs: {width}x{height} for {potential_image.full_image_url}")
        
        if width is None or height is None:
            if self._check_cancelled(cancel_event, f"before calling get_image_dimensions for {potential_image.full_image_url}"):
                return None
            logger.debug(f"[{self.service_name}] Dimensions not in extra_data or invalid, calling base get_image_dimensions for {potential_image.full_image_url}")
            width, height = super().get_image_dimensions(potential_image.full_image_url, 
                                                         cancel_event=cancel_event)

        if self._check_cancelled(cancel_event, f"after get_image_dimensions for {potential_image.full_image_url}"):
            return None

        if width and height and width > 0 and height > 0:
            original_type_str: Optional[str] = None
            discogs_image_type = potential_image.extra_data.get('discogs_image_type')
            if discogs_image_type and isinstance(discogs_image_type, str):
                original_type_str = discogs_image_type.capitalize()

            return ImageResult.from_potential_image(
                potential_image=potential_image,
                full_width=width,
                full_height=height,
                original_type=original_type_str
            )
        else:
            if not self._check_cancelled(cancel_event, "at end of resolve_image_details (failed to get dimensions)"):
                logger.warning(f"[{self.service_name}] Could not resolve dimensions for Discogs image: {potential_image.full_image_url}")
            return None
