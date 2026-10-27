"""Text to speech (used by the 朗读 / Read aloud tab).

Two engines:
  Edge online voices (edge-tts): Microsoft neural voices, including Canadian accents; needs internet.
  Kokoro offline voices (kokoro-onnx): downloads a ~120 MB model the first time, then works offline.
With the engine set to "自动" (auto), Edge is tried first; if it can't connect, the whole text
uses Kokoro instead (if it has been downloaded).

Text is synthesized sentence by sentence, with a fixed pause between sentences. The result
holds each sentence's position in the text and its start/end time in the audio: used to
highlight the sentence being read, and to cut out each sentence when comparing.
The same text + voice + rate + pause is only synthesized once; results are cached in
app_paths.CACHE_DIR.
"""
import asyncio
import hashlib
import io
import json
import os
import re
import threading
import urllib.request
import wave
from dataclasses import dataclass

import av
import numpy as np

import app_paths
from i18n import tr

SR = 24000   # Both engines output 24 kHz mono

# (id saved in settings = Edge voice name, display name, Kokoro voice, Kokoro accent)
VOICES = [
    ("en-CA-ClaraNeural", tr("加拿大 · 女 · Clara"), "af_heart", "en-us"),
    ("en-CA-LiamNeural", tr("加拿大 · 男 · Liam"), "am_michael", "en-us"),
    ("en-US-AriaNeural", tr("美国 · 女 · Aria"), "af_bella", "en-us"),
    ("en-US-GuyNeural", tr("美国 · 男 · Guy"), "am_adam", "en-us"),
    ("en-US-JennyNeural", tr("美国 · 女 · Jenny"), "af_nicole", "en-us"),
    ("en-GB-SoniaNeural", tr("英国 · 女 · Sonia"), "bf_emma", "en-gb"),
    ("en-GB-RyanNeural", tr("英国 · 男 · Ryan"), "bm_george", "en-gb"),
]
VOICE_IDS = [v[0] for v in VOICES]
VOICE_NAMES = {v[0]: v[1] for v in VOICES}
ENGINES = {"auto": tr("自动（在线优先）"), "edge": tr("只用在线"), "offline": tr("只用离线")}
ENGINE_NAMES = {"edge": tr("Edge 在线语音"), "kokoro": tr("Kokoro 离线语音")}
SPEED_RANGE = (0.5, 1.5)
PAUSE_RANGE = (0.0, 3.0)

KOKORO_DIR = os.path.join(app_paths.MODELS_DIR, "kokoro")
_KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_FILES = ["kokoro-v1.0.int8.onnx", "voices-v1.0.bin"]
OFFLINE_SIZE_MB = 120
TTS_CACHE = os.path.join(app_paths.CACHE_DIR, "tts")


class TTSError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Speech:
    path: str        # The synthesized WAV
    engine: str      # "edge" / "kokoro"
    spans: list      # Each sentence's (start, end) character position in the text
    times: list      # Each sentence's (start, end) time in the audio, in seconds


# ---------- Sentence splitting ----------
_SENT_END = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s|$)|\n")
_ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "e.g", "i.e", "no", "prof"}


def split_sentences(text):
    """Return each sentence's (start, end) character position (surrounding spaces removed; parts with no letters or digits are skipped)."""
    spans, start = [], 0

    def add(a, b):
        seg = text[a:b]
        lead = len(seg) - len(seg.lstrip())
        seg = seg.strip()
        if any(ch.isalnum() for ch in seg):
            spans.append((a + lead, a + lead + len(seg)))

    for m in _SENT_END.finditer(text):
        if m.group() != "\n":
            word = re.search(r"([A-Za-z.]+)\.*$", text[start:m.start()])
            if word and word.group(1).lower().rstrip(".") in _ABBREV:
                continue   # Abbreviations like Mr. / e.g. don't end a sentence
            end = m.end()
        else:
            end = m.start()
        add(start, end)
        start = m.end()
    add(start, len(text))
    return spans


# ---------- Edge online voices ----------
def _edge_say(text, voice, speed):
    import edge_tts

    rate = f"{round((speed - 1) * 100):+d}%"

    async def run():
        data = bytearray()
        com = edge_tts.Communicate(text, voice, rate=rate,
                                   connect_timeout=8, receive_timeout=30)
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                data += chunk["data"]
        return bytes(data)

    data = asyncio.run(run())
    if not data:
        raise TTSError(tr("Edge 在线语音没有返回声音"))
    return _decode(data)


def _decode(data):
    """mp3 bytes -> 24 kHz mono float32."""
    out = []
    with av.open(io.BytesIO(data)) as c:
        rs = av.AudioResampler(format="flt", layout="mono", rate=SR)
        for frame in c.decode(c.streams.audio[0]):
            out += [f.to_ndarray().reshape(-1) for f in rs.resample(frame)]
        out += [f.to_ndarray().reshape(-1) for f in rs.resample(None)]
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)


# ---------- Kokoro offline voices ----------
_kokoro = None
_kokoro_lock = threading.Lock()


def offline_ready():
    return all(os.path.isfile(os.path.join(KOKORO_DIR, f)) for f in KOKORO_FILES)


def download_offline(progress=None, cancel=None):
    """Download the Kokoro model (~120 MB). progress(bytes done, total bytes). Writes a .part file first and renames it when complete."""
    os.makedirs(KOKORO_DIR, exist_ok=True)
    sizes = {}
    for name in KOKORO_FILES:   # Ask for the total size first, so the progress bar is accurate
        req = urllib.request.Request(_KOKORO_URL + name, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            sizes[name] = int(r.headers.get("Content-Length") or 0)
    total, done = sum(sizes.values()), 0
    for name in KOKORO_FILES:
        dst = os.path.join(KOKORO_DIR, name)
        if os.path.isfile(dst) and os.path.getsize(dst) == sizes[name]:
            done += sizes[name]
            continue
        tmp = dst + ".part"
        try:
            with urllib.request.urlopen(_KOKORO_URL + name, timeout=60) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 16):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def _kokoro_say(text, voice_id, speed):
    global _kokoro
    with _kokoro_lock:
        if _kokoro is None:
            if not offline_ready():
                raise TTSError(tr("离线语音还没下载：在「朗读」页点「下载离线语音」"))
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(os.path.join(KOKORO_DIR, KOKORO_FILES[0]),
                             os.path.join(KOKORO_DIR, KOKORO_FILES[1]))
        _, _, kvoice, lang = next(v for v in VOICES if v[0] == voice_id)
        have = _kokoro.get_voices()
        if kvoice not in have:   # The model doesn't have this voice: use another one of the same gender
            same = [v for v in have if v[:2] == kvoice[:2]] or [v for v in have if v[1] == kvoice[1]]
            kvoice = (same or list(have))[0]
        audio, sr = _kokoro.create(text, voice=kvoice, speed=speed, lang=lang)
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sr != SR:   # Current models are 24 kHz already; just in case
        audio = np.interp(np.arange(0, len(audio), sr / SR), np.arange(len(audio)), audio)
    return audio.astype(np.float32)


# ---------- Synthesizing the whole text ----------
def _cache_paths(text, voice, speed, pause, engine):
    key = json.dumps([text, voice, round(speed, 2), round(pause, 2), engine], ensure_ascii=False)
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(TTS_CACHE, h + ".wav"), os.path.join(TTS_CACHE, h + ".json")


def _from_cache(text, voice, speed, pause, engine):
    wav, meta = _cache_paths(text, voice, speed, pause, engine)
    try:
        with open(meta, encoding="utf-8") as f:
            m = json.load(f)
        if os.path.isfile(wav):
            return Speech(wav, engine, [tuple(s) for s in m["spans"]],
                          [tuple(t) for t in m["times"]])
    except (OSError, ValueError, KeyError):
        pass
    return None


def _render(text, spans, voice, speed, pause, engine, progress, cancel):
    say = _edge_say if engine == "edge" else _kokoro_say
    pieces, times, t = [], [], 0.0
    gap = np.zeros(int(pause * SR), np.float32)
    for i, (a, b) in enumerate(spans):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if progress:
            progress(i, len(spans), engine)
        audio = say(text[a:b], voice, speed)
        times.append((round(t, 3), round(t + len(audio) / SR, 3)))
        pieces += [audio, gap]
        t += (len(audio) + len(gap)) / SR
    if progress:
        progress(len(spans), len(spans), engine)
    pcm = np.clip(np.concatenate(pieces[:-1]) * 32767, -32768, 32767).astype("<i2")
    wav, meta = _cache_paths(text, voice, speed, pause, engine)
    os.makedirs(TTS_CACHE, exist_ok=True)
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    with open(meta, "w", encoding="utf-8") as f:
        json.dump({"spans": spans, "times": times}, f)
    return Speech(wav, engine, spans, times)


def synthesize(text, voice, speed=1.0, pause=0.6, engine="auto", progress=None, cancel=None):
    """Synthesize the whole text into one WAV and return a Speech.
    progress(sentences done, total sentences, engine); cancel is a threading.Event - when set, raises Cancelled."""
    spans = split_sentences(text)
    if not spans:
        raise TTSError(tr("没有可以朗读的文字"))
    if voice not in VOICE_NAMES:
        voice = VOICE_IDS[0]
    order = {"auto": ["edge", "kokoro"], "edge": ["edge"], "offline": ["kokoro"]}[engine]
    for eng in order:   # Already in the cache: no need to synthesize again
        hit = _from_cache(text, voice, speed, pause, eng)
        if hit and hit.spans == spans:
            return hit
    try:
        return _render(text, spans, voice, speed, pause, order[0], progress, cancel)
    except Cancelled:
        raise
    except Exception as e:
        if order[0] != "edge":
            raise
        if len(order) == 1 or not offline_ready():
            raise TTSError(tr("连不上 Edge 在线语音（{e}）", e=e)) from e
        # "自动" (auto): if online fails, use the offline voice for the whole text (never mix two voices)
        return _render(text, spans, voice, speed, pause, "kokoro", progress, cancel)
