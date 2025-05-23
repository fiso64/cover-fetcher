# services/worker.py
import logging
import multiprocessing
import queue 
import sys
from dataclasses import dataclass, field
from typing import Any, Tuple, Optional, List, Dict, TYPE_CHECKING

from .models import PotentialImage, ImageResult

if TYPE_CHECKING: # slow imports
    from .service_manager import ServiceManager
    from retrievers.base_retriever import AbstractImageRetriever

logger = logging.getLogger(__name__)

# --- Command Payload Dataclasses ---
@dataclass
class CMD_Search:
    artist: str
    album: str
    front_only_setting: bool
    active_services_config: List[Tuple[str, bool]]
    batch_size: Optional[int] = None

@dataclass
class CMD_RequestMore:
    service_name: str
    active_services_config: List[Tuple[str, bool]]
    batch_size: Optional[int] = None

@dataclass
class CMD_CancelSearch: # Empty payload, acts as a signal
    pass

@dataclass
class CMD_Shutdown: # Empty payload, acts as a signal
    pass

# --- Event Payload Dataclasses ---
@dataclass
class EVT_ServiceAlbumSearchSucceeded:
    # This event indicates that the initial album candidate search for a service
    # has completed successfully (i.e., the search_album_candidates method returned
    # without an error). It always includes num_candidates, which can be zero if
    # no candidates were found. This event is sent before any potential images
    # are listed or resolved for those candidates.
    # Error cases during album search are handled by EVT_ServiceBatchErrored.
    service_name: str
    num_candidates: int

@dataclass
class EVT_PotentialImageFound:
    service_name: str
    potential_image: PotentialImage 

@dataclass
class EVT_ImageResolved:
    service_name: str
    image_result: ImageResult

@dataclass
class EVT_ServiceBatchSucceeded:
    service_name: str
    has_more: bool

@dataclass
class EVT_ServiceBatchCancelled:
    service_name: str

@dataclass
class EVT_ServiceBatchErrored: # For errors specific to batch processing within a service
    service_name: str
    error_message: str

@dataclass
class EVT_ServiceError: # For general service errors
    service_name: str 
    message: str

@dataclass
class EVT_WorkerReady: # Empty payload, acts as a signal
    pass

@dataclass
class EVT_AllSearchesConcluded: # Empty payload
    pass

@dataclass
class EVT_WorkerShutdownComplete: # Empty payload
    pass


def setup_worker_logging(log_level: int, log_file_path: Optional[str] = None):
    # Configure logging for the worker process based on parameters from main process
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear() # Clear any pre-existing handlers

    # Console Handler (always add to worker's stderr)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(log_level)
    root_logger.addHandler(console_handler)

    if log_file_path:
        try:
            # The main process should have ensured the directory exists.
            # The worker just attempts to open/append to the file.
            file_handler = logging.FileHandler(log_file_path, mode='a', encoding='utf-8')
            file_handler.setFormatter(log_formatter)
            file_handler.setLevel(log_level)
            root_logger.addHandler(file_handler)
        except Exception as e:
            # If file logging setup fails in worker, log an error to its console.
            # This uses the root_logger which now has at least a console handler.
            logging.error(f"[Worker] Error setting up log file '{log_file_path}': {e}. Logging to console only.", exc_info=True)
    
    # Get the logger for this specific module (services.worker) AFTER root configuration.
    # This allows this module's logger instance (defined at the top of the file) to pick up the new config.
    worker_module_logger = logging.getLogger(__name__) # Could also be logging.getLogger("services.worker")

    # Set specific log levels for noisy libraries within the worker process
    musicbrainzngs_logger_worker = logging.getLogger('musicbrainzngs')
    musicbrainzngs_logger_worker.setLevel(logging.WARNING)
    
    urllib_logger_worker = logging.getLogger('urllib3')
    urllib_logger_worker.setLevel(logging.WARNING)
    
    pil_logger_worker = logging.getLogger('PIL')
    pil_logger_worker.setLevel(logging.WARNING)
    
    # Use the now-configured logger for this module
    worker_module_logger.info(f"[Worker] Logging configured. Root level: {logging.getLevelName(log_level)}. File: '{log_file_path if log_file_path else 'None'}'. Library levels set to WARNING.")


class Worker:
    def __init__(self, command_queue: multiprocessing.Queue, event_queue: multiprocessing.Queue, initial_search_params: Optional[CMD_Search] = None): # Type hint was Tuple, should be CMD_Search
        self.command_queue = command_queue
        self.event_queue = event_queue
        self.service_manager: Optional[ServiceManager] = None
        self.initial_search_params = initial_search_params
        self._should_shutdown = False
        self.command_handlers = {
            CMD_Search: self._handle_search_command,
            CMD_CancelSearch: self._handle_cancel_search_command,
            CMD_RequestMore: self._handle_request_more_command,
            CMD_Shutdown: self._handle_shutdown_command,
        }
        # No session_config or current_services_config_list stored here anymore

    def _initialize_service_manager(self):
        from .service_manager import ServiceManager
        logger.info("[Worker] Initializing ServiceManager...")
        try:
            self.service_manager = ServiceManager()
            self.service_manager.set_callbacks(
                # prepared_cb is renamed to album_search_succeeded_cb
                album_search_succeeded_cb=lambda s_name, num_candidates: self.event_queue.put(EVT_ServiceAlbumSearchSucceeded(s_name, num_candidates)),
                potential_cb=lambda s_name, p_image: self.event_queue.put(EVT_PotentialImageFound(s_name, p_image)),
                resolved_cb=lambda s_name, i_result: self.event_queue.put(EVT_ImageResolved(s_name, i_result)),
                batch_completed_cb=lambda s_name, has_more: self.event_queue.put(EVT_ServiceBatchSucceeded(s_name, has_more)),
                batch_cancelled_cb=lambda s_name: self.event_queue.put(EVT_ServiceBatchCancelled(s_name)),
                batch_error_cb=lambda s_name, err_msg: self.event_queue.put(EVT_ServiceBatchErrored(s_name, err_msg)),
                all_done_cb=lambda: self.event_queue.put(EVT_AllSearchesConcluded()),
                error_cb=lambda s_name, msg: self.event_queue.put(EVT_ServiceError(s_name, msg))
            )
            logger.info("[Worker] ServiceManager initialized and callbacks set.")
            # Send EVT_WorkerReady to GUI
            self.event_queue.put(EVT_WorkerReady())
        except Exception as e:
            logger.error(f"[Worker] Failed to initialize ServiceManager: {e}", exc_info=True)
            self.event_queue.put(EVT_ServiceError("WorkerInitialization", f"Failed to init ServiceManager: {e}"))
            self._should_shutdown = True

    def run(self):
        # Logging is now set up by worker_process_main BEFORE Worker instance is created and run() is called.
        logger.info("[Worker] Worker process started.") # This logger will use the configuration set in worker_process_main
        self._initialize_service_manager()

        if self.initial_search_params and self.service_manager and not self._should_shutdown:
            # initial_search_params is now expected to be a CMD_Search object or None
            if isinstance(self.initial_search_params, CMD_Search):
                search_payload = self.initial_search_params
                logger.info(f"[Worker] Auto-starting search from CLI params: Artist='{search_payload.artist}', Album='{search_payload.album}', BatchSize: {search_payload.batch_size}")
                # Ensure active_services_config is List[Tuple[str, bool]]
                # This check might be redundant if the CLI parser already ensures this type for the dataclass field
                if search_payload.active_services_config and isinstance(search_payload.active_services_config, list) and \
                   search_payload.active_services_config[0] and isinstance(search_payload.active_services_config[0], list):
                    search_payload.active_services_config = [tuple(s) for s in search_payload.active_services_config]
                
                self.service_manager.start_album_art_search(
                    search_payload.artist,
                    search_payload.album,
                    search_payload.front_only_setting,
                    search_payload.active_services_config,
                    search_payload.batch_size
                )
            elif self.initial_search_params is not None: # It's not None, but not the expected type
                 logger.error(f"[Worker] Invalid initial_search_params type: {type(self.initial_search_params)}. Expected CMD_Search.")


        while not self._should_shutdown:
            try:
                command_obj = self.command_queue.get(timeout=0.1) # Now gets the command object directly
                command_class = type(command_obj)
                command_type_name = command_class.__name__
                logger.debug(f"[Worker] Received command object: {command_type_name} with data: {command_obj if len(str(command_obj)) < 200 else str(command_obj)[:200] + '...'}")

                if not self.service_manager and not isinstance(command_obj, CMD_Shutdown): # Keep this check for early exit if SM not ready
                    logger.error("[Worker] ServiceManager not initialized, cannot process non-shutdown command.")
                    continue

                handler = self.command_handlers.get(command_class)
                if handler:
                    handler(command_obj)
                else:
                    logger.warning(f"[Worker] Unknown command object type: {command_type_name}. No handler registered.")

            except queue.Empty:
                pass
            except (EOFError, BrokenPipeError) as e:
                logger.error(f"[Worker] IPC Pipe broken, shutting down: {e}")
                self._should_shutdown = True
                if self.service_manager:
                    self.service_manager.shutdown()
            except Exception as e:
                logger.error(f"[Worker] Error in command loop: {e}", exc_info=True)
                self.event_queue.put(EVT_ServiceError("WorkerLoop", f"Unhandled error: {e}"))

        logger.info("[Worker] Shutting down service manager if not already done.")
        if self.service_manager and hasattr(self.service_manager, '_shutdown_event') and not self.service_manager._shutdown_event.is_set():
             self.service_manager.shutdown()

        logger.info("[Worker] Worker process finishing.")
        self.event_queue.put(EVT_WorkerShutdownComplete())

    def _handle_search_command(self, command_obj: CMD_Search):
        logger.info(f"[Worker] Handling SearchCommand for Artist: '{command_obj.artist}', Album: '{command_obj.album}', FrontOnly: {command_obj.front_only_setting}, BatchSize: {command_obj.batch_size}")
        if self.service_manager:
            self.service_manager.start_album_art_search(
                command_obj.artist,
                command_obj.album,
                command_obj.front_only_setting,
                command_obj.active_services_config,
                command_obj.batch_size
            )
        else:
            logger.error("[Worker] ServiceManager not available to handle SearchCommand.")

    def _handle_cancel_search_command(self, command_obj: CMD_CancelSearch):
        logger.info("[Worker] Handling CancelSearchCommand.")
        if self.service_manager:
            self.service_manager.cancel_current_search()
        else:
            logger.error("[Worker] ServiceManager not available to handle CancelSearchCommand.")

    def _handle_request_more_command(self, command_obj: CMD_RequestMore):
        logger.info(f"[Worker] Handling RequestMoreCommand for Service: '{command_obj.service_name}', BatchSize: {command_obj.batch_size}")
        if self.service_manager:
            self.service_manager.request_more_for_service(
                command_obj.service_name,
                command_obj.active_services_config,
                command_obj.batch_size
            )
        else:
            logger.error("[Worker] ServiceManager not available to handle RequestMoreCommand.")

    def _handle_shutdown_command(self, command_obj: CMD_Shutdown):
        logger.info("[Worker] Handling ShutdownCommand.")
        self._should_shutdown = True
        if self.service_manager:
            self.service_manager.shutdown()


def worker_process_main(
    command_queue: multiprocessing.Queue,
    event_queue: multiprocessing.Queue,
    initial_search_params: Optional[CMD_Search] = None,
    # New parameters for logging, passed from the main process
    log_level_from_main: int = logging.DEBUG,  # Default, but should be overridden
    log_file_path_from_main: Optional[str] = None
):
    # Setup logging for this worker process using parameters from the main process
    # This is the VERY FIRST thing to do in the new process.
    setup_worker_logging(log_level=log_level_from_main, log_file_path=log_file_path_from_main)
    
    # Now initialize and run the worker
    worker = Worker(command_queue, event_queue, initial_search_params)
    worker.run()
