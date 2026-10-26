"""Where the program keeps its files: the settings file and the recordings folder.

Running `python app.py` directly: everything goes in the program folder (settings.json, recordings/).
In the packaged .app / .exe the program folder is read-only, so files go in the user folder:
  Settings    Mac: ~/Library/Application Support/ListenStudio/settings.json
              Windows: %APPDATA%\\ListenStudio\\settings.json
  Recordings  ~/Music/ListenStudio/
An old jingting_settings.json is moved here automatically on first start.
"""
import os
import shutil
import sys

APP_NAME = "ListenStudio"
FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(__file__))


def _user_dir(name):
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/Application Support/{name}")
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), name)


def data_dir():
    d = _user_dir(APP_NAME) if FROZEN else HERE
    os.makedirs(d, exist_ok=True)
    return d


SETTINGS_FILE = os.path.join(data_dir(), "settings.json")
RECORDINGS_DIR = (os.path.join(os.path.expanduser("~/Music"), APP_NAME) if FROZEN
                  else os.path.join(HERE, "recordings"))
READER_DRAFT = os.path.join(data_dir(), "reader_draft.txt")   # The text being written on the 朗读 (Read aloud) tab

# Downloaded offline models (always in the user folder, packaged or not; never in the program folder / git)
MODELS_DIR = os.path.join(_user_dir(APP_NAME), "models")
# Cache that can be deleted at any time and rebuilt (audio generated for reading aloud)
if sys.platform == "darwin":
    CACHE_DIR = os.path.expanduser(f"~/Library/Caches/{APP_NAME}")
else:
    CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"),
                             APP_NAME, "cache")

# Settings files from before the rename (next to the script, or in the old packaged app's user folder)
LEGACY_SETTINGS = [
    os.path.join(HERE, "jingting_settings.json"),
    os.path.join(_user_dir("jingting"), "jingting_settings.json"),
]


def migrate_settings():
    """If there is no settings.json yet but an old jingting_settings.json exists, move the old file here."""
    if os.path.exists(SETTINGS_FILE):
        return
    for old in LEGACY_SETTINGS:
        if os.path.isfile(old):
            try:
                shutil.move(old, SETTINGS_FILE)
            except OSError:
                pass  # If it can't be moved, never mind: use the default settings
            return
