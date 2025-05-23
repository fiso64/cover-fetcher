# cli.py
import argparse
import copy # For deepcopy
import logging
import pathlib
import sys
import traceback # For formatting exceptions, if any part of CLI needs detailed error logging
import tempfile # For saving embedded art
import os # For working with temporary file paths
from typing import Optional, Tuple, Any, TYPE_CHECKING, List, Dict

# Conditional import for type hinting CMD_Search, and actual import later
if TYPE_CHECKING:
    from services.worker import CMD_Search # Assuming CMD_Search is in services.worker

logger = logging.getLogger(__name__)

# Single source of truth for CLI argument definitions
# Each entry is a tuple: (list_of_flags_or_name, options_dictionary)
# 'dest' is only specified in options_dictionary if it differs from what argparse infers.
ARG_DEFINITIONS: List[Tuple[List[str], Dict[str, Any]]] = [
    (["-r", "--artist"],        {"type": str, "help": "Start a search with album artist"}),
    (["-a", "--album"],         {"type": str, "help": "Start a search with album title (required if --artist is provided)"}),
    (["query"],                 {"nargs": '?', "type": str, "help": "Album name, 'Artist - Album' string, or path to a music file (acts like --from-file if path)."}),
    (["--front-only"],          {"action": "store_true", "help": "Only search for front cover images"}),
    (["--no-front-only"],       {"action": "store_true", "help": "Search for all image types (disable front-only mode)"}),
    (["--services"],            {"type": str, "help": "Comma-separated list of services to enable (e.g. 'bandcamp,last.fm')"}),
    (["-o", "--output-dir"],    {"type": str, "help": "Set default output directory for saving images"}),
    (["-f", "--filename"],      {"type": str, "help": "Set default filename (without extension) for saved images"}),
    (["-y", "--no-save-prompt"],{"action": "store_true", "help": "Save images directly to output dir without showing file dialog"}),
    (["--exit-on-download"],    {"action": "store_true", "help": "Exit application after successfully downloading an image"}),
    (["-i", "--from-file"],     {
                                    "type": str,
                                    "help": "Extracts information from a music file. "
                                            "Artist/Album: Populated from metadata if --artist/--album are not specified. If --artist=\"\" or --album=\"\" is given, "
                                            "those will be used (effectively disabling metadata extraction for that field). An album name is still required for a search. "
                                            "Output Directory: Set to the file's parent if --output-dir is not specified. "
                                            "Min Width/Height: If a local cover is found and --min-width/--min-height are not explicitly set, "
                                            "they will be derived from the existing art's dimensions, aiming to find a strictly larger image. "
                                            "Explicit CLI arguments (e.g., --artist \"\", --output-dir /p, --min-width 0) always take precedence."
                                }),
    (["--batch-size"],          {"type": int, "help": "Number of potential images to fetch and process per service in each batch (e.g., 5)"}),
    (["--min-width"],           {"type": int, "help": "Minimum width for downloaded images (pixels)"}),
    (["--min-height"],          {"type": int, "help": "Minimum height for downloaded images (pixels)"}),
    (["--existing-art-path"],   {"type": str, "help": "Path to an existing album art image to display initially."}),
    (["--log-file"],            {"type": str, "dest": "log_file", "help": "Path to a file for logging output."}),
]

class ArgumentParserError(Exception):
    """Custom exception for parsing errors when GUI is active."""
    pass

class ArgumentParserHelpRequested(Exception):
    """Custom exception for when help is requested and GUI is active."""
    def __init__(self, arg_definitions: List[Tuple[List[str], Dict[str, Any]]]):
        super().__init__("Help requested")
        self.arg_definitions = arg_definitions

class CustomArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, is_console_mode: bool = False, **kwargs):
        self.is_console_mode = is_console_mode
        # The 'add_help' argument is expected to be in kwargs,
        # passed from the instantiation site in _parse_arguments.
        # Its value will be `is_console_mode`.
        super().__init__(*args, **kwargs)


    def error(self, message: str):
        if self.is_console_mode:
            # Replicates default behavior more closely for console
            self.print_usage(sys.stderr)
            args = {'prog': self.prog, 'message': message}
            self.exit(2, '%(prog)s: error: %(message)s\n' % args)
        else:
            raise ArgumentParserError(message)

    # Override _print_message to suppress output if not in console mode for help text
    def _print_message(self, message: str, file = None):
        if self.is_console_mode:
            super()._print_message(message, file)
        # else: suppress message printing for GUI mode, it will be in dialog

    def exit(self, status: int = 0, message: Optional[str] = None):
        if message:
            self._print_message(message, sys.stderr) # prints if console_mode
        
        if self.is_console_mode:
            sys.exit(status)
        else:
            # In GUI mode, argparse calls exit() after print_help() for -h or --help.
            # Since we've (conditionally) suppressed print_help via _print_message,
            # and we pre-check for -h/--help in _parse_arguments,
            # this path signals that help was requested and successfully processed by argparse.
            # We re-raise our custom help exception here to be caught by _parse_arguments.
            # This ensures flow control passes back correctly.
            # This will only be hit if our pre-check didn't catch -h/--help for some reason
            # AND add_help was True in constructor (which it is not anymore).
            # For safety, if this is ever reached in GUI mode, assume help or unhandled exit.
            # However, with add_help=False and pre-checking, this shouldn't be hit by -h/--help.
            # It might be hit by other actions like --version if we added it.
            # For now, this acts as a fallback.
            # If we are here, it's an exit not due to parser.error()
            if status == 0: # Potentially help/version
                 raise ArgumentParserHelpRequested(ARG_DEFINITIONS)
            else: # Potentially an error that bypassed .error()
                 raise ArgumentParserError(message or "Argument parsing caused an exit.")


def _parse_arguments(is_console_mode: bool) -> Tuple[argparse.Namespace, argparse.ArgumentParser]:
    """
    Parses command-line arguments using CustomArgumentParser.
    Raises ArgumentParserError for parsing errors in GUI mode.
    Raises ArgumentParserHelpRequested for help requests in GUI mode.
    """
    # Explicitly check for help arguments before initializing the full parser
    # This allows us to trigger our custom help dialog without argparse intervening too much.
    if not is_console_mode and ('-h' in sys.argv or '--help' in sys.argv):
        raise ArgumentParserHelpRequested(ARG_DEFINITIONS)

    parser = CustomArgumentParser(
        description="Cover Fetcher",
        is_console_mode=is_console_mode,
        # add_help is True if console_mode, so std help prints. False if GUI, so we can show dialog.
        add_help=is_console_mode 
    )

    for flags_or_name, options_dict in ARG_DEFINITIONS:
        parser.add_argument(*flags_or_name, **options_dict)

    try:
        args = parser.parse_args()
    except ArgumentParserError: # Already handled by CustomArgumentParser.error
        raise
    except ArgumentParserHelpRequested: # Already handled by CustomArgumentParser.exit (less likely now)
        raise
    except Exception as e: # Catch other potential argparse issues
        if not is_console_mode:
            raise ArgumentParserError(f"Failed to parse arguments: {e}")
        else:
            # Let default argparse error handling take over or re-raise
            # For console mode, parser.error would have exited, so this is unusual
            # Re-raise to ensure console sees it if it's not an SystemExit
            if not isinstance(e, SystemExit): 
                parser.error(f"Unexpected parsing error: {e}") # This will use CustomAP.error
            raise # If SystemExit, let it propagate


    # --- Argument Validation / Post-processing not directly tied to config modification or from_file ---
    
    if args.query:
        # Check if the positional query argument is an existing file path.
        # This check must happen before treating 'query' as an "Artist - Album" string.
        try:
            # Expand user and check if it's a file.
            # We don't resolve() here yet, as is_file() should work on relative/absolute paths.
            # resolve() will be used when setting args.from_file to store a canonical path.
            potential_file_path = pathlib.Path(args.query).expanduser()
            if potential_file_path.is_file():
                if args.from_file is not None:
                    # Conflict: positional query is a file AND --from-file is also explicitly set.
                    parser.error("Cannot use a file path as a positional query when --from-file is also specified.")
                else:
                    # Positional query is a file, and --from-file was not otherwise specified.
                    # Treat the query as the source for --from-file.
                    logger.info(f"Positional query argument '{args.query}' is an existing file. Processing as --from-file.")
                    args.from_file = str(potential_file_path.resolve()) # Store the absolute, resolved path
                    args.query = None  # Clear query to prevent it from being parsed as "Artist - Album".
        except OSError as e:
            # This might happen for exceptionally long or malformed path strings, though unlikely for typical argv.
            logger.debug(f"Could not evaluate positional query '{args.query}' as a potential file path due to OSError: {e}. Will proceed to treat as string query.")
            # Let it fall through; if it's not a valid file path or causes OS error, it will be treated as a regular string query.
        except Exception as e: # Catch any other unexpected error during path processing
            logger.warning(f"Unexpected error while checking if query '{args.query}' is a file path: {e}. Will proceed to treat as string query.")


    # These parser.error() calls will now go through CustomArgumentParser.error()
    # This 'if args.query:' will now only be true if:
    # 1. The query was not a file.
    # 2. The query was a file, but --from-file was also specified (which would have errored above).
    # 3. An unexpected error occurred while checking if the query was a file.
    if args.query:
        if args.artist is not None or args.album is not None:
            parser.error("Cannot use 'query' argument with --artist or --album.")
        if " - " in args.query:
            artist, album = args.query.split(" - ", 1)
            args.artist = artist.strip()
            args.album = album.strip()
        else:
            args.album = args.query.strip()

    if args.artist and not args.album: # Album might be "" if explicitly passed, which is different from None
        if args.album is None: # If --artist is given, --album must also be given (even if empty)
            parser.error("--album is required when --artist is provided.")

    if args.front_only and args.no_front_only:
        parser.error("Cannot specify both --front-only and --no-front-only")
    
    return args, parser # Parser instance might still be useful for _apply_general_cli_overrides error reporting


def _apply_general_cli_overrides(
    args: argparse.Namespace, 
    initial_ui_config: dict, 
    default_config_base: dict, 
    parser: argparse.ArgumentParser
) -> None:
    """Applies general CLI arguments (not --from-file specific setup) to initial_ui_config."""

    if args.existing_art_path: # This path might come from --from-file or direct --existing-art-path
        art_path = pathlib.Path(args.existing_art_path).expanduser()
        if art_path.is_file():
            initial_ui_config["current_album_art_path"] = str(art_path.resolve())
        else:
            logger.warning(f"Specified existing art path is not a file or does not exist: {art_path}")
            # Only error out if --existing-art-path was EXPLICITLY provided and is invalid.
            if any(arg_part.startswith('--existing-art-path') for arg_part in sys.argv):
                 parser.error(f"Explicitly provided --existing-art-path '{art_path}' is not a valid file.")

    if args.front_only: # args.no_front_only already handled by parser mutual exclusivity in _parse_arguments
        initial_ui_config["front_only"] = True
    elif args.no_front_only:
        initial_ui_config["front_only"] = False

    if args.output_dir: # This dir might come from --from-file or direct --output-dir
        output_dir_path = pathlib.Path(args.output_dir).expanduser()
        # Allow if it's a dir OR if it doesn't exist yet (will be created on save)
        if output_dir_path.is_dir() or not output_dir_path.exists():
            initial_ui_config["default_output_dir"] = str(output_dir_path)
        else: # Exists but is not a directory (e.g., it's a file)
            logger.error(f"Invalid output directory specified (exists but is not a directory): {output_dir_path}")
            parser.error(f"Output directory '{output_dir_path}' exists and is not a directory.")

    if args.filename:
        clean_filename = args.filename.strip()
        if clean_filename:
            initial_ui_config["default_filename"] = clean_filename
        else:
            parser.error("Empty filename specified via --filename.")

    if args.no_save_prompt:
        initial_ui_config["no_save_prompt"] = True

    if args.exit_on_download:
        initial_ui_config["exit_on_download"] = True

    if args.services:
        cli_service_names_input_lower_set = {name.strip().lower() for name in args.services.split(',') if name.strip()}
        
        # Helper to check if a service list from config is valid and extract canonical names
        def _get_valid_base_service_config(cfg_list) -> List[Tuple[str, bool]]:
            valid_config = []
            if not isinstance(cfg_list, list): return []
            for s_entry in cfg_list:
                if (isinstance(s_entry, (list, tuple)) and len(s_entry) == 2 and
                        isinstance(s_entry[0], str) and isinstance(s_entry[1], bool)):
                    valid_config.append((s_entry[0], s_entry[1])) # (CanonicalName, OriginalEnabledState)
                else:
                    logger.warning(f"Malformed service entry in base config: {s_entry}. Skipping.")
            return valid_config

        # Determine the base service configuration and order.
        # Priority: initial_ui_config (user's saved order) -> default_config_base.
        base_service_config = _get_valid_base_service_config(initial_ui_config.get("services"))
        if not base_service_config:
            logger.warning("User 'services' config malformed/missing. Falling back to default for CLI --services processing.")
            base_service_config = _get_valid_base_service_config(default_config_base.get("services", []))
            if not base_service_config:
                logger.error("Default 'services' config also malformed or empty. Cannot process --services.")
                # base_service_config remains empty, loop below will produce empty list.

        # Validate that all CLI-provided service names are known (exist in base_service_config)
        known_canonical_names_lower_set = {canonical_name.lower() for canonical_name, _ in base_service_config}
        for cli_name_lower in cli_service_names_input_lower_set:
            if cli_name_lower not in known_canonical_names_lower_set:
                available_names_str = ', '.join(sorted([name for name, _ in base_service_config]))
                parser.error(f"Service '{cli_name_lower}' is not a recognized service. Choose from: {available_names_str or 'None available'}")
        
        # Build the new services list, preserving order from base_service_config
        # Services mentioned in CLI are enabled, others are disabled.
        final_services_list_of_lists = []
        for canonical_name, _original_enabled_state in base_service_config:
            is_enabled_by_cli = canonical_name.lower() in cli_service_names_input_lower_set
            final_services_list_of_lists.append([canonical_name, is_enabled_by_cli])
        
        initial_ui_config["services"] = final_services_list_of_lists

    if args.batch_size is not None:
        if args.batch_size < 1: parser.error("--batch-size must be a positive integer.")
        initial_ui_config["batch_size"] = args.batch_size

    # min_width/min_height might be set by --from-file or directly by CLI.
    # _handle_from_file_logic already modified args.min_width/args.min_height if needed.
    if args.min_width is not None:
        if args.min_width < 0: parser.error("--min-width must be a non-negative integer.")
        initial_ui_config["min_width"] = args.min_width

    if args.min_height is not None:
        if args.min_height < 0: parser.error("--min-height must be a non-negative integer.")
        initial_ui_config["min_height"] = args.min_height


def _handle_from_file_logic(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """
    Handles all logic related to the --from-file argument.
    Modifies `args` in-place with extracted metadata, output dir, and existing art info.
    """
    if not args.from_file:
        return

    file_path_obj = pathlib.Path(args.from_file).expanduser()
    if not file_path_obj.exists():
        parser.error(f"File not found: {file_path_obj}")

    # --- Metadata Extraction (taglib) ---
    try:
        import taglib # Import only when needed
        with taglib.File(str(file_path_obj)) as audio_file:
            artist_tags = audio_file.tags.get("ALBUMARTIST", audio_file.tags.get("ARTIST", [""]))
            album_tags = audio_file.tags.get("ALBUM", [""])
            
            artist_from_file = artist_tags[0] if artist_tags else ""
            album_from_file = album_tags[0] if album_tags else ""

            if args.artist is None: # Only set from file if not provided via CLI
                args.artist = artist_from_file
            if args.album is None: # Only set from file if not provided via CLI
                args.album = album_from_file
            
            logger.info(f"After taglib processing for {file_path_obj}: Artist='{args.artist}', Album='{args.album}'")
            
            if args.output_dir is None: # Only set from file if not provided via CLI
                args.output_dir = str(file_path_obj.parent)
                logger.info(f"Set output directory from file to: {args.output_dir}")

    except ImportError:
        logger.warning("python-taglib library not found. Cannot extract metadata from music file. To enable this, 'pip install python-taglib'.")
    except taglib.TaglibError as e:
        logger.error(f"Error reading metadata from {file_path_obj} (taglib): {e}")
        parser.error(f"Could not read metadata from file {file_path_obj} (taglib error).")
    except Exception as e: # Catch other potential errors during file processing
        logger.error(f"Error processing file metadata for {file_path_obj}: {e}\n{traceback.format_exc()}")
        parser.error(f"Failed to process file metadata for {file_path_obj}.")

    # After potential metadata extraction (even if it failed but didn't exit),
    # ensure album (which is mandatory for search) is present if artist is.
    # If --from-file was used, and album is still None or empty, it's an issue.
    if args.album == "" or args.album is None: # Checking explicitly for empty string too
        logger.error(f"Album is mandatory for a search. It was not found in metadata of '{file_path_obj}' or not acceptably provided via --album argument in conjunction with --from-file.")
        parser.error(f"Album is mandatory. No valid album name derived from file '{file_path_obj}' or --album argument.")


    # --- Attempt to find existing cover art (external or embedded) for --from-file ---
    if args.existing_art_path is None: # Only try if --existing-art-path wasn't explicitly given
        music_file_parent_dir = file_path_obj.parent
        found_art_path_str = None
        
        # 1. Search for common external art files
        pattern_bases = ["cover", "folder", "album", "front"]
        image_extensions = [".jpg", ".jpeg", ".png"]
        try:
            items_in_dir = list(music_file_parent_dir.iterdir())
            for base_name in pattern_bases:
                if found_art_path_str: break
                for ext in image_extensions:
                    target_filename_lower = (base_name + ext).lower()
                    for item in items_in_dir:
                        if item.is_file() and item.name.lower() == target_filename_lower:
                            found_art_path_str = str(item)
                            logger.info(f"Found existing art (pattern match): {found_art_path_str}")
                            break 
                    if found_art_path_str: break
            
            if not found_art_path_str: # Fallback: first image file
                for item in items_in_dir:
                    if item.is_file() and item.suffix.lower() in image_extensions:
                        found_art_path_str = str(item)
                        logger.info(f"Found first available image file as existing art: {found_art_path_str}")
                        break
        except OSError as e:
            logger.warning(f"Could not list directory {music_file_parent_dir} to find external art: {e}")


        # 2. If no external art found, try to extract embedded art
        if not found_art_path_str:
            logger.info(f"No external art file found in {music_file_parent_dir}. Attempting to extract embedded art from {file_path_obj}.")
            best_picture_data = None
            best_picture_ext = ".jpg"
            try:
                import mutagen
                m_file = mutagen.File(str(file_path_obj))
                if m_file:
                    pictures_to_check = []
                    if m_file.pictures: pictures_to_check.extend(m_file.pictures) # FLAC, Ogg
                    if hasattr(m_file, 'tags'): # ID3, MP4
                        if isinstance(m_file.tags, mutagen.id3.ID3): pictures_to_check.extend(m_file.tags.getall('APIC'))
                        elif isinstance(m_file.tags, mutagen.mp4.MP4Tags):
                            covr_tags = m_file.tags.get('covr'); 
                            if covr_tags: pictures_to_check.extend(covr_tags)
                    
                    front_cover_data, front_cover_ext, any_cover_data, any_cover_ext = None, None, None, None
                    for pic in pictures_to_check:
                        pic_data, pic_ext_current, pic_type = None, None, getattr(pic, 'type', 0) # type 3 is front cover
                        
                        if hasattr(pic, 'data'): # mutagen.flac.Picture, mutagen.id3.APIC
                            pic_data = pic.data
                            mime_type = getattr(pic, 'mime', '').lower()
                            if 'jpeg' in mime_type or 'jpg' in mime_type: pic_ext_current = ".jpg"
                            elif 'png' in mime_type: pic_ext_current = ".png"
                        elif isinstance(pic, mutagen.mp4.MP4Cover): # mutagen.mp4.MP4Cover
                            pic_data = bytes(pic) # Data is the object itself
                            if pic.imageformat == mutagen.mp4.MP4Cover.FORMAT_JPEG: pic_ext_current = ".jpg"
                            elif pic.imageformat == mutagen.mp4.MP4Cover.FORMAT_PNG: pic_ext_current = ".png"
                        
                        if not pic_data or not pic_ext_current: continue

                        if pic_type == 3: front_cover_data, front_cover_ext = pic_data, pic_ext_current; break
                        if any_cover_data is None: any_cover_data, any_cover_ext = pic_data, pic_ext_current
                    
                    if front_cover_data: best_picture_data, best_picture_ext = front_cover_data, front_cover_ext
                    elif any_cover_data: best_picture_data, best_picture_ext = any_cover_data, any_cover_ext

            except ImportError: logger.warning("Mutagen library not found. Cannot extract embedded album art. To enable this, 'pip install mutagen'.")
            except Exception as e: logger.error(f"Error extracting embedded art using Mutagen from {file_path_obj}: {e}\n{traceback.format_exc()}")

            if best_picture_data:
                try:
                    fd, temp_image_path = tempfile.mkstemp(suffix=best_picture_ext, prefix="aad_embedded_")
                    with os.fdopen(fd, 'wb') as tmp_file: tmp_file.write(best_picture_data)
                    found_art_path_str = temp_image_path
                    logger.info(f"Successfully extracted embedded art to temporary file: {found_art_path_str}")
                except Exception as e:
                    logger.error(f"Failed to save extracted embedded art to temporary file: {e}\n{traceback.format_exc()}")
                    if 'temp_image_path' in locals() and os.path.exists(temp_image_path):
                        try: os.remove(temp_image_path)
                        except OSError: pass
                    found_art_path_str = None
        
        # 3. If art was found (external or embedded), set args.existing_art_path and try to get dimensions
        if found_art_path_str:
            args.existing_art_path = found_art_path_str
            if args.min_width is None or args.min_height is None: # Only if not explicitly set by CLI
                try:
                    from PIL import Image # Import only when needed
                    with Image.open(found_art_path_str) as img:
                        img_width, img_height = img.size
                        
                        # Store whether CLI set these, to correctly apply +1 logic
                        cli_set_min_width = args.min_width is not None
                        cli_set_min_height = args.min_height is not None
                        
                        derived_w, derived_h = None, None

                        if not cli_set_min_width:
                            derived_w = img_width
                            args.min_width = img_width # Temporarily assign for logic below
                        if not cli_set_min_height:
                            derived_h = img_height
                            args.min_height = img_height # Temporarily assign

                        # Apply +1 logic: if width was derived, increment it.
                        # If width was CLI and height derived, increment height.
                        if derived_w is not None: # Width was derived
                            args.min_width = derived_w + 1
                            logger.info(f"Set min_width to {args.min_width} (derived from existing art {derived_w}px + 1) as --min-width was not specified.")
                            if derived_h is not None: # Height also derived, no +1 needed as width already covers "strictly better"
                                logger.info(f"Set min_height to {args.min_height} (derived from existing art {derived_h}px) as --min-height was not specified and width was incremented.")
                        elif derived_h is not None: # Width was CLI-set, Height was derived
                            args.min_height = derived_h + 1
                            logger.info(f"Set min_height to {args.min_height} (derived from existing art {derived_h}px + 1) as --min-height was not specified (and --min-width was CLI-provided).")
                except ImportError: logger.warning("Pillow (PIL) library not found. Cannot extract dimensions from existing art. 'pip install Pillow'.")
                except Exception as e: logger.warning(f"Could not read dimensions from existing art {found_art_path_str}: {e}")


def _prepare_auto_search_payload(
    args: argparse.Namespace, 
    initial_ui_config: dict, 
    default_config_base: dict
) -> Optional["CMD_Search"]:
    """Prepares the CMD_Search payload if an auto-search is indicated by args (i.e., album is present)."""
    if not args.album: # Album (even if empty string from CLI) is the primary trigger
        return None

    # This import is late because CMD_Search might have its own dependencies
    # that are not strictly needed if no auto-search is performed.
    from services.worker import CMD_Search 

    # Use current UI config values for the payload, falling back to defaults if not set.
    services_cfg = initial_ui_config.get("services", default_config_base.get("services", []))
    # Ensure services_cfg has the right format (list of tuples) for CMD_Search
    if services_cfg and isinstance(services_cfg[0], list):
        services_cfg_tuples = [tuple(s) for s in services_cfg]
    else:
        services_cfg_tuples = services_cfg # Assume it's already list of tuples or empty

    batch_size = initial_ui_config.get("batch_size", default_config_base.get("batch_size", 5)) # Sensible default
    front_only = initial_ui_config.get("front_only", default_config_base.get("front_only", True)) # Sensible default

    payload = CMD_Search(
        artist=args.artist if args.artist else "", # CMD_Search expects non-None artist
        album=args.album, # Known to be non-None if we are in this function
        front_only_setting=front_only,
        active_services_config=services_cfg_tuples,
        batch_size=batch_size
    )
    logger.info(f"CLI auto-search payload prepared: Artist='{payload.artist}', Album='{payload.album}'")
    return payload


def process_cli_arguments(
    user_config_base: dict,
    default_config_base: dict,
    is_console_mode: bool
) -> Tuple[Optional[dict], bool, Optional["CMD_Search"], Optional[str], Optional[List[Tuple[List[str], Dict[str, Any]]]]]:
    """
    Parses CLI arguments, applies them to a copy of user_config_base,
    and determines if an initial search should be performed.

    Catches ArgumentParserError and ArgumentParserHelpRequested from _parse_arguments.

    Returns:
        A tuple containing:
        - initial_ui_config (Optional[dict]): Config with CLI overrides, or None on CLI error/help.
        - perform_auto_search (bool): True if an auto-search should be launched.
        - initial_search_payload (Optional["CMD_Search"]): Payload for auto-search, or None.
        - cli_error_message (Optional[str]): Error message if parsing failed (for GUI dialog).
        - help_arg_definitions (Optional[List[Dict]]): Arg definitions for custom help dialog, if help requested.
    """
    try:
        args, parser = _parse_arguments(is_console_mode=is_console_mode)
    except ArgumentParserError as e:
        # In console mode, CustomArgumentParser.error would have already exited.
        # This catch is primarily for GUI mode.
        logger.error(f"CLI Argument Parsing Error: {e}")
        return None, False, None, str(e), None
    except ArgumentParserHelpRequested as e:
        # In console mode, help is printed and exited by CustomArgumentParser.
        # This catch is for GUI mode to show the custom help dialog.
        logger.info("CLI Help Requested (GUI mode).")
        return None, False, None, None, e.arg_definitions
    
    # --- At this point, argument parsing was successful ---

    # Handle --from-file logic (modifies args in-place with extracted data)
    _handle_from_file_logic(args, parser) # parser is passed for its .error() method

    # Prepare initial UI configuration by deep copying the user's base configuration
    initial_ui_config = copy.deepcopy(user_config_base)

    # Apply general CLI overrides to the initial_ui_config, using default_config_base for reference
    # parser is passed for its .error() method, which now uses CustomArgumentParser's logic
    _apply_general_cli_overrides(args, initial_ui_config, default_config_base, parser)

    # Determine if an auto-search should be performed based on the presence of album info
    perform_auto_search = bool(args.album) # True if args.album is not None and not an empty string
    
    initial_search_payload = None
    if perform_auto_search:
        initial_search_payload = _prepare_auto_search_payload(args, initial_ui_config, default_config_base)
        if initial_search_payload is None: # Should not happen if perform_auto_search is True
            perform_auto_search = False 
            logger.error("Auto-search was indicated but payload preparation failed.")
        
    return initial_ui_config, perform_auto_search, initial_search_payload, None, None
