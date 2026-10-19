"""
精听页（app.py 的「精听」标签）：自动按静音断句，每句播完自动暂停。
这个文件只是模块，程序从 app.py 启动：python app.py

把音频或视频文件拖进窗口（或点拖放区选择文件）即可开始。
窗口里会显示当前句的放大波形和整段总览，点总览可以跳到那一句。
视频文件会同时显示画面，每句的画面和声音同步播放、同步暂停。

按键：
  空格        播放下一句（播放中按空格 = 停止）
  R / ↓       重听当前句
  ← / →       上一句 / 下一句
  T           显示 / 隐藏字幕
  + / -       音量大 / 小（超过 100% 自动限幅，不爆音）

依赖：pip install av numpy sounddevice pillow tkinterdnd2
  字幕是可选功能（在"⚙ 设置"里打开），另需：pip install faster-whisper
  （也可以用 openai-whisper；有 NVIDIA 显卡、装了带 CUDA 的 torch 时自动用 GPU；
   识别结果有缓存，每个文件只识别一次）

断句参数点右上角"⚙ 设置"修改，改完立即重新断句，
并保存到 settings.json（位置见 app_paths.py），下次打开自动沿用。
命令行参数（--threshold / --min-silence / --max-len / --pad）见 app.py。
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
import ui_style

import av
import numpy as np
import sounddevice as sd
from PIL import ImageTk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # 没装 tkinterdnd2 时退回到只能点按钮选文件
    TkinterDnD = None

AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma",
              ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".wmv",
              ".ts", ".mpg", ".mpeg", ".3gp")
DROP_HINT = ("把音频或视频文件拖到这里（或点这里选择），自动断句后逐句精听" if TkinterDnD
             else "点击这里选择音频或视频文件")
UI_FONT = ui_style.FONT

# ---------- 设置（settings.json，位置见 app_paths.py） ----------
SETTINGS_FILE = app_paths.SETTINGS_FILE
DEFAULTS = {
    "threshold": None,    # 静音阈值 dB（相对峰值）；None = 按本底噪声自动定
    "min_silence": 0.35,  # 多长的静音算断句（秒）
    "max_len": 6.0,       # 超过这么长（秒）的句子再切；0 = 不再切
    "pad": 0.15,          # 每句前后多留的时间（秒）
    "volume": 2.0,        # 音量倍数；超过 1 时自动限幅，不会爆音
    "subtitles": "off",   # 字幕：off 关闭 / always 一直显示 / after 听完再显示
    "whisper_model": "small.en",
    "mic": None,          # 麦克风 / 扬声器的名字；None = 跟随系统默认
    "speaker": None,
    "auto_next": False,   # 一句播完后自动播放下一句
    "auto_delay": 3.0,    # 自动播放下一句前等几秒
}
AUTO_DELAY_RANGE = (0.0, 30.0)
SYSTEM_DEVICE = "跟随系统默认"
SUB_MODES = {"off": "关闭", "always": "一直显示", "after": "听完一句再显示",
             "manual": "手动显示"}  # manual：每句都先藏着，点按钮 / 按 T 才显示
# 识别模型：转录页和精听字幕共用一个（settings.json 里的 whisper_model），
# 加载好的模型也共用 _whisper_models，同一个模型只加载一次
WHISPER_MODELS = ["tiny.en", "base.en", "small.en", "medium.en", "turbo", "large-v3"]
MODEL_LOCK = threading.Lock()  # 同一时间只让一边用模型（openai-whisper 不能两个线程同时用）


def load_settings():
    app_paths.migrate_settings()  # 旧的 jingting_settings.json 第一次会自动改名搬过来
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass  # 没有文件或文件坏了就用默认值
    return s


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except OSError as ex:
        messagebox.showwarning("设置", f"设置没能保存：\n{ex}")


def audio_devices(kind):
    """kind = "input" / "output"，返回 {名字: 设备号}。
    Windows 上同一个设备在 MME / DirectSound / WASAPI 下各出现一次，只留默认的那套；
    MME 的名字最多 31 个字，从别的那套里借完整名字。"""
    key = f"max_{kind}_channels"
    api = sd.default.hostapi
    devs = [d for d in sd.query_devices() if d[key] > 0]
    full = [d["name"] for d in devs if d["hostapi"] != api]
    out = {}
    for d in devs:
        if d["hostapi"] != api or "Sound Mapper" in d["name"]:  # Mapper 就是系统默认
            continue
        name = next((f for f in full if f.startswith(d["name"])), d["name"])
        out.setdefault(name, d["index"])
    return out


def apply_audio_devices(opts):
    """把设置里选的麦克风 / 扬声器设成 sounddevice 的默认设备，录音和两页的播放都会用它。
    选的设备拔掉了就退回系统默认。"""
    try:
        ins, outs = audio_devices("input"), audio_devices("output")
    except Exception:  # 没有声卡之类，就全用系统默认
        ins, outs = {}, {}
    sd.default.device = (ins.get(opts["mic"]), outs.get(opts["speaker"]))


# ---------- 读取音频 ----------
def load_audio(path, layout="stereo", rate=None):
    """解码成 float32 数组 (采样数, 声道数)；rate 不填就用原采样率。"""
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


# ---------- 静音检测切句 ----------
def noise_threshold(db, ratio=0.5):
    """按本底噪声定阈值：取底噪（最安静 5% 的帧）和语音（最响 10% 的帧）之间的位置。
    ratio=0.5 即两者正中间；呼吸声、底噪一般低于这里，正常说话高于这里。"""
    noise = np.percentile(db, 5)
    speech = np.percentile(db, 90)
    return noise + ratio * (speech - noise)


def split_long(db, thr, s, e, max_f, min_piece):
    """超长句在句内最长的停顿处切开，递归直到每段都不超过 max_f 帧。
    停顿 = 连续低于静音阈值的帧；一样长时选更靠近句子中间的。"""
    if e - s <= max_f:
        return [(s, e)]
    lo, hi = s + min_piece, e - min_piece
    if hi <= lo:
        return [(s, e)]
    quiet = np.concatenate([[False], db[s:e] < thr, [False]])
    edges = np.flatnonzero(np.diff(quiet.astype(np.int8)))
    mid = (s + e) / 2
    best = None
    for a, b in zip(edges[::2] + s, edges[1::2] + s):  # 每段停顿 [a, b)
        c = (a + b) // 2
        if lo <= c <= hi:
            key = (b - a, -abs(c - mid))
            if best is None or key > best[0]:
                best = (key, c)
    if best is not None:
        cut = int(best[1])
    else:  # 句内完全没有停顿：退而求其次，切在 0.2 秒内最安静的地方
        sm = np.convolve(db[s:e], np.ones(20) / 20, mode="same")
        cut = lo + int(np.argmin(sm[lo - s:hi - s]))
    return (split_long(db, thr, s, cut, max_f, min_piece)
            + split_long(db, thr, cut, e, max_f, min_piece))


def split_sentences(audio, sr, threshold_db=None, min_silence=0.35,
                    min_len=0.4, pad=0.15, max_len=6.0):
    """返回 (句子列表[(起, 止) 采样点], 实际使用的阈值 dB)。
    threshold_db=None 时按本底噪声自动定阈值；max_len>0 时超长句再切。"""
    mono = audio.mean(axis=1)
    hop = int(sr * 0.01)  # 10ms 一帧
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
        pieces = (split_long(db, threshold_db, s, e, int(max_len / 0.01), 150)  # 每段至少 1.5 秒
                  if max_len > 0 else [(s, e)])
        result += [(a * hop, min(len(mono), b * hop)) for a, b in pieces]
    return result, threshold_db


# ---------- 音量 ----------
def boost(x, sr, gain, ceiling=0.95):
    """放大 gain 倍，并用限幅器压住峰值：响的地方自动收一点，不会爆音。
    做法：每 5ms 一块算出"不超过 ceiling 需要的增益"，向前后各看 20ms 取最小值
    （提前收、慢慢放），再平滑后逐个采样乘上去。"""
    if gain <= 1.0:
        return x * gain
    y = x * gain
    blk = max(1, int(sr * 0.005))
    n = -(-len(y) // blk)
    peak = np.abs(np.pad(y, ((0, n * blk - len(y)), (0, 0)))).reshape(n, -1).max(axis=1)
    g = np.minimum(1.0, ceiling / np.maximum(peak, 1e-9))
    r = 4  # ±4 块 = ±20ms 取最小
    gp = np.pad(g, r, mode="edge")
    g = np.min([gp[i:i + n] for i in range(2 * r + 1)], axis=0)
    g = np.convolve(np.pad(g, 2, mode="edge"), np.ones(5) / 5, mode="valid")  # ±10ms 平滑
    g = np.interp(np.arange(len(y)), np.arange(n) * blk + blk / 2, g)
    return np.clip(y * g[:, None], -ceiling, ceiling).astype(np.float32)


def fmt(sec):
    return f"{int(sec // 60):02d}:{sec % 60:04.1f}"


# ---------- 波形 ----------
ENV_SEC = 0.001  # 包络精度：每 1ms 取一个峰值


def make_envelope(audio, sr):
    """把音频压成每 ENV_SEC 一个峰值（0~1），画波形时只用这个，不碰原始数据。"""
    hop = max(1, int(sr * ENV_SEC))
    peak = np.abs(audio).max(axis=1)
    n = len(peak) // hop
    env = peak[: n * hop].reshape(n, hop).max(axis=1)
    return env / (env.max() + 1e-10), hop


def column_peaks(env, a, b, width):
    """env[a:b] 均分成 width 列，每列取最大值。"""
    b = max(b, a + 1)
    idx = np.linspace(a, b, width + 1).astype(int)[:-1]
    return np.maximum.reduceat(env[:b], idx)


# ---------- 字幕（可选，需要 pip install faster-whisper 或 openai-whisper） ----------
CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache"),
                         "jingting", "subs")
_whisper_models = {}

# 优先用 faster-whisper（CTranslate2）：CPU 上比 openai-whisper 快（算词时间戳快 2~3 倍），
# 也不用装 1GB+ 的 torch。有 NVIDIA 显卡、又装了带 CUDA 的 torch 时还是让 openai-whisper 用 GPU。
# 这段代码本进程用，也拼在交给外部 Python 跑的脚本前面（所以只能用标准库）。
FW_LOADER = r'''
import os, platform, sys
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # Windows 没开开发者模式时的提示

def load_fw(name):
    """faster-whisper 模型；没装、或者应该交给 openai-whisper 用 GPU 时抛 ImportError。"""
    try:
        import torch
        gpu = torch.cuda.is_available()
    except Exception:  # 没装 torch，或者 torch 坏了（ARM 机器上常见）
        gpu = False
    if gpu:
        raise ImportError("用 openai-whisper + GPU")
    from faster_whisper import WhisperModel
    # Windows ARM 上的 CTranslate2 是 x64 模拟运行的，int8 反而比 float32 慢
    arm_win = sys.platform == "win32" and platform.machine() == "ARM64"
    return WhisperModel(name, device="cpu", compute_type="float32" if arm_win else "int8")
'''
exec(FW_LOADER)


def subs_cache_path(path, model_name):
    """识别结果按 文件路径+大小+修改时间+模型 缓存，同一个文件只识别一次。"""
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|{model_name}"
    return os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest() + ".json")


def transcribe_words(path, model_name):
    """返回 [(开始秒, 结束秒, 词), ...]。第一次用某个模型会比较慢（加载模型）。"""
    cache = subs_cache_path(path, model_name)
    try:
        with open(cache, encoding="utf-8") as f:
            return [tuple(w) for w in json.load(f)]
    except (OSError, ValueError):
        pass
    audio16k = load_audio(path, layout="mono", rate=16000)[0][:, 0]  # Whisper 要 16kHz 单声道
    try:
        words = _transcribe_here(audio16k, model_name)
    except ImportError:
        # 打包好的 .app / .exe 里没有 Whisper（带上 torch 会大 1GB+），
        # 就去找电脑上装了 Whisper 的 Python，让它来识别
        words = _transcribe_external(audio16k, model_name)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False)
    except OSError:
        pass
    return words


def _transcribe_here(audio16k, model_name):
    """在本进程里用 Whisper 识别（直接跑脚本、环境里装了 Whisper 时）。
    转录页正在用模型时，在这里排队等它用完。"""
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
        pass  # 没有 faster-whisper，用 openai-whisper
    import torch
    import whisper
    # Windows 上没有 Triton，Whisper 算词时间戳时会退回普通实现并刷警告；
    # 只影响对齐那一小步（识别本身仍在 GPU 上），结果一样，屏蔽掉免得吓人
    warnings.filterwarnings("ignore", message="Failed to launch Triton kernels")
    if model_name not in _whisper_models:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        _whisper_models[model_name] = whisper.load_model(model_name, device=device)
    model = _whisper_models[model_name]
    result = model.transcribe(audio16k, word_timestamps=True, verbose=None, language=lang,
                              fp16=model.device.type == "cuda")
    return [(w["start"], w["end"], w["word"])
            for seg in result["segments"] for w in seg.get("words", [])]


# 交给外部 Python 跑的识别脚本：读 .npy 音频 → Whisper → 把词写成 JSON
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
# 外部 Python 能用的条件：有 numpy，faster-whisper / openai-whisper 有一个就行
HAS_WHISPER = ("import numpy, importlib.util as u; "
               "assert u.find_spec('faster_whisper') or u.find_spec('whisper')")
INSTALL_HINT = "pip3 install faster-whisper" if sys.platform == "darwin" else "pip install faster-whisper"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows 上不弹黑色命令行窗口
_external_python = []  # 找到的 Python 路径（找过一次就记住）


def find_external_python():
    """找一个装了 Whisper 的 Python。从 Finder 打开 .app 时 PATH 很短，所以把常见位置都试一遍。
    也可以用环境变量 JINGTING_PYTHON 直接指定。"""
    if _external_python:
        return _external_python[0]
    cands = [os.environ.get("JINGTING_PYTHON")]
    cands += sorted(glob.glob("/Library/Frameworks/Python.framework/Versions/*/bin/python3"),
                    reverse=True)  # python.org 装的 Python（新版本优先）
    cands += ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
              shutil.which("python3"), shutil.which("python")]
    seen = set()
    for py in cands:
        # /usr/bin/python3 在没装开发工具的 Mac 上会弹"安装命令行工具"，跳过
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
        raise ImportError(f"要显示字幕需要先安装 Whisper：在终端运行 {INSTALL_HINT}")
    with tempfile.TemporaryDirectory() as d:
        audio_path, out_path = os.path.join(d, "audio.npy"), os.path.join(d, "words.json")
        np.save(audio_path, audio16k.astype(np.float32))
        r = subprocess.run([py, "-c", WHISPER_HELPER, audio_path, model_name, out_path],
                           capture_output=True, text=True, creationflags=NO_WINDOW)
        if r.returncode != 0:
            last = (r.stderr.strip().splitlines() or ["未知错误"])[-1]
            raise RuntimeError(f"{os.path.basename(py)}: {last}")
        with open(out_path, encoding="utf-8") as f:
            return [tuple(w) for w in json.load(f)]


def sentence_texts(words, segs, sr):
    """把每个词分到它中点所在的那一句；落在句间空隙的词归到最近的一句。"""
    texts = [[] for _ in segs]
    starts = [a for a, b in segs]
    for start, end, word in words:
        mid = (start + end) / 2 * sr
        i = max(0, bisect.bisect_right(starts, mid) - 1)  # 开始时间不晚于 mid 的最后一句
        if mid > segs[i][1] and i + 1 < len(segs) and segs[i + 1][0] - mid < mid - segs[i][1]:
            i += 1  # 在空隙里，离下一句更近
        texts[i].append(word)
    return ["".join(t).strip() for t in texts]


# ---------- 视频画面 ----------
def has_video(path):
    """有真正的视频画面才算（mp3/m4a 里的封面图也是一路"视频"，要排除）。"""
    with av.open(path) as c:
        return any(not (vs.disposition & av.stream.Disposition.attached_pic)
                   for vs in c.streams.video)


def fit(size, box):
    """把 size 等比缩放到能放进 box 的最大尺寸（宽高取偶数，缩放器要求）。"""
    (w, h), (bw, bh) = size, box
    k = min(bw / w, bh / h)
    return max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)


class VideoDecoder:
    """后台线程解码一段时间内的画面，缩放好后放进 frames 队列：(时间秒, PIL 图)。
    每次 start 都是一个新任务；旧任务发现 gen 变了就自己退出。"""

    MAX_QUEUE = 30  # 最多提前解码这么多帧，防止吃内存

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
        """解码 [t0, t1] 秒的画面；t0 == t1 时只取 t0 那一帧（暂停时显示用）。"""
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
                # seek 会落到 t0 之前最近的关键帧，从那里解码到 t0（这段不缩放，省时间）
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
        except Exception:  # 画面坏了不影响听，声音照常放
            pass


# ---------- 播放器界面 ----------
class Player:
    def __init__(self, root, opts, parent=None):
        """parent：嵌进别的程序（比如 app.py 的一个标签页）时传一个 tk.Frame；
        不传就是独立窗口，和以前一样。"""
        self.root, self.opts = root, opts
        self.embedded = parent is not None
        parent = parent or root
        self.active = True  # 嵌入时由外面控制：不在当前标签页就不响应快捷键
        self.audio, self.sr, self.segs = None, None, []
        self.cur = 0
        self.played = False
        self.playing = False
        self.loading = False
        self.env, self.env_hop = None, 1
        self.t0 = 0.0  # 当前句开始播放的时刻
        self.view = (0, 1)  # 放大波形显示的采样范围
        self.video = None   # VideoDecoder；纯音频文件为 None
        self.photo = None   # 当前显示的画面（要留着引用，否则 Tk 会把图丢掉）
        self.pending = None  # 等画面准备好再开始放声音：(句号, 截止时刻)

        if not self.embedded:
            root.title("精听")
            root.geometry("760x560")

        ui_style.apply(root)
        apply_audio_devices(opts)
        P = ui_style.PAD

        # --- 工具栏（和转录页同一排版：左边打开文件，右边音量 / 设置） ---
        top = self.top = ttk.Frame(parent, padding=(P, P, P, P))
        top.pack(fill="x")
        ttk.Button(top, text="打开文件…", takefocus=False,
                   command=self.browse).pack(side="left")
        ttk.Button(top, text="⚙ 设置", takefocus=False,
                   command=self.open_settings).pack(side="right")
        vol = ttk.Frame(top)
        vol.pack(side="right", padx=(0, 12))
        self.vol_label = ttk.Label(vol, width=10, anchor="e")
        self.vol_label.pack(side="left", padx=(0, 6))
        self.vol_var = tk.DoubleVar(value=round(opts["volume"] * 100))
        self.vol_scale = ttk.Scale(vol, from_=50, to=250, orient="horizontal", length=140,
                                   variable=self.vol_var, takefocus=False,
                                   command=lambda v: self.set_volume(round(float(v) / 10) / 10))
        self.vol_scale.pack(side="left")
        self.vol_save_job = None
        self.set_volume(opts["volume"], save=False)

        # --- 拖放区 ---
        self.drop = ui_style.drop_zone(parent, DROP_HINT)
        self.drop.pack(fill="x", padx=P)
        self.name, self.thr = "", None
        self.settings_win = None
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
                 text="空格 下一句 / 停止    R 重听    ← → 上下句    T 显示/隐藏字幕    + - 音量"
                 + ("" if self.embedded else "    Q 退出") + "\n"
                 "鼠标：点波形/画面重听，点总览跳到那一句；"
                 "在波形/画面上滚轮 = 上下句，在音量上滚轮 = 调音量"
                 ).pack(side="bottom", pady=(4, P))

        # --- 字幕（设置里打开才显示） ---
        # 用只读的 Text 而不是 Label：外观一样，但可以用鼠标选中、Ctrl+C 复制
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
        menu.add_command(label="复制", command=self.copy_selection)
        menu.add_command(label="复制整句", command=lambda: self.copy_text(self.sub_text))
        rclick = "<Button-2>" if sys.platform == "darwin" else "<Button-3>"
        self.sub.bind(rclick, lambda e: menu.tk_popup(e.x_root, e.y_root))
        self.sub.bind("<Control-a>", lambda e: (self.sub.tag_add("sel", "1.0", "end-1c"), "break")[1])
        self.path = None
        self.words, self.words_key, self.texts = None, None, []
        self.sub_error = None
        self.sub_gen = 0
        self.revealed = False  # "听完再显示"模式：当前句字幕是否已揭晓（换句就重置）
        self.hidden = False    # "一直显示"模式：按 T 临时隐藏

        # --- 鼠标控制按钮（和键盘快捷键做的事一样） ---
        bar = self.bar = ttk.Frame(parent)
        bar.pack(pady=(2, 6))
        self.buttons = {}
        for key, text, cmd, width in (
                ("prev", "⏮ 上一句", self.prev, 9),
                ("replay", "↻ 重听", lambda: self.play(self.cur), 8),
                ("main", "▶ 播放", self.on_space, 10),
                ("next", "下一句 ⏭", self.next, 9),
                ("sub", "显示字幕", self.toggle_sub, 9)):
            # takefocus=False：按钮不抢键盘焦点，否则按空格会把按钮也"按"一次
            b = ttk.Button(bar, text=text, width=width, command=cmd, style="Big.TButton",
                           takefocus=False, cursor="hand2")
            b.pack(side="left", padx=3)
            self.buttons[key] = b
        # 字幕模式下拉框：不用进设置就能切换（和设置里的是同一个选项）
        self.sub_mode_var = tk.StringVar(value=SUB_MODES[opts["subtitles"]])
        mode_of = {v: k for k, v in SUB_MODES.items()}
        om = ttk.Combobox(bar, textvariable=self.sub_mode_var, values=list(SUB_MODES.values()),
                          state="readonly", width=12, takefocus=False, font=(UI_FONT, 10))

        def on_mode(e):
            self.set_sub_mode(mode_of[self.sub_mode_var.get()])
            om.selection_clear()
            parent.focus_set()  # 焦点还给页面，空格等快捷键才继续有效
        om.bind("<<ComboboxSelected>>", on_mode)
        om.pack(side="left", padx=(6, 3), ipady=3)

        # 自动下一句：一句播完，等 N 秒自动播放下一句
        self.auto_job = None   # root.after 的任务；None = 没有在倒计时
        self.auto_due = 0.0    # 倒计时结束的时刻（time.monotonic）
        auto = ttk.Frame(bar)
        auto.pack(side="left", padx=(12, 0))
        self.auto_var = tk.BooleanVar(value=opts["auto_next"])
        ttk.Checkbutton(auto, text="自动下一句", variable=self.auto_var, takefocus=False,
                        command=self.on_auto_change).pack(side="left")
        self.delay_var = tk.StringVar(value=f"{opts['auto_delay']:g}")
        lo, hi = AUTO_DELAY_RANGE
        self.delay_spin = ttk.Spinbox(auto, from_=lo, to=hi, increment=0.5, width=4,
                                      textvariable=self.delay_var, command=self.on_auto_change)
        self.delay_spin.pack(side="left", padx=(4, 2))
        ttk.Label(auto, text="秒后").pack(side="left")
        self.delay_spin.bind("<Return>", lambda e: self.on_auto_change(give_back_focus=True))
        self.delay_spin.bind("<FocusOut>", lambda e: self.on_auto_change())

        # --- 视频画面（只有视频文件才显示） ---
        self.screen = tk.Canvas(parent, width=640, height=360, bg="black",
                                highlightthickness=0, cursor="hand2")
        self.screen.bind("<Button-1>", lambda e: self.play(self.cur))
        self.screen.bind("<Configure>", lambda e: self.on_screen_resize())
        self.resize_job = None
        self.screen_img = self.screen.create_image(0, 0, anchor="center")

        # --- 波形：上面是当前句放大，下面是整段总览 ---
        self.detail = tk.Canvas(parent, height=140, bg="white", highlightthickness=1,
                                highlightbackground=ui_style.DROP_BORDER, cursor="hand2")
        self.detail.pack(fill="both", expand=True, padx=P, pady=(4, 4))
        self.overview = tk.Canvas(parent, height=60, bg="#fafafa", highlightthickness=1,
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

        # --- 鼠标滚轮：在波形/画面上滚 = 上下句；在音量上滚 = 调音量 ---
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
        if not self.embedded:  # 嵌入时由外面的程序负责退出
            for k in ("q", "Q", "<Escape>"):
                self.bind_key(k, self.quit)
            root.protocol("WM_DELETE_WINDOW", self.quit)

        self.status.config(text="拖入音频文件开始")
        self.update_buttons()
        self.poll()

    def bind_key(self, key, fn):
        """快捷键绑在整个窗口上；不在当前标签页、或者正在别的输入框里打字时不响应。"""
        def handler(e):
            w = e.widget
            typing = (isinstance(w, tk.Misc) and w is not self.sub
                      and w.winfo_class() in ("Entry", "TEntry", "Spinbox", "TSpinbox",
                                              "TCombobox", "Text"))
            if self.active and not typing:
                fn()
        self.root.bind(key, handler, add="+")

    # ---------- 选文件 ----------
    def on_drop(self, event):
        files = self.root.tk.splitlist(event.data)  # 处理带空格的路径
        if files:
            self.open_file(files[0])

    def browse(self):
        path = filedialog.askopenfilename(
            filetypes=[("音频/视频", " ".join("*" + e for e in AUDIO_EXTS)),
                       ("所有文件", "*.*")])
        if path:
            self.open_file(path)

    def open_file(self, path):
        if self.loading:
            return
        if not os.path.isfile(path):
            messagebox.showerror("错误", f"不是文件：\n{path}")
            return
        self.stop_all()
        self.loading = True
        name = os.path.basename(path)
        self.drop.config(text=f"{name}\n正在读取并断句……")
        self.status.config(text="读取中，请稍候")
        threading.Thread(target=self._load, args=(path,), daemon=True).start()

    def _load(self, path):
        try:
            audio, sr = load_audio(path)
            segs, thr = self.split(audio, sr)
            env = make_envelope(audio, sr)
            err = None if segs else "没有检测到句子，试试在设置里调低静音阈值"
            try:
                video = VideoDecoder(path) if has_video(path) else None
            except Exception:  # 画面读不了就当纯音频用
                video = None
        except Exception as ex:  # 解码失败等
            audio = sr = segs = env = thr = video = None
            err = f"无法读取：{ex}"
        self.root.after(0, self._loaded, path, audio, sr, segs, err, env, thr, video)

    def _loaded(self, path, audio, sr, segs, err, env=None, thr=None, video=None):
        self.loading = False
        name = os.path.basename(path)
        if err:
            self.drop.config(text=f"{name}\n（{err}）\n可以再拖一个文件进来")
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
            self.root.title(f"精听 - {name}")
        self.set_video(video)
        self.show_file_info()
        self.start_subs()
        self.refresh("按空格开始")
        self.preview()

    # ---------- 字幕 ----------
    def start_subs(self):
        """字幕打开时在后台识别（有缓存就直接读）；识别完自动显示。"""
        self.sub_error = None
        model = self.opts["whisper_model"]
        if self.opts["subtitles"] == "off" or not self.path:
            return
        if self.words_key == (self.path, model):
            return  # 这个文件用这个模型已经识别过
        self.words, self.texts = None, []
        self.sub_gen += 1
        gen, path = self.sub_gen, self.path

        def work():
            try:
                words, err = transcribe_words(path, model), None
            except ImportError as ex:
                words, err = None, str(ex)
            except Exception as ex:
                words, err = None, f"字幕识别失败：{ex}"
            self.root.after(0, self._subs_done, gen, path, model, words, err)

        threading.Thread(target=work, daemon=True).start()

    def _subs_done(self, gen, path, model, words, err):
        if gen != self.sub_gen:
            return  # 中途换了文件或模型，结果作废
        self.sub_error = err
        if words is not None:
            self.words, self.words_key = words, (path, model)
            self.texts = sentence_texts(words, self.segs, self.sr)
        self.update_sub()

    def set_sub(self, text, color):
        """改字幕框里的文字（只读 Text：先解锁、改完再锁上，用户不能打字但能选中）。"""
        if text == self.sub.get("1.0", "end-1c") and color == self.sub.cget("fg"):
            return  # 没变就不重写，免得把用户正在选的文字弄没
        self.sub.config(state="normal", fg=color)
        self.sub.delete("1.0", "end")
        self.sub.insert("1.0", text, "center")
        self.sub.config(state="disabled")
        self.fit_sub_height()

    def fit_sub_height(self):
        """按换行后的实际行数调整高度（1~4 行）。"""
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
        except tk.TclError:  # 没选中任何文字
            self.copy_text(self.sub_text)

    def update_sub(self):
        mode = self.opts["subtitles"]
        if mode == "off" or not self.segs:
            text, color = "", "#222"
        elif self.sub_error:
            text, color = self.sub_error, "#c33"
        elif self.words is None:
            text, color = f"字幕识别中……（{self.opts['whisper_model']}，第一次会慢一些）", "#888"
        elif mode == "after" and not self.revealed:
            text, color = "（听完这句显示字幕，按 T 立即显示）", "#aaa"
        elif mode == "manual" and not self.revealed:
            text, color = "（字幕已隐藏，点“显示字幕”或按 T）", "#aaa"
        elif mode == "always" and self.hidden:
            text, color = "（字幕已隐藏，按 T 显示）", "#aaa"
        else:
            text, color = self.texts[self.cur] or "（这句没识别出文字）", "#222"
        # 只有真正的字幕才给"复制整句"用，提示语不算
        self.sub_text = self.texts[self.cur] if color == "#222" and self.texts else ""
        self.set_sub(text, color)
        # 字幕关闭时把这一行收起来，不留空白
        if mode == "off" and self.sub.winfo_manager():
            self.sub.pack_forget()
        elif mode != "off" and not self.sub.winfo_manager():
            self.sub.pack(before=self.bar, fill="x", padx=ui_style.PAD, pady=(2, 4))
        self.update_buttons()

    def toggle_sub(self):
        """T 键 / 字幕按钮："一直显示"模式下整体隐藏/显示；
        "听完再显示"和"手动显示"模式下揭晓/再盖住当前句。"""
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
        """主窗口下拉框切换字幕模式：立即生效并保存，需要时开始识别。"""
        if mode == self.opts["subtitles"]:
            return
        self.opts["subtitles"] = mode
        self.revealed = self.hidden = False
        save_settings(self.opts)
        self.start_subs()
        self.update_sub()

    def set_video(self, video):
        """有画面就把视频区插到波形上面，波形和大字缩小；纯音频恢复原样。"""
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
        how = "自动" if self.opts["threshold"] is None else "手动"
        thr_txt = f"，静音阈值 {self.thr:.0f} dB（{how}）" if self.thr is not None else ""
        self.drop.config(text=f"{self.name}\n共 {len(self.segs)} 句{thr_txt}"
                              "（拖入其他文件可替换）")

    def resplit(self):
        """设置改了之后，用新参数重新断句；尽量停在原来听到的位置。"""
        if self.audio is None:
            return
        segs, thr = self.split(self.audio, self.sr)
        if not segs:
            messagebox.showwarning("设置", "用这组参数没有检测到句子，试试调低静音阈值")
            return
        self.stop_all()
        pos = self.segs[self.cur][0]
        self.segs, self.thr = segs, thr
        self.cur = max(i for i, (s, e) in enumerate(segs) if s <= pos or i == 0)
        self.revealed = False
        if self.words is not None:  # 字幕不用重新识别，按新句子重新分配就行
            self.texts = sentence_texts(self.words, segs, self.sr)
        self.show_file_info()
        self.refresh("已按新设置重新断句")
        self.preview()

    # ---------- 设置窗口 ----------
    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        o = self.opts
        win = self.settings_win = tk.Toplevel(self.root)
        win.title("设置")
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
        rows = [  # (键, 标题, 最小, 最大, 步长, 说明)
            ("threshold", "静音阈值 (dB)", -60, -5, 1,
             "呼吸声被当成说话 → 调高（如 -25）；吞字 → 调低（如 -40）"),
            ("min_silence", "断句静音 (秒)", 0.1, 1.5, 0.05,
             "多长的静音算一句结束；调小断得更细"),
            ("max_len", "最长一句 (秒)", 0, 30, 0.5,
             "超过就在句内最长停顿处再切；0 = 不再切"),
            ("pad", "前后留白 (秒)", 0, 0.5, 0.05,
             "每句前后多播一点，防止吞音"),
        ]
        frm = ttk.Frame(win, padding=(16, 12, 16, 4))
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
                ttk.Checkbutton(frm, text="按本底噪声自动", variable=auto,
                                command=lambda: sync_auto()).grid(row=r, column=2, sticky="w",
                                                                  padx=(10, 0))
            ttk.Label(frm, text=hint, style="Hint.TLabel").grid(
                row=r + 1, column=0, columnspan=3, sticky="w")
            r += 2

        # --- 字幕 ---
        ttk.Separator(frm).grid(row=r, column=0, columnspan=3, sticky="we", pady=(12, 4))
        label_of = SUB_MODES
        key_of = {v: k for k, v in label_of.items()}
        sub_mode = tk.StringVar(value=label_of.get(o["subtitles"], label_of["off"]))
        sub_model = tk.StringVar(value=o["whisper_model"])
        ttk.Label(frm, text="字幕").grid(row=r + 1, column=0, sticky="w", pady=(6, 0))
        om = ttk.Combobox(frm, textvariable=sub_mode, values=list(label_of.values()),
                          state="readonly", width=14)
        om.grid(row=r + 1, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(frm, text="识别模型").grid(row=r + 2, column=0, sticky="w", pady=(6, 0))
        om = ttk.Combobox(frm, textvariable=sub_model, values=WHISPER_MODELS,
                          state="readonly", width=14)
        om.grid(row=r + 2, column=1, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(frm, style="Hint.TLabel", justify="left",
                 text="转录页和精听字幕共用这个模型；字幕每个文件只识别一次（结果会缓存）\n"
                      "模型越大越准越慢：tiny / base 快，small 平衡，medium / large 准").grid(
            row=r + 3, column=0, columnspan=3, sticky="w")

        # --- 音频设备 ---
        ttk.Separator(frm).grid(row=r + 4, column=0, columnspan=3, sticky="we", pady=(12, 4))
        mic = tk.StringVar(value=o["mic"] or SYSTEM_DEVICE)
        speaker = tk.StringVar(value=o["speaker"] or SYSTEM_DEVICE)
        dev_boxes = {}
        for i, (kind, label, var) in enumerate((("input", "麦克风", mic),
                                                ("output", "扬声器", speaker))):
            ttk.Label(frm, text=label).grid(row=r + 5 + i, column=0, sticky="w", pady=(6, 0))
            dev_boxes[kind] = ttk.Combobox(frm, textvariable=var, state="readonly", width=44)
            dev_boxes[kind].grid(row=r + 5 + i, column=1, columnspan=2, sticky="w", pady=(6, 0))
        hint = ttk.Frame(frm)
        hint.grid(row=r + 7, column=0, columnspan=3, sticky="we")
        ttk.Label(hint, style="Hint.TLabel",
                  text="录音、两页的播放都用这里选的设备；刚插上耳机就点「刷新」").pack(side="left")
        ttk.Button(hint, text="刷新", width=6,
                   command=lambda: load_devices()).pack(side="right", pady=(4, 0))

        def load_devices():
            for kind, var in (("input", mic), ("output", speaker)):
                try:
                    names = list(audio_devices(kind))
                except Exception:
                    names = []
                dev_boxes[kind]["values"] = [SYSTEM_DEVICE] + names
                if var.get() not in names:  # 选过的设备现在没插着
                    var.set(SYSTEM_DEVICE)

        def sync_auto():
            spins["threshold"].config(state="disabled" if auto.get() else "normal")

        def read_form():
            try:
                s = {k: float(v.get()) for k, v in vars_.items()}
            except (tk.TclError, ValueError):
                messagebox.showerror("设置", "请填数字", parent=win)
                return None
            if auto.get():
                s["threshold"] = None
            s["subtitles"] = key_of[sub_mode.get()]
            s["whisper_model"] = sub_model.get()
            for k, var in (("mic", mic), ("speaker", speaker)):
                s[k] = None if var.get() == SYSTEM_DEVICE else var.get()
            return s

        def apply():
            s = read_form()
            if s is None:
                return
            self.opts.update(s)
            save_settings(self.opts)
            apply_audio_devices(self.opts)
            self.resplit()
            self.start_subs()
            self.update_sub()
            if self.thr is not None and auto.get():  # 显示自动算出的阈值，方便参考
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
        for text, cmd in (("应用并保存", apply), ("恢复默认", reset), ("关闭", win.destroy)):
            ttk.Button(btns, text=text, width=10, command=cmd).pack(side="left", padx=4)
        win.bind("<Return>", lambda e: apply())
        win.bind("<Escape>", lambda e: win.destroy())
        sync_auto()
        load_devices()

    # ---------- 播放 ----------
    # ---------- 自动下一句 ----------
    def on_auto_change(self, give_back_focus=False):
        """勾选框 / 秒数改了：检查秒数、保存设置；关掉时取消正在进行的倒计时。"""
        lo, hi = AUTO_DELAY_RANGE
        try:
            delay = min(hi, max(lo, float(self.delay_var.get())))
        except ValueError:
            delay = self.opts["auto_delay"]  # 填的不是数字：恢复原来的值
        self.delay_var.set(f"{delay:g}")
        changed = (self.opts["auto_next"], self.opts["auto_delay"]) != (self.auto_var.get(), delay)
        self.opts["auto_next"], self.opts["auto_delay"] = self.auto_var.get(), delay
        if changed:
            save_settings(self.opts)
        if not self.opts["auto_next"] and self.auto_job:
            self.cancel_auto()
            self.refresh("⏸ 已暂停，按空格继续")
        if give_back_focus:
            self.bar.master.focus_set()  # 焦点还给页面，空格等快捷键才继续有效

    def schedule_auto(self):
        """当前句刚播完时调用：开了自动下一句、又不是最后一句，就开始倒计时。"""
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
            self.stop_all()  # 倒计时中按空格 = 取消自动播放，停在这一句
            self.refresh("已停止")
        elif not self.played:
            self.play(self.cur)
        elif self.cur < len(self.segs) - 1:
            self.play(self.cur + 1)
        else:
            self.refresh("已经是最后一句（← 返回，R 重听）")

    def play(self, idx):
        if not self.segs:
            return
        self.stop_all()
        if idx != self.cur:
            self.revealed = False  # 换了一句，字幕重新盖住（重听同一句不盖）
        self.cur = idx
        self.played = True
        if self.video:
            # 先让画面解码到这句开头，最多等 1.5 秒，再一起开始，避免声音先跑
            s, e = self.segs[idx]
            self.video.start(s / self.sr, e / self.sr, self.screen_box())
            self.pending = (idx, time.monotonic() + 1.5)
            self.refresh("▶ 准备画面……")
        else:
            self.start_audio()

    def prev(self):
        self.play(max(0, self.cur - 1))

    def next(self):
        self.play(min(len(self.segs) - 1, self.cur + 1))

    def bind_wheel(self, widget, fn, throttle=0.15):
        """滚轮往上 fn(-1)，往下 fn(+1)。throttle 秒内只算一次，触控板一划不会连跳好几句。"""
        def handler(e):
            now = time.monotonic()
            if now - self.wheel_last < throttle:
                return "break"
            d = {4: 1, 5: -1}.get(e.num, e.delta)  # Linux 用 Button-4/5，其他系统用 delta
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
            main = "■ 停止"
        elif not self.played:
            main = "▶ 开始"
        else:
            main = "▶ 继续"  # 和空格一样：播放下一句
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
                        text="隐藏字幕" if self.sub_shown() else "显示字幕")
        self.sub_mode_var.set(SUB_MODES[self.opts["subtitles"]])

    def set_volume(self, v, save=True):
        """改音量：下一次播放生效；停一会儿再存盘，拖滑块时不会一直写文件。"""
        v = round(min(2.5, max(0.5, v)), 2)  # 再大也只是限幅压得更狠，不会更响
        self.opts["volume"] = v
        self.vol_var.set(round(v * 100))
        self.vol_label.config(text=f"音量 {v * 100:.0f}%")
        if save:
            if self.vol_save_job:
                self.root.after_cancel(self.vol_save_job)
            self.vol_save_job = self.root.after(800, lambda: save_settings(self.opts))

    def start_audio(self):
        self.pending = None
        s, e = self.segs[self.cur]
        sd.play(boost(self.audio[s:e], self.sr, self.opts["volume"]), self.sr)
        self.t0 = time.monotonic()
        self.playing = True
        self.refresh("▶ 播放中")

    def stop_all(self):
        sd.stop()
        self.playing = False
        self.pending = None
        self.cancel_auto()
        if self.video:
            self.video.cancel()

    def preview(self):
        """暂停状态下显示当前句第一帧。"""
        if self.video and self.segs:
            self.root.update_idletasks()  # 视频区刚显示出来时先让它排好版，才知道多大
            t = self.segs[self.cur][0] / self.sr
            self.video.start(t, t, self.screen_box())

    def poll(self):
        if self.pending and (self.video.frames or time.monotonic() > self.pending[1]):
            self.start_audio()
        if self.playing:
            stream = sd.get_stream()
            if stream is None or not stream.active:
                self.playing = False
                if self.video:
                    self.video.cancel()  # 画面停在最后显示的那一帧
                if self.opts["subtitles"] == "after":
                    self.revealed = True  # 听完了，揭晓字幕（手动模式不自动揭晓）
                if self.schedule_auto():
                    self.refresh(self.auto_msg())
                elif self.opts["auto_next"] and self.cur == len(self.segs) - 1:
                    self.refresh("✓ 已播完最后一句")
                else:
                    self.refresh("⏸ 已暂停，按空格继续")
            else:
                s, e = self.segs[self.cur]
                pos = min(e, s + (time.monotonic() - self.t0) * self.sr)
                self.move_playhead(pos)
                if self.video:
                    self.show_due_frame(pos / self.sr)
        elif self.video and self.video.frames and not self.pending:
            self.show_frame(self.video.frames.pop()[1])  # 预览帧
            self.video.frames.clear()
        if self.auto_job and not self.playing:
            self.status.config(text=self.auto_msg())  # 倒计时：每次刷新剩下的秒数
        self.root.after(15 if self.video else 30, self.poll)

    def auto_msg(self):
        left = max(0.0, self.auto_due - time.monotonic())
        return f"⏸ {left:.1f} 秒后自动播放下一句（空格 = 取消）"

    # ---------- 画面 ----------
    def show_due_frame(self, now):
        """按声音的时钟显示画面：取出所有时间已到的帧，只显示最新的（落后了就跳帧）。"""
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
        """窗口大小变了：暂停时按新尺寸重新取一帧（停一下再取，拖动窗口时不会狂解码）。"""
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
        self.big.config(text=f"第 {self.cur + 1} / {len(self.segs)} 句")
        self.info.config(text=f"{fmt(s / self.sr)} – {fmt(e / self.sr)}"
                              f"（{(e - s) / self.sr:.1f} 秒）")
        self.status.config(text=msg)
        self.update_sub()
        self.update_buttons()
        self.draw_waves()

    # ---------- 波形 ----------
    def draw_waves(self):
        if self.env is None or not self.segs:
            return
        hop, total = self.env_hop, len(self.audio)
        s, e = self.segs[self.cur]

        # 当前句放大：前后各多看 0.3 秒，方便看清断句位置
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

        # 整段总览：浅色块是每一句，深色块是当前句
        cv = self.overview
        cv.delete("all")
        w, h = cv.winfo_width(), cv.winfo_height()
        if w > 2:
            mid = h / 2
            for i, (a, b) in enumerate(self.segs):
                cv.create_rectangle(self.to_x(a, w, 0, total), 2, self.to_x(b, w, 0, total),
                                    h - 2, outline="",
                                    fill="#9cc3ff" if i == self.cur else "#e1ecff")
            pk = column_peaks(self.env, 0, len(self.env), w)
            for x, v in enumerate(pk):
                y = v * (mid - 3)
                cv.create_line(x, mid - y, x, mid + y + 1, fill="#5a6475")
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
        # 点在句子里就选这句，点在静音处就选最近的一句
        dist = [0 if a <= pos <= b else min(abs(pos - a), abs(pos - b))
                for a, b in self.segs]
        self.play(dist.index(min(dist)))

    def quit(self):
        self.stop_all()
        self.root.destroy()
