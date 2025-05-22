# utils/helpers.py
import json
import pathlib
import os
import logging
from dataclasses import dataclass, field
from typing import Optional, Any, Dict, List, Tuple # Added List
from enum import Enum # Added Enum
from utils.helpers import get_bundle_dir

logger = logging.getLogger(__name__)

# --- Global variables to store loading errors from initial load ---
_APP_CONFIG_LOAD_ERROR_MSG: Optional[str] = None
_USER_CONFIG_LOAD_ERROR_MSG: Optional[str] = None


# NEW: Definitions for configuration items that will appear in a settings UI
class ConfigCategory(Enum):
    BEHAVIOR = "Behavior"
    APPEARANCE = "Appearance"

class ConfigUIType(Enum):
    BOOL = "bool"
    PATH_STRING = "path_string" # For LineEdit that expects a path
    STRING = "string"         # For general LineEdit
    CHOICE = "choice"         # For ComboBox/Dropdown
    NUMBER = "number"         # For QSpinBox or similar numerical input

@dataclass
class ConfigChoice:
    value: str
    display: str # User-facing text for the choice

@dataclass
class ConfigItem:
    key: str                  # Internal key used in config dictionaries
    label: str                # User-facing label for the setting in UI
    ui_type: ConfigUIType     # Determines the type of UI widget
    category: ConfigCategory  # NEW: Category for grouping in UI
    default: Any              # Default value for this setting
    show_in_settings_ui: bool = True # New field: controls visibility in settings UI
    tooltip: Optional[str] = None # Optional tooltip for UI
    choices: Optional[List[ConfigChoice]] = None # For CHOICE type
    placeholder: Optional[str] = None          # For STRING types, placeholder text
    min_val: Optional[int] = None              # For NUMBER type, minimum value
    max_val: Optional[int] = None              # For NUMBER type, maximum value

# Central definition for user-configurable settings (excluding 'services')
CONFIG_ITEM_DEFINITIONS: List[ConfigItem] = [
    ConfigItem(
        key="front_only",
        label="Front Covers Only",
        ui_type=ConfigUIType.BOOL,
        category=ConfigCategory.BEHAVIOR,
        default=True,
        show_in_settings_ui=False, # This item will not be shown in the settings dialog
        tooltip="If checked, only search for front cover art. Uncheck to find other types like back, inlay, etc."
    ),
    ConfigItem(
        key="default_output_dir",
        label="Default Save Directory",
        ui_type=ConfigUIType.PATH_STRING,
        category=ConfigCategory.BEHAVIOR,
        default="~/Downloads",
        tooltip="The default folder where images will be saved. Use '~' for your home directory."
    ),
    ConfigItem(
        key="default_filename",
        label="Default Filename",
        ui_type=ConfigUIType.STRING,
        category=ConfigCategory.BEHAVIOR,
        default=None, # MainWindow logic handles None: uses "album_art" or "{artist} - {album}"
        placeholder="e.g. Cover",
        tooltip="Default filename without extension. Leave blank for default behavior (artist-album)."
    ),
    ConfigItem(
        key="no_save_prompt",
        label="Skip 'Save As' Dialog",
        ui_type=ConfigUIType.BOOL,
        category=ConfigCategory.BEHAVIOR,
        default=False,
        tooltip="If checked, images will be saved directly to the default save directory without showing the 'Save As...' dialog."
    ),
    ConfigItem(
        key="exit_on_download",
        label="Exit After Successful Download",
        ui_type=ConfigUIType.BOOL,
        category=ConfigCategory.BEHAVIOR,
        default=False,
        tooltip="If checked, the application will close automatically after an image is successfully downloaded."
    ),
     ConfigItem(
        key="batch_size",
        label="Image Search Batch Size",
        ui_type=ConfigUIType.NUMBER,
        category=ConfigCategory.BEHAVIOR,
        default=5,
        min_val=1,
        max_val=50,
        tooltip="Number of potential images to fetch and process per service in each batch."
    ),
    ConfigItem(
        key="theme",
        label="Application Theme",
        ui_type=ConfigUIType.CHOICE,
        category=ConfigCategory.APPEARANCE,
        default="auto",
        choices=[
            ConfigChoice(value="auto", display="Auto (System Default)"),
            ConfigChoice(value="light", display="Light"),
            ConfigChoice(value="dark", display="Dark"),
        ],
        tooltip="Select the visual theme for the application."
    ),
    ConfigItem(
        key="thumbnail_size",
        label="Thumbnail Display Size",
        ui_type=ConfigUIType.NUMBER,
        category=ConfigCategory.APPEARANCE,
        default=180,
        min_val=40,
        max_val=10000,
        tooltip="Max width/height for thumbnails in pixels."
    )
]
APP_CONFIG_FILE = get_bundle_dir() / "app_config.json"
USER_CONFIG_DIR = pathlib.Path.home() / ".config" / "cover_fetcher"
USER_CONFIG_FILE = USER_CONFIG_DIR / "config.json"

# TODO: Stop using globals
APP_CONFIG: Dict[str, Any] = {}
USER_CONFIG: Dict[str, Any] = {}

# Construct DEFAULT_CONFIG:
# Start with 'services' which is handled specially and not in CONFIG_ITEM_DEFINITIONS
_default_config_base = {
    "services": [
        ("iTunes", True),
        ("Last.fm", True),
        ("MusicBrainz", True),
        ("Bandcamp", True),
        ("Discogs", True),
        ("VGMdb", False),
    ]
}
# Populate the rest of the defaults from CONFIG_ITEM_DEFINITIONS
for item_def in CONFIG_ITEM_DEFINITIONS:
    _default_config_base[item_def.key] = item_def.default

DEFAULT_CONFIG: Dict[str, Any] = _default_config_base

DEFAULT_REQUESTS_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.82 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
}

def load_config(path: pathlib.Path, is_critical: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Loads configuration from a JSON file.
    Returns a tuple: (config_data, error_message).
    config_data is the loaded dictionary, {} if user file not found, or None if critical error / corruption.
    error_message contains the error description if any.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        logger.info(f"Successfully loaded config from {path}")
        return config_data, None
    except FileNotFoundError:
        if is_critical:
            logger.error(f"CRITICAL: Application config file {path} not found.")
            return None, f"Critical application config file not found: {path.name}. Some features may not work."
        else: # User config file not found is normal on first run or if deleted.
            logger.info(f"User config file {path} not found. Will use defaults or create a new one on save.")
            return {}, None # Return empty dict (no user overrides), no error message.
    except json.JSONDecodeError as e:
        logger.error(f"Could not decode config file {path}. Error: {e}.")
        
        base_corrupted_name_stem = path.stem + ".corrupted"
        corrupted_file_suffix = path.suffix # Usually .json
        
        corrupted_file_path = path.with_stem(base_corrupted_name_stem) # path.with_stem keeps suffix
                                                                      # e.g. config.json -> config.corrupted.json

        counter = 0
        while corrupted_file_path.exists():
            counter += 1
            # For config.json -> config.corrupted.1.json, config.corrupted.2.json etc.
            new_stem = f"{base_corrupted_name_stem}.{counter}"
            corrupted_file_path = path.with_stem(new_stem)

        try:
            if path.exists(): # Ensure file exists before trying to rename
                path.rename(corrupted_file_path)
                logger.info(f"Backed up corrupted config file {path.name} to {corrupted_file_path.name}")
                return None, f"Error decoding config file {path.name} (JSON format error).\nIt has been backed up as {corrupted_file_path.name}."
            else: # Should not happen if FileNotFoundError is caught first, but as a safeguard
                return None, f"Error decoding config file {path.name} (JSON format error), but original file was not found to back up."

        except OSError as ose:
            logger.error(f"Could not back up corrupted file {path.name} to {corrupted_file_path.name}: {ose}")
            return None, f"Error decoding config file {path.name} (JSON format error).\nCould not back it up to {corrupted_file_path.name} due to a system error: {ose}"
    except Exception as e:
        logger.error(f"Unexpected error loading config file {path}: {e}", exc_info=True)
        return None, f"An unexpected error occurred while loading {path.name}: {e}"


def save_user_config() -> bool:
    """Saves the user configuration to the default user config file.
    
    Returns:
        True if save was successful, False otherwise
    """
    logger.info(f"Saving user config to {USER_CONFIG_FILE}")
    try:
        # Create parent directories if they don't exist
        USER_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        with open(USER_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(USER_CONFIG, f, indent=4)
        return True
    except (IOError, TypeError) as e:
        logger.error(f"Could not save user config to {USER_CONFIG_FILE}: {e}")
        return False

def get_user_downloads_folder() -> pathlib.Path:
    """Returns the path to the user's Downloads folder."""
    return pathlib.Path.home() / "Downloads"

def _initialize_configs_and_globals():
    """
    Loads configurations from files and populates the global
    APP_CONFIG and USER_CONFIG dictionaries. Manages errors during loading.
    This function is run at module import time.
    """
    global APP_CONFIG, USER_CONFIG, _APP_CONFIG_LOAD_ERROR_MSG, _USER_CONFIG_LOAD_ERROR_MSG

    # --- Load App Config (e.g., API keys from bundled file) ---
    # This is critical; if it fails, some app functionalities might be impaired.
    app_config_data_from_file, app_load_err = load_config(APP_CONFIG_FILE, is_critical=True)
    _APP_CONFIG_LOAD_ERROR_MSG = app_load_err
    
    APP_CONFIG.clear()
    if app_config_data_from_file is not None:
        APP_CONFIG.update(app_config_data_from_file)
    else:
        # app_config.json is missing or corrupt. APP_CONFIG will be empty.
        # Services relying on API keys from here might use defaults (None) or fail.
        logger.warning("APP_CONFIG is empty due to loading errors. API keys or app-specific settings might be missing.")

    # --- Load User Config ---
    # This is not critical if the file doesn't exist (e.g., first run).
    # If it exists but is corrupt, it's an error, and defaults will be used.
    user_config_data_from_file, user_load_err = load_config(USER_CONFIG_FILE, is_critical=False)
    _USER_CONFIG_LOAD_ERROR_MSG = user_load_err

    # --- Populate USER_CONFIG (the effective configuration) ---
    # Order of precedence:
    # 1. User's settings from USER_CONFIG_FILE (if loaded successfully)
    # 2. API keys from APP_CONFIG (if present and not overridden by user)
    # 3. DEFAULT_CONFIG (hardcoded application defaults)

    USER_CONFIG.clear()
    
    # Start with DEFAULT_CONFIG
    USER_CONFIG.update(DEFAULT_CONFIG.copy()) # Use a copy

    # Layer APP_CONFIG specific keys (like API keys) if they exist
    # These are typically keys not meant for user direct edit via settings UI but can be in app_config.json
    # and potentially overridden if the user manually puts them in their user_config.json
    for key in ["discogs_token", "lastfm_key"]: # Example API keys from APP_CONFIG
        if key in APP_CONFIG and APP_CONFIG[key] is not None:
            USER_CONFIG[key] = APP_CONFIG[key]

    # Layer user's preferences from their config file
    if user_config_data_from_file is not None: # This will be {} if file not found, None if corrupted
        for key, value in user_config_data_from_file.items():
            USER_CONFIG[key] = value

    logger.info("Configuration loading and processing complete.")
    if _APP_CONFIG_LOAD_ERROR_MSG:
        logger.error(f"App config loading error: {_APP_CONFIG_LOAD_ERROR_MSG}")
    if _USER_CONFIG_LOAD_ERROR_MSG:
        logger.warning(f"User config loading issue: {_USER_CONFIG_LOAD_ERROR_MSG}")


def get_initial_config_loading_errors() -> List[str]:
    """Returns a list of error messages encountered during initial config loading."""
    errors = []
    if _APP_CONFIG_LOAD_ERROR_MSG:
        errors.append(_APP_CONFIG_LOAD_ERROR_MSG)
    if _USER_CONFIG_LOAD_ERROR_MSG:
        errors.append(_USER_CONFIG_LOAD_ERROR_MSG)
    return errors

# --- Initialize configurations when this module is imported ---
# TODO: Refactor (will require configs to not be global).
_initialize_configs_and_globals()
