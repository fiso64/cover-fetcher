# retrievers/vgmdb.py
import logging
import requests # Still needed for Response type hint and exceptions
from lxml import html
import urllib.parse
import re
from typing import List, Optional, Tuple, Dict, Any
import threading
# import cloudscraper # No longer imported at top-level

from .base_retriever import (
    AbstractImageRetriever, AlbumCandidate, PotentialImage, ImageResult,
    RetrieverError, RetrieverNetworkError, RetrieverAPIError, RetrieverDataError, RetrieverInputError
)
from utils.config import DEFAULT_REQUESTS_HEADERS

logger = logging.getLogger(__name__)

class VGMDBRetriever(AbstractImageRetriever):
    """
    Retrieves album art from VGMdb.net. Because album searches do not work when an artist is included,
    this retriever ignores all provided artist strings.
    """
    service_name = "VGMdb"
    
    def __init__(self):
        super().__init__()
        self.base_url = "https://vgmdb.net"
        
        # Pattern to parse image URLs from media.vgm.io or medium-media.vgm.io
        # Group 1: scheme (http:// or https://)
        # Group 2: "medium-" prefix (optional)
        # Group 3: "media.vgm.io/albums/" followed by the image path
        self.vgmdb_image_url_pattern = re.compile(
            r"^(https?://)(medium-)?(media\.vgm\.io/albums/.*)$", re.IGNORECASE
        )
        
        # Regex to extract URL from style="background-image: url('...')"
        self.bg_image_url_extract_pattern = re.compile(r"background-image:\s*url\(['\"]?([^'\"]+)['\"]?\)")
        
        # Pattern to detect album page URL (e.g. after a redirect)
        self.album_page_url_pattern = re.compile(r"^https://vgmdb\.net/album/(\d+)")

        # self.scraper = cloudscraper.create_scraper() # Handled by base class now

    def _derive_image_urls(self, image_url: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Derives thumbnail (medium-media...) and full-size (media...) image URLs 
        from a VGMdb media.vgm.io image URL.
        Input can be either medium-media... or media...
        """
        cleaned_url = image_url.split('?')[0] # Remove query params
        
        match = self.vgmdb_image_url_pattern.match(cleaned_url)
        
        if not match:
            # Try to handle if URL is already absolute and starts with base_url + /db/assets... (less common source)
            if cleaned_url.startswith(self.base_url + "/db/assets/covers/"):
                 # This case is not directly handled by vgmdb_image_url_pattern
                 # but if it appears, we'd need different logic, or ensure inputs are always media.vgm.io
                 logger.warning(f"[{self.service_name}] Image URL '{cleaned_url}' is a direct vgmdb.net asset URL, not directly parseable by current media.vgm.io pattern for deriving thumb/full. Pattern: {self.vgmdb_image_url_pattern.pattern}. Returning as is.")
                 return cleaned_url, cleaned_url # Fallback, treat as full and thumb

            logger.warning(f"[{self.service_name}] Image URL '{cleaned_url}' (from '{image_url}') does not match VGMdb media pattern: {self.vgmdb_image_url_pattern.pattern}")
            return None, None

        scheme = match.group(1)  # e.g., "https://"
        # group(2) is "medium-" or None
        main_path_part = match.group(3) # e.g., "media.vgm.io/albums/some/path.jpg"
            
        full_url = f"{scheme}{main_path_part}"
        # The main_path_part by definition does not start with "medium-".
        thumb_url = f"{scheme}medium-{main_path_part}"
            
        return thumb_url, full_url

    def search_album_candidates(self, artist: str, album: str, 
                                cancel_event: Optional[threading.Event] = None) -> List[AlbumCandidate]:
        if self._check_cancelled(cancel_event, "before starting search_album_candidates"):
            return []
        logger.info(f"[{self.service_name}] Searching for VGMdb album candidates: '{album}'")
        if artist:
            logger.info(f"[{self.service_name}] VGMdb does not support artists in search queries. Ignoring artist: '{artist}'")

        search_query = re.sub(r'[^\w\s\-.:()]', ' ', album.strip()).strip()
        
        if not search_query:
            msg = "Both album search term is effectively empty for VGMdb search."
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverInputError(msg)
        
        search_url = f"{self.base_url}/search?q={urllib.parse.quote_plus(search_query)}&type="
        logger.info(f"[{self.service_name}] Searching VGMdb: {search_url}")

        search_response_obj = super()._perform_http_get_request(
            url=search_url,
            cancel_event=cancel_event,
            request_context=f"VGMdb album search for '{search_query}'",
            expect_html_cloudflare=True # Indicate this request might need scraper
        )

        if self._check_cancelled(cancel_event, "after VGMdb search request") or search_response_obj is None:
            logger.debug(f"[{self.service_name}] Search cancelled or no response from VGMdb for '{search_query}'.")
            return []
        
        final_url_after_redirects = search_response_obj.url
        content_type = search_response_obj.headers.get('Content-Type', '').lower()
        if 'text/html' not in content_type:
            msg = f"Expected HTML from VGMdb search '{search_query}' (final URL: {final_url_after_redirects}), got {content_type}."
            logger.warning(f"[{self.service_name}] {msg}")
            raise RetrieverDataError(msg, url=final_url_after_redirects)

        if not search_response_obj.content:
            msg = f"No content received from VGMdb search '{search_query}' (final URL: {final_url_after_redirects})."
            logger.error(f"[{self.service_name}] {msg}")
            raise RetrieverDataError(msg, url=final_url_after_redirects)

        try:
            tree = html.fromstring(search_response_obj.content)
        except Exception as e_parse:
            if self._check_cancelled(cancel_event, "in HTML parse for search results"): return []
            msg = f"Failed to parse HTML from VGMdb search for '{search_query}' (final URL: {final_url_after_redirects})"
            logger.error(f"[{self.service_name}] {msg}: {e_parse}", exc_info=True)
            raise RetrieverDataError(msg, original_exception=e_parse, url=final_url_after_redirects) from e_parse

        album_page_redirect_match = self.album_page_url_pattern.match(final_url_after_redirects)
        if album_page_redirect_match:
            album_id_from_url = album_page_redirect_match.group(1)
            logger.info(f"[{self.service_name}] Search for '{search_query}' redirected to single album page: {final_url_after_redirects}")
            
            album_title_nodes = tree.xpath('//div[@id="innermain"]//h1//span[@class="albumtitle" and @lang="en"]/descendant-or-self::*/text()')
            extracted_album_title = " ".join(t.strip() for t in album_title_nodes).strip() if album_title_nodes else None

            if not extracted_album_title:
                title_tag_nodes = tree.xpath('//title/text()')
                if title_tag_nodes:
                    full_title_text = title_tag_nodes[0].strip()
                    parsed_title = re.sub(r'\s*\[[^\]]+\]\s*-\s*VGMdb$', '', full_title_text, flags=re.IGNORECASE)
                    parsed_title = re.sub(r'\s*-\s*VGMdb$', '', parsed_title, flags=re.IGNORECASE)
                    if parsed_title == full_title_text: 
                        parsed_title = full_title_text.rsplit(' - VGMdb', 1)[0]
                    extracted_album_title = parsed_title.strip()
            
            if not extracted_album_title:
                extracted_album_title = album 
                logger.warning(f"[{self.service_name}] Could not parse album title from redirected page {final_url_after_redirects}. Using query album name: '{album}'.")

            candidate = AlbumCandidate(
                identifier=final_url_after_redirects,
                album_name=extracted_album_title,
                artist_name=artist, 
                source_service=self.service_name,
                extra_data={'vgmdb_id': album_id_from_url, 'search_redirected': True}
            )
            return [candidate]

        no_results_indicator = tree.xpath('//h3[@class="label" and starts-with(normalize-space(text()), "0 album results for")]')
        if no_results_indicator:
            logger.info(f"[{self.service_name}] VGMdb search for '{search_query}' explicitly indicated '0 album results'. URL: {search_url}")
            return []
        
        search_results_elements = tree.xpath(
            '//table[.//td//a[contains(@class, "albumtitle")]]//tbody/tr[td[3]/a[contains(@class, "albumtitle")]]'
        )
        
        if not search_results_elements:
            page_text_lower = search_response_obj.text[:1500].lower() if hasattr(search_response_obj, 'text') else ""
            if "your search query was too short" in page_text_lower or "must be at least 3 characters" in page_text_lower:
                logger.info(f"[{self.service_name}] VGMdb search for '{search_query}' resulted in 'query too short'. No results. URL: {search_url}")
                return []
            if "no results found" in page_text_lower:
                 logger.info(f"[{self.service_name}] VGMdb search for '{search_query}' text indicates 'no results found'. URL: {search_url}")
                 return []

            msg = (f"VGMdb search for '{search_query}' did not redirect, had no '0 album results' header, "
                   f"and no album items found using XPath. Page structure may have changed or no results. URL: {search_url}")
            logger.warning(f"[{self.service_name}] {msg}")
            return []

        all_candidates: List[AlbumCandidate] = []
        processed_links = set()

        for result_el in search_results_elements:
            if self._check_cancelled(cancel_event, "in search results loop"): break
            
            album_link_node = result_el.xpath('./td[3]/a[@class="albumtitle" or contains(@class, "album-")]') 
            if not album_link_node:
                logger.debug(f"[{self.service_name}] Skipping VGMdb search result item (no album link node).")
                continue
            
            album_page_link_relative = album_link_node[0].get("href")
            found_album_name = album_link_node[0].get("title", "").strip()

            if not album_page_link_relative or not found_album_name:
                logger.debug(f"[{self.service_name}] Skipping VGMdb search result (missing href/title). Href: '{album_page_link_relative}', Title: '{found_album_name}'.")
                continue
            
            album_page_link_absolute = urllib.parse.urljoin(self.base_url, album_page_link_relative.strip())
            if album_page_link_absolute in processed_links:
                continue
            
            candidate = AlbumCandidate(
                identifier=album_page_link_absolute,
                album_name=found_album_name,
                artist_name=artist, 
                source_service=self.service_name,
                extra_data={'search_result_album_text': found_album_name} 
            )
            all_candidates.append(candidate)
            processed_links.add(album_page_link_absolute)
        
        if self._check_cancelled(cancel_event, "after processing search results"): return []

        if search_results_elements and not all_candidates:
            logger.warning(
                f"[{self.service_name}] VGMdb search for '{search_query}' found {len(search_results_elements)} "
                f"potential items, but none processed into candidates. URL: {search_url}"
            )
        
        if not all_candidates:
            logger.info(f"[{self.service_name}] No VGMdb album candidates identified for '{search_query}'.")
            return [] 
        
        logger.info(f"[{self.service_name}] Found {len(all_candidates)} potential VGMdb album candidates for '{search_query}'.")
        return all_candidates

    def list_potential_images(self, candidate: AlbumCandidate, 
                              cancel_event: Optional[threading.Event] = None) -> List[PotentialImage]:
        if candidate.source_service != self.service_name:
            logger.error(f"[{self.service_name}] Non-VGMdb candidate passed.")
            return []
        if self._check_cancelled(cancel_event, "before listing potential images"): return []

        album_page_url = str(candidate.identifier)
        logger.info(f"[{self.service_name}] Listing images for VGMdb candidate '{candidate.album_name}' from: {album_page_url}")

        page_response_obj = super()._perform_http_get_request(
            url=album_page_url,
            cancel_event=cancel_event,
            request_context=f"VGMdb album page for '{candidate.album_name}'",
            expect_html_cloudflare=True # Indicate this request might need scraper
        )

        if self._check_cancelled(cancel_event, "after album page request") or page_response_obj is None:
            return []

        content_type = page_response_obj.headers.get('Content-Type', '').lower()
        if 'text/html' not in content_type:
            raise RetrieverDataError(f"Expected HTML from VGMdb album page '{album_page_url}', got {content_type}.", url=album_page_url)

        try:
            tree = html.fromstring(page_response_obj.content)
        except Exception as e_parse:
            if self._check_cancelled(cancel_event, "in HTML parse for album page"): return []
            raise RetrieverDataError(f"Failed to parse HTML from VGMdb album page '{album_page_url}'", original_exception=e_parse, url=album_page_url) from e_parse
        
        potential_images: List[PotentialImage] = []
        gallery_images_processed_successfully = False

        gallery_div_list = tree.xpath('//div[@id="cover_gallery"]')
        if gallery_div_list:
            gallery_div = gallery_div_list[0]
            # XPath: find <a> tags with class 'highslide' and an href, within any table inside the gallery_div
            gallery_item_links = gallery_div.xpath('.//table//a[contains(@class, "highslide") and @href]')
            logger.debug(f"[{self.service_name}] Found {len(gallery_item_links)} items in #cover_gallery for '{candidate.album_name}'.")

            processed_gallery_full_urls = set() # To avoid duplicates from gallery itself

            for item_link_el in gallery_item_links:
                if self._check_cancelled(cancel_event, "in VGMdb gallery loop"): break
                
                medium_img_url_relative = item_link_el.get("href")
                if not medium_img_url_relative:
                    continue
                
                medium_img_url_abs = urllib.parse.urljoin(album_page_url, medium_img_url_relative.strip())

                # Get image type description from <h4 class="label"> inside the <a>
                label_text_nodes = item_link_el.xpath('./h4[@class="label"]/descendant-or-self::*/text()')
                image_type_desc = " ".join(t.strip() for t in label_text_nodes).strip()
                
                is_front = image_type_desc.lower().startswith(("front", "cover"))
                
                thumb_url, full_url = self._derive_image_urls(medium_img_url_abs)
                
                if full_url and full_url not in processed_gallery_full_urls:
                    actual_thumb_url = thumb_url if thumb_url else full_url 
                    if not actual_thumb_url:
                        logger.warning(f"[{self.service_name}] Gallery: Full URL {full_url} derived but thumb URL is None for {medium_img_url_abs}. Skipping.")
                        continue

                    pi = PotentialImage(
                        identifier=full_url,
                        thumbnail_url=actual_thumb_url,
                        full_image_url=full_url,
                        source_candidate=candidate,
                        is_front=is_front,
                        original_type=image_type_desc,
                        extra_data={'vgmdb_image_type': image_type_desc}
                    )
                    potential_images.append(pi)
                    processed_gallery_full_urls.add(full_url)
                    gallery_images_processed_successfully = True # Mark that we got at least one
                    logger.debug(f"[{self.service_name}] Added from gallery: {full_url} (Type: '{image_type_desc}', Front: {is_front})")
            
            if gallery_images_processed_successfully:
                 logger.info(f"[{self.service_name}] Successfully processed {len(potential_images)} image(s) from #cover_gallery for '{candidate.album_name}'.")


        # Fallback to #coverart if gallery wasn't found or yielded no images
        if not gallery_images_processed_successfully:
            if gallery_div_list: # Means gallery was found but yielded no usable images
                 logger.info(f"[{self.service_name}] #cover_gallery was present but yielded no images for '{candidate.album_name}'. Falling back to #coverart.")
            else: # Means #cover_gallery div was not found at all
                 logger.info(f"[{self.service_name}] #cover_gallery not found for '{candidate.album_name}'. Falling back to #coverart.")

            coverart_divs = tree.xpath('//div[@id="coverart"]')
            if coverart_divs:
                coverart_div = coverart_divs[0]
                style_attr = coverart_div.get("style", "")
                bg_url_match = self.bg_image_url_extract_pattern.search(style_attr)
                
                if bg_url_match:
                    raw_thumb_url_from_style = bg_url_match.group(1)
                    abs_thumb_url = urllib.parse.urljoin(album_page_url, raw_thumb_url_from_style) if not raw_thumb_url_from_style.startswith('http') else raw_thumb_url_from_style
                    
                    thumb_url, full_url = self._derive_image_urls(abs_thumb_url)
                    
                    if full_url:
                        actual_thumb_url = thumb_url if thumb_url else full_url
                        if not actual_thumb_url: # Should not happen if full_url is good
                            logger.warning(f"[{self.service_name}] Fallback #coverart: Full URL {full_url} derived but thumb URL is None for {abs_thumb_url}. Skipping.")
                        else:
                            # Check if this image was already added (e.g., if gallery processing failed mid-way but this is a duplicate)
                            # This is a simple check; more robust deduplication might be needed if complex scenarios arise.
                            if not any(pi.full_image_url == full_url for pi in potential_images):
                                pi = PotentialImage(
                                    identifier=full_url, thumbnail_url=actual_thumb_url, full_image_url=full_url,
                                    source_candidate=candidate,
                                    is_front=True, # Assume #coverart is front
                                    original_type='Cover Art',
                                    extra_data={'vgmdb_image_type': 'Cover Art'}
                                )
                                potential_images.append(pi)
                                logger.info(f"[{self.service_name}] Added from #coverart fallback: {full_url}")
                            else:
                                logger.debug(f"[{self.service_name}] Fallback #coverart: Image {full_url} already found, not re-adding.")
                    else:
                        logger.warning(f"[{self.service_name}] Fallback #coverart: Could not derive valid full URL from style URL: '{abs_thumb_url}' for '{candidate.album_name}'.")
                else:
                    logger.warning(f"[{self.service_name}] Fallback #coverart: Could not extract background-image URL from style '{style_attr}' for '{candidate.album_name}'.")
            else: # #coverart div not found
                 logger.warning(f"[{self.service_name}] Fallback #coverart: <div id='coverart'> not found for '{candidate.album_name}'.")


        if not potential_images:
            logger.warning(f"[{self.service_name}] No potential images found for '{candidate.album_name}' from gallery or #coverart.")
        else:
            # Sort images to prioritize "front" covers if multiple images were found
            potential_images.sort(key=lambda img: not img.is_front) # False (is_front=True) comes before True (is_front=False)
            logger.info(f"[{self.service_name}] Finalized {len(potential_images)} potential image(s) for '{candidate.album_name}'. Front cover prioritized: {potential_images[0].full_image_url if potential_images and potential_images[0].is_front else 'N/A'}.")
        
        return potential_images

    def resolve_image_details(self, potential_image: PotentialImage, 
                              cancel_event: Optional[threading.Event] = None) -> Optional[ImageResult]:
        if potential_image.source_candidate.source_service != self.service_name:
             logger.error(f"[{self.service_name}] Non-VGMdb PotentialImage passed.")
             return None
        if self._check_cancelled(cancel_event, "before resolving image details"): return None

        logger.debug(f"[{self.service_name}] Resolving details for VGMdb image: {potential_image.full_image_url}")
        
        width, height = super().get_image_dimensions(potential_image.full_image_url, 
                                                     cancel_event=cancel_event)
        
        if self._check_cancelled(cancel_event, f"after get_image_dimensions for {potential_image.full_image_url}"):
            return None

        if width and height and width > 0 and height > 0:
            return ImageResult.from_potential_image(
                potential_image=potential_image,
                full_width=width,
                full_height=height
            )
        else:
            if not self._check_cancelled(cancel_event, "at end of resolve_image_details (failed dimensions)"):
                 logger.warning(f"[{self.service_name}] Could not get dimensions for VGMdb image: {potential_image.full_image_url}.")
            return None