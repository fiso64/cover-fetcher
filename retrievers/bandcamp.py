# services/bandcamp.py
import logging
from lxml import html
import urllib.parse
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

class BandcampRetriever(AbstractImageRetriever):
    service_name = "Bandcamp"
    
    def __init__(self):
        super().__init__()
        self.base_url = "https://bandcamp.com"
        # Pattern to capture base URL and extension of bcbits images
        # Example: https://f4.bcbits.com/img/a1234567890_10.jpg
        # Group 1: https://f4.bcbits.com/img/a1234567890 (base)
        # Group 2: _10 (optional size suffix like _10, _7, etc.)
        # Group 3: jpg (extension)
        self.bcbits_img_pattern = re.compile(
            r"^(https://[^/]+\.bcbits\.com/img/[a-zA-Z0-9]+)(_[0-9]+)?\.(jpg|png|gif|jpeg)$",
            re.IGNORECASE
        )
        # self.scraper = cloudscraper.create_scraper() # Handled by base class now

    def _derive_image_urls(self, bcbits_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Derives thumbnail and full-size image URLs from a given Bandcamp bcbits.com image URL.
        Example: .../img/somehash_7.jpg -> (.../img/somehash_7.jpg, .../img/somehash_0.jpg)
        _0 is typically the largest/original version.
        _7 is a common small thumbnail size (150x150).
        """
        match = self.bcbits_img_pattern.match(bcbits_url)
        if not match:
            logger.warning(f"[{self.service_name}] Could not parse bcbits URL: {bcbits_url} with pattern {self.bcbits_img_pattern.pattern}")
            return None, None
        
        base_part = match.group(1)  # e.g., https://f4.bcbits.com/img/a1234567890
        extension = match.group(3)  # e.g., jpg
        
        full_image_url = f"{base_part}_0.{extension}"    # _0 is largest
        thumbnail_url = f"{base_part}_7.{extension}" # _7 is a common small preview size (150px)
        return thumbnail_url, full_image_url

    def search_album_candidates(self, artist: str, album: str, 
                                cancel_event: Optional[threading.Event] = None) -> List[AlbumCandidate]:
        if self._check_cancelled(cancel_event, "before starting search_album_candidates"):
            return []
        logger.info(f"[{self.service_name}] Searching for Bandcamp album candidates: '{album}' by '{artist}'")

        query_parts = []
        if album:
            album_clean = re.sub(r'[^\w\s-]', ' ', album.strip()).strip()
            if album_clean: query_parts.append(album_clean)
        if artist:
            artist_clean = re.sub(r'[^\w\s-]', ' ', artist.strip()).strip()
            if artist_clean: query_parts.append(artist_clean)

        if not query_parts:
            msg = "Both artist and album search terms are effectively empty for Bandcamp search."
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverInputError(msg)
        
        search_query = " ".join(query_parts)
        # &item_type=a ensures we search for albums.
        search_url = f"{self.base_url}/search?q={urllib.parse.quote_plus(search_query)}&item_type=a"
        logger.info(f"[{self.service_name}] Searching Bandcamp: {search_url}")

        # Call the base class method, which now handles scraper fallback
        search_response_obj = super()._perform_http_get_request(
            url=search_url,
            cancel_event=cancel_event,
            request_context=f"Bandcamp album search for '{search_query}'",
            expect_html_cloudflare=True # Indicate this request might need scraper
        )

        if self._check_cancelled(cancel_event, "after Bandcamp search request") or search_response_obj is None:
            # search_response_obj will be None if cancelled during _perform_http_get_request
            logger.debug(f"[{self.service_name}] Search cancelled or no response from Bandcamp for '{search_query}'.")
            return []
        
        # Validate content type if necessary (though _perform_http_get_request doesn't do this by default)
        content_type = search_response_obj.headers.get('Content-Type', '').lower()
        if 'text/html' not in content_type:
            # This is an unexpected response type from Bandcamp search.
            msg = f"Expected HTML from Bandcamp search '{search_query}', got {content_type}."
            logger.warning(f"[{self.service_name}] {msg} URL: {search_url}")
            # This could be a RetrieverDataError if the content itself is the problem,
            # or RetrieverAPIError if it implies an API-level issue despite a 200 OK.
            # For now, let's treat it as a data error as the content is not what we can parse.
            raise RetrieverDataError(msg, url=search_url)

        if not search_response_obj.content: # Should not happen if status was 200 OK, but good to check
            msg = f"No content received from Bandcamp search '{search_query}' despite successful request."
            logger.error(f"[{self.service_name}] {msg} URL: {search_url}")
            raise RetrieverDataError(msg, url=search_url)

        try:
            # Using search_response_obj.content from the previous step
            tree = html.fromstring(search_response_obj.content)
        except Exception as e_parse: # lxml can raise various errors
            if self._check_cancelled(cancel_event, "in HTML parse exception handler for search results"):
                return []
            msg = f"Failed to parse HTML from Bandcamp search response for '{search_query}'"
            logger.error(f"[{self.service_name}] {msg} URL: {search_url}: {e_parse}", exc_info=True)
            raise RetrieverDataError(msg, original_exception=e_parse, url=search_url) from e_parse

        # Check for Bandcamp's "no results" indicator first
        # Example: <div id="search-no-results" class="no-results">...</div>
        # We look for the presence of an element with this ID.
        no_results_indicator = tree.xpath('//div[@id="search-no-results"]')
        if no_results_indicator:
            logger.info(f"[{self.service_name}] Bandcamp search for '{search_query}' explicitly indicated no results. URL: {search_url}")
            return [] # This is a successful search with zero results.
        
        # XPath to find list items representing album search results
        search_results_elements = tree.xpath(
            '//li[contains(@class, "searchresult") and @data-search and .//div[@class="itemtype" and normalize-space(text())="ALBUM"]]'
        )
        
        if not search_results_elements:
            # If we reach here, it means no "no results" indicator was found,
            # but our primary XPath for album results also found nothing.
            # This strongly suggests our XPath is outdated or the page structure changed unexpectedly.
            msg = (f"Bandcamp search for '{search_query}' did not indicate 'no results', "
                   f"but no album items were found using primary XPath. "
                   f"Page structure may have changed or XPath is outdated. URL: {search_url}")
            logger.warning(f"[{self.service_name}] {msg}")
            page_snippet = search_response_obj.text[:1000] if hasattr(search_response_obj, 'text') else "N/A"
            logger.debug(f"[{self.service_name}] Page snippet for '{search_query}':\n{page_snippet}")
            raise RetrieverDataError(msg, url=search_url)

        all_candidates: List[AlbumCandidate] = []
        processed_links = set()
        any_image_source_found_overall = False # Flag to track if any image was found

        for result_el in search_results_elements:
            if self._check_cancelled(cancel_event, "in search results loop"):
                break
            
            album_name_nodes = result_el.xpath('.//div[@class="heading"]/a')
            artist_name_text_nodes = result_el.xpath('.//div[@class="subhead"]/text()')
            img_src_nodes = result_el.xpath('.//a[@class="artcont"]//img/@src')

            if not album_name_nodes:
                # Log skipping this specific item but don't error out the whole search
                logger.warning(f"[{self.service_name}] Skipping a Bandcamp search result item for '{search_query}' due to missing album name/link. This might indicate an XPath issue for this specific item type or a malformed entry.")
                logger.debug(f"[{self.service_name}] Skipped item HTML snippet: {html.tostring(result_el, pretty_print=True, encoding='unicode')[:300]}")
                continue 
            
            found_album_name_el = album_name_nodes[0]
            album_page_link_from_search = found_album_name_el.get("href")
            if not album_page_link_from_search: # Should be caught by `if not album_name_nodes` if `<a>` is there but href is missing.
                logger.warning(f"[{self.service_name}] Skipping a Bandcamp search result item for '{search_query}' due to missing href in album link.")
                continue

            parsed_original_link = urllib.parse.urlparse(album_page_link_from_search)
            album_page_link_cleaned = urllib.parse.urlunparse(
                parsed_original_link._replace(query='', fragment='')
            )
            if not parsed_original_link.scheme or not parsed_original_link.netloc:
                album_page_link_cleaned = urllib.parse.urljoin(self.base_url, album_page_link_cleaned)
            
            if album_page_link_cleaned in processed_links:
                continue

            found_album_name = found_album_name_el.text_content().strip()
            found_artist_name_raw = "".join(t.strip() for t in artist_name_text_nodes).strip()
            
            found_artist_name = found_artist_name_raw
            if found_artist_name_raw.lower().startswith("by "):
                found_artist_name = found_artist_name_raw[3:].strip()
            
            if not found_artist_name and artist_name_text_nodes: # If xpath found nodes but text was empty/whitespace
                 logger.debug(f"[{self.service_name}] Artist name XPath found nodes but resulted in empty string for '{found_album_name}'.")
            elif not artist_name_text_nodes: # If artist name XPath found no nodes at all
                 logger.debug(f"[{self.service_name}] Artist name XPath found no nodes for '{found_album_name}'.")


            search_thumb_url, search_full_url = None, None
            img_src_from_search = img_src_nodes[0] if img_src_nodes else None
            if img_src_from_search:
                any_image_source_found_overall = True # Mark that at least one image source was found
                search_thumb_url, search_full_url = self._derive_image_urls(img_src_from_search)
                if not search_full_url:
                    logger.debug(f"[{self.service_name}] Could not derive full image URL from search result src: {img_src_from_search} for album '{found_album_name}'.")
            else:
                # Log missing image for this specific item but don't error out yet
                logger.debug(f"[{self.service_name}] No image src found in search result via XPath for album '{found_album_name}'.")

            extra_d = {
                'search_result_artist_text': found_artist_name,
                'search_result_album_text': found_album_name,
            }
            if search_thumb_url and search_full_url:
                extra_d['direct_thumbnail_url'] = search_thumb_url
                extra_d['direct_full_image_url'] = search_full_url
            
            candidate = AlbumCandidate(
                identifier=album_page_link_cleaned,
                album_name=found_album_name,
                artist_name=found_artist_name,
                source_service=self.service_name,
                extra_data=extra_d
            )
            all_candidates.append(candidate)
            processed_links.add(album_page_link_cleaned)
        
        if self._check_cancelled(cancel_event, "after processing search results"):
            return []

        # Post-loop checks:
        if search_results_elements and not all_candidates:
            # This means the main XPath found items, but all were skipped (e.g., missing album name).
            # This is a strong indicator of a problem with sub-XPaths for essential data.
            msg = (f"Bandcamp search for '{search_query}' found {len(search_results_elements)} result items, "
                   f"but ALL failed processing due to missing essential data (e.g., album name/link). "
                   f"Sub-XPaths for essential data like album name/link might be outdated. URL: {search_url}")
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverDataError(msg, url=search_url)

        if all_candidates and not any_image_source_found_overall:
            # We successfully created candidates, but couldn't find an image source for ANY of them.
            # This indicates the image XPath is likely broken or images are no longer in search results.
            msg = (f"Bandcamp search for '{search_query}' yielded {len(all_candidates)} candidates, "
                   f"but NO image sources were found for ANY of them using the image XPath. "
                   f"The XPath for images in search results might be outdated. URL: {search_url}")
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverDataError(msg, url=search_url)
        
        if not all_candidates:
            # This path is now only reached if:
            # 1. `no_results_indicator` was true (returned [] much earlier).
            # 2. `search_results_elements` was empty AND `no_results_indicator` was false (raised RetrieverDataError earlier).
            # 3. `search_results_elements` had items, but ALL failed essential data (raised RetrieverDataError above).
            # So, if we reach here with no candidates, it implies the "no results" path was taken, or an error was already raised.
            # The logger.info is fine for the genuine "no results" case.
            logger.info(f"[{self.service_name}] No Bandcamp album candidates identified for '{search_query}' (either genuine no results, or all potential items were unprocessable).")
            return [] # Return empty list for genuine "no results" or if errors were handled by raising.
        
        logger.info(f"[{self.service_name}] Found {len(all_candidates)} potential Bandcamp album candidates for '{search_query}'.")
        return all_candidates

    def list_potential_images(self, candidate: AlbumCandidate, 
                              cancel_event: Optional[threading.Event] = None) -> List[PotentialImage]:
        if candidate.source_service != self.service_name:
            logger.error(f"[{self.service_name}] Non-Bandcamp candidate passed to list_potential_images.")
            return []
        if self._check_cancelled(cancel_event, "before starting list_potential_images"):
            return []

        logger.info(f"[{self.service_name}] Listing potential images for Bandcamp candidate '{candidate.album_name}' using pre-fetched URLs from search results.")

        # Retrieve image URLs stored during search_album_candidates
        direct_thumb_url = candidate.extra_data.get('direct_thumbnail_url')
        direct_full_url = candidate.extra_data.get('direct_full_image_url')

        if not direct_full_url or not direct_thumb_url:
            msg = (f"Bandcamp candidate '{candidate.album_name}' (ID: {candidate.identifier}) "
                   f"is missing 'direct_thumbnail_url' or 'direct_full_image_url' in extra_data. "
                   f"Cannot list potential images.")
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverDataError(msg) # Or RetrieverInputError if considered bad input to this method
        
        potential_image = PotentialImage(
            identifier=direct_full_url,  # Use full image URL as the unique ID for this PotentialImage
            thumbnail_url=direct_thumb_url,
            full_image_url=direct_full_url,
            source_candidate=candidate,
            extra_data={'album_page_url': str(candidate.identifier)} # Store album page URL for context/debugging
        )
        
        logger.info(f"[{self.service_name}] Derived 1 potential image from search result data for Bandcamp candidate '{candidate.album_name}'.")
        return [potential_image]

    def resolve_image_details(self, potential_image: PotentialImage, 
                              cancel_event: Optional[threading.Event] = None) -> Optional[ImageResult]:
        if potential_image.source_candidate.source_service != self.service_name:
             logger.error(f"[{self.service_name}] Non-Bandcamp PotentialImage passed to resolve_image_details.")
             return None
        if self._check_cancelled(cancel_event, "before starting resolve_image_details"):
            return None

        logger.debug(f"[{self.service_name}] Resolving details for Bandcamp image: {potential_image.full_image_url}")
        
        # Use the parent's method to get dimensions. This typically involves standard HTTP HEAD/GET requests.
        # This assumes bcbits.com (where images are hosted) is not Cloudflare-protected like bandcamp.com.
        # If bcbits.com images also require Cloudflare bypass, this part would need to use self.scraper.
        width, height = super().get_image_dimensions(potential_image.full_image_url,
                                                     cancel_event=cancel_event)
        
        if self._check_cancelled(cancel_event, f"after get_image_dimensions for {potential_image.full_image_url}"):
            return None

        if width and height and width > 0 and height > 0:
            logger.debug(f"[{self.service_name}] Resolved dimensions for {potential_image.full_image_url}: {width}x{height}")
            return ImageResult.from_potential_image(
                potential_image=potential_image,
                full_width=width,
                full_height=height
            )
        else:
            if not self._check_cancelled(cancel_event, "at end of resolve_image_details (failed to get dimensions)"):
                 logger.warning(f"[{self.service_name}] Could not get dimensions for Bandcamp image: {potential_image.full_image_url}.")
            return None