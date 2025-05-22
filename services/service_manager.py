# services/service_manager.py
import concurrent.futures
import logging
from typing import List, Dict, Callable, Any, Tuple, Optional, Union
from collections import deque
import threading 
import time
import copy 

from services.models import AlbumCandidate, PotentialImage, ImageResult
from retrievers.base_retriever import AbstractImageRetriever
from utils.config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)

def _log_executor_task_exceptions(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        func_name_for_log = getattr(fn, '__name__', str(fn))
        logger.error(
            f"Unhandled exception in background task executed by ThreadPoolExecutor: {func_name_for_log}",
            exc_info=True
        )

ServiceAlbumSearchSucceededCb = Callable[[str, int], None] # service_name, num_candidates
PotentialImageFoundCb = Callable[[str, PotentialImage], None]
ImageResolvedCb = Callable[[str, ImageResult], None]
# ServiceBatchDoneCb is removed and replaced by the following three:
ServiceBatchCompletedSuccessfullyCb = Callable[[str, bool], None] # service_name, has_more
ServiceBatchCancelledCb = Callable[[str], None] # service_name
ServiceBatchErrorCb = Callable[[str, str], None] # service_name, error_message
AllServicesSearchesDoneCb = Callable[[], None]
ServiceErrorCb = Callable[[str, str], None] # For general service errors

class ServiceProcessingState:
    def __init__(self):
        self.candidates_queue: deque[AlbumCandidate] = deque()
        self.current_candidate_pis: deque[PotentialImage] = deque() 
        self.active_candidate: Optional[AlbumCandidate] = None
        self.is_listing_for_active_candidate: bool = False
        self.all_candidates_processed_for_listing: bool = False
        self.total_images_resolved_this_session: int = 0
        self.cancel_event: threading.Event = threading.Event()

DEFAULT_IMAGES_PER_BATCH = 5

class ServiceManager:
    def __init__(self, max_concurrent_image_resolutions_per_service: int = 3):
        self.retrievers: Dict[str, AbstractImageRetriever] = {}
        self._service_data: Dict[str, ServiceProcessingState] = {}
        self._service_locks: Dict[str, threading.RLock] = {}
        
        all_known_service_names = list(AbstractImageRetriever._registry.keys())
        logger.debug(f"[serviceManager] Got all service names: {all_known_service_names}")
        if not all_known_service_names:
            logger.warning("AbstractImageRetriever registry is empty, falling back to DEFAULT_CONFIG for service names.")
            all_known_service_names = [s[0] for s in DEFAULT_CONFIG.get("services", [])]

        successful_initializations = 0
        for service_name in all_known_service_names:
            self._service_data[service_name] = ServiceProcessingState()
            self._service_locks[service_name] = threading.RLock()
            retriever_class = AbstractImageRetriever.get_retriever_class(service_name)
            if retriever_class:
                try:
                    self.retrievers[service_name] = retriever_class()
                    successful_initializations += 1
                    logger.info(f"Successfully initialized retriever for service: {service_name}")
                except Exception as e:
                    logger.error(f"Failed to initialize retriever for {service_name}: {e}", exc_info=True)
            else:
                logger.warning(f"No retriever class registered for service: {service_name}")

        if successful_initializations == 0:
            logger.warning("ServiceManager initialized with NO active retrievers based on registry/defaults!")
        else:
            logger.info(f"ServiceManager initialized {successful_initializations} total retrievers.")
            
        num_potential_retrievers = len(self.retrievers) if self.retrievers else 1
        self.max_concurrent_resolutions = max_concurrent_image_resolutions_per_service * num_potential_retrievers
        
        self.image_resolution_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_concurrent_resolutions if self.max_concurrent_resolutions > 0 else 1, 
            thread_name_prefix="ImageResolver"
        )
        self.service_processing_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=num_potential_retrievers if num_potential_retrievers > 0 else 1, 
            thread_name_prefix="ServiceProcessor"
        )
        
        self._active_search_services_count = 0
        self._shutdown_event = threading.Event()
        self._global_search_count_lock = threading.Lock()
        self.current_search_front_only: bool = False
        self._config_for_current_search: List[Tuple[str, bool]] = [] # Stores config for active search

        self.on_service_album_search_succeeded: Optional[ServiceAlbumSearchSucceededCb] = None
        self.on_potential_image_found: Optional[PotentialImageFoundCb] = None
        self.on_image_resolved: Optional[ImageResolvedCb] = None
        # self.on_service_batch_processing_done is replaced by:
        self.on_service_batch_completed_successfully: Optional[ServiceBatchCompletedSuccessfullyCb] = None
        self.on_service_batch_cancelled: Optional[ServiceBatchCancelledCb] = None
        self.on_service_batch_error: Optional[ServiceBatchErrorCb] = None # For batch processing errors
        self.on_all_service_searches_concluded: Optional[AllServicesSearchesDoneCb] = None
        self.on_service_error: Optional[ServiceErrorCb] = None # For general errors
        
    def set_callbacks(self, album_search_succeeded_cb, potential_cb, resolved_cb,
                      batch_completed_cb, batch_cancelled_cb, batch_error_cb, # Updated params
                      all_done_cb, error_cb):
        self.on_service_album_search_succeeded = album_search_succeeded_cb
        self.on_potential_image_found = potential_cb
        self.on_image_resolved = resolved_cb
        # self.on_service_batch_processing_done assignment removed
        self.on_service_batch_completed_successfully = batch_completed_cb
        self.on_service_batch_cancelled = batch_cancelled_cb
        self.on_service_batch_error = batch_error_cb
        self.on_all_service_searches_concluded = all_done_cb
        self.on_service_error = error_cb # This remains for general service errors

    def _reset_internal_state(self, active_services_config: List[Tuple[str, bool]]):
        logger.info("[ServiceManager] Resetting internal state for new search.")
        
        if self._shutdown_event.is_set():
            logger.info("[ServiceManager] Shutdown in progress, minimal reset for events only.")
            for service_name in self._service_data.keys(): # Iterate all existing service data
                self._service_data[service_name].cancel_event.set()
            return

        active_service_names_for_search = {name for name, enabled in active_services_config if enabled}

        for service_name, state_to_reset in self._service_data.items():
            with self._service_locks[service_name]:
                if service_name in active_service_names_for_search:
                    state_to_reset.cancel_event.clear() 
                else:
                    state_to_reset.cancel_event.set() 

                state_to_reset.candidates_queue.clear()
                state_to_reset.current_candidate_pis.clear()
                state_to_reset.active_candidate = None
                state_to_reset.is_listing_for_active_candidate = False
                state_to_reset.all_candidates_processed_for_listing = False
                state_to_reset.total_images_resolved_this_session = 0

                # Retrievers are now stateless and reset_state() has been removed.
                # Some other parts of service_manager might be unnecessary now, but
                # have been kept around just in case.

                # if service_name in self.retrievers:
                #     self.retrievers[service_name].reset_state()
        
        self._active_search_services_count = 0

    def start_album_art_search(self, artist: str, album: str, front_only: bool, 
                               active_services_config: List[Tuple[str, bool]],
                               batch_size: Optional[int] = None):
        if self._shutdown_event.is_set():
            logger.warning("[ServiceManager] Shutdown in progress, aborting new search.")
            if self.on_all_service_searches_concluded:
                self.on_all_service_searches_concluded()
            return

        self._reset_internal_state(active_services_config)
        self.current_search_front_only = front_only
        self._config_for_current_search = list(active_services_config) 
        
        services_to_search_this_run = []
        for service_name, enabled in active_services_config:
            if enabled:
                if service_name in self.retrievers and service_name in self._service_data:
                    services_to_search_this_run.append(service_name)
                else:
                    logger.error(f"Service '{service_name}' is configured as enabled but no retriever or state instance found. Skipping for this search.")
        
        if not services_to_search_this_run:
            logger.warning("[ServiceManager] No active and valid retrievers for this search based on provided config. Aborting search.")
            if self.on_all_service_searches_concluded:
                self.on_all_service_searches_concluded()
            return

        self._active_search_services_count = len(services_to_search_this_run)
        for service_name in services_to_search_this_run:
            retriever = self.retrievers[service_name]
            service_state = self._service_data[service_name]
            
            if service_state.cancel_event.is_set(): 
                logger.warning(f"[{service_name}] Starting search, but service cancel event was already set (e.g. from reset or previous cancel). This search for the service will likely be skipped or curtailed.")
                with self._global_search_count_lock:
                    if self._active_search_services_count > 0: self._active_search_services_count -=1
                    if self._active_search_services_count == 0 and self.on_all_service_searches_concluded and not self._shutdown_event.is_set():
                        self.on_all_service_searches_concluded()
                # If search is skipped due to pre-cancellation, directly signal batch cancelled for this service.
                # The on_service_album_search_succeeded event is not sent as the search didn't occur.
                if self.on_service_batch_cancelled: self.on_service_batch_cancelled(service_name)
                continue

            _batch_size_to_use = batch_size if batch_size is not None and batch_size > 0 else DEFAULT_IMAGES_PER_BATCH
            self.service_processing_executor.submit(
                _log_executor_task_exceptions,
                self._search_and_process_initial_batch,
                retriever, 
                artist, 
                album,
                self.current_search_front_only,
                _batch_size_to_use, 
                service_state.cancel_event, 
                self._shutdown_event,
                active_services_config 
            )

    def _search_and_process_initial_batch(self, retriever: AbstractImageRetriever, artist: str, album: str,
                                          front_only_for_this_task: bool,
                                          initial_batch_size: int, 
                                          cancel_event: threading.Event, shutdown_event: threading.Event,
                                          current_active_services_config: List[Tuple[str, bool]]):
        service_name = retriever.service_name
        
        def _check_cancelled(context_msg: str = "") -> bool:
            if shutdown_event.is_set():
                logger.info(f"[{service_name}] Operation cancelled (shutdown event): {context_msg}")
                return True
            if cancel_event.is_set():
                logger.info(f"[{service_name}] Operation cancelled (service event): {context_msg}")
                return True
            return False

        if _check_cancelled(f"at start of _search_and_process_initial_batch for '{album}'"):
            with self._global_search_count_lock: 
                if self._active_search_services_count > 0: self._active_search_services_count -= 1
                if self._active_search_services_count == 0 and self.on_all_service_searches_concluded and not shutdown_event.is_set():
                    self.on_all_service_searches_concluded()
            return 
        
        is_still_configured_and_enabled = any(s_name == service_name and enabled for s_name, enabled in current_active_services_config)
        if not is_still_configured_and_enabled:
            logger.info(f"[{service_name}] Skipped initial search as service is no longer active or configured as enabled in current search config.")
            with self._global_search_count_lock: 
                if self._active_search_services_count > 0: self._active_search_services_count -=1
                if self._active_search_services_count == 0 and self.on_all_service_searches_concluded and not shutdown_event.is_set():
                    self.on_all_service_searches_concluded()
            return 

        try:
            if _check_cancelled(f"before search_album_candidates for '{album}'"): return
            logger.info(f"[{service_name}] Starting initial search for '{album}' by '{artist}'.")
            candidates = retriever.search_album_candidates(artist, album, cancel_event) 
            if _check_cancelled(f"after search_album_candidates for '{album}'"): return

            # Album search completed successfully (even if no candidates found)
            num_found_candidates = len(candidates) if candidates else 0
            if self.on_service_album_search_succeeded:
                self.on_service_album_search_succeeded(service_name, num_found_candidates)

            lock = self._service_locks[service_name]
            with lock:
                if _check_cancelled(f"before processing candidates for '{album}' (inside lock)"): return
                state = self._service_data[service_name]
                if candidates: # num_found_candidates > 0
                    state.candidates_queue.extend(candidates)
                    # The on_service_album_search_succeeded was already called above
                    self._process_image_batch_for_service(service_name, retriever, initial_batch_size, front_only_for_this_task, cancel_event, shutdown_event, current_active_services_config)
                else: # num_found_candidates == 0
                    state.all_candidates_processed_for_listing = True
                    # The on_service_album_search_succeeded was already called above with 0
                    # Successfully processed (found no candidates), so batch completed with no more.
                    if self.on_service_batch_completed_successfully: self.on_service_batch_completed_successfully(service_name, False)
        except Exception as e:
            if not _check_cancelled(f"in exception handler for '{album}'"): 
                logger.error(f"[{service_name}] Error during initial search: {e}", exc_info=True)
                # Use the general service error for candidate search errors
                if self.on_service_error: self.on_service_error(service_name, f"Error searching candidates: {str(e)}")
            # Also indicate batch error for this service's initial processing
            if not _check_cancelled("before error callbacks in initial search") :
                # EVT_ServiceAlbumSearchSucceeded is NOT sent on error.
                # The EVT_ServiceBatchErrored event below covers this failure for the batch.
                if self.on_service_batch_error: self.on_service_batch_error(service_name, f"Error during initial search phase: {str(e)}")
        finally:
            with self._global_search_count_lock:
                if self._active_search_services_count > 0: self._active_search_services_count -= 1
                logger.debug(f"[{service_name}] Decremented _active_search_services_count to {self._active_search_services_count} after initial search phase.")
                if self._active_search_services_count == 0 and self.on_all_service_searches_concluded and not shutdown_event.is_set():
                    logger.info("[ServiceManager] All initial service search attempts concluded (count reached zero).")
                    self.on_all_service_searches_concluded()

    def request_more_for_service(self, service_name: str, current_active_services_config: List[Tuple[str, bool]],
                                 batch_size: Optional[int] = None):
        if self._shutdown_event.is_set():
            logger.info(f"[{service_name}] 'request_more' ignored due to shutdown signal.")
            # If shutdown, consider it a cancellation of any pending batch work
            if self.on_service_batch_cancelled: self.on_service_batch_cancelled(service_name)
            return

        is_currently_enabled = any(s_name == service_name and enabled for s_name, enabled in current_active_services_config)
        if not is_currently_enabled:
            logger.info(f"[{service_name}] 'request_more' ignored as service is currently disabled in passed config.")
            # If disabled, consider it a cancellation for this request
            if self.on_service_batch_cancelled: self.on_service_batch_cancelled(service_name)
            return

        if service_name not in self.retrievers or service_name not in self._service_data:
            logger.warning(f"[ServiceManager] 'request_more' called for unknown or incompletely initialized service: {service_name}")
            if self.on_service_error: self.on_service_error(service_name, "Service not fully initialized for 'request_more'.")
            # Signal batch error for this specific request
            if self.on_service_batch_error: self.on_service_batch_error(service_name, "Service not available for 'request_more'.")
            return
        
        service_state = self._service_data[service_name]
        if service_state.cancel_event.is_set(): 
            logger.info(f"[{service_name}] 'request_more' ignored as service cancel event is set.")
            # If already cancelled, emit the batch cancelled event
            if self.on_service_batch_cancelled: self.on_service_batch_cancelled(service_name)
            return

        retriever = self.retrievers[service_name]
        logger.info(f"[{service_name}] UI requested more images.")
        self.service_processing_executor.submit(
            _log_executor_task_exceptions,
            self._process_image_batch_for_service,
            service_name,
            retriever,
            batch_size if batch_size is not None and batch_size > 0 else DEFAULT_IMAGES_PER_BATCH, 
            self.current_search_front_only,
            service_state.cancel_event,
            self._shutdown_event,
            current_active_services_config
        )

    def _process_image_batch_for_service(self, service_name: str, retriever: AbstractImageRetriever, num_to_send: int,
                                         front_only_for_this_task: bool,
                                         cancel_event: threading.Event, shutdown_event: threading.Event,
                                         current_active_services_config: List[Tuple[str, bool]]):
        sent_this_batch = 0
        # _batch_done_cb_called_for_this_invocation flag is removed.
        # Each logical path (success, cancel, error) will call its specific callback once.

        def _check_cancelled(context_msg: str = "") -> bool:
            if shutdown_event.is_set():
                logger.info(f"[{service_name}] Batch processing cancelled (shutdown event): {context_msg}")
                return True
            if cancel_event.is_set():
                logger.info(f"[{service_name}] Batch processing cancelled (service event): {context_msg}")
                return True
            return False

        try:
            lock = self._service_locks[service_name]
            with lock:
                if _check_cancelled("at start of _process_image_batch_for_service (in lock)"):
                    if self.on_service_batch_cancelled:
                        self.on_service_batch_cancelled(service_name)
                    return
                
                state = self._service_data[service_name] 
                while sent_this_batch < num_to_send:
                    if _check_cancelled("in PI processing loop"): break
                    if not state.current_candidate_pis: 
                        if state.all_candidates_processed_for_listing: break 
                        if state.is_listing_for_active_candidate: 
                            if _check_cancelled("while waiting for PIs from active candidate"): break
                            lock.release()
                            try: time.sleep(0.1) 
                            finally: lock.acquire() 
                            if _check_cancelled("after reacquiring lock while waiting for PIs"): break
                            state = self._service_data[service_name] 
                            continue 
                        if not state.candidates_queue: 
                            state.all_candidates_processed_for_listing = True
                            logger.info(f"[{service_name}] All album candidates processed for listing.")
                            break
                        state.active_candidate = state.candidates_queue.popleft()
                        state.is_listing_for_active_candidate = True
                        active_cand_display = state.active_candidate.album_name if state.active_candidate else "N/A"
                        if _check_cancelled(f"before listing PIs for {active_cand_display}"): break
                        logger.info(f"[{service_name}] Listing PIs for: {active_cand_display}")
                        active_candidate_copy = copy.deepcopy(state.active_candidate) if state.active_candidate else None
                        lock.release()
                        new_pis = []
                        try:
                            if active_candidate_copy: 
                                new_pis_unfiltered = retriever.list_potential_images(active_candidate_copy, cancel_event)
                                if front_only_for_this_task:
                                    original_pi_count = len(new_pis_unfiltered)
                                    new_pis = [pi for pi in new_pis_unfiltered if pi.is_front]
                                    if new_pis or original_pi_count > 0: 
                                        logger.info(f"[{service_name}] Front covers filter (active: {front_only_for_this_task}): {original_pi_count} -> {len(new_pis)} PIs for '{active_cand_display}'.")
                                else: new_pis = new_pis_unfiltered
                        except Exception as e_list: 
                            if not _check_cancelled("in list_potential_images exception (outside lock)"):
                                logger.error(f"[{service_name}] Error listing PIs for {active_cand_display}: {e_list}", exc_info=True)
                                if self.on_service_error: self.on_service_error(service_name, f"Error listing for {active_cand_display[:30]}.")
                        finally: lock.acquire() 
                        if _check_cancelled(f"after listing PIs for {active_cand_display} (reacquired lock)"): break
                        state = self._service_data[service_name] 
                        if state.active_candidate and active_candidate_copy and \
                           state.active_candidate.identifier == active_candidate_copy.identifier:
                            # Retrievers are expected to set PotentialImage.source_candidate.
                            # Album/artist names are then available via potential_image.source_candidate.album_name.
                            # No direct augmentation of PotentialImage instances for these fields is needed here.
                            state.current_candidate_pis.extend(new_pis)
                            logger.info(f"[{service_name}] Found {len(new_pis)} PIs for {active_cand_display}.")
                        else:
                             logger.info(f"[{service_name}] Active candidate changed or cleared while listing PIs for '{active_cand_display}'. Discarding {len(new_pis)} PIs.")
                        if state.active_candidate and active_candidate_copy and \
                           state.active_candidate.identifier == active_candidate_copy.identifier:
                            state.is_listing_for_active_candidate = False
                        elif not state.active_candidate: state.is_listing_for_active_candidate = False 
                        
                        if not state.current_candidate_pis and not _check_cancelled("after attempting to get PIs for candidate"): 
                            logger.info(f"[{service_name}] No PIs from {active_cand_display}, trying next if available.")
                            if state.active_candidate and active_candidate_copy and \
                               state.active_candidate.identifier == active_candidate_copy.identifier: state.active_candidate = None 
                            continue 
                    if state.current_candidate_pis:
                        if _check_cancelled("before sending PI to resolve"): break
                        pi_to_send = state.current_candidate_pis.popleft()
                        if self.on_potential_image_found: self.on_potential_image_found(service_name, pi_to_send)
                        self.image_resolution_executor.submit(
                            _log_executor_task_exceptions, self._resolve_image_and_callback,
                            retriever, pi_to_send, cancel_event, shutdown_event)
                        sent_this_batch += 1
                    if not state.current_candidate_pis and state.active_candidate and not _check_cancelled("after processing a PI candidate"): 
                        logger.debug(f"[{service_name}] Exhausted PIs for candidate {state.active_candidate.album_name}")
                        state.active_candidate = None
                # End of while loop or broken out due to cancellation/exhaustion
                
                # Determine final state
                if _check_cancelled("at end of batch processing logic, before final callback"):
                    if self.on_service_batch_cancelled:
                        self.on_service_batch_cancelled(service_name)
                else:
                    # If not cancelled, determine 'has_more' based on actual remaining items
                    has_more_overall = bool(state.current_candidate_pis or state.candidates_queue or state.is_listing_for_active_candidate)
                    if not has_more_overall: 
                        logger.info(f"[{service_name}] Batch completed. No more images expected from this service in current search.")
                    else: 
                        logger.info(f"[{service_name}] Batch completed. Has more: {has_more_overall} (PIs: {len(state.current_candidate_pis)}, Candidates: {len(state.candidates_queue)}, Listing: {state.is_listing_for_active_candidate})")
                    
                    if self.on_service_batch_completed_successfully:
                        self.on_service_batch_completed_successfully(service_name, has_more_overall)
                # An explicit return is not strictly needed here if this is the end of the try block
                # and the except/finally below handle other exit paths.

        except Exception as e:
            # Check for cancellation *before* logging/reporting as a processing error,
            # as the error might be a consequence of cancellation (e.g., closed connections).
            if not _check_cancelled("in main batch processing exception handler"):
                logger.error(f"[{service_name}] Error in _process_image_batch_for_service: {e}", exc_info=True)
                error_msg = f"Batch processing error: {str(e)[:100]}"
                if self.on_service_batch_error:
                    self.on_service_batch_error(service_name, error_msg)
                # Optionally, also call the general service error if it's severe,
                # but EVT_SERVICE_BATCH_ERROR should cover this specific context.
                # if self.on_service_error: self.on_service_error(service_name, error_msg)
            else:
                # If it was cancelled leading to this exception path, report as cancelled.
                if self.on_service_batch_cancelled:
                    self.on_service_batch_cancelled(service_name)
        # The 'finally' block for a fallback callback is removed, as each path (try success, try cancel, except)
        # should now explicitly call its respective callback.


    def _resolve_image_and_callback(self, retriever: AbstractImageRetriever, potential_image: PotentialImage,
                                    cancel_event: threading.Event, shutdown_event: threading.Event):
        service_name = retriever.service_name
        def _check_cancelled(context_msg: str = "") -> bool:
            if shutdown_event.is_set():
                logger.info(f"[{service_name}] Image resolution cancelled (shutdown event): {context_msg} for {potential_image.full_image_url}")
                return True
            if cancel_event.is_set():
                logger.info(f"[{service_name}] Image resolution cancelled (service event): {context_msg} for {potential_image.full_image_url}")
                return True
            return False
        if _check_cancelled(f"before resolving"): return
        try:
            image_result = retriever.resolve_image_details(potential_image, cancel_event)
            if _check_cancelled(f"after resolving"): return
            if image_result:
                # ImageResult.album_name and ImageResult.artist_name are properties
                # that derive their values from image_result.source_candidate.
                # Retrievers (or ImageResult.from_potential_image) are responsible for ensuring
                # image_result.source_candidate is correctly populated from potential_image.source_candidate.
                # The following lines attempting to set properties or access non-existent fields
                # on potential_image are removed.
                # image_result.album_name = potential_image.source_candidate.album_name # This would be an attempt to set a property
                # image_result.artist_name = potential_image.source_candidate.artist_name # This would be an attempt to set a property

                if self.on_image_resolved: self.on_image_resolved(service_name, image_result)
                lock = self._service_locks[service_name]
                with lock: 
                    state = self._service_data[service_name]
                    if not state.cancel_event.is_set(): 
                        state.total_images_resolved_this_session +=1
            else:
                if not _check_cancelled("after failed resolve (no result)"):
                    logger.warning(f"[{service_name}] Failed to resolve image details for {potential_image.full_image_url} (no result).")
        except Exception as e:
            if not _check_cancelled("in resolve_image exception"):
                logger.error(f"[{service_name}] Error resolving image {potential_image.full_image_url}: {e}", exc_info=True)

    def cancel_current_search(self):
        logger.info("[ServiceManager] Cancellation of current search requested.")
        if self._shutdown_event.is_set():
            logger.info("[ServiceManager] Shutdown already in progress, cancel_current_search doing nothing further.")
            return

        active_services_were_signalled_to_cancel = 0
        for service_name, enabled in self._config_for_current_search: 
            if enabled and service_name in self._service_data:
                state = self._service_data[service_name]
                if not state.cancel_event.is_set(): 
                    logger.info(f"[{service_name}] Setting cancel_event due to user search cancellation request.")
                    state.cancel_event.set() 
                    active_services_were_signalled_to_cancel += 1
        
        if active_services_were_signalled_to_cancel == 0:
            logger.info("[ServiceManager] cancel_current_search called, but no active services (based on current search config) needed signalling, or they were already cancelled.")
            with self._global_search_count_lock:
                if self._active_search_services_count == 0 and self.on_all_service_searches_concluded and not self._shutdown_event.is_set():
                    logger.info("[ServiceManager] Triggering on_all_service_searches_concluded from cancel_current_search as search count is zero and no new services were signalled to cancel.")
                    self.on_all_service_searches_concluded()
                    
    def shutdown(self):
        logger.info("[ServiceManager] Initiating shutdown sequence.")
        self._shutdown_event.set() 
        for service_name, state in self._service_data.items(): 
            logger.info(f"[{service_name}] Setting cancel event due to global shutdown.")
            state.cancel_event.set()
        logger.info("[ServiceManager] Shutting down service_processing_executor.")
        self.service_processing_executor.shutdown(wait=True, cancel_futures=True) 
        logger.info("[ServiceManager] Shutting down image_resolution_executor.")
        self.image_resolution_executor.shutdown(wait=True, cancel_futures=True)
        logger.info("[ServiceManager] Executors shutdown complete.")