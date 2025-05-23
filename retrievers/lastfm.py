# services/lastfm.py
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

class LastFmRetriever(AbstractImageRetriever):
    service_name = "Last.fm"
    EXPAND_GALLERIES = False # If False, derive image from search result thumbnail. If True, scrape gallery page.
    PLACEHOLDER_IMAGE_HASH = "c6f59c1e5e7240a4c0d427abd71f3dbb"

    def __init__(self):
        super().__init__()
        self.base_url = "https://www.last.fm"
        # Pattern to capture:
        # Group 1: Base URL part (e.g., "https://lastfm.freetls.fastly.net/i/u/")
        # Ignored: Size component (e.g., "64s", "174s", "o")
        # Group 2: Hash and filename extension (e.g., "4017e047b76041850e59c83280b6f8f9.jpg")
        self.fastly_thumb_pattern = re.compile(r"^(https://lastfm\.freetls\.fastly\.net/i/u/)[^/]+/(.+)$")

    def get_image_dimensions(self, image_url: str, extra_headers: Optional[dict] = None,
                             cancel_event: Optional[threading.Event] = None) -> Tuple[Optional[int], Optional[int]]:
        if self._check_cancelled(cancel_event, f"at start of LastFm get_image_dimensions for {image_url}"):
            return None, None
            
        url_match = re.search(r"/(\d+x\d+|\d+x0|0x\d+)/", image_url) 
        if url_match:
            try:
                dims_part = url_match.group(1)
                if 'x' in dims_part:
                    width_str, height_str = dims_part.split('x')
                    width = int(width_str)
                    height = int(height_str)
                    if width > 0 and height == 0: return width, width 
                    if height > 0 and width == 0: return height, height 
                    if width > 0 and height > 0: return width, height
            except ValueError:
                logger.warning(f"[{self.service_name}] Could not parse dimensions from URL match: {dims_part} for {image_url}. Falling back.")
        
        return super().get_image_dimensions(image_url, extra_headers, cancel_event)

    def search_album_candidates(self, artist: str, album: str, 
                                cancel_event: Optional[threading.Event] = None) -> List[AlbumCandidate]:
        if self._check_cancelled(cancel_event, "before starting search_album_candidates"):
            return []
        
        # Only simple input cleaning, otherwise last.fm might not return any matches
        search_query = f"{artist} {album}".replace('.', ' ').replace(':', ' ').replace('-', ' ').strip()

        if not search_query:
            msg = "Both artist and album search terms are effectively empty for Last.fm search."
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverInputError(msg)

        search_url = f"{self.base_url}/search/albums?q={urllib.parse.quote_plus(search_query)}"
        logger.info(f"[{self.service_name}] Searching Last.fm: {search_url}")

        search_response_obj = super()._perform_http_get_request(
            url=search_url,
            cancel_event=cancel_event,
            request_context=f"Last.fm album search for '{search_query}'",
            expect_html_cloudflare=True # use cloudscraper as fallback if requests.get fails
        )

        if self._check_cancelled(cancel_event, "after Last.fm search request") or search_response_obj is None:
            logger.debug(f"[{self.service_name}] Search cancelled or no response from Last.fm for '{search_query}'.")
            return []

        content_type = search_response_obj.headers.get('Content-Type', '').lower()
        if 'text/html' not in content_type:
            msg = f"Expected HTML from Last.fm search '{search_query}', got {content_type}."
            logger.warning(f"[{self.service_name}] {msg} URL: {search_url}")
            raise RetrieverDataError(msg, url=search_url)

        if not search_response_obj.content:
            msg = f"No content received from Last.fm search '{search_query}' despite successful request."
            logger.error(f"[{self.service_name}] {msg} URL: {search_url}")
            raise RetrieverDataError(msg, url=search_url)

        try:
            tree = html.fromstring(search_response_obj.content)
        except Exception as e:
            if not self._check_cancelled(cancel_event, "in HTML parse exception for search results"):
                logger.error(f"[{self.service_name}] Failed to parse HTML from search response {search_url}: {e}")
            return []

        # Check for Last.fm's "no results" message
        # <p class="message">No albums found.</p>
        # or <p class="message">No results for <em>query</em>.</p>
        no_results_indicator = tree.xpath('//p[@class="message" and (contains(normalize-space(text()), "No albums found") or contains(normalize-space(text()), "No results for"))]')
        if no_results_indicator:
            logger.info(f"[{self.service_name}] Last.fm search for '{search_query}' explicitly indicated no results. URL: {search_url}")
            return []

        search_results_elements = tree.xpath('//div[contains(@class, "album-result-inner")]')
        if not search_results_elements:
            # No "no results" message, but main XPath failed.
            msg = (f"Last.fm search for '{search_query}' did not indicate 'no results', "
                   f"but no album items were found using primary XPath '//div[contains(@class, \"album-result-inner\")]'. "
                   f"Page structure may have changed or XPath is outdated. URL: {search_url}")
            logger.warning(f"[{self.service_name}] {msg}")
            page_snippet = search_response_obj.text[:1000] if hasattr(search_response_obj, 'text') else "N/A"
            logger.debug(f"[{self.service_name}] Page snippet for '{search_query}':\n{page_snippet}")
            raise RetrieverDataError(msg, url=search_url)

        raw_potential_matches_data = []
        any_item_processed_successfully = False

        for result_el in search_results_elements:
            if self._check_cancelled(cancel_event, "in search results loop"):
                break
            album_name_nodes = result_el.xpath('.//h4[contains(@class, "album-result-heading")]/a[@class="link-block-target"]')
            artist_name_nodes = result_el.xpath('.//p[contains(@class, "album-result-artist")]/a')

            if not album_name_nodes: continue
            found_album_name_el = album_name_nodes[0]
            album_page_link = found_album_name_el.get("href")
            
            if not album_page_link or not album_page_link.startswith("/music/"): continue

            original_album_text = found_album_name_el.text_content().strip()
            original_artist_text = artist_name_nodes[0].text_content().strip() if artist_name_nodes else "N/A"
            
            search_thumb_url_for_candidate = None
            if not self.EXPAND_GALLERIES: # Only extract if we might use it
                album_image_nodes = result_el.xpath('.//img[contains(@class, "album-result-image")]/@src')
                if album_image_nodes:
                    temp_search_thumb_url = album_image_nodes[0]
                    match = self.fastly_thumb_pattern.match(temp_search_thumb_url)
                    if match:
                        _, hash_filename_part = match.groups()
                        clean_hash = hash_filename_part.split('.')[0]
                        if clean_hash != self.PLACEHOLDER_IMAGE_HASH:
                            search_thumb_url_for_candidate = temp_search_thumb_url
                        else:
                            logger.debug(f"[{self.service_name}] Search result for '{original_album_text}' by '{original_artist_text}' has placeholder image ({temp_search_thumb_url}). Ignoring this thumbnail for candidate.")
                    else:
                        # This case is unlikely for well-formed Last.fm image URLs but handled defensively.
                        logger.warning(f"[{self.service_name}] Search thumb URL {temp_search_thumb_url} for '{original_album_text}' did not match Fastly pattern. Storing as is, but it might be problematic.")
                        search_thumb_url_for_candidate = temp_search_thumb_url


            any_item_processed_successfully = True # Mark that we got at least one item's core data
            
            raw_potential_matches_data.append({
                "album_name_lower": original_album_text.lower(),
                "artist_name_lower": original_artist_text.lower(),
                "link": album_page_link, 
                "original_album_text": original_album_text,
                "original_artist_text": original_artist_text,
                "search_thumb_url": search_thumb_url_for_candidate
            })
        
        if self._check_cancelled(cancel_event, "after processing search results, before sorting"):
            return []

        norm_album_query = album.lower().strip() if album else ""
        norm_artist_query = artist.lower().strip() if artist else ""

        priority1_candidates: List[AlbumCandidate] = []
        priority2_candidates: List[AlbumCandidate] = []
        priority3_candidates: List[AlbumCandidate] = []
        processed_links = set()

        def create_candidate(item_data: Dict[str, Any]) -> AlbumCandidate:
            extra_data = { 
                "original_album_text": item_data["original_album_text"],
                "original_artist_text": item_data["original_artist_text"]
            }
            if item_data.get("search_thumb_url"): # This check ensures None is not added
                extra_data["search_thumb_url"] = item_data["search_thumb_url"]
            
            return AlbumCandidate(
                identifier=item_data["link"], 
                album_name=item_data.get("original_album_text"),
                artist_name=item_data.get("original_artist_text"),
                source_service=self.service_name,
                extra_data=extra_data
            )

        if norm_album_query and norm_artist_query:
            for item in raw_potential_matches_data:
                if self._check_cancelled(cancel_event, "during P1 candidate filtering"): break
                if item["album_name_lower"] == norm_album_query and \
                   item["artist_name_lower"] == norm_artist_query:
                    if item["link"] not in processed_links:
                        priority1_candidates.append(create_candidate(item))
                        processed_links.add(item["link"])
            if self._check_cancelled(cancel_event, "after P1 candidate filtering"): return []
        
        if norm_album_query:
            for item in raw_potential_matches_data:
                if self._check_cancelled(cancel_event, "during P2 candidate filtering"): break
                if item["album_name_lower"] == norm_album_query: 
                    if item["link"] not in processed_links:
                        if not norm_artist_query or item["artist_name_lower"] == norm_artist_query:
                             priority2_candidates.append(create_candidate(item))
                             processed_links.add(item["link"])
            if self._check_cancelled(cancel_event, "after P2 candidate filtering"): return []
        
        for item in raw_potential_matches_data:
            if self._check_cancelled(cancel_event, "during P3 candidate filtering"): break
            if item["link"] not in processed_links:
                priority3_candidates.append(create_candidate(item))
                processed_links.add(item["link"])
        if self._check_cancelled(cancel_event, "after P3 candidate filtering"): return []
        
        all_candidates = priority1_candidates + priority2_candidates + priority3_candidates
        
        if search_results_elements and not any_item_processed_successfully:
            # Main XPath found items, but loop failed to process ANY of them for essential data.
            msg = (f"Last.fm search for '{search_query}' found {len(search_results_elements)} result items, "
                   f"but ALL failed processing for essential data (e.g., album name/link). "
                   f"Sub-XPaths for essential data might be outdated. URL: {search_url}")
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverDataError(msg, url=search_url)

        if not all_candidates:
            # This implies either a genuine "no results" (handled earlier),
            # or filtering removed all items, or an error was already raised.
            logger.info(f"[{self.service_name}] No suitable album candidates identified after ordering and checks for '{search_query}'.")
            return []
        
        logger.info(f"[{self.service_name}] Found {len(all_candidates)} potential album candidates for '{search_query}', ordered by relevance.")
        return all_candidates

    def list_potential_images(self, candidate: AlbumCandidate, 
                              cancel_event: Optional[threading.Event] = None) -> List[PotentialImage]:
        if not isinstance(candidate, AlbumCandidate) or candidate.source_service != self.service_name:
            logger.error(f"[{self.service_name}] Invalid or non-Last.fm candidate provided to list_potential_images.")
            return []
        if self._check_cancelled(cancel_event, "before starting list_potential_images"):
            return []

        if not self.EXPAND_GALLERIES:
            search_thumb_url = candidate.extra_data.get("search_thumb_url")
            # search_thumb_url is None if it was a placeholder or not found during candidate search
            if search_thumb_url:
                match = self.fastly_thumb_pattern.match(search_thumb_url)
                if match:
                    base_url_part, filename_part = match.groups() # filename_part includes .ext
                    
                    # Double-check for placeholder, though search_album_candidates should filter it
                    clean_hash_from_search_thumb = filename_part.split('.')[0]
                    if clean_hash_from_search_thumb == self.PLACEHOLDER_IMAGE_HASH:
                        logger.warning(f"[{self.service_name}] Search thumb URL '{search_thumb_url}' for '{candidate.album_name}' unexpectedly resolved to placeholder here. Skipping.")
                        return []

                    derived_thumbnail_url = f"{base_url_part}174s/{filename_part}"
                    derived_full_image_url = f"{base_url_part}o/{filename_part}"
                    
                    potential_img = PotentialImage(
                        identifier=derived_full_image_url,
                        thumbnail_url=derived_thumbnail_url,
                        full_image_url=derived_full_image_url,
                        source_candidate=candidate,
                        extra_data={
                            'derived_from_search_thumb': True, 
                            'original_search_thumb': search_thumb_url,
                            'gallery_page_url': f"{self.base_url}{str(candidate.identifier)}/+images" # For reference
                        }
                    )
                    logger.info(f"[{self.service_name}] Derived image from search result for '{candidate.album_name}': {derived_full_image_url}")
                    return [potential_img]
                else:
                    msg = f"Could not parse provided search_thumb_url '{search_thumb_url}' using Fastly pattern for candidate '{candidate.album_name}' (EXPAND_GALLERIES=False)."
                    logger.warning(f"[{self.service_name}] {msg}")
                    # This is a data error because the URL from candidate.extra_data was unparseable by our expected pattern.
                    raise RetrieverDataError(msg)
            else: # No search_thumb_url (either not found or was placeholder)
                logger.info(f"[{self.service_name}] No usable search_thumb_url for candidate '{candidate.album_name}' (EXPAND_GALLERIES=False). No image derived from search result.")
            return [] # Valid: no image was found in search result, or it was a placeholder.

        # ----- Logic for EXPAND_GALLERIES = True (scrape gallery page) -----
        album_page_link = str(candidate.identifier) 
        gallery_page_url = f"{self.base_url}{album_page_link}/+images"
        
        logger.info(f"[{self.service_name}] Listing potential images (from gallery) for: '{candidate.album_name}' (Artist: '{candidate.artist_name}') from {gallery_page_url}")

        gallery_response_obj = super()._perform_http_get_request(
            url=gallery_page_url,
            cancel_event=cancel_event,
            request_context=f"Last.fm gallery page for '{candidate.album_name}'",
            expect_html_cloudflare=True
        )

        if self._check_cancelled(cancel_event, "after fetching gallery page") or gallery_response_obj is None:
            logger.debug(f"[{self.service_name}] Gallery page fetch cancelled or no response for '{candidate.album_name}'.")
            return []
        
        gallery_content_type = gallery_response_obj.headers.get('Content-Type', '').lower()
        if 'text/html' not in gallery_content_type:
            msg = f"Expected HTML from Last.fm gallery page '{gallery_page_url}', got {gallery_content_type}."
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverDataError(msg, url=gallery_page_url)

        if not gallery_response_obj.content:
            msg = f"No content received from Last.fm gallery page '{gallery_page_url}'."
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverDataError(msg, url=gallery_page_url)

        try:
            gallery_tree = html.fromstring(gallery_response_obj.content)
        except Exception as e_parse:
            if self._check_cancelled(cancel_event, "in HTML parse exception for gallery page"):
                return []
            msg = f"Failed to parse HTML from gallery page {gallery_page_url}"
            logger.error(f"[{self.service_name}] {msg}: {e_parse}", exc_info=True)
            raise RetrieverDataError(msg, original_exception=e_parse, url=gallery_page_url) from e_parse

        # Last.fm gallery pages don't have a specific "no images" message typically,
        # an empty image_list_items XPath result implies no images or XPath broken.
        image_list_items = gallery_tree.xpath('//a[contains(@class, "image-list-item")]')
        logger.debug(f"[{self.service_name}] Found {len(image_list_items)} image items on gallery page for '{candidate.album_name}'.")
        
        collected_potential_images: List[PotentialImage] = []
        seen_full_urls = set() 

        for item_el in image_list_items:
            if self._check_cancelled(cancel_event, "in gallery image items loop"):
                break
            thumb_src_nodes = item_el.xpath('./img/@src')
            if not thumb_src_nodes: continue
            
            thumbnail_url_from_gallery = thumb_src_nodes[0] # e.g., .../300x300/hash.jpg or .../174s/hash.jpg
            match = self.fastly_thumb_pattern.match(thumbnail_url_from_gallery)
            if not match:
                logger.warning(f"[{self.service_name}] Gallery thumbnail URL {thumbnail_url_from_gallery} did not match Fastly pattern for '{candidate.album_name}'. Skipping.")
                continue

            base_url_part, hash_filename_part = match.groups() # hash_filename_part includes .ext
            clean_hash = hash_filename_part.split('.')[0] 

            if clean_hash == self.PLACEHOLDER_IMAGE_HASH:
                logger.debug(f"[{self.service_name}] Gallery item for '{candidate.album_name}' is a placeholder image ({thumbnail_url_from_gallery}). Skipping.")
                continue
            
            full_image_url_from_gallery = f"{base_url_part}o/{hash_filename_part}" # Use 'o' for original size

            if full_image_url_from_gallery in seen_full_urls:
                logger.debug(f"[{self.service_name}] Duplicate full image URL {full_image_url_from_gallery} on gallery page for '{candidate.album_name}'. Skipping.")
                continue
            seen_full_urls.add(full_image_url_from_gallery)
            
            image_identifier = full_image_url_from_gallery 

            potential_img = PotentialImage(
                identifier=image_identifier,
                thumbnail_url=thumbnail_url_from_gallery,
                full_image_url=full_image_url_from_gallery,
                source_candidate=candidate,
                extra_data={'gallery_page_url': gallery_page_url, 'derived_from_gallery': True} 
            )
            collected_potential_images.append(potential_img)
            logger.debug(f"[{self.service_name}] Found potential image (from gallery) for '{candidate.album_name}': Full URL='{full_image_url_from_gallery}'")
        
        if self._check_cancelled(cancel_event, "after processing gallery items"):
            return []

        if gallery_response_obj and not image_list_items and not collected_potential_images:
            # We fetched the gallery page, but our XPath found no image items.
            # This implies the XPath is broken or the page structure for galleries changed.
            # (We check gallery_response_obj to ensure we actually attempted a fetch).
            msg = (f"Last.fm gallery page for '{candidate.album_name}' ({gallery_page_url}) was fetched, "
                   f"but no image items were found using XPath '//a[contains(@class, \"image-list-item\")]'. "
                   f"Page structure may have changed or XPath is outdated.")
            logger.warning(f"[{self.service_name}] {msg}")
            page_snippet = gallery_response_obj.text[:1000] if hasattr(gallery_response_obj, 'text') else "N/A"
            logger.debug(f"[{self.service_name}] Gallery page snippet for '{candidate.album_name}':\n{page_snippet}")
            # Unlike search, an empty gallery might just mean no images were uploaded.
            # However, if the XPath *itself* is the problem, we'd want to know.
            # For now, returning [] as "no images found on gallery", but this could be a DataError if XPaths are assumed stable.
            # Let's be cautious: if the XPath finds *nothing*, it's more likely an XPath issue than truly zero images IF images are expected.
            # But Last.fm might legitimately have empty galleries.
            # Let's log a warning if XPath is empty but don't raise DataError unless we are very sure.
            # For consistency with Bandcamp, if main list xpath fails to find items, it IS a data error.
            raise RetrieverDataError(msg, url=gallery_page_url)


        logger.info(f"[{self.service_name}] Found {len(collected_potential_images)} unique potential images (from gallery) for candidate '{candidate.album_name}'.")
        return collected_potential_images

    def resolve_image_details(self, potential_image: PotentialImage, 
                              cancel_event: Optional[threading.Event] = None) -> Optional[ImageResult]:
        if not isinstance(potential_image, PotentialImage) or potential_image.source_candidate.source_service != self.service_name:
            logger.error(f"[{self.service_name}] Invalid or non-Last.fm PotentialImage provided to resolve_image_details.")
            return None
        if self._check_cancelled(cancel_event, "before starting resolve_image_details"):
            return None

        logger.debug(f"[{self.service_name}] Resolving details for image: {potential_image.full_image_url}")

        width, height = self.get_image_dimensions(potential_image.full_image_url,
                                                  cancel_event=cancel_event)

        if self._check_cancelled(cancel_event, f"after get_image_dimensions for {potential_image.full_image_url}"):
            return None

        if width and height:
            logger.debug(f"[{self.service_name}] Successfully resolved dimensions for {potential_image.full_image_url}: {width}x{height}")
            return ImageResult.from_potential_image(
                potential_image=potential_image,
                full_width=width,
                full_height=height
            )
        else:
            if not self._check_cancelled(cancel_event, "at end of resolve_image_details (failed to get dimensions)"):
                logger.warning(f"[{self.service_name}] Could not get dimensions for Last.fm image: {potential_image.full_image_url}.")
            return None