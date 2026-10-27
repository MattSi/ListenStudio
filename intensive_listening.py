"""
The intensive listening tab (the "精听" tab in app.py): splits audio into sentences at the
silences and pauses after each sentence.
This file is a module only; start the program from app.py: python app.py

Drop an audio or video file into the window (or click the drop zone to choose a file) to start.
The window shows a zoomed waveform of the current sentence and an overview of the whole file;
click the overview to jump to that sentence.
For video files the picture is shown too; picture and sound play and pause together for each sentence.

Keys:
  Space       play the next sentence (Space while playing = stop)
  R / Down    replay the current sentence
  Left/Right  previous / next sentence
  T           show / hide subtitles
  + / -       volume up / down (above 100% a limiter stops clipping)
  [ / ]       slower / faster (0.5x - 1.5x, speed changes without changing pitch)

Dependencies: pip install av numpy sounddevice pillow tkinterdnd2
  Subtitles are optional (turn them on in "⚙ 设置"); they also need: pip install faster-whisper
  (openai-whisper also works; with an NVIDIA GPU and torch with CUDA it uses the GPU;
   results are cached, so each file is only recognized once)

Sentence-split options are changed with "⚙ 设置" (top right); changes re-split right away
and are saved to settings.json (location: see app_paths.py), so they are kept next time.
Command-line options (--threshold / --min-silence / --max-len / --pad): see app.py.
"""

import bisect
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import warnings
from collections import deque
from tkinter import filedialog, messagebox, ttk

import app_paths
import audio_io
import ui_style
import i18n
from i18n import tr

import av
import numpy as np
import sounddevice as sd
from PIL import ImageTk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Without tkinterdnd2, fall back to choosing files with the button only
    TkinterDnD = None

AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma",
              ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".wmv",
              ".ts", ".mpg", ".mpeg", ".3gp")
DROP_HINT = (tr("把音频或视频文件拖到这里（或点这里选择），自动断句后逐句精听") if TkinterDnD
             else tr("点击这里选择音频或视频文件"))
UI_FONT = ui_style.FONT

# ---------- Settings (settings.json, location: see app_paths.py) ----------
SETTINGS_FILE = app_paths.SETTINGS_FILE
DEFAULTS = {
    "threshold": None,    # Silence threshold in dB (relative to the peak); None = set automatically from the noise floor
    "min_silence": 0.35,  # How long a silence ends a sentence (seconds)
    "max_len": 6.0,       # Sentences longer than this (seconds) are split again; 0 = don't
    "pad": 0.15,          # Extra time kept before and after each sentence (seconds)
    "volume": 2.0,        # Volume multiplier; above 1 a limiter stops clipping
    "speed": 1.0,         # Playback speed (changes speed without changing pitch)
    "language": "en",     # UI language: "en" / "zh" (see i18n.py; takes effect after restart)
    "subtitles": "off",   # Subtitles: off / always (always shown) / after (shown after the sentence has played)
    "whisper_model": "small.en",
    "mic": None,          # Mic / speaker names; None = follow the system default
    "speaker": None,
    "auto_next": False,   # Play the next sentence automatically after one finishes
    "auto_delay": 3.0,    # Seconds to wait before playing the next sentence automatically
    "rec_length": "60",   # Recording length on the transcribe tab: "60" / "90" seconds, or "custom"
    "rec_custom_min": 5.0,  # Minutes for "自定" (custom)
    "tts_voice": "en-CA-ClaraNeural",  # 朗读 (Read aloud) tab: voice (see tts.VOICES)
    "tts_speed": 1.0,     # Speech rate multiplier
    "tts_pause": 0.6,     # Pause between sentences (seconds)
    "tts_engine": "auto",  # auto = online first / edge = online only / offline = offline only
    "cmp_auto": True,     # 朗读 tab, compare by sentence: go on to the next sentence automatically
}
AUTO_DELAY_RANGE = (0.0, 30.0)
SYSTEM_DEVICE = tr("跟随系统默认")
SUB_MODES = {"off": tr("关闭"), "always": tr("一直显示"), "after": tr("听完一句再显示"),
             "manual": tr("手动显示")}  # manual: every sentence starts hidden; shown only with the button / T
# Recognition model: the transcribe tab and the subtitles share one (whisper_model in settings.json),
# and also share the loaded models in _whisper_models, so each model is loaded only once
WHISPER_MODELS = ["tiny.en", "base.en", "small.en", "medium.en", "turbo", "large-v3"]
MODEL_LOCK = threading.Lock()  # Only one side may use a model at a time (openai-whisper can't be used by two threads at once)


def load_settings():
    app_paths.migrate_settings()  # An old jingting_settings.json is renamed and moved here the first time
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass  # No file, or a broken file: use the defaults
    return s


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except OSError as ex:
        messagebox.showwarning(tr("设置"), tr("设置没能保存：\n{ex}", ex=ex))


def audio_devices(kind):
    """kind = "input" / "output"; returns {name: device index}.
    On Windows the same device appears once each under MME / DirectSound / WASAPI; keep only the default API's set.
    MME names are cut at 31 characters, so borrow the full name from another API."""
    key = f"max_{kind}_channels"
    api = sd.default.hostapi
    devs = [d for d in sd.query_devices() if d[key] > 0]
    full = [d["name"] for d in devs if d["hostapi"] != api]
    out = {}
    for d in devs:
        if d["hostapi"] != api or "Sound Mapper" in d["name"]:  # The Mapper is the system default
            continue
        name = next((f for f in full if f.startswith(d["name"])), d["name"])
        out.setdefault(name, d["index"])
    return out


def apply_audio_devices(opts):
    """Make the mic / speaker chosen in settings sounddevice's default devices; recording and playback on both tabs use them.
    If the chosen device is unplugged, fall back to the system default."""
    try:
        ins, outs = audio_devices("input"), audio_devices("output")
    except Exception:  # No sound card or similar: use the system defaults for everything
        ins, outs = {}, {}
    sd.default.device = (ins.get(opts["mic"]), outs.get(opts["speaker"]))


# ---------- Reading audio ----------
def load_audio(path, layout="stereo", rate=None):
    """Decode into a float32 array (samples, channels); without rate, keep the original sample rate."""
    container = av.open(path)
    stream = container.streams.audio[0]
    sr = rate or stream.rate
    ch = 1 if layout == "mono" else 2
    resampler = av.AudioResampler(format="flt", layout=layout, rate=sr)
    chunks = []

    def collect(frames):
        if frames is None:
            return
        if not isinstance(frames, list):
            frames = [frames]
        for f in frames:
            chunks.append(f.to_ndarray().reshape(-1, ch))

    for frame in container.decode(stream):
        collect(resampler.resample(frame))
    collect(resampler.resample(None))  # flush
    container.close()
    audio = np.concatenate(chunks).astype(np.float32)
    return audio, sr


# ---------- Splitting sentences at silences ----------
def noise_threshold(db, ratio=0.5):
    """Set the threshold from the noise floor: a point between the noise (quietest 5% of frames) and the speech (loudest 10%).
    ratio=0.5 is exactly in the middle; breathing and background noise are usually below it, normal speech above it."""
    noise = np.percentile(db, 5)
    speech = np.percentile(db, 90)
    return noise + ratio * (speech - noise)


def split_long(db, thr, s, e, max_f, min_piece):
    """Split an overly long sentence at its longest internal pause, recursively, until no part is longer than max_f frames.
    Pause = consecutive frames below the silence threshold; if two are equally long, pick the one nearer the middle."""
    if e - s <= max_f:
        return [(s, e)]
    lo, hi = s + min_piece, e - min_piece
    if hi <= lo:
        return [(s, e)]
    quiet = np.concatenate([[False], db[s:e] < thr, [False]])
    edges = np.flatnonzero(np.diff(quiet.astype(np.int8)))
    mid = (s + e) / 2
    best = None
    for a, b in zip(edges[::2] + s, edges[1::2] + s):  # Each pause is [a, b)
        c = (a + b) // 2
        if lo <= c <= hi:
            key = (b - a, -abs(c - mid))
            if best is None or key > best[0]:
                best = (key, c)
    if best is not None:
        cut = int(best[1])
    else:  # No pause at all inside the sentence: second best, cut at the quietest 0.2 s
        sm = np.convolve(db[s:e], np.ones(20) / 20, mode="same")
        cut = lo + int(np.argmin(sm[lo - s:hi - s]))
    return (split_long(db, thr, s, cut, max_f, min_piece)
            + split_long(db, thr, cut, e, max_f, min_piece))


def split_sentences(audio, sr, threshold_db=None, min_silence=0.35,
                    min_len=0.4, pad=0.15, max_len=6.0):
    """Return (list of sentences [(start, end) in samples], the threshold actually used in dB).
    threshold_db=None sets the threshold from the noise floor; max_len>0 splits overly long sentences again."""
    mono = audio.mean(axis=1)
    hop = int(sr * 0.01)  # 10 ms per frame
    n = len(mono) // hop
    frames = mono[: n * hop].reshape(n, hop)
    rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-10
    db = 20 * np.log10(rms / rms.max())
    if threshold_db is None:
        threshold_db = noise_threshold(db)
    voiced = db > threshold_db

    min_sil_frames = int(min_silence / 0.01)
    segments = []
    start = None
    silent_run = 0
    last_voiced = None
    for i, v in enumerate(voiced):
        if v:
            if start is None:
                start = i
            last_voiced = i
            silent_run = 0
        elif start is not None:
            silent_run += 1
            if silent_run >= min_sil_frames:
                segments.append((start, last_voiced))
                start = None
                silent_run = 0
    if start is not None:
        segments.append((start, last_voiced))

    result = []
    pad_f = int(pad / 0.01)
    for s, e in segments:
        if (e - s) * 0.01 < min_len:
            continue
        s, e = max(0, s - pad_f), min(n, e + pad_f + 1)
        pieces = (split_long(db, threshold_db, s, e, int(max_len / 0.01), 150)  # Each part at least 1.5 s
                  if max_len > 0 else [(s, e)])
        result += [(a * hop, min(len(mono), b * hop)) for a, b in pieces]
    return result, threshold_db


# ---------- Volume ----------
# ---------- Playback speed ----------
SPEEDS = [0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5]


def speed_label(v):
    return f"{v:g}×"


def stretch(x, sr, rate):
    """Change speed without changing pitch: uses ffmpeg's atempo filter (built into PyAV), so slowing down doesn't make the voice deeper.
    x is float32 of shape (samples, 2); returns a new array about len(x) / rate long."""
    if abs(rate - 1.0) < 1e-6:
        return x
    g = av.filter.Graph()
    src = g.add_abuffer(format="flt", sample_rate=sr, layout="stereo")
    tempo = g.add("atempo", f"{rate}")  # One atempo filter supports 0.5 - 2.0
    sink = g.add("abuffersink")
    src.link_to(tempo)
    tempo.link_to(sink)
    g.configure()
    frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(x, dtype=np.float32).reshape(1, -1),
                                       format="flt", layout="stereo")
    frame.sample_rate, frame.pts = sr, 0
    g.push(frame)
    g.push(None)  # End of input: flush what is left in the filter
    out = []
    while True:
        try:
            out.append(g.pull().to_ndarray().reshape(-1, 2))
        except (av.error.EOFError, av.error.BlockingIOError, BlockingIOError):
            break
    return np.concatenate(out) if out else x


def boost(x, sr, gain, ceiling=0.95):
    """Amplify by gain and hold the peaks down with a limiter: loud parts are turned down a little, so it never clips.
    How: for each 5 ms block, compute "the gain that stays under ceiling", take the minimum over 20 ms
    before and after (turn down early, release slowly), smooth it, then multiply sample by sample."""
    if gain <= 1.0:
        return x * gain
    y = x * gain
    blk = max(1, int(sr * 0.005))
    n = -(-len(y) // blk)
    peak = np.abs(np.pad(y, ((0, n * blk - len(y)), (0, 0)))).reshape(n, -1).max(axis=1)
    g = np.minimum(1.0, ceiling / np.maximum(peak, 1e-9))
    r = 4  # Minimum over +-4 blocks = +-20 ms
    gp = np.pad(g, r, mode="edge")
    g = np.min([gp[i:i + n] for i in range(2 * r + 1)], axis=0)
    g = np.convolve(np.pad(g, 2, mode="edge"), np.ones(5) / 5, mode="valid")  # Smooth over +-10 ms
    g = np.interp(np.arange(len(y)), np.arange(n) * blk + blk / 2, g)
    return np.clip(y * g[:, None], -ceiling, ceiling).astype(np.float32)


def fmt(sec):
    return f"{int(sec // 60):02d}:{sec % 60:04.1f}"


# ---------- Waveform ----------
ENV_SEC = 0.001  # Envelope resolution: one peak per 1 ms


def make_envelope(audio, sr):
    """Reduce the audio to one peak (0-1) per ENV_SEC; waveforms are drawn from this only, never from the raw data."""
    hop = max(1, int(sr * ENV_SEC))
    peak = np.abs(audio).max(axis=1)
    n = len(peak) // hop
    env = peak[: n * hop].reshape(n, hop).max(axis=1)
    return env / (env.max() + 1e-10), hop


def column_peaks(env, a, b, width):
    """Split env[a:b] into width equal columns and take the maximum of each."""
    b = max(b, a + 1)
    idx = np.linspace(a, b, width + 1).astype(int)[:-1]
    return np.maximum.reduceat(env[:b], idx)


# ---------- Subtitles (optional; need pip install faster-whisper or openai-whisper) ----------
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"),
                         "jingting", "subs")
_whisper_models = {}

# Prefer faster-whisper (CTranslate2): faster than openai-whisper on CPU (2-3x faster word timestamps),
# and no need to install 1GB+ of torch. With an NVIDIA GPU and torch with CUDA, openai-whisper still uses the GPU.
# This code runs in this process and is also put in front of the script given to an external Python (so standard library only).
FW_LOADER = r'''
import os, platform, sys
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # hide the warning shown on Windows without Developer Mode

def load_fw(name):
    """A faster-whisper model; raises ImportError if it isn't installed, or if openai-whisper should use the GPU instead."""
    try:
        import torch
        gpu = torch.cuda.is_available()
    except Exception:  # no torch, or a broken torch (common on ARM machines)
        gpu = False
    if gpu:
        raise ImportError("用 openai-whisper + GPU")
    from faster_whisper import WhisperModel
    # On Windows ARM, CTranslate2 runs under x64 emulation, where int8 is actually slower than float32
    arm_win = sys.platform == "win32" and platform.machine() == "ARM64"
    return WhisperModel(name, device="cpu", compute_type="float32" if arm_win else "int8")
'''
exec(FW_LOADER)


def subs_cache_path(path, model_name):
    """Results are cached by file path + size + modified time + model, so each file is only recognized once."""
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|{model_name}"
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json")


def transcribe_words(path, model_name):
    """Return [(start s, end s, word), ...]. The first use of a model is slow (it has to load)."""
    cache = subs_cache_path(path, model_name)
    try:
        with open(cache, encoding="utf-8") as f:
            return [tuple(w) for w in json.load(f)]
    except (OSError, ValueError):
        pass
    audio16k = load_audio(path, layout="mono", rate=16000)[0][:, 0]  # Whisper wants 16 kHz mono
    try:
        words = _transcribe_here(audio16k, model_name)
    except ImportError:
        # The packaged .app / .exe has no Whisper (torch would add 1GB+),
        # so find a Python on the computer that has Whisper and let it do the recognition
        words = _transcribe_external(audio16k, model_name)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False)
    except OSError:
        pass
    return words


def _transcribe_here(audio16k, model_name):
    """Recognize with Whisper in this process (when running the script with Whisper installed).
    If the transcribe tab is using the model, wait here until it's done."""
    with MODEL_LOCK:
        return _transcribe_here_locked(audio16k, model_name)


def _transcribe_here_locked(audio16k, model_name):
    lang = "en" if model_name.endswith(".en") else None
    try:
        key = ("fw", model_name)
        if key not in _whisper_models:
            _whisper_models[key] = load_fw(model_name)
        segs, _ = _whisper_models[key].transcribe(audio16k, language=lang,
                                                  word_timestamps=True, beam_size=1)
        return [(w.start, w.end, w.word) for s in segs for w in s.words or []]
    except ImportError:
        pass  # No faster-whisper: use openai-whisper
    import torch
    import whisper
    # There is no Triton on Windows, so Whisper falls back to a plain implementation for word timestamps and prints warnings;
    # it only affects that small alignment step (recognition itself still runs on the GPU) and the result is the same, so hide them
    warnings.filterwarnings("ignore", message="Failed to launch Triton kernels")
    if model_name not in _whisper_models:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        _whisper_models[model_name] = whisper.load_model(model_name, device=device)
    model = _whisper_models[model_name]
    result = model.transcribe(audio16k, word_timestamps=True, verbose=None, language=lang,
                              fp16=model.device.type == "cuda")
    return [(w["start"], w["end"], w["word"])
            for seg in result["segments"] for w in seg.get("words", [])]


# Recognition script run by an external Python: read .npy audio -> Whisper -> write the words as JSON
WHISPER_HELPER = FW_LOADER + r"""
import json, warnings
warnings.filterwarnings("ignore")
import numpy as np
audio_path, name, out_path = sys.argv[1:4]
audio, lang = np.load(audio_path), "en" if name.endswith(".en") else None
try:
    segs, _ = load_fw(name).transcribe(audio, language=lang, word_timestamps=True, beam_size=1)
    words = [(w.start, w.end, w.word) for s in segs for w in s.words or []]
except ImportError:
    import torch, whisper
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = whisper.load_model(name, device=device)
    r = model.transcribe(audio, word_timestamps=True, verbose=None, language=lang,
                         fp16=device == "cuda")
    words = [(w["start"], w["end"], w["word"]) for s in r["segments"] for w in s.get("words", [])]
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(words, f, ensure_ascii=False)
"""
# An external Python is usable if it has numpy and either faster-whisper or openai-whisper
HAS_WHISPER = ("import numpy, importlib.util as u; "
               "assert u.find_spec('faster_whisper') or u.find_spec('whisper')")
INSTALL_HINT = "pip3 install faster-whisper" if sys.platform == "darwin" else "pip install faster-whisper"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # On Windows, don't pop up a black console window
_external_python = []  # Path of the Python we found (remembered after the first search)


def find_external_python():
    """Find a Python that has Whisper. When the .app is opened from Finder, PATH is very short, so try all the usual places.
    It can also be set directly with the JINGTING_PYTHON environment variable."""
    if _external_python:
        return _external_python[0]
    cands = [os.environ.get("JINGTING_PYTHON")]
    cands += sorted(glob.glob("/Library/Frameworks/Python.framework/Versions/*/bin/python3"),
                    reverse=True)  # Python installed from python.org (newest version first)
    cands += ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
              shutil.which("python3"), shutil.which("python")]
    seen = set()
    for py in cands:
        # On a Mac without developer tools, /usr/bin/python3 pops up "install command line tools": skip it
        if not py or py in seen or py == "/usr/bin/python3" or not os.path.isfile(py):
            continue
        seen.add(py)
        try:
            ok = subprocess.run([py, "-c", HAS_WHISPER], capture_output=True,
                                timeout=120, creationflags=NO_WINDOW).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            _external_python.append(py)
            return py
    return None


def _transcribe_external(audio16k, model_name):
    py = find_external_python()
    if not py:
        raise ImportError(tr("要显示字幕需要先安装 Whisper：在终端运行 {cmd}", cmd=INSTALL_HINT))
    with tempfile.TemporaryDirectory() as d:
        audio_path, out_path = os.path.join(d, "audio.npy"), os.path.join(d, "words.json")
        np.save(audio_path, audio16k.astype(np.float32))
        r = subprocess.run([py, "-c", WHISPER_HELPER, audio_path, model_name, out_path],
                           capture_output=True, text=True, creationflags=NO_WINDOW)
        if r.returncode != 0:
            last = (r.stderr.strip().splitlines() or [tr("未知错误")])[-1]
            raise RuntimeError(f"{os.path.basename(py)}: {last}")
        with open(out_path, encoding="utf-8") as f:
            return [tuple(w) for w in json.load(f)]


def sentence_texts(words, segs, sr):
    """Put each word in the sentence that contains its midpoint; words in a gap between sentences go to the nearest sentence."""
    texts = [[] for _ in segs]
    starts = [a for a, b in segs]
    for start, end, word in words:
        mid = (start + end) / 2 * sr
        i = max(0, bisect.bisect_right(starts, mid) - 1)  # The last sentence that starts no later than mid
        if mid > segs[i][1] and i + 1 < len(segs) and segs[i + 1][0] - mid < mid - segs[i][1]:
            i += 1  # In a gap, and nearer to the next sentence
        texts[i].append(word)
    return ["".join(t).strip() for t in texts]


# ---------- Video picture ----------
def has_video(path):
    """Only real video counts (cover art in mp3/m4a is also a "video" stream and must be skipped)."""
    with av.open(path) as c:
        return any(not (vs.disposition & av.stream.Disposition.attached_pic)
                   for vs in c.streams.video)


def fit(size, box):
    """Scale size, keeping its shape, to the largest size that fits in box (even width and height, as the scaler requires)."""
    (w, h), (bw, bh) = size, box
    k = min(bw / w, bh / h)
    return max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)


class VideoDecoder:
    """A background thread decodes the frames of a time range, scales them and puts them in the frames queue: (time s, PIL image).
    Every start() is a new job; an old job sees that gen changed and stops by itself."""

    MAX_QUEUE = 30  # Decode at most this many frames ahead, to save memory

    def __init__(self, path):
        self.path = path
        self.gen = 0
        self.frames = deque()
        with av.open(path) as c:
            vs = next(v for v in c.streams.video
                      if not (v.disposition & av.stream.Disposition.attached_pic))
            self.index = vs.index
            self.size = (vs.codec_context.width, vs.codec_context.height)
            self.fps = float(vs.average_rate or 25)

    def start(self, t0, t1, box):
        """Decode the frames of [t0, t1] seconds; with t0 == t1 only the frame at t0 (shown while paused)."""
        self.cancel()
        threading.Thread(target=self._run, args=(self.gen, t0, t1, box),
                         daemon=True).start()

    def cancel(self):
        self.gen += 1
        self.frames.clear()

    def _run(self, gen, t0, t1, box):
        try:
            with av.open(self.path) as c:
                vs = c.streams[self.index]
                vs.thread_type = "AUTO"
                w, h = fit(self.size, box)
                # seek lands on the nearest keyframe before t0; decode from there up to t0 (without scaling, to save time)
                c.seek(max(0, int(t0 / vs.time_base)), stream=vs)
                early = t0 - 0.5 / self.fps
                for f in c.decode(vs):
                    if gen != self.gen:
                        return
                    if f.time is None or f.time < early:
                        continue
                    img = f.to_image(width=w, height=h)
                    while len(self.frames) >= self.MAX_QUEUE and gen == self.gen:
                        time.sleep(0.01)
                    if gen != self.gen:
                        return
                    self.frames.append((f.time, img))
                    if f.time >= t1:
                        return
        except Exception:  # A broken picture doesn't stop listening: sound plays as usual
            pass


# ---------- Player UI ----------
class Player:
    def __init__(self, root, opts, parent=None):
        """parent: pass a tk.Frame to build inside another program (e.g. a tab in app.py);
        without it, it's a window of its own, as before."""
        self.root, self.opts = root, opts
        self.embedded = parent is not None
        parent = parent or root
        self.active = True  # When embedded, the outside decides: shortcuts are ignored unless this is the current tab
        self.audio, self.sr, self.segs = None, None, []
        self.cur = 0
        self.played = False
        self.playing = False
        self.loading = False
        self.env, self.env_hop = None, 1
        self.t0 = 0.0  # When the current sentence started playing
        self.view = (0, 1)  # Sample range shown in the zoomed waveform
        self.video = None   # VideoDecoder; None for audio-only files
        self.photo = None   # The picture being shown (keep a reference, or Tk throws the image away)
        self.pending = None  # Wait until the picture is ready before starting the sound: (sentence no., deadline)

        if not self.embedded:
            root.title(tr("精听"))
            root.geometry("760x560")

        ui_style.apply(root)
        apply_audio_devices(opts)
        # After audio is re-initialized (idle too long / retry after an error), set the chosen mic / speaker again
        audio_io.on_reinit(lambda: apply_audio_devices(self.opts))
        P = ui_style.PAD

        # --- Toolbar (same layout as the transcribe tab: open file on the left, volume / settings on the right) ---
        top = self.top = ttk.Frame(parent, padding=(P, P, P, P))
        top.pack(fill="x")
        ttk.Button(top, text=tr("打开文件…"), takefocus=False,
                   command=self.browse).pack(side="left")
        ttk.Button(top, text=tr("⚙ 设置"), takefocus=False,
                   command=self.open_settings).pack(side="right")
        vol = ttk.Frame(top)
        vol.pack(side="right", padx=(0, 12))
        self.vol_label = ttk.Label(vol, width=12, anchor="e")
        self.vol_label.pack(side="left", padx=(0, 6))
        self.vol_var = tk.DoubleVar(value=round(opts["volume"] * 100))
        self.vol_scale = ttk.Scale(vol, from_=50, to=250, orient="horizontal", length=140,
                                   variable=self.vol_var, takefocus=False,
                                   command=lambda v: self.set_volume(round(float(v) / 10) / 10))
        self.vol_scale.pack(side="left")
        self.vol_save_job = None
        self.set_volume(opts["volume"], save=False)

        # Playback speed: changes speed without changing pitch; also [ and ] keys, and the mouse wheel over the box
        spd = ttk.Frame(top)
        spd.pack(side="right", padx=(0, 16))
        ttk.Label(spd, text=tr("速度")).pack(side="left", padx=(0, 6))
        self.speed_var = tk.StringVar()
        self.speed_box = ttk.Combobox(spd, textvariable=self.speed_var, state="readonly",
                                      values=[speed_label(v) for v in SPEEDS], width=6,
                                      takefocus=False)
        self.speed_box.pack(side="left")

        def on_speed(e):
            self.set_speed(SPEEDS[self.speed_box.current()])
            self.speed_box.selection_clear()
            parent.focus_set()  # Give focus back to the page, so Space and other shortcuts keep working
        self.speed_box.bind("<<ComboboxSelected>>", on_speed)
        self.play_ratio = 1.0  # Original samples / samples actually played (used for the playhead position at other speeds)
        self.set_speed(opts["speed"], save=False)

        # --- Drop zone ---
        self.drop = ui_style.drop_zone(parent, DROP_HINT)
        self.drop.pack(fill="x", padx=P)
        self.name, self.thr = "", None
        self.settings_win = None
        self.restart = None  # Set by app.py: restart the program (used when the UI language changes)
        self.drop.bind("<Button-1>", lambda e: self.browse())
        if TkinterDnD:
            for w in (self.drop, parent):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)

        self.big = tk.Label(parent, font=(UI_FONT, 28, "bold"))
        self.big.pack(pady=(16, 4))
        self.info = tk.Label(parent, font=(UI_FONT, 12))
        self.info.pack()
        self.status = tk.Label(parent, font=(UI_FONT, 11), fg=ui_style.OK)
        self.status.pack(pady=(6, 0))
        tk.Label(parent, fg=ui_style.MUTED, font=(UI_FONT, 9),
                 text=tr("空格 下一句 / 停止    R 重听    ← → 上下句    T 显示/隐藏字幕    + - 音量    [ ] 速度")
                 + ("" if self.embedded else tr("    Q 退出")) + tr("\n"
                 "鼠标：点波形/画面重听，点总览跳到那一句；"
                 "在波形/画面上滚轮 = 上下句，在音量上滚轮 = 调音量")
                 ).pack(side="bottom", pady=(4, P))

        # --- Subtitles (only shown when turned on in settings) ---
        # A read-only Text instead of a Label: looks the same, but text can be selected with the mouse and copied with Ctrl+C
        self.sub = tk.Text(parent, font=(ui_style.TEXT_FONT, 15),
                           height=1, wrap="word", relief="flat", borderwidth=0,
                           highlightthickness=0, bg=parent.cget("bg"), cursor="xterm",
                           selectbackground="#cfe0ff", selectforeground="#000",
                           inactiveselectbackground="#cfe0ff", takefocus=False)
        self.sub.tag_configure("center", justify="center")
        self.sub.config(state="disabled")
        self.sub.pack(fill="x", padx=P, pady=(2, 4))
        self.sub.bind("<Configure>", lambda e: self.fit_sub_height())
        self.sub_text = ""
        menu = tk.Menu(root, tearoff=False)
        menu.add_command(label=tr("复制"), command=self.copy_selection)
        menu.add_command(label=tr("复制整句"), command=lambda: self.copy_text(self.sub_text))
        rclick = "<Button-2>" if sys.platform == "darwin" else "<Button-3>"
        self.sub.bind(rclick, lambda e: menu.tk_popup(e.x_root, e.y_root))
        self.sub.bind("<Control-a>", lambda e: (self.sub.tag_add("sel", "1.0", "end-1c"), "break")[1])
        self.path = None
        self.words, self.words_key, self.texts = None, None, []
        self.sub_error = None
        self.sub_gen = 0
        self.revealed = False  # "after" mode: whether the current sentence's subtitle has been revealed (reset when the sentence changes)
        self.hidden = False    # "always" mode: hidden for now with T

        # --- Mouse control buttons (they do the same as the keyboard shortcuts) ---
        bar = self.bar = ttk.Frame(parent)
        bar.pack(pady=(2, 6))
        self.buttons = {}
        for key, text, cmd, width in (
                ("prev", tr("⏮ 上一句"), self.prev, 9),
                ("replay", tr("↻ 重听"), lambda: self.play(self.cur), 8),
                ("main", tr("▶ 播放"), self.on_space, 10),
                ("next", tr("下一句 ⏭"), self.next, 9),
                ("sub", tr("显示字幕"), self.toggle_sub, 9)):
            # takefocus=False: buttons don't take keyboard focus, otherwise Space would also "press" the button
            b = ttk.Button(bar, text=text, width=width, command=cmd, style="Big.TButton",
                           takefocus=False, cursor="hand2")
            b.pack(side="left", padx=3)
            self.buttons[key] = b
        # Subtitle mode dropdown: switch without opening the settings (same option as in the settings)
        self.sub_mode_var = tk.StringVar(value=SUB_MODES[opts["subtitles"]])
        mode_of = {v: k for k, v in SUB_MODES.items()}
        om = ttk.Combobox(bar, textvariable=self.sub_mode_var, values=list(SUB_MODES.values()),
                          state="readonly", width=12, takefocus=False, font=(UI_FONT, 10))

        def on_mode(e):
            self.set_sub_mode(mode_of[self.sub_mode_var.get()])
            om.selection_clear()
            parent.focus_set()  # Give focus back to the page, so Space and other shortcuts keep working
        om.bind("<<ComboboxSelected>>", on_mode)
        om.pack(side="left", padx=(6, 3), ipady=3)

        # Auto next: when a sentence finishes, wait N seconds, then play the next one
        self.auto_job = None   # The root.after job; None = no countdown running
        self.auto_due = 0.0    # When the countdown ends (time.monotonic)
        auto = ttk.Frame(bar)
        auto.pack(side="left", padx=(12, 0))
        self.auto_var = tk.BooleanVar(value=opts["auto_next"])
        ttk.Checkbutton(auto, text=tr("自动下一句"), variable=self.auto_var, takefocus=False,
                        command=self.on_auto_change).pack(side="left")
        self.delay_var = tk.StringVar(value=f"{opts['auto_delay']:g}")
        lo, hi = AUTO_DELAY_RANGE
        self.delay_spin = ttk.Spinbox(auto, from_=lo, to=hi, increment=0.5, width=4,
                                      textvariable=self.delay_var, command=self.on_auto_change)
        self.delay_spin.pack(side="left", padx=(4, 2))
        ttk.Label(auto, text=tr("秒后")).pack(side="left")
        self.delay_spin.bind("<Return>", lambda e: self.on_auto_change(give_back_focus=True))
        self.delay_spin.bind("<FocusOut>", lambda e: self.on_auto_change())

        # --- Video picture (only shown for video files) ---
        self.screen = tk.Canvas(parent, width=640, height=360, bg="black",
                                highlightthickness=0, cursor="hand2")
        self.screen.bind("<Button-1>", lambda e: self.play(self.cur))
        self.screen.bind("<Configure>", lambda e: self.on_screen_resize())
        self.resize_job = None
        self.screen_img = self.screen.create_image(0, 0, anchor="center")

        # --- Waveforms: zoomed current sentence on top, whole-file overview below ---
        self.detail = tk.Canvas(parent, height=140, bg="white", highlightthickness=1,
                                highlightbackground=ui_style.DROP_BORDER, cursor="hand2")
        self.detail.pack(fill="both", expand=True, padx=P, pady=(4, 4))
        self.overview = tk.Canvas(parent, height=68, bg="#fafafa", highlightthickness=1,
                                  highlightbackground=ui_style.DROP_BORDER, cursor="hand2")
        self.overview.pack(fill="x", padx=P)
        for cv in (self.detail, self.overview):
            cv.bind("<Configure>", lambda e: self.draw_waves())
        self.detail.bind("<Button-1>", lambda e: self.play(self.cur))
        self.overview.bind("<Button-1>", self.on_overview_click)

        self.bind_key("<space>", self.on_space)
        for k in ("r", "R", "<Down>"):
            self.bind_key(k, lambda: self.play(self.cur))
        self.bind_key("<Left>", self.prev)
        self.bind_key("<Right>", self.next)

        # --- Mouse wheel: over the waveform/picture = previous/next sentence; over the volume = change volume ---
        self.wheel_last = 0.0
        for w in (self.detail, self.overview, self.screen):
            self.bind_wheel(w, lambda step: self.next() if step > 0 else self.prev())
        for w in (vol, self.vol_label, self.vol_scale):
            self.bind_wheel(w, lambda step: self.set_volume(self.opts["volume"] - 0.1 * step),
                            throttle=0.03)
        for k in ("t", "T"):
            self.bind_key(k, self.toggle_sub)
        for k in ("+", "=", "<KP_Add>"):
            self.bind_key(k, lambda: self.set_volume(self.opts["volume"] + 0.25))
        for k in ("-", "<KP_Subtract>"):
            self.bind_key(k, lambda: self.set_volume(self.opts["volume"] - 0.25))
        self.bind_key("[", lambda: self.step_speed(-1))
        self.bind_key("]", lambda: self.step_speed(+1))
        for w in (spd, self.speed_box):  # Wheel up = one step faster
            self.bind_wheel(w, lambda step: self.step_speed(-step), throttle=0.1)
        if not self.embedded:  # When embedded, the outer program takes care of quitting
            for k in ("q", "Q", "<Escape>"):
                self.bind_key(k, self.quit)
            root.protocol("WM_DELETE_WINDOW", self.quit)

        self.status.config(text=tr("拖入音频文件开始"))
        self.update_buttons()
        self.poll()

    def bind_key(self, key, fn):
        """Shortcuts are bound to the whole window; they are ignored when this isn't the current tab, or while typing in another input box."""
        def handler(e):
            w = e.widget
            typing = (isinstance(w, tk.Misc) and w is not self.sub
                      and w.winfo_class() in ("Entry", "TEntry", "Spinbox", "TSpinbox",
                                              "TCombobox", "Text"))
            if self.active and not typing:
                fn()
        self.root.bind(key, handler, add="+")

    # ---------- Choosing a file ----------
    def on_drop(self, event):
        files = self.root.tk.splitlist(event.data)  # Handles paths that contain spaces
        if files:
            self.open_file(files[0])

    def browse(self):
        path = filedialog.askopenfilename(
            filetypes=[(tr("音频/视频"), " ".join("*" + e for e in AUDIO_EXTS)),
                       (tr("所有文件"), "*.*")])
        if path:
            self.open_file(path)

    def open_file(self, path):
        if self.loading:
            return
        if not os.path.isfile(path):
            messagebox.showerror(tr("错误"), tr("不是文件：\n{path}", path=path))
            return
        self.stop_all()
        self.loading = True
        name = os.path.basename(path)
        self.drop.config(text=tr("{name}\n正在读取并断句……", name=name))
        self.status.config(text=tr("读取中，请稍候"))
        threading.Thread(target=self._load, args=(path,), daemon=True).start()

    def _load(self, path):
        try:
            audio, sr = load_audio(path)
            segs, thr = self.split(audio, sr)
            env = make_envelope(audio, sr)
            err = None if segs else tr("没有检测到句子，试试在设置里调低静音阈值")
            try:
                video = VideoDecoder(path) if has_video(path) else None
            except Exception:  # If the picture can't be read, treat it as audio only
                video = None
        except Exception as ex:  # Decoding failed, etc.
            audio = sr = segs = env = thr = video = None
            err = tr("无法读取：{ex}", ex=ex)
        self.root.after(0, self._loaded, path, audio, sr, segs, err, env, thr, video)

    def _loaded(self, path, audio, sr, segs, err, env=None, thr=None, video=None):
        self.loading = False
        name = os.path.basename(path)
        if err:
            self.drop.config(text=tr("{name}\n（{err}）\n可以再拖一个文件进来", name=name, err=err))
            self.status.config(text="")
            return
        self.audio, self.sr, self.segs = audio, sr, segs
        self.env, self.env_hop = env if env else make_envelope(audio, sr)
        self.cur = 0
        self.played = False
        self.name, self.thr = name, thr
        self.path = path
        self.words, self.words_key, self.texts = None, None, []
        self.revealed = False
        if not self.embedded:
            self.root.title(tr("精听 - {name}", name=name))
        self.set_video(video)
        self.show_file_info()
        self.start_subs()
        self.refresh(tr("按空格开始"))
        self.preview()

    # ---------- Subtitles ----------
    def start_subs(self):
        """When subtitles are on, recognize in the background (read the cache if there is one); show them when done."""
        self.sub_error = None
        model = self.opts["whisper_model"]
        if self.opts["subtitles"] == "off" or not self.path:
            return
        if self.words_key == (self.path, model):
            return  # This file was already recognized with this model
        self.words, self.texts = None, []
        self.sub_gen += 1
        gen, path = self.sub_gen, self.path

        def work():
            try:
                words, err = transcribe_words(path, model), None
            except ImportError as ex:
                words, err = None, str(ex)
            except Exception as ex:
                words, err = None, tr("字幕识别失败：{ex}", ex=ex)
            self.root.after(0, self._subs_done, gen, path, model, words, err)

        threading.Thread(target=work, daemon=True).start()

    def _subs_done(self, gen, path, model, words, err):
        if gen != self.sub_gen:
            return  # The file or model changed meanwhile: throw the result away
        self.sub_error = err
        if words is not None:
            self.words, self.words_key = words, (path, model)
            self.texts = sentence_texts(words, self.segs, self.sr)
        self.update_sub()

    def set_sub(self, text, color):
        """Change the text in the subtitle box (read-only Text: unlock, change, lock again; the user can't type but can select)."""
        if text == self.sub.get("1.0", "end-1c") and color == self.sub.cget("fg"):
            return  # If nothing changed, don't rewrite it, or the user's selection would be lost
        self.sub.config(state="normal", fg=color)
        self.sub.delete("1.0", "end")
        self.sub.insert("1.0", text, "center")
        self.sub.config(state="disabled")
        self.fit_sub_height()

    def fit_sub_height(self):
        """Set the height from the real number of lines after wrapping (1-4 lines)."""
        n = self.sub.count("1.0", "end", "displaylines")
        n = n[0] if isinstance(n, tuple) else n
        self.sub.config(height=max(1, min(4, n or 1)))

    def copy_text(self, text):
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def copy_selection(self):
        try:
            self.copy_text(self.sub.get("sel.first", "sel.last"))
        except tk.TclError:  # Nothing selected
            self.copy_text(self.sub_text)

    def update_sub(self):
        mode = self.opts["subtitles"]
        if mode == "off" or not self.segs:
            text, color = "", "#222"
        elif self.sub_error:
            text, color = self.sub_error, "#c33"
        elif self.words is None:
            text, color = tr("字幕识别中……（{model}，第一次会慢一些）", model=self.opts['whisper_model']), "#888"
        elif mode == "after" and not self.revealed:
            text, color = tr("（听完这句显示字幕，按 T 立即显示）"), "#aaa"
        elif mode == "manual" and not self.revealed:
            text, color = tr("（字幕已隐藏，点“显示字幕”或按 T）"), "#aaa"
        elif mode == "always" and self.hidden:
            text, color = tr("（字幕已隐藏，按 T 显示）"), "#aaa"
        else:
            text, color = self.texts[self.cur] or tr("（这句没识别出文字）"), "#222"
        # Only a real subtitle is used for "复制整句" (copy whole sentence); hint text is not
        self.sub_text = self.texts[self.cur] if color == "#222" and self.texts else ""
        self.set_sub(text, color)
        # When subtitles are off, collapse this row so it leaves no gap
        if mode == "off" and self.sub.winfo_manager():
            self.sub.pack_forget()
        elif mode != "off" and not self.sub.winfo_manager():
            self.sub.pack(before=self.bar, fill="x", padx=ui_style.PAD, pady=(2, 4))
        self.update_buttons()

    def toggle_sub(self):
        """T key / subtitle button: in "always" mode hide/show everything;
        in "after" and "manual" modes reveal/cover the current sentence."""
        if self.opts["subtitles"] == "always":
            self.hidden = not self.hidden
        else:
            self.revealed = not self.revealed
        self.update_sub()

    def sub_shown(self):
        mode = self.opts["subtitles"]
        return ((mode == "always" and not self.hidden)
                or (mode in ("after", "manual") and self.revealed))

    def set_sub_mode(self, mode):
        """Subtitle mode changed in the main window dropdown: takes effect right away and is saved; starts recognition if needed."""
        if mode == self.opts["subtitles"]:
            return
        self.opts["subtitles"] = mode
        self.revealed = self.hidden = False
        save_settings(self.opts)
        self.start_subs()
        self.update_sub()

    def set_video(self, video):
        """For video, put the picture area above the waveforms and make the waveforms and big title smaller; for audio only, restore the layout."""
        self.video = video
        self.photo = None
        self.screen.itemconfig(self.screen_img, image="")
        if video:
            if not self.screen.winfo_ismapped():
                self.screen.pack(before=self.detail, fill="both", expand=True,
                                 padx=ui_style.PAD, pady=(4, 4))
            self.detail.config(height=90)
            self.detail.pack_configure(expand=False)
            self.big.config(font=(UI_FONT, 18, "bold"))
            self.big.pack_configure(pady=(8, 2))
            if self.root.winfo_height() < 820:
                self.root.geometry(f"{max(self.root.winfo_width(), 960)}x860")
        else:
            self.screen.pack_forget()
            self.detail.config(height=140)
            self.detail.pack_configure(expand=True)
            self.big.config(font=(UI_FONT, 28, "bold"))
            self.big.pack_configure(pady=(16, 4))

    def split(self, audio, sr):
        o = self.opts
        return split_sentences(audio, sr, o["threshold"], o["min_silence"],
                               pad=o["pad"], max_len=o["max_len"])

    def show_file_info(self):
        how = tr("自动") if self.opts["threshold"] is None else tr("手动")
        thr_txt = tr("，静音阈值 {thr:.0f} dB（{how}）", thr=self.thr, how=how) if self.thr is not None else ""
        self.drop.config(text=tr("{name}\n共 {n} 句{thr}（拖入其他文件可替换）", name=self.name, n=len(self.segs), thr=thr_txt))

    def resplit(self):
        """After the settings change, split again with the new options; try to stay where you were listening."""
        if self.audio is None:
            return
        segs, thr = self.split(self.audio, self.sr)
        if not segs:
            messagebox.showwarning(tr("设置"), tr("用这组参数没有检测到句子，试试调低静音阈值"))
            return
        self.stop_all()
        pos = self.segs[self.cur][0]
        self.segs, self.thr = segs, thr
        self.cur = max(i for i, (s, e) in enumerate(segs) if s <= pos or i == 0)
        self.revealed = False
        if self.words is not None:  # No need to recognize the subtitles again; just reassign them to the new sentences
            self.texts = sentence_texts(self.words, segs, self.sr)
        self.show_file_info()
        self.refresh(tr("已按新设置重新断句"))
        self.preview()

    # ---------- Settings window ----------
    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        o = self.opts
        win = self.settings_win = tk.Toplevel(self.root)
        win.title(tr("设置"))
        win.resizable(False, False)
        win.transient(self.root)
        win.configure(bg=ttk.Style(win).lookup("TFrame", "background") or win.cget("bg"))

        auto = tk.BooleanVar(value=o["threshold"] is None)
        manual_thr = o["threshold"] if o["threshold"] is not None else round(self.thr or -30)
        vars_ = {
            "threshold": tk.DoubleVar(value=manual_thr),
            "min_silence": tk.DoubleVar(value=o["min_silence"]),
            "max_len": tk.DoubleVar(value=o["max_len"]),
            "pad": tk.DoubleVar(value=o["pad"]),
        }
        rows = [  # (key, title, min, max, step, explanation)
            ("threshold", tr("静音阈值 (dB)"), -60, -5, 1,
             tr("呼吸声被当成说话 → 调高（如 -25）；吞字 → 调低（如 -40）")),
            ("min_silence", tr("断句静音 (秒)"), 0.1, 1.5, 0.05,
             tr("多长的静音算一句结束；调小断得更细")),
            ("max_len", tr("最长一句 (秒)"), 0, 30, 0.5,
             tr("超过就在句内最长停顿处再切；0 = 不再切")),
            ("pad", tr("前后留白 (秒)"), 0, 0.5, 0.05,
             tr("每句前后多播一点，防止吞音")),
        ]
        # --- UI language (label in both languages so it can always be found) ---
        lang_frm = ttk.Frame(win, padding=(16, 12, 16, 0))
        lang_frm.pack(fill="x")
        lang_of = {v: k for k, v in i18n.LANGUAGES.items()}
        lang_var = tk.StringVar(value=i18n.LANGUAGES.get(o["language"], i18n.LANGUAGES["en"]))
        ttk.Label(lang_frm, text=tr("语言 / Language")).pack(side="left", padx=(0, 10))
        ttk.Combobox(lang_frm, textvariable=lang_var, values=list(i18n.LANGUAGES.values()),
                     state="readonly", width=10).pack(side="left")
        ttk.Label(lang_frm, text=tr("（重启后生效）"), style="Hint.TLabel").pack(side="left", padx=8)

        frm = ttk.Frame(win, padding=(16, 4, 16, 4))
        frm.pack(fill="both")
        spins = {}
        r = 0
        for key, label, lo, hi, step, hint in rows:
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=(6, 0), padx=(0, 10))
            sp = ttk.Spinbox(frm, from_=lo, to=hi, increment=step, width=8,
                             textvariable=vars_[key])
            sp.grid(row=r, column=1, sticky="w", pady=(6, 0))
            spins[key] = sp
            if key == "threshold":
                ttk.Checkbutton(frm, text=tr("按本底噪声自动"), variable=auto,
                                command=lambda: sync_auto()).grid(row=r, column=2, sticky="w",
                                                                  padx=(10, 0))
            ttk.Label(frm, text=hint, style="Hint.TLabel").grid(
                row=r + 1, column=0, columnspan=3, sticky="w")
            r += 2

        # --- Subtitles ---
        ttk.Separator(frm).grid(row=r, column=0, columnspan=3, sticky="we", pady=(12, 4))
        label_of = SUB_MODES
        key_of = {v: k for k, v in label_of.items()}
        sub_mode = tk.StringVar(value=label_of.get(o["subtitles"], label_of["off"]))
        sub_model = tk.StringVar(value=o["whisper_model"])
        ttk.Label(frm, text=tr("字幕")).grid(row=r + 1, column=0, sticky="w", pady=(6, 0))
        om = ttk.Combobox(frm, textvariable=sub_mode, values=list(label_of.values()),
                          state="readonly", width=14)
        om.grid(row=r + 1, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(frm, text=tr("识别模型")).grid(row=r + 2, column=0, sticky="w", pady=(6, 0))
        om = ttk.Combobox(frm, textvariable=sub_model, values=WHISPER_MODELS,
                          state="readonly", width=14)
        om.grid(row=r + 2, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(frm, style="Hint.TLabel", justify="left",
                 text=tr("转录页和精听字幕共用这个模型；字幕每个文件只识别一次（结果会缓存）\n"
                      "模型越大越准越慢：tiny / base 快，small 平衡，medium / large 准")).grid(
            row=r + 3, column=0, columnspan=3, sticky="w")

        # --- Audio devices ---
        ttk.Separator(frm).grid(row=r + 4, column=0, columnspan=3, sticky="we", pady=(12, 4))
        mic = tk.StringVar(value=o["mic"] or SYSTEM_DEVICE)
        speaker = tk.StringVar(value=o["speaker"] or SYSTEM_DEVICE)
        dev_boxes = {}
        for i, (kind, label, var) in enumerate((("input", tr("麦克风"), mic),
                                                ("output", tr("扬声器"), speaker))):
            ttk.Label(frm, text=label).grid(row=r + 5 + i, column=0, sticky="w", pady=(6, 0))
            dev_boxes[kind] = ttk.Combobox(frm, textvariable=var, state="readonly", width=44)
            dev_boxes[kind].grid(row=r + 5 + i, column=1, columnspan=2, sticky="w", pady=(6, 0))
        hint = ttk.Frame(frm)
        hint.grid(row=r + 7, column=0, columnspan=3, sticky="we")
        ttk.Label(hint, style="Hint.TLabel",
                  text=tr("录音、两页的播放都用这里选的设备；刚插上耳机就点「刷新」")).pack(side="left")
        ttk.Button(hint, text=tr("刷新"), width=6,
                   command=lambda: load_devices()).pack(side="right", pady=(4, 0))

        def load_devices():
            for kind, var in (("input", mic), ("output", speaker)):
                try:
                    names = list(audio_devices(kind))
                except Exception:
                    names = []
                dev_boxes[kind]["values"] = [SYSTEM_DEVICE] + names
                if var.get() not in names:  # The device chosen before is not plugged in now
                    var.set(SYSTEM_DEVICE)

        def sync_auto():
            spins["threshold"].config(state="disabled" if auto.get() else "normal")

        def read_form():
            try:
                s = {k: float(v.get()) for k, v in vars_.items()}
            except (tk.TclError, ValueError):
                messagebox.showerror(tr("设置"), tr("请填数字"), parent=win)
                return None
            if auto.get():
                s["threshold"] = None
            s["subtitles"] = key_of[sub_mode.get()]
            s["whisper_model"] = sub_model.get()
            for k, var in (("mic", mic), ("speaker", speaker)):
                s[k] = None if var.get() == SYSTEM_DEVICE else var.get()
            s["language"] = lang_of[lang_var.get()]
            return s

        def apply():
            s = read_form()
            if s is None:
                return
            self.opts.update(s)
            save_settings(self.opts)
            if s["language"] != i18n.LANG:  # Labels are built at start, so a new language needs a restart
                if self.restart is None:
                    messagebox.showinfo(tr("设置"), tr("界面语言改好了，下次打开程序时生效。"), parent=win)
                elif messagebox.askyesno(tr("设置"), tr("界面语言改好了，重启程序后生效。现在重启吗？"),
                                         parent=win):
                    self.restart()
                    return
            apply_audio_devices(self.opts)
            self.resplit()
            self.start_subs()
            self.update_sub()
            if self.thr is not None and auto.get():  # Show the automatically computed threshold, for reference
                vars_["threshold"].set(round(self.thr))

        def reset():
            auto.set(DEFAULTS["threshold"] is None)
            for k, v in vars_.items():
                if DEFAULTS[k] is not None:
                    v.set(DEFAULTS[k])
            sub_mode.set(label_of[DEFAULTS["subtitles"]])
            sub_model.set(DEFAULTS["whisper_model"])
            mic.set(SYSTEM_DEVICE)
            speaker.set(SYSTEM_DEVICE)
            sync_auto()

        btns = ttk.Frame(win, padding=(0, 10, 0, 12))
        btns.pack()
        for text, cmd in ((tr("应用并保存"), apply), (tr("恢复默认"), reset), (tr("关闭##button"), win.destroy)):
            ttk.Button(btns, text=text, width=10, command=cmd).pack(side="left", padx=4)
        win.bind("<Return>", lambda e: apply())
        win.bind("<Escape>", lambda e: win.destroy())
        sync_auto()
        load_devices()

    # ---------- Playback ----------
    # ---------- Auto next ----------
    def on_auto_change(self, give_back_focus=False):
        """The checkbox / seconds changed: check the seconds and save settings; when turned off, cancel a running countdown."""
        lo, hi = AUTO_DELAY_RANGE
        try:
            delay = min(hi, max(lo, float(self.delay_var.get())))
        except ValueError:
            delay = self.opts["auto_delay"]  # Not a number: restore the old value
        self.delay_var.set(f"{delay:g}")
        changed = (self.opts["auto_next"], self.opts["auto_delay"]) != (self.auto_var.get(), delay)
        self.opts["auto_next"], self.opts["auto_delay"] = self.auto_var.get(), delay
        if changed:
            save_settings(self.opts)
        if not self.opts["auto_next"] and self.auto_job:
            self.cancel_auto()
            self.refresh(tr("⏸ 已暂停，按空格继续"))
        if give_back_focus:
            self.bar.master.focus_set()  # Give focus back to the page, so Space and other shortcuts keep working

    def schedule_auto(self):
        """Called just after the current sentence finishes: if auto next is on and this isn't the last sentence, start the countdown."""
        if not self.opts["auto_next"] or self.cur >= len(self.segs) - 1:
            return False
        delay = self.opts["auto_delay"]
        self.auto_due = time.monotonic() + delay
        self.auto_job = self.root.after(int(delay * 1000), self._auto_advance)
        return True

    def cancel_auto(self):
        if self.auto_job:
            self.root.after_cancel(self.auto_job)
            self.auto_job = None

    def _auto_advance(self):
        self.auto_job = None
        if self.active and self.segs and self.cur < len(self.segs) - 1:
            self.play(self.cur + 1)

    def on_space(self):
        if not self.segs:
            return
        if self.playing or self.pending or self.auto_job:
            self.stop_all()  # Space during the countdown = cancel auto play and stay on this sentence
            self.refresh(tr("已停止"))
        elif not self.played:
            self.play(self.cur)
        elif self.cur < len(self.segs) - 1:
            self.play(self.cur + 1)
        else:
            self.refresh(tr("已经是最后一句（← 返回，R 重听）"))

    def play(self, idx):
        if not self.segs:
            return
        self.stop_all()
        if idx != self.cur:
            self.revealed = False  # A different sentence: cover the subtitle again (not when replaying the same one)
        self.cur = idx
        self.played = True
        if self.video:
            # First let the picture decode up to the start of this sentence (wait at most 1.5 s), then start both together, so the sound doesn't run ahead
            s, e = self.segs[idx]
            self.video.start(s / self.sr, e / self.sr, self.screen_box())
            self.pending = (idx, time.monotonic() + 1.5)
            self.refresh(tr("▶ 准备画面……"))
        else:
            self.start_audio()

    def prev(self):
        self.play(max(0, self.cur - 1))

    def next(self):
        self.play(min(len(self.segs) - 1, self.cur + 1))

    def bind_wheel(self, widget, fn, throttle=0.15):
        """Wheel up calls fn(-1), wheel down fn(+1). Counted only once per throttle seconds, so one trackpad swipe doesn't skip several sentences."""
        def handler(e):
            now = time.monotonic()
            if now - self.wheel_last < throttle:
                return "break"
            d = {4: 1, 5: -1}.get(e.num, e.delta)  # Linux uses Button-4/5, other systems use delta
            if d:
                self.wheel_last = now
                fn(-1 if d > 0 else 1)
            return "break"
        widget.bind("<MouseWheel>", handler)
        if sys.platform.startswith("linux"):
            widget.bind("<Button-4>", handler)
            widget.bind("<Button-5>", handler)

    def update_buttons(self):
        if self.playing or self.pending or self.auto_job:
            main = tr("■ 停止")
        elif not self.played:
            main = tr("▶ 开始")
        else:
            main = tr("▶ 继续")  # Same as Space: play the next sentence
        b = self.buttons
        b["main"].config(text=main)
        has = "normal" if self.segs else "disabled"
        for k in ("main", "replay"):
            b[k].config(state=has)
        b["prev"].config(state="normal" if self.segs and self.cur > 0 else "disabled")
        b["next"].config(state="normal" if self.segs and self.cur < len(self.segs) - 1
                         else "disabled")
        b["sub"].config(state="disabled" if self.opts["subtitles"] == "off" or not self.segs
                        else "normal",
                        text=tr("隐藏字幕") if self.sub_shown() else tr("显示字幕"))
        self.sub_mode_var.set(SUB_MODES[self.opts["subtitles"]])

    def set_volume(self, v, save=True):
        """Change the volume: takes effect on the next playback; saved after a short pause, so dragging the slider doesn't keep writing the file."""
        v = round(min(2.5, max(0.5, v)), 2)  # Higher values would only limit harder, not get louder
        self.opts["volume"] = v
        self.vol_var.set(round(v * 100))
        self.vol_label.config(text=tr("音量 {v:.0f}%", v=v * 100))
        if save:
            if self.vol_save_job:
                self.root.after_cancel(self.vol_save_job)
            self.vol_save_job = self.root.after(800, lambda: save_settings(self.opts))

    def set_speed(self, v, save=True):
        """Change the playback speed. While playing, replay this sentence from the start at the new speed, so you hear the difference right away."""
        v = min(SPEEDS, key=lambda x: abs(x - v))  # Snap to the nearest step
        self.opts["speed"] = v
        self.speed_var.set(speed_label(v))
        if save:
            if getattr(self, "speed_save_job", None):
                self.root.after_cancel(self.speed_save_job)
            self.speed_save_job = self.root.after(800, lambda: save_settings(self.opts))
            if getattr(self, "playing", False) or getattr(self, "pending", None):
                self.play(self.cur)

    def step_speed(self, step):
        i = SPEEDS.index(self.opts["speed"]) + step
        if 0 <= i < len(SPEEDS):
            self.set_speed(SPEEDS[i])

    def playing_msg(self):
        v = self.opts["speed"]
        return tr("▶ 播放中") if v == 1.0 else tr("▶ 播放中（{s}）", s=speed_label(v))

    def start_audio(self):
        self.pending = None
        s, e = self.segs[self.cur]
        data = self.audio[s:e]
        try:
            data = stretch(data, self.sr, self.opts["speed"])
        except Exception:  # If changing the speed fails, play at normal speed; listening still works
            audio_io.log.exception("Speed change failed; playing at normal speed")
        self.play_ratio = (e - s) / max(1, len(data))
        try:
            # On error, re-initialize audio and try once more (see audio_io)
            audio_io.call(sd.play, boost(data, self.sr, self.opts["volume"]), self.sr)
        except audio_io.AudioError as ex:
            self.playing = False
            if self.video:
                self.video.cancel()
            self.show_error(str(ex))
            return
        self.t0 = time.monotonic()
        self.playing = True
        self.refresh(self.playing_msg())

    def show_error(self, msg):
        """Show an error in the status line (red text); the next refresh restores the normal color."""
        if self.segs:
            self.refresh(msg)
        self.status.config(text=msg, fg="#c33")

    def stop_all(self):
        try:
            sd.stop()
        except Exception:  # Stopping must work even if the device is gone: reset the state anyway; the next playback re-initializes
            audio_io.log.exception("Error while stopping playback")
        self.playing = False
        self.pending = None
        self.cancel_auto()
        if self.video:
            self.video.cancel()

    def preview(self):
        """While paused, show the first frame of the current sentence."""
        if self.video and self.segs:
            self.root.update_idletasks()  # The picture area has just appeared: let it lay out first so we know its size
            t = self.segs[self.cur][0] / self.sr
            self.video.start(t, t, self.screen_box())

    def poll(self):
        # Whatever goes wrong in this round, always schedule the next one - before, one error stopped this loop for good,
        # after which the playback state never updated again, and it looked like the buttons did nothing
        try:
            self._poll_once()
        except Exception:
            audio_io.log.exception("Error in poll")
        self.root.after(15 if self.video else 30, self.poll)

    def stream_active(self):
        try:
            stream = sd.get_stream()
            return stream is not None and stream.active
        except Exception:  # The stream's device is gone: treat it as finished
            audio_io.log.exception("Error while checking playback state")
            return False

    def _poll_once(self):
        if self.pending and (self.video.frames or time.monotonic() > self.pending[1]):
            self.start_audio()
        if self.playing:
            if not self.stream_active():
                self.playing = False
                if self.video:
                    self.video.cancel()  # The picture stays on the last frame shown
                if self.opts["subtitles"] == "after":
                    self.revealed = True  # Finished listening: reveal the subtitle (manual mode doesn't reveal by itself)
                if self.schedule_auto():
                    self.refresh(self.auto_msg())
                elif self.opts["auto_next"] and self.cur == len(self.segs) - 1:
                    self.refresh(tr("✓ 已播完最后一句"))
                else:
                    self.refresh(tr("⏸ 已暂停，按空格继续"))
            else:
                s, e = self.segs[self.cur]
                pos = min(e, s + (time.monotonic() - self.t0) * self.sr * self.play_ratio)  # At other speeds, scale back to the position in the original audio
                self.move_playhead(pos)
                if self.video:
                    self.show_due_frame(pos / self.sr)
        elif self.video and self.video.frames and not self.pending:
            self.show_frame(self.video.frames.pop()[1])  # Preview frame
            self.video.frames.clear()
        if self.auto_job and not self.playing:
            self.status.config(text=self.auto_msg())  # Countdown: update the seconds left each time

    def auto_msg(self):
        left = max(0.0, self.auto_due - time.monotonic())
        return tr("⏸ {left:.1f} 秒后自动播放下一句（空格 = 取消）", left=left)

    # ---------- Picture ----------
    def show_due_frame(self, now):
        """Show frames by the sound's clock: take out all frames whose time has come and show only the newest (drop frames when behind)."""
        q, img = self.video.frames, None
        while q and q[0][0] <= now:
            img = q.popleft()[1]
        if img is not None:
            self.show_frame(img)

    def show_frame(self, img):
        self.photo = ImageTk.PhotoImage(img)
        self.screen.itemconfig(self.screen_img, image=self.photo)
        self.center_frame()

    def on_screen_resize(self):
        """The window size changed: while paused, get a frame at the new size (after a short wait, so dragging the window doesn't decode like mad)."""
        self.center_frame()
        if self.resize_job:
            self.root.after_cancel(self.resize_job)
        if not (self.playing or self.pending):
            self.resize_job = self.root.after(200, self.preview)

    def center_frame(self):
        self.screen.coords(self.screen_img, self.screen.winfo_width() / 2,
                           self.screen.winfo_height() / 2)

    def screen_box(self):
        return max(64, self.screen.winfo_width()), max(36, self.screen.winfo_height())

    def refresh(self, msg):
        s, e = self.segs[self.cur]
        self.big.config(text=tr("第 {i} / {n} 句", i=self.cur + 1, n=len(self.segs)))
        self.info.config(text=tr("{a} – {b}（{d:.1f} 秒）", a=fmt(s / self.sr), b=fmt(e / self.sr), d=(e - s) / self.sr))
        self.status.config(text=msg, fg=ui_style.OK)
        self.update_sub()
        self.update_buttons()
        self.draw_waves()

    # ---------- Waveforms ----------
    def draw_waves(self):
        if self.env is None or not self.segs:
            return
        hop, total = self.env_hop, len(self.audio)
        s, e = self.segs[self.cur]

        # Zoomed current sentence: show 0.3 s more on each side, to see clearly where the cut is
        cv = self.detail
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w > 2:
            margin = int(0.3 * self.sr)
            self.view = (max(0, s - margin), min(total, e + margin))
            a, b = self.view
            mid = h / 2
            pk = column_peaks(self.env, a // hop, b // hop, w)
            xs, xe = self.to_x(s, w, *self.view), self.to_x(e, w, *self.view)
            cv.create_rectangle(xs, 0, xe, h, fill="#eef4ff", outline="")
            for x, v in enumerate(pk):
                color = "#2a6fdb" if xs <= x <= xe else "#c4c4c4"
                y = v * (mid - 6)
                cv.create_line(x, mid - y, x, mid + y + 1, fill=color)
            cv.create_text(6, 4, anchor="nw", fill="#888", font=(UI_FONT, 9),
                           text=fmt(a / self.sr))
            cv.create_text(w - 6, 4, anchor="ne", fill="#888", font=(UI_FONT, 9),
                           text=fmt(b / self.sr))
            self.detail_head = cv.create_line(-5, 0, -5, h, fill="#e5484d", width=2)

        # Whole-file overview: a light block for each sentence; current sentence = dark block + blue frame + blue waveform + a ▼ on top, easy to spot even with many sentences
        cv = self.overview
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w > 2:
            top = 9  # Height kept free at the top for the ▼
            mid = (top + h) / 2
            for a, b in self.segs:
                cv.create_rectangle(self.to_x(a, w, 0, total), top, self.to_x(b, w, 0, total),
                                    h - 2, outline="", fill="#e1ecff")
            xs, xe = self.to_x(s, w, 0, total), self.to_x(e, w, 0, total)
            if xe - xs < 8:  # For very short sentences the frame is at least 8 px wide, or it can't be seen
                c = (xs + xe) / 2
                xs, xe = c - 4, c + 4
            cv.create_rectangle(xs, top, xe, h - 2, fill="#a9c8ff", outline="#2a6fdb", width=2)
            pk = column_peaks(self.env, 0, len(self.env), w)
            for x, v in enumerate(pk):
                y = v * (h - 2 - mid - 2)
                cv.create_line(x, mid - y, x, mid + y + 1,
                               fill="#1f5fd0" if xs <= x <= xe else "#7d8696")
            c = min(max((xs + xe) / 2, 6), w - 6)  # Keep the ▼ from being cut off at the edge for the first / last sentence
            cv.create_polygon(c - 6, 0, c + 6, 0, c, top - 1, fill="#2a6fdb", outline="")
            self.overview_head = cv.create_line(-5, 0, -5, h, fill="#e5484d", width=2)

    @staticmethod
    def to_x(sample, width, a, b):
        return (sample - a) / max(1, b - a) * width

    def move_playhead(self, pos):
        for cv, item, view in ((self.detail, "detail_head", self.view),
                               (self.overview, "overview_head", (0, len(self.audio)))):
            if hasattr(self, item):
                x = self.to_x(pos, cv.winfo_width(), *view)
                cv.coords(getattr(self, item), x, 0, x, cv.winfo_height())

    def on_overview_click(self, event):
        if not self.segs:
            return
        pos = event.x / max(1, self.overview.winfo_width()) * len(self.audio)
        # Click inside a sentence selects it; click in a silence selects the nearest sentence
        dist = [0 if a <= pos <= b else min(abs(pos - a), abs(pos - b))
                for a, b in self.segs]
        self.play(dist.index(min(dist)))

    def quit(self):
        self.stop_all()
        self.root.destroy()
