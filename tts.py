"""文字转语音（「朗读」页用）。

两种引擎：
  Edge 在线语音（edge-tts）：微软的神经网络语音，有加拿大口音；要联网。
  Kokoro 离线语音（kokoro-onnx）：第一次要下载约 120 MB 模型，之后不用联网。
引擎选「自动」时先用 Edge，连不上就整段改用 Kokoro（下载过的话）。

文字按句子分开合成，句子之间插入固定停顿；结果里有每句在文字里的位置和在音频里的
起止时间，朗读时用来高亮正在读的那一句，逐句对比时用来切出每一句。
同样的文字 + 语音 + 语速 + 停顿只合成一次，结果缓存在 app_paths.CACHE_DIR。
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

SR = 24000   # 两种引擎都输出 24 kHz 单声道

# (设置里存的 id = Edge 语音名, 显示名, Kokoro 语音, Kokoro 口音)
VOICES = [
    ("en-CA-ClaraNeural", "加拿大 · 女 · Clara", "af_heart", "en-us"),
    ("en-CA-LiamNeural", "加拿大 · 男 · Liam", "am_michael", "en-us"),
    ("en-US-AriaNeural", "美国 · 女 · Aria", "af_bella", "en-us"),
    ("en-US-GuyNeural", "美国 · 男 · Guy", "am_adam", "en-us"),
    ("en-US-JennyNeural", "美国 · 女 · Jenny", "af_nicole", "en-us"),
    ("en-GB-SoniaNeural", "英国 · 女 · Sonia", "bf_emma", "en-gb"),
    ("en-GB-RyanNeural", "英国 · 男 · Ryan", "bm_george", "en-gb"),
]
VOICE_IDS = [v[0] for v in VOICES]
VOICE_NAMES = {v[0]: v[1] for v in VOICES}
ENGINES = {"auto": "自动（在线优先）", "edge": "只用在线", "offline": "只用离线"}
ENGINE_NAMES = {"edge": "Edge 在线语音", "kokoro": "Kokoro 离线语音"}
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
    path: str        # 合成好的 WAV
    engine: str      # "edge" / "kokoro"
    spans: list      # 每句在文字里的 (起, 止) 字符位置
    times: list      # 每句在音频里的 (起, 止) 秒


# ---------- 断句 ----------
_SENT_END = re.compile(r"[.!?]+[\"'”’)\]]*(?=\s|$)|\n")
_ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "e.g", "i.e", "no", "prof"}


def split_sentences(text):
    """返回每句的 (起, 止) 字符位置（去掉了两头空白；没有字母数字的段落跳过）。"""
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
                continue   # Mr. / e.g. 之类的缩写，不算一句结束
            end = m.end()
        else:
            end = m.start()
        add(start, end)
        start = m.end()
    add(start, len(text))
    return spans


# ---------- Edge 在线语音 ----------
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
        raise TTSError("Edge 在线语音没有返回声音")
    return _decode(data)


def _decode(data):
    """mp3 字节 → 24 kHz 单声道 float32。"""
    out = []
    with av.open(io.BytesIO(data)) as c:
        rs = av.AudioResampler(format="flt", layout="mono", rate=SR)
        for frame in c.decode(c.streams.audio[0]):
            out += [f.to_ndarray().reshape(-1) for f in rs.resample(frame)]
        out += [f.to_ndarray().reshape(-1) for f in rs.resample(None)]
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)


# ---------- Kokoro 离线语音 ----------
_kokoro = None
_kokoro_lock = threading.Lock()


def offline_ready():
    return all(os.path.isfile(os.path.join(KOKORO_DIR, f)) for f in KOKORO_FILES)


def download_offline(progress=None, cancel=None):
    """下载 Kokoro 模型（约 120 MB）。progress(已下载字节, 总字节)。先写 .part，下完再改名。"""
    os.makedirs(KOKORO_DIR, exist_ok=True)
    sizes = {}
    for name in KOKORO_FILES:   # 先问清楚总大小，进度条才准
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
                raise TTSError("离线语音还没下载：在「朗读」页点「下载离线语音」")
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(os.path.join(KOKORO_DIR, KOKORO_FILES[0]),
                             os.path.join(KOKORO_DIR, KOKORO_FILES[1]))
        _, _, kvoice, lang = next(v for v in VOICES if v[0] == voice_id)
        have = _kokoro.get_voices()
        if kvoice not in have:   # 模型里没有这个声音：换一个同性别的
            same = [v for v in have if v[:2] == kvoice[:2]] or [v for v in have if v[1] == kvoice[1]]
            kvoice = (same or list(have))[0]
        audio, sr = _kokoro.create(text, voice=kvoice, speed=speed, lang=lang)
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sr != SR:   # 现在的模型就是 24 kHz；以防万一
        audio = np.interp(np.arange(0, len(audio), sr / SR), np.arange(len(audio)), audio)
    return audio.astype(np.float32)


# ---------- 合成整段 ----------
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
    """把整段文字合成成一个 WAV，返回 Speech。
    progress(已合成句数, 总句数, 引擎)；cancel 是 threading.Event，设了就抛 Cancelled。"""
    spans = split_sentences(text)
    if not spans:
        raise TTSError("没有可以朗读的文字")
    if voice not in VOICE_NAMES:
        voice = VOICE_IDS[0]
    order = {"auto": ["edge", "kokoro"], "edge": ["edge"], "offline": ["kokoro"]}[engine]
    for eng in order:   # 缓存里已经有，就不用再合成
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
            raise TTSError(f"连不上 Edge 在线语音（{e}）") from e
        # 「自动」：在线不行就整段改用离线语音（不混用两种声音）
        return _render(text, spans, voice, speed, pause, "kokoro", progress, cancel)
