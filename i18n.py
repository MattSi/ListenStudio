"""UI language (English / Chinese).

The Chinese text in the code is the key; English comes from the table in i18n_en.py:

    tr("开始转录")                  -> "Transcribe" in English, "开始转录" in Chinese
    tr("第 {i} / {n} 句", i=3, n=9)  -> "Sentence 3 / 9" / "第 3 / 9 句"  (str.format)
    tr("关闭##button")              -> same Chinese word, different English: "##context" picks
                                      the translation and is dropped in Chinese

The language is read from settings.json the first time this module is imported, so labels
built at import time (menus, dropdown lists) are already in the right language. Changing it
in Settings takes effect after a restart. A missing translation falls back to the Chinese.
"""
import json

import app_paths
from i18n_en import EN

LANGUAGES = {"en": "English", "zh": "中文"}
DEFAULT = "en"


def _saved_language():
    try:
        with open(app_paths.SETTINGS_FILE, encoding="utf-8") as f:
            lang = json.load(f).get("language")
    except (OSError, ValueError):
        lang = None
    return lang if lang in LANGUAGES else DEFAULT


LANG = _saved_language()


def tr(zh, **kwargs):
    text = EN.get(zh, zh.split("##")[0]) if LANG == "en" else zh.split("##")[0]
    return text.format(**kwargs) if kwargs else text
