"""程序存东西的位置：设置文件、录音文件夹。

直接跑 python app.py：都放在程序文件夹里（settings.json、recordings/）。
打包成 .app / .exe 后程序目录不能写，改放到用户目录：
  设置  Mac: ~/Library/Application Support/ListenStudio/settings.json
        Windows: %APPDATA%\\ListenStudio\\settings.json
  录音  ~/Music/ListenStudio/
以前的 jingting_settings.json 会在第一次启动时自动搬过来。
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

# 改名前的设置文件（脚本旁边的、以前打包版用户目录里的）
LEGACY_SETTINGS = [
    os.path.join(HERE, "jingting_settings.json"),
    os.path.join(_user_dir("jingting"), "jingting_settings.json"),
]


def migrate_settings():
    """还没有 settings.json、但有旧的 jingting_settings.json 时，把旧文件搬过来。"""
    if os.path.exists(SETTINGS_FILE):
        return
    for old in LEGACY_SETTINGS:
        if os.path.isfile(old):
            try:
                shutil.move(old, SETTINGS_FILE)
            except OSError:
                pass  # 搬不动就算了，用默认设置
            return
