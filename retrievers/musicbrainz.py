# services/musicbrainz.py
import musicbrainzngs
import logging
from typing import List, Optional, Dict, Any
import threading
import requests

from .base_retriever import (
    AbstractImageRetriever, AlbumCandidate, PotentialImage, ImageResult,
    RetrieverError, RetrieverNetworkError, RetrieverAPIError, RetrieverDataError, RetrieverInputError
)
from utils.config import APP_CONFIG, DEFAULT_REQUESTS_HEADERS

logger = logging.getLogger(__name__)

MAX_FINAL_RELEASE_CANDIDATES = 25

from .base_retriever import AbstractImageRetriever, AlbumCandidate, PotentialImage, ImageResult
from utils.config import APP_CONFIG, DEFAULT_REQUESTS_HEADERS


class MusicBrainzRetriever(AbstractImageRetriever):
    service_name = "MusicBrainz"

    def __init__(self):
        super().__init__()
        self.caa_user_agent_component = f"{APP_CONFIG.get('musicbrainz_app_name', 'GenericArtBot')}/{APP_CONFIG.get('musicbrainz_app_version', '0.1')} ( {APP_CONFIG.get('musicbrainz_contact_email', 'issues@example.com')} )"
        
        try:
            musicbrainzngs.set_useragent(
                APP_CONFIG.get('musicbrainz_app_name', 'GenericArtBot'),
                APP_CONFIG.get('musicbrainz_app_version', '0.1'),
                APP_CONFIG.get('musicbrainz_contact_email', 'issues@example.com')
            )
        except Exception as e:
            logger.error(f"[{self.service_name}] Failed to set User-Agent for musicbrainzngs: {e}")

    def search_album_candidates(self, artist: str, album: str, 
                                cancel_event: Optional[threading.Event] = None) -> List[AlbumCandidate]:
        if self._check_cancelled(cancel_event, "before starting search_album_candidates"):
            return []

        if not album and not artist: # Or a more specific check if one is always needed
            msg = "Both artist and album search terms are empty for MusicBrainz search."
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverInputError(msg)
        
        logger.info(f"[{self.service_name}] Searching for MusicBrainz releases: '{album}' by '{artist}'")
        
        direct_release_search_results: List[Dict[str, Any]] = []
        
        base_api_params = {
            "release": album, # musicbrainzngs uses 'release' for album title
            "limit": MAX_FINAL_RELEASE_CANDIDATES,
        }
        if artist:
            base_api_params["artist"] = artist
        
        api_call_description = f"MusicBrainz search for '{album}' by '{artist}'"
        
        # Define preferred primary types for first two attempts
        # MusicBrainz API allows "OR" for multiple types in a single string
        preferred_primary_types = "Album OR EP OR Single"

        try:
            # Attempt 1: Strict search for Official releases (no primary type specified yet).
            # This aims for the most exact title/artist match first.
            params_attempt1 = {
                **base_api_params,
                "status": "Official",
                "strict": True
            }
            logger.debug(f"[{self.service_name}] {api_call_description} (Attempt 1: Strict, Official, Any Type). Params: {params_attempt1}")

            if self._check_cancelled(cancel_event, "before MB search_releases (attempt 1)"): return []
            result_attempt1 = musicbrainzngs.search_releases(**params_attempt1)
            if self._check_cancelled(cancel_event, "after MB search_releases (attempt 1)"): return []
            direct_release_search_results = result_attempt1.get('release-list', [])

            # Attempt 2: Non-strict search for Official preferred primary types (Album, EP, Single)
            # This is used if the pure strict search yielded no results.
            if not direct_release_search_results:
                logger.info(f"[{self.service_name}] No results from strict, official (any type) search. Broadening to non-strict, official, preferred types: {preferred_primary_types}.")
                params_attempt2 = {
                    **base_api_params,
                    "primarytype": preferred_primary_types,
                    "status": "Official",
                    "strict": False
                }
                logger.debug(f"[{self.service_name}] {api_call_description} (Attempt 2: Non-Strict, Official, Types: {preferred_primary_types}). Params: {params_attempt2}")

                if self._check_cancelled(cancel_event, "before MB search_releases (attempt 2)"): return []
                result_attempt2 = musicbrainzngs.search_releases(**params_attempt2)
                if self._check_cancelled(cancel_event, "after MB search_releases (attempt 2)"): return []
                direct_release_search_results = result_attempt2.get('release-list', [])
            
            # Attempt 3: Non-strict search for ANY primary type and ANY status (if previous attempts yielded no results)
            if not direct_release_search_results:
                logger.info(f"[{self.service_name}] No results from non-strict official search for preferred types. Broadening to non-strict, any type, any status.")
                params_attempt3 = {**base_api_params, "strict": False}
                # primarytype and status are deliberately omitted to search for everything
                logger.debug(f"[{self.service_name}] {api_call_description} (Attempt 3: Non-Strict, Any Type, Any Status). Params: {params_attempt3}")
                
                if self._check_cancelled(cancel_event, "before MB search_releases (attempt 3)"): return []
                result_attempt3 = musicbrainzngs.search_releases(**params_attempt3)
                if self._check_cancelled(cancel_event, "after MB search_releases (attempt 3)"): return []
                direct_release_search_results = result_attempt3.get('release-list', [])

        except musicbrainzngs.AuthenticationError as e_auth:
            if self._check_cancelled(cancel_event, "in MB AuthenticationError handler"): return []
            msg = f"MusicBrainz authentication error during {api_call_description}: {e_auth}"
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverAPIError(msg, status_code=401, original_exception=e_auth) from e_auth
        except musicbrainzngs.ResponseError as e_resp: # Covers HTTP errors like 400, 503
            if self._check_cancelled(cancel_event, "in MB ResponseError handler"): return []
            # cause might be an HTTPError instance with a response attribute
            status_code = None
            if hasattr(e_resp, 'cause') and hasattr(e_resp.cause, 'response') and hasattr(e_resp.cause.response, 'status_code'):
                status_code = e_resp.cause.response.status_code
            msg = f"MusicBrainz API response error during {api_call_description} (HTTP Status: {status_code or 'Unknown'}): {e_resp}"
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverAPIError(msg, status_code=status_code, original_exception=e_resp) from e_resp
        except musicbrainzngs.NetworkError as e_net:
            if self._check_cancelled(cancel_event, "in MB NetworkError handler"): return []
            msg = f"MusicBrainz network error during {api_call_description}: {e_net}"
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverNetworkError(msg, original_exception=e_net) from e_net
        except musicbrainzngs.UsageError as e_usage: # Incorrect usage of the library
            if self._check_cancelled(cancel_event, "in MB UsageError handler"): return []
            msg = f"MusicBrainz library usage error during {api_call_description}: {e_usage}"
            logger.error(f"[{self.service_name}] {msg}", exc_info=True) # Log with exc_info for debugging
            raise RetrieverInputError(msg, original_exception=e_usage) from e_usage # Treat as input error to our retriever
        except Exception as e_generic: # Catch-all for other unexpected errors
            if self._check_cancelled(cancel_event, "in unexpected error handler during MB release search"): return []
            msg = f"Unexpected error during {api_call_description}: {e_generic}"
            logger.error(f"[{self.service_name}] {msg}", exc_info=True)
            raise RetrieverError(msg, original_exception=e_generic) from e_generic

        if not direct_release_search_results:
            logger.info(f"[{self.service_name}] No MusicBrainz releases found for '{album}' by '{artist}' after search attempts.")
            # This is a valid "no results found" scenario after successful API calls.
            return []
        
        # Results from MusicBrainz are generally sorted by relevance (ext:score).
        # We will refine this sorting:
        # 1. Primary sort: by ext:score (descending - higher score is better).
        # 2. Secondary sort: by primary type ("Album" > "EP" > "Single" > Others).
        # The list is already limited by MAX_FINAL_RELEASE_CANDIDATES from the API call.
        
        type_order_map = {"Album": 0, "EP": 1, "Single": 2}

        def get_primary_type_sort_value(release_data: Dict[str, Any]) -> int:
            # Lower value means higher preference for sorting
            rg_info = release_data.get('release-group', {})
            primary_type = rg_info.get('primary-type')
            if not primary_type and rg_info: # musicbrainzngs sometimes uses 'type' in release-group
                 primary_type = rg_info.get('type')
            return type_order_map.get(primary_type, 3) # Other types get value 3

        # Sort by score (descending) then by primary type preference (ascending by sort value)
        direct_release_search_results.sort(
            key=lambda rel_data: (
                -int(rel_data.get('ext:score', 0)),  # Negative for descending score
                get_primary_type_sort_value(rel_data)  # Ascending by type preference
            )
        )
        
        final_album_candidates: List[AlbumCandidate] = []
        for rel_data in direct_release_search_results:
            if self._check_cancelled(cancel_event, "in loop creating final AlbumCandidate objects"):
                break
            
            release_id = rel_data.get('id', 'UnknownID')
            artist_name_str = None
            
            # 1. Try release's artist-credit-phrase
            _rel_ac_phrase = rel_data.get('artist-credit-phrase')
            if isinstance(_rel_ac_phrase, str) and _rel_ac_phrase.strip():
                artist_name_str = _rel_ac_phrase
            else:
                # 2. Try release group's artist-credit-phrase (if release one was missing)
                _rg_data_from_release = rel_data.get('release-group', {})
                _rg_ac_phrase = _rg_data_from_release.get('artist-credit-phrase')
                if isinstance(_rg_ac_phrase, str) and _rg_ac_phrase.strip():
                    artist_name_str = _rg_ac_phrase
                # No further fallbacks to mb_utils formatting
            
            # Final safety check for empty artist string
            if not artist_name_str or not artist_name_str.strip(): # Check if None or empty after assignment
                artist_name_str = "Unknown Artist"
                logger.debug(f"[{self.service_name}] Could not determine artist name for release ID {release_id} from artist-credit-phrase. Defaulting to 'Unknown Artist'.")

            rg_info = rel_data.get('release-group', {}) 

            final_album_candidates.append(AlbumCandidate(
                identifier=release_id, 
                album_name=rel_data.get('title'),
                artist_name=artist_name_str,
                source_service=self.service_name,
                extra_data={
                    'original_title': rel_data.get('title'),
                    'release_group_id': rg_info.get('id'),
                    'release_group_title': rg_info.get('title'), 
                    'release_group_primary_type': rg_info.get('primary-type') or rg_info.get('type'),
                    'release_group_secondary_types': rg_info.get('secondary-type-list', []),
                    'release_date': rel_data.get('date'),
                    'release_country': rel_data.get('country'),
                    'release_status': rel_data.get('status'),
                    'release_packaging': rel_data.get('packaging'),
                    'disambiguation': rel_data.get('disambiguation'),
                    'media_count': len(rel_data.get('medium-list', [])),
                    'track_count': rel_data.get('track-count'),
                    'api_score': int(rel_data.get('ext:score', 0))
                }
            ))
        
        logger.info(f"[{self.service_name}] Returning {len(final_album_candidates)} MusicBrainz release candidates for '{album}' by '{artist}'.")
        return final_album_candidates

    def list_potential_images(self, candidate: AlbumCandidate, 
                              cancel_event: Optional[threading.Event] = None) -> List[PotentialImage]:
        if candidate.source_service != self.service_name or not isinstance(candidate.identifier, str) or not candidate.identifier:
            msg = f"Invalid candidate for MusicBrainz list_potential_images: {candidate}. Expected string MBID."
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverInputError(msg)
        if self._check_cancelled(cancel_event, "before starting list_potential_images"):
            return []

        release_mbid = candidate.identifier 
        logger.info(f"[{self.service_name}] Listing CAA images for Release MBID: {release_mbid} ('{candidate.album_name}')")

        caa_url = f"https://coverartarchive.org/release/{release_mbid}"
        caa_headers = DEFAULT_REQUESTS_HEADERS.copy()
        caa_headers["User-Agent"] = self.caa_user_agent_component

        caa_json_data_any = None
        try:
            # Use _make_generic_json_request as CAA returns JSON
            caa_json_data_any = super()._make_generic_json_request(
                url=caa_url,
                extra_headers=caa_headers,
                cancel_event=cancel_event,
                request_context=f"CAA JSON fetch for release {release_mbid}"
            )
        except RetrieverAPIError as e_api:
            if e_api.status_code == 404:
                logger.info(f"[{self.service_name}] CAA returned 404 for release {release_mbid} ('{candidate.album_name}'), indicating no cover art found. URL: {caa_url}")
                return [] # Treat 404 from CAA as "no images found"
            # For other API errors (non-404), re-raise them to be handled by ServiceManager
            logger.warning(f"[{self.service_name}] Non-404 API error ({e_api.status_code}) from CAA for {release_mbid}: {e_api.message}")
            raise # Re-raise the original RetrieverAPIError
            # Other RetrieverError subtypes (NetworkError, DataError from _make_generic_json_request)
            # will propagate automatically and be handled by ServiceManager.
            
        if self._check_cancelled(cancel_event, "after CAA JSON request") or caa_json_data_any is None:
            logger.info(f"[{self.service_name}] CAA JSON request cancelled or no data returned for release {release_mbid}.")
            return []

        if not isinstance(caa_json_data_any, dict):
            msg = f"CAA response for release {release_mbid} was not a JSON object (dictionary) as expected. Got type: {type(caa_json_data_any)}."
            logger.error(f"[{self.service_name}] {msg} URL: {caa_url}. Response: {str(caa_json_data_any)[:200]}")
            raise RetrieverDataError(msg, url=caa_url)
        
        caa_json_data: Dict[str, Any] = caa_json_data_any

        if 'images' not in caa_json_data and 'message' in caa_json_data:
            logger.info(f"[{self.service_name}] CAA for {release_mbid} returned message: '{caa_json_data.get('message')}' and no 'images' array. No images found.")
            return []

        release_images_data = caa_json_data.get("images", [])
        if not isinstance(release_images_data, list):
            msg = f"CAA 'images' field for release {release_mbid} is not a list. Got type: {type(release_images_data)}."
            logger.error(f"[{self.service_name}] {msg} URL: {caa_url}. Full JSON: {str(caa_json_data)[:200]}")
            raise RetrieverDataError(msg, url=caa_url)

        if not release_images_data:
            logger.info(f"[{self.service_name}] CAA for {release_mbid} has no 'images' in its data or the array is empty.")
            return [] 

        potential_images: List[PotentialImage] = []
        try:
            for img_data in release_images_data:
                if self._check_cancelled(cancel_event, "in CAA image data loop"):
                    break
                full_url = img_data.get("image")
                if not full_url: 
                    logger.debug(f"[{self.service_name}] Skipping CAA image data with no 'image' URL for release {release_mbid}.")
                    continue
                if full_url.startswith("http://"): full_url = full_url.replace("http://", "https://", 1)

                thumb_large = img_data.get("thumbnails", {}).get("large")
                if thumb_large and thumb_large.startswith("http://"): thumb_large = thumb_large.replace("http://", "https://", 1)
                thumb_small = img_data.get("thumbnails", {}).get("small")
                if thumb_small and thumb_small.startswith("http://"): thumb_small = thumb_small.replace("http://", "https://", 1)
                thumb_url_to_use = thumb_small or thumb_large or full_url 
                
                image_types = img_data.get("types", [])
                is_front = not image_types or len(image_types) == 1 and image_types[0] == "Front"
                
                current_extra_data = {
                    'caa_types': image_types,
                }

                potential_images.append(PotentialImage(
                    identifier=full_url, 
                    thumbnail_url=thumb_url_to_use,
                    full_image_url=full_url,
                    source_candidate=candidate,
                    original_type=", ".join(image_types) if image_types else None,
                    extra_data=current_extra_data,
                    is_front=is_front 
                ))
        except Exception as e_proc: 
            if self._check_cancelled(cancel_event, "in unexpected CAA image data processing error handler"): return []
            msg = f"Unexpected error processing the content of CAA JSON data for release {release_mbid}"
            logger.error(f"[{self.service_name}] {msg}: {e_proc}. URL: {caa_url}", exc_info=True)
            raise RetrieverDataError(msg, original_exception=e_proc, url=caa_url) from e_proc
        
        if self._check_cancelled(cancel_event, "before sorting potential images from CAA"):
            return []

        potential_images.sort(key=lambda img: img.is_front, reverse=True)
        
        logger.info(f"[{self.service_name}] Found {len(potential_images)} potential images from CAA for Release MBID: {release_mbid}.")
        return potential_images

    def resolve_image_details(self, potential_image: PotentialImage, 
                              cancel_event: Optional[threading.Event] = None) -> Optional[ImageResult]:
        if potential_image.source_candidate.source_service != self.service_name:
            logger.error(f"[{self.service_name}] Non-MusicBrainz PotentialImage passed to resolve_image_details.")
            return None
        if self._check_cancelled(cancel_event, "before starting resolve_image_details"):
            return None

        logger.debug(f"[{self.service_name}] Resolving details for MusicBrainz/CAA image: {potential_image.full_image_url}")
        
        headers_for_dim_check = None
        if "coverartarchive.org" in potential_image.full_image_url:
            headers_for_dim_check = DEFAULT_REQUESTS_HEADERS.copy()
            headers_for_dim_check["User-Agent"] = self.caa_user_agent_component
        
        width, height = super().get_image_dimensions(potential_image.full_image_url, 
                                                     extra_headers=headers_for_dim_check,
                                                     cancel_event=cancel_event)
        
        if self._check_cancelled(cancel_event, f"after get_image_dimensions for {potential_image.full_image_url}"):
            return None

        if width and height and width > 0 and height > 0:
            logger.debug(f"[{self.service_name}] Resolved dimensions for {potential_image.full_image_url}: {width}x{height}")
            
            original_type_str = "Unknown"
            caa_types = potential_image.extra_data.get('caa_types', [])
            if caa_types:
                original_type_str = ", ".join(sorted(list(set(caa_types))))
            elif potential_image.is_front: 
                original_type_str = "Front"

            return ImageResult.from_potential_image(
                potential_image=potential_image,
                full_width=width,
                full_height=height,
                original_type=original_type_str
            )
        else: 
            if not self._check_cancelled(cancel_event, "at end of resolve_image_details (failed to get dimensions)"):
                logger.warning(f"[{self.service_name}] Could not get dimensions for MusicBrainz/CAA image: {potential_image.full_image_url}")
            return None