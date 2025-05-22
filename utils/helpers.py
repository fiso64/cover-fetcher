import sys
import pathlib

def get_bundle_dir() -> pathlib.Path:
    """
    Returns the base directory for the application.
    For bundled apps, it's the executable's dir or _MEIPASS.
    For development, it's the project root (assumed to be parent of 'utils' and 'assets').
    """
    if getattr(sys, 'frozen', False):
        if hasattr(sys, '_MEIPASS'):
            return pathlib.Path(sys._MEIPASS) # one-file
        else:
            return pathlib.Path(sys.executable).parent # standalone
    else:
        # Development mode:
        # __file__ in utils/helpers.py -> project_root/utils/helpers.py
        # .parent -> project_root/utils
        # .parent.parent -> project_root
        return pathlib.Path(__file__).resolve().parent.parent