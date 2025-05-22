# services/itunes.py
import logging
import re
from typing import List, Optional, Tuple, Dict, Any
import threading
import requests

from .base_retriever import (
    AbstractImageRetriever, AlbumCandidate, PotentialImage, ImageResult,
    RetrieverError, RetrieverNetworkError, RetrieverAPIError, RetrieverDataError, RetrieverInputError
)
from utils.config import DEFAULT_REQUESTS_HEADERS

logger = logging.getLogger(__name__)

# Max results to request from iTunes Search API
ITUNES_SEARCH_LIMIT = 25
# Can also use .png instead of .jpg here for potentially even higher quality images,
# at the cost of significantly slower image dimensions resolves
MAXRES_EXTENSION = "999999999x0w-999.jpg"
TARGET_THUMBNAIL_DIMENSION_STRING = "300x300"

class ITunesRetriever(AbstractImageRetriever):
    service_name = "iTunes"

    def __init__(self):
        super().__init__()
        self.base_search_url = "https://itunes.apple.com/search"
        
    def _derive_image_urls(self, base_artwork_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Derives a target-sized thumbnail and the best possible full resolution image URL
        from a base iTunes artwork URL.
        """
        if not base_artwork_url:
            return None, None

        # Initialize with base_artwork_url as fallback
        derived_thumb_url = base_artwork_url
        derived_full_url = base_artwork_url

        match = re.search(r'^(.*)/(\d+x\d+)([^/]*)$', base_artwork_url)
        
        if match:
            url_prefix_part = match.group(1)
            original_dim_part_str = match.group(2) # e.g., "100x100"
            suffix_part = match.group(3)           # e.g., "bb.jpg"

            # 1. Derive Thumbnail URL using TARGET_THUMBNAIL_DIMENSION_STRING
            derived_thumb_url = f"{url_prefix_part}/{TARGET_THUMBNAIL_DIMENSION_STRING}{suffix_part}"

            # 2. Derive Full Image URL
            current_best_full_url = base_artwork_url # Start with original
            original_w = 0
            try:
                w_str, _ = original_dim_part_str.split('x', 1)
                original_w = int(w_str)
            except (ValueError, IndexError):
                # If original_w cannot be determined, we'll be more aggressive in replacing it
                logger.debug(f"[{self.service_name}] Could not parse width from original_dim_part: {original_dim_part_str} in {base_artwork_url}")

            derived_full_url = f"{url_prefix_part}/{MAXRES_EXTENSION}"
        
        # Ensure HTTPS for all URLs
        if derived_thumb_url and derived_thumb_url.startswith("http://"):
            derived_thumb_url = "https://" + derived_thumb_url[len("http://"):]
        if derived_full_url and derived_full_url.startswith("http://"):
            derived_full_url = "https://" + derived_full_url[len("http://"):]
            
        return derived_thumb_url, derived_full_url

    def search_album_candidates(self, artist: str, album: str, 
                                cancel_event: Optional[threading.Event] = None) -> List[AlbumCandidate]:
        if self._check_cancelled(cancel_event, "before starting search_album_candidates"):
            return []
        
        artist = artist if artist else ""
        search_term = f"{artist} {album}".strip()
        if not search_term:
            msg = "Both artist and album search terms are effectively empty."
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverInputError(msg)

        logger.info(f"[{self.service_name}] Searching for iTunes albums: '{search_term}'")
        
        params = {
            "term": search_term,
            "entity": "album",
            "media": "music",
            "limit": ITUNES_SEARCH_LIMIT,
            "country": "US" # Defaulting to US store, as per API docs (default is US if omitted)
        }

        # Call the generic request method from the base class directly
        response_data_any = super()._make_generic_json_request(
            url=self.base_search_url,
            params=params,
            cancel_event=cancel_event,
            request_context=f"iTunes album search for '{search_term}'"
        )

        if self._check_cancelled(cancel_event, "after iTunes search request") or response_data_any is None:
            logger.debug(f"[{self.service_name}] Search cancelled or no data from API call for '{search_term}'.")
            return []

        if not isinstance(response_data_any, dict):
            msg = f"iTunes search response for '{search_term}' was not a JSON object (dictionary)."
            logger.error(f"[{self.service_name}] {msg} Got type: {type(response_data_any)}. Response: {str(response_data_any)[:200]}")
            raise RetrieverDataError(msg, url=self.base_search_url)
        
        response_data: Dict[str, Any] = response_data_any # Now we know it's a dict

        if "results" not in response_data:
            msg = f"iTunes search response for '{search_term}' missing 'results' key."
            logger.warning(f"[{self.service_name}] {msg} Response: {str(response_data)[:200]}")
            raise RetrieverDataError(msg, url=self.base_search_url)

        album_candidates: List[AlbumCandidate] = []
        for item in response_data.get("results", []):
            if self._check_cancelled(cancel_event, "in iTunes results loop processing"):
                break

            if item.get("wrapperType") == "collection" and item.get("collectionType") == "Album":
                collection_id = item.get("collectionId")
                collection_name = item.get("collectionName")
                artist_name_itunes = item.get("artistName")
                
                # artworkUrl100 is common, artworkUrl60 as fallback.
                base_artwork_url = item.get("artworkUrl100") or item.get("artworkUrl60")

                if collection_id and collection_name and artist_name_itunes and base_artwork_url:
                    # Ensure IDs are strings for consistency with other retrievers if they use MBIDs etc.
                    album_candidates.append(AlbumCandidate(
                        identifier=str(collection_id), 
                        album_name=collection_name,
                        artist_name=artist_name_itunes,
                        source_service=self.service_name,
                        extra_data={
                            "base_artwork_url": base_artwork_url,
                            "itunes_release_date": item.get("releaseDate"),
                            "itunes_primary_genre": item.get("primaryGenreName")
                        }
                    ))
        
        logger.info(f"[{self.service_name}] Found {len(album_candidates)} iTunes album candidates for '{search_term}'.")
        return album_candidates

    def list_potential_images(self, candidate: AlbumCandidate, 
                              cancel_event: Optional[threading.Event] = None) -> List[PotentialImage]:
        if candidate.source_service != self.service_name:
            logger.error(f"[{self.service_name}] Invalid candidate (source: {candidate.source_service}) for list_potential_images.")
            return []
        if self._check_cancelled(cancel_event, "before starting list_potential_images"):
            return []

        base_artwork_url = candidate.extra_data.get("base_artwork_url")
        if not base_artwork_url:
            msg = f"Candidate '{candidate.identifier}' ('{candidate.album_name}') missing 'base_artwork_url' in extra_data for iTunes."
            logger.warning(f"[{self.service_name}] {msg}")
            # This indicates a problem with how the AlbumCandidate was constructed by this retriever
            raise RetrieverDataError(msg) 

        thumb_url, full_url = self._derive_image_urls(base_artwork_url)

        if not thumb_url or not full_url:
            msg = f"Could not derive valid image URLs for candidate '{candidate.identifier}' from base: '{base_artwork_url}'"
            logger.warning(f"[{self.service_name}] {msg}")
            # This implies the base_artwork_url was malformed or unparseable in an unexpected way by _derive_image_urls
            raise RetrieverDataError(msg) 
        
        # iTunes album search typically yields one primary cover art per album result.
        potential_image = PotentialImage(
            identifier=full_url, # Use the full URL as its unique ID for this service
            thumbnail_url=thumb_url,
            full_image_url=full_url,
            source_candidate=candidate,
            is_front=True # Assumed true for album covers from iTunes search
        )
        logger.debug(f"[{self.service_name}] Derived potential image for '{candidate.album_name}': Thumb='{thumb_url}', Full='{full_url}'")
        return [potential_image]

    def resolve_image_details(self, potential_image: PotentialImage, 
                              cancel_event: Optional[threading.Event] = None) -> Optional[ImageResult]:
        if potential_image.source_candidate.source_service != self.service_name:
            logger.error(f"[{self.service_name}] Non-iTunes PotentialImage passed to resolve_image_details.")
            return None
        if self._check_cancelled(cancel_event, "before starting resolve_image_details"):
            return None

        logger.debug(f"[{self.service_name}] Resolving details for iTunes image: {potential_image.full_image_url}")
        
        width, height = super().get_image_dimensions(potential_image.full_image_url, 
                                                     cancel_event=cancel_event)
        
        if self._check_cancelled(cancel_event, f"after get_image_dimensions for {potential_image.full_image_url}"):
            return None

        if width and height and width > 0 and height > 0:
            logger.debug(f"[{self.service_name}] Resolved dimensions for {potential_image.full_image_url}: {width}x{height}")
            
            # is_front, album_name, artist_name will be taken from potential_image.
            return ImageResult.from_potential_image(
                potential_image=potential_image,
                full_width=width,
                full_height=height
            )
        else: 
            if not self._check_cancelled(cancel_event, "at end of resolve_image_details (failed to get dimensions)"):
                 logger.warning(f"[{self.service_name}] Could not get dimensions for iTunes image: {potential_image.full_image_url}")
            return None