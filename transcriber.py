"""转录页（app.py 的「转录 / 录音」标签）: drag-and-drop audio -> English text with
OpenAI Whisper, plus playback, 60/90 s recording and saving audio.
This file is a module only; start the program with: python app.py

Requirements:
    pip install -U av numpy tkinterdnd2 sounddevice
    Decoding, playback and format conversion use PyAV (no ffmpeg command-line tools).
    Transcription needs openai-whisper: in this Python if it's installed (pip install
    openai-whisper), otherwise another Python on the computer that has it is used
    (see intensive_listening.find_external_python) - that's how the packaged .app works.
"""
import bisect
import importlib
import inspect
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import types
import wave
from tkinter import filedialog, messagebox, ttk

import av
import numpy as np
from tkinterdnd2 import DND_FILES

import app_paths
import intensive_listening
import ui_style

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma",
              ".webm", ".mp4", ".mkv", ".mov"}
MODELS = ["tiny.en", "base.en", "small.en", "medium.en", "turbo", "large-v3"]
DROP_HINT = "把音频或视频文件拖到这里（或点这里选择），然后点「开始转录」"
NO_WINDOW = intensive_listening.NO_WINDOW   # hide the helper Python's console on Windows
REC_DIR = app_paths.RECORDINGS_DIR
REC_LENGTHS = (60, 90)   # seconds
DEFAULT_MIC = "系统默认麦克风"


# Run by an external Python that has Whisper (when this one doesn't, e.g. the packaged .app):
# reads 16 kHz mono .npy audio, prints JSON lines: ["progress", phase, pct] and
# ["seg", start, end, text]. Cancel = the app kills the process.
TRANSCRIBE_HELPER = r"""
import inspect, json, sys, types, warnings
warnings.filterwarnings("ignore")
import numpy as np, torch, whisper, whisper.transcribe

def emit(*a):
    print(json.dumps(a), flush=True)

class Bar:   # tqdm stand-in that reports progress
    def __init__(self, *a, total=None, unit="", **k):
        self.total, self.n, self.last = total or 0, 0, -1
        self.phase = "转录中" if unit == "frames" else "下载模型"
    def update(self, n=1):
        self.n += n
        if self.total:
            pct = min(100.0, 100.0 * self.n / self.total)
            if int(pct) != self.last:
                self.last = int(pct)
                emit("progress", self.phase, pct)
    def __enter__(self): return self
    def __exit__(self, *e): return False
    def __getattr__(self, name): return lambda *a, **k: None

for mod in (whisper, sys.modules["whisper.transcribe"]):  # attribute is the function
    t = getattr(mod, "tqdm", None)
    if inspect.ismodule(t):
        mod.tqdm = types.SimpleNamespace(tqdm=Bar)
    elif t is not None:
        mod.tqdm = Bar

audio_path, name = sys.argv[1:3]
device = "cuda" if torch.cuda.is_available() else "cpu"
model = whisper.load_model(name, device=device)
emit("progress", "转录中", 0)
r = model.transcribe(np.load(audio_path), language="en", task="transcribe",
                     verbose=False, fp16=device == "cuda")
for s in r["segments"]:
    emit("seg", s["start"], s["end"], s["text"].strip())
"""


class Cancelled(Exception):
    pass


def media_duration(path):
    """Length in seconds (0.0 if unknown)."""
    with av.open(path) as c:
        if c.duration:
            return c.duration / av.time_base
        s = c.streams.audio[0]
        return float(s.duration * s.time_base) if s.duration else 0.0


class Player:
    """Plays any file PyAV can read: PyAV decodes to PCM, sounddevice plays it."""
    SR, CH = 44100, 2   # output sample rate, channels (int16)

    def __init__(self):
        self.path = None
        self.duration = 0.0
        self._start = 0.0         # position (sec) where the current decode run began
        self._frames = 0          # frames written since _start
        self._thread = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.ended = False
        self.error = None

    @property
    def playing(self):
        return bool(self._thread and self._thread.is_alive() and not self._paused.is_set())

    @property
    def position(self):
        return min(self._start + self._frames / self.SR, self.duration or float("inf"))

    def load(self, path):
        self.stop()
        self.path, self._start, self._frames = path, 0.0, 0
        try:
            self.duration = media_duration(path)
        except Exception:
            self.duration = 0.0   # unknown; the app fills it in from the transcript

    def play(self, pos=None, paused=False):
        if not self.path:
            return
        if pos is None:
            if self._thread and self._thread.is_alive():   # resume from pause
                self._paused.clear()
                return
            pos = 0.0 if self.ended else self.position
        self.stop()
        self._start, self._frames = max(0.0, pos), 0
        self.ended, self.error = False, None
        self._stop = threading.Event()   # one per run, so a slow old thread can't resume
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
        self._thread = threading.Thread(target=self._run,
                                        args=(self.path, self._start, self._stop), daemon=True)
        self._thread.start()

    def pause(self):
        self._paused.set()

    def seek(self, pos):
        if self._thread and self._thread.is_alive():
            self.play(pos, paused=self._paused.is_set())
        else:
            self._start, self._frames, self.ended = max(0.0, pos), 0, False

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _pcm(self, path, start, stop):
        """Yield int16 interleaved PCM bytes from `start` seconds to the end."""
        with av.open(path) as c:
            stream = c.streams.audio[0]
            if start > 0:   # lands on a packet at or before `start`; the rest is trimmed below
                c.seek(int(start / stream.time_base), stream=stream)
            rs = av.AudioResampler(format="s16", layout="stereo", rate=self.SR)
            skip = None     # output frames still to drop before `start`
            for frame in c.decode(stream):
                if stop.is_set():
                    return
                if skip is None:
                    t = frame.time
                    skip = max(0, round((start - t) * self.SR)) if t is not None else 0
                for out in rs.resample(frame):
                    data = out.to_ndarray().reshape(-1, self.CH)
                    if skip:
                        drop = min(skip, len(data))
                        data, skip = data[drop:], skip - drop
                    if len(data):
                        yield data.tobytes()
            for out in rs.resample(None):   # flush
                yield out.to_ndarray().reshape(-1, self.CH).tobytes()

    def _run(self, path, start, stop):
        frame_bytes = 2 * self.CH
        pcm = self._pcm(path, start, stop)
        try:
            try:
                import sounddevice as sd
            except ImportError:
                raise RuntimeError("没有安装 sounddevice，请运行："
                                   "pip install sounddevice") from None
            with sd.RawOutputStream(samplerate=self.SR, channels=self.CH,
                                    dtype="int16") as stream:
                while not stop.is_set():
                    if self._paused.is_set():
                        if stream.active:
                            stream.stop()
                        stop.wait(0.05)
                        continue
                    if not stream.active:
                        stream.start()
                    data = next(pcm, None)
                    if data is None:
                        if not stop.is_set():
                            self.ended = True
                        break
                    stream.write(data)
                    if stop.is_set():   # a newer run owns _frames now
                        break
                    self._frames += len(data) // frame_bytes
        except Exception as e:
            if not stop.is_set():
                self.error = str(e)
        finally:
            pcm.close()   # closes the file


OUT_CODECS = {".wav": "pcm_s16le", ".mp3": "libmp3lame", ".m4a": "aac", ".flac": "flac"}


def convert_audio(src, dst):
    """Re-encode the first audio stream of `src` into `dst` (format from the extension)."""
    ext = os.path.splitext(dst)[1].lower()
    if ext not in OUT_CODECS:
        raise ValueError(f"不支持的格式：{ext or '（没有扩展名）'}，请用 wav / mp3 / m4a / flac")
    tmp = dst + ".part" + ext   # write aside first so a failure doesn't leave half a file
    try:
        with av.open(src) as inp, av.open(tmp, "w") as out:
            ins = inp.streams.audio[0]
            rate = ins.rate or 44100
            if ext == ".mp3" and rate not in (32000, 44100, 48000):
                rate = 44100            # MP3 only allows a few sample rates
            layout = "mono" if ins.channels == 1 else "stereo"
            st = out.add_stream(OUT_CODECS[ext], rate=rate, layout=layout)
            if ext in (".mp3", ".m4a"):
                st.bit_rate = 192000
            cc = st.codec_context
            rs = av.AudioResampler(format=cc.codec.audio_formats[0].name, layout=layout,
                                   rate=rate, frame_size=cc.frame_size or None)

            def write(frames):
                for f in frames:
                    f.pts = None        # let the encoder number the packets
                    out.mux(st.encode(f))

            for frame in inp.decode(ins):
                write(rs.resample(frame))
            write(rs.resample(None))
            out.mux(st.encode(None))    # flush the encoder
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class Recorder:
    """Records the default microphone to a mono WAV, stopping itself after `limit` seconds."""
    SR = 44100

    def __init__(self):
        self._stream = None
        self._chunks = []
        self.frames = 0
        self.limit = 0
        self.path = None

    @property
    def active(self):
        return self._stream is not None          # started and not yet finished

    @property
    def elapsed(self):
        return self.frames / self.SR

    @property
    def time_up(self):
        return self.active and self.frames >= self.limit * self.SR

    @staticmethod
    def input_devices():
        """[(name, index), ...] of microphones. Windows lists each mic once per host API
        (MME / DirectSound / WASAPI ...), so only the default host API's are kept."""
        import sounddevice as sd
        api = sd.default.hostapi
        devs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        # MME cuts names at 31 chars; borrow the full name from another host API
        full = [d["name"] for d in devs if d["hostapi"] != api]
        out = []
        for d in devs:
            if d["hostapi"] != api or "Sound Mapper" in d["name"]:  # Mapper = the default mic
                continue
            name = next((f for f in full if f.startswith(d["name"])), d["name"])
            out.append((name, d["index"]))
        return out

    def start(self, seconds, path, device=None):
        """device: a sounddevice input index, None = the system default microphone."""
        import sounddevice as sd
        self._chunks, self.frames = [], 0
        self.limit, self.path = seconds, path
        max_frames = seconds * self.SR

        def callback(indata, frames, time_info, status):
            take = min(frames, max_frames - self.frames)
            if take > 0:
                self._chunks.append(bytes(indata)[:take * 2])   # int16 mono = 2 bytes/frame
                self.frames += take
            if self.frames >= max_frames:
                raise sd.CallbackStop

        self._stream = sd.RawInputStream(samplerate=self.SR, channels=1, dtype="int16",
                                         device=device, callback=callback)
        self._stream.start()

    def finish(self):
        """Stop (if still running), write the WAV and return its path (None if empty)."""
        if not self._stream:
            return None
        self._stream.stop()
        self._stream.close()
        self._stream = None
        if not self.frames:
            return None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with wave.open(self.path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.SR)
            w.writeframes(b"".join(self._chunks))
        self._chunks = []
        return self.path


class App:
    def __init__(self, root, parent=None):
        """parent: a frame to build into (e.g. a tab in app.py); None = own window."""
        self.root = root
        self.embedded = parent is not None
        parent = parent or root
        if not self.embedded:
            root.title("转录 / 录音 - Whisper")
            root.geometry("760x580")
        self.base_title = root.title()
        self._worker_thread = None

        self._models = {}          # name -> loaded model (cached)
        self._busy = False
        self._cancel = threading.Event()
        self._events = queue.Queue()
        self._patched = False
        self.current_file = None
        self.segments = []         # list of (start_sec, end_sec, text)
        self.player = Player()
        self._seeking = False      # True while the user drags the seek slider
        self._cur_line = None      # transcript line highlighted for playback
        self.recorder = Recorder()

        ui_style.apply(root)
        P = ui_style.PAD
        BW = 11   # 每行最左边那个按钮的宽度，三行对齐

        # --- top bar ---
        top = self.top = ttk.Frame(parent, padding=(P, P, P, P))
        top.pack(fill="x")
        ttk.Label(top, text="模型").pack(side="left")
        self.model_var = tk.StringVar(value="small.en")
        self.model_box = ttk.Combobox(top, textvariable=self.model_var, values=MODELS,
                                      state="readonly", width=11)
        self.model_box.pack(side="left", padx=(6, 12))
        self.open_btn = ttk.Button(top, text="打开文件…", command=self.browse)
        self.open_btn.pack(side="left")
        self.ts_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="显示时间戳", variable=self.ts_var,
                        command=self.render).pack(side="left", padx=12)
        self.save_audio_btn = ttk.Button(top, text="保存音频…", command=self.save_audio)
        self.save_audio_btn.pack(side="right")
        ttk.Button(top, text="保存文字…", command=self.save).pack(side="right", padx=4)
        ttk.Button(top, text="复制文字", command=self.copy).pack(side="right")

        # --- drop zone ---
        self.drop = ui_style.drop_zone(parent, DROP_HINT)
        self.drop.pack(fill="x", padx=P)
        self.drop.bind("<Button-1>", lambda e: None if self._busy or self.recorder.active
                       else self.browse())
        for w in (self.drop, parent):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", self.on_drop)

        # --- microphone ---
        mic = ttk.Frame(parent, padding=(P, P, P, 0))
        mic.pack(fill="x")
        ttk.Label(mic, text="麦克风").pack(side="left")
        self.mic_var = tk.StringVar(value=DEFAULT_MIC)
        self.mic_box = ttk.Combobox(mic, textvariable=self.mic_var, state="readonly")
        self.mic_box.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.mic_refresh = ttk.Button(mic, text="刷新", width=6, command=self._load_mics)
        self.mic_refresh.pack(side="left")
        self._mics = {}            # combobox label -> sounddevice index
        self._load_mics()

        # --- recorder ---
        rec = ttk.Frame(parent, padding=(P, P, P, 0))
        rec.pack(fill="x")
        self.rec_btn = ttk.Button(rec, text="● 录音", width=BW, command=self.toggle_record)
        self.rec_btn.pack(side="left")
        self.rec_len = tk.IntVar(value=REC_LENGTHS[0])
        self.rec_radios = [ttk.Radiobutton(rec, text=f"{n} 秒", value=n, variable=self.rec_len)
                           for n in REC_LENGTHS]
        for r in self.rec_radios:
            r.pack(side="left", padx=(10, 0))
        self.rec_dot = tk.Label(rec, text="", fg=ui_style.REC, width=6, anchor="w",
                                font=(ui_style.FONT, 10, "bold"))
        self.rec_dot.pack(side="left", padx=(8, 0))
        self.auto_tx = tk.BooleanVar(value=True)
        ttk.Checkbutton(rec, text="录完自动转录",
                        variable=self.auto_tx).pack(side="right")
        self.rec_time = tk.StringVar(value="00:00 / 01:00")
        ttk.Label(rec, textvariable=self.rec_time, width=13, anchor="e").pack(side="right")
        self.rec_bar = ttk.Progressbar(rec, mode="determinate", maximum=100)
        self.rec_bar.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.rec_len.trace_add("write", lambda *a: self._refresh_recorder())

        # --- start / cancel + progress ---
        ctl = ttk.Frame(parent, padding=(P, P, P, 0))
        ctl.pack(fill="x")
        self.start_btn = ttk.Button(ctl, text="开始转录", width=BW, command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(ctl, text="取消", width=8, command=self.cancel)
        self.cancel_btn.pack(side="left", padx=(4, 12))
        self.pct = tk.StringVar(value="0%")
        ttk.Label(ctl, textvariable=self.pct, width=13, anchor="e").pack(side="right")
        self.progress = ttk.Progressbar(ctl, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 6))

        # --- audio player ---
        ply = ttk.Frame(parent, padding=(P, P, P, 0))
        ply.pack(fill="x")
        self.play_btn = ttk.Button(ply, text="▶ 播放", width=BW, command=self.toggle_play)
        self.play_btn.pack(side="left")
        self.stop_btn = ttk.Button(ply, text="■ 停止", width=8, command=self.stop_play)
        self.stop_btn.pack(side="left", padx=(4, 12))
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(ply, textvariable=self.time_var, width=13, anchor="e").pack(side="right")
        self.seek_var = tk.DoubleVar(value=0)
        self.seek = ttk.Scale(ply, from_=0, to=1, variable=self.seek_var,
                              command=self._on_seek_drag)
        self.seek.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.seek.bind("<ButtonPress-1>", lambda e: setattr(self, "_seeking", True))
        self.seek.bind("<ButtonRelease-1>", self._on_seek_release)

        # --- output text ---
        frame = ttk.Frame(parent, padding=(P, P, P, 4))
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="word", font=(ui_style.TEXT_FONT, 12), undo=True,
                            relief="flat", padx=8, pady=6, spacing1=2, spacing3=2,
                            highlightthickness=1, highlightbackground=ui_style.DROP_BORDER,
                            highlightcolor=ui_style.ACCENT)
        sb = ttk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_configure("ts", foreground=ui_style.ACCENT, underline=True,
                                font=(ui_style.TEXT_FONT, 10))
        self.text.tag_configure("now", background="#fff3b0")
        # click a timestamp (or double-click any line) to play from there
        self.text.tag_bind("ts", "<Button-1>", self._on_text_click)
        self.text.tag_bind("ts", "<Enter>", lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind("ts", "<Leave>", lambda e: self.text.config(cursor="xterm"))
        self.text.bind("<Double-Button-1>", self._on_text_click)

        # --- status bar ---
        self.status = tk.StringVar(value="拖入或录一段音频开始")
        ttk.Label(parent, textvariable=self.status, foreground=ui_style.OK,
                  padding=(P, 0, P, P)).pack(fill="x")

        self._update_buttons()
        if not self.embedded:
            root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)

    def shutdown(self):
        self.player.stop()
        self.recorder.finish()   # keep whatever was recorded so far

    def on_close(self):
        self.shutdown()
        self.root.destroy()

    # ---------- recording ----------
    def toggle_record(self):
        if self.recorder.active:
            self._finish_record()   # stopped early by the user
            return
        if self._busy:
            return
        self.player.stop()          # don't record our own playback
        name = time.strftime("rec_%Y%m%d_%H%M%S.wav")
        try:
            self.recorder.start(self.rec_len.get(), os.path.join(REC_DIR, name),
                                device=self._mics.get(self.mic_var.get()))
        except Exception as e:
            messagebox.showerror("录音失败", f"打不开麦克风：\n{e}")
            return
        self.status.set(f"正在录音（{self.rec_len.get()} 秒，到时间自动停止；点「停止录音」可提前结束）")
        self._update_buttons()

    def _finish_record(self):
        try:
            path = self.recorder.finish()
        except Exception as e:
            messagebox.showerror("录音失败", str(e))
            path = None
        self._update_buttons()
        if not path:
            self.status.set("录音已停止，没有录到声音")
            return
        self.select(path)
        self.status.set(f"录好了 {self.fmt_time(self.recorder.elapsed)}，"
                        f"已存到 {path}")
        if self.auto_tx.get():
            self.start()

    def _load_mics(self):
        """(Re)fill the microphone list, e.g. after plugging in a headset."""
        try:
            devices = Recorder.input_devices()
        except Exception:          # sounddevice missing / no audio system
            devices = []
        self._mics = {DEFAULT_MIC: None}
        for name, idx in devices:
            self._mics.setdefault(name, idx)
        self.mic_box["values"] = list(self._mics)
        if self.mic_var.get() not in self._mics:   # the chosen mic was unplugged
            self.mic_var.set(DEFAULT_MIC)

    def _refresh_recorder(self):
        r = self.recorder
        total = r.limit if r.active else self.rec_len.get()
        done = r.elapsed if r.active else 0
        self.rec_time.set(f"{self.fmt_time(done)} / {self.fmt_time(total)}")
        self.rec_bar["value"] = 100 * done / total if total else 0
        self.rec_btn.config(text="■ 停止录音" if r.active else "● 录音",
                            state="disabled" if self._busy and not r.active else "normal")
        for rb in self.rec_radios:
            rb.config(state="disabled" if r.active else "normal")
        self.mic_box.config(state="disabled" if r.active else "readonly")
        self.mic_refresh.config(state="disabled" if r.active else "normal")
        # blinking red dot while recording (dot toggles every 0.5s, "REC" stays)
        if r.active:
            dot = "●" if int(time.time() * 2) % 2 == 0 else "  "
            self.rec_dot.config(text=f"{dot} 录音中")
            self.root.title(f"● 录音中 - {self.base_title}")
        elif self.rec_dot.cget("text"):
            self.rec_dot.config(text="")
            self.root.title(self.base_title)

    # ---------- playback ----------
    def toggle_play(self):
        if not self.player.path:
            return
        if self.player.playing:
            self.player.pause()
        else:
            self.player.play()
        self._refresh_player()

    def stop_play(self):
        self.player.stop()
        self.player.seek(0)
        self._refresh_player()

    def play_from(self, sec):
        self.player.play(sec)
        self._refresh_player()

    def _on_seek_drag(self, value):
        if self._seeking:  # show the target time while dragging
            self.time_var.set(f"{self.fmt_time(float(value))} / "
                              f"{self.fmt_time(self.player.duration)}")

    def _on_seek_release(self, event):
        self._seeking = False
        self.player.seek(self.seek_var.get())
        self._refresh_player()

    def _on_text_click(self, event):
        line = int(self.text.index(f"@{event.x},{event.y}").split(".")[0])
        if 1 <= line <= len(self.segments):   # one segment per line
            self.play_from(self.segments[line - 1][0])
            return "break"

    def _refresh_player(self):
        p = self.player
        if p.error:
            self.status.set(f"播放出错：{p.error}")
            p.error = None
        has_file = bool(p.path)
        self.play_btn.config(text="❚❚ 暂停" if p.playing else "▶ 播放",
                             state="normal" if has_file else "disabled")
        self.stop_btn.config(state="normal" if has_file else "disabled")
        self.seek.config(to=max(p.duration, 1))
        pos = p.position
        if not self._seeking:
            self.seek_var.set(pos)
            self.time_var.set(f"{self.fmt_time(pos)} / {self.fmt_time(p.duration)}")
        self._highlight(pos if (p.playing or pos > 0) else None)

    def _highlight(self, pos):
        line = None
        if pos is not None and self.segments:
            i = bisect.bisect_right([s[0] for s in self.segments], pos) - 1
            if i >= 0 and pos <= self.segments[i][1] + 0.5:
                line = i + 1
        if line == self._cur_line:
            return
        self.text.tag_remove("now", "1.0", "end")
        if line:
            self.text.tag_add("now", f"{line}.0", f"{line}.end")
            if self.player.playing:
                self.text.see(f"{line}.0")
        self._cur_line = line

    # ---------- timestamps ----------
    @staticmethod
    def fmt_time(sec):
        sec = int(sec)
        h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def insert_segment(self, seg):
        start, end, text = seg
        if self.ts_var.get():
            self.text.insert("end", f"[{self.fmt_time(start)} - {self.fmt_time(end)}] ", "ts")
        self.text.insert("end", text + "\n")

    def render(self):
        """Redraw all segments (used when the timestamp toggle changes)."""
        self.text.delete("1.0", "end")
        for seg in self.segments:
            self.insert_segment(seg)
        self._cur_line = None   # re-apply playback highlight on next tick

    # ---------- input ----------
    def on_drop(self, event):
        files = self.root.tk.splitlist(event.data)  # handles paths with spaces
        if files:
            self.select(files[0])

    def browse(self):
        path = filedialog.askopenfilename(
            filetypes=[("音频/视频", " ".join("*" + e for e in sorted(AUDIO_EXTS))),
                       ("所有文件", "*.*")])
        if path:
            self.select(path)

    def select(self, path):
        """Pick a file; transcription only begins when Start is clicked."""
        if self._busy:
            self.status.set("正在转录，先点「取消」再换文件")
            return
        if self.recorder.active:
            self.status.set("正在录音，先停止录音再换文件")
            return
        if not os.path.isfile(path):
            messagebox.showerror("错误", f"不是文件：\n{path}")
            return
        if os.path.splitext(path)[1].lower() not in AUDIO_EXTS:
            if not messagebox.askyesno("未知类型",
                                       "这看起来不像音频文件，还是要用它吗？"):
                return
        self.current_file = path
        self.player.load(path)
        self.segments = []
        self.text.delete("1.0", "end")
        self._cur_line = None
        self._show_file("点「开始转录」把它转成文字")
        self._set_progress(0)
        self.status.set("文件已就绪，可以播放，或点「开始转录」")
        self._update_buttons()

    # ---------- start / cancel ----------
    def _update_buttons(self):
        locked = self._busy or self.recorder.active
        self.start_btn.config(state="normal" if self.current_file and not locked
                              else "disabled")
        self.cancel_btn.config(state="normal" if self._busy else "disabled")
        self.open_btn.config(state="disabled" if locked else "normal")
        self.model_box.config(state="disabled" if self._busy else "readonly")
        self.save_audio_btn.config(state="normal" if self.current_file and
                                   not self.recorder.active else "disabled")
        self._refresh_recorder()

    def _set_progress(self, pct):
        self.progress["value"] = pct
        self.pct.set(f"{int(pct)}%")

    def start(self):
        if self._busy or not self.current_file:
            return
        self._busy = True
        self._cancel.clear()
        self.segments = []
        self.text.delete("1.0", "end")
        self._cur_line = None
        self._set_progress(0)
        self._update_buttons()
        self._worker_thread = threading.Thread(
            target=self._worker, args=(self.current_file, self.model_var.get()), daemon=True)
        self._worker_thread.start()

    def cancel(self):
        if self._busy:
            self._cancel.set()
            self.cancel_btn.config(state="disabled")
            self.status.set("正在取消……")

    # ---------- whisper progress hook ----------
    def _make_bar_class(self):
        """A tqdm stand-in: Whisper reports progress to it, and it raises on cancel.
        Only our own worker thread is tracked, so other Whisper users in the same
        process (e.g. the 精听 subtitles in app.py) are left alone."""
        app = self

        class Bar:
            def __init__(self, *args, total=None, unit="", **kwargs):
                self.total = total or 0
                self.n = 0
                self.mine = threading.current_thread() is app._worker_thread
                # "frames" = transcription, "iB" = model download
                self.phase = "转录中" if unit == "frames" else "下载模型"

            def update(self, n=1):
                if not self.mine:
                    return
                if app._cancel.is_set():
                    raise Cancelled()
                self.n += n
                if self.total:
                    app._events.put(("progress", (self.phase,
                                                  min(100.0, 100.0 * self.n / self.total))))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __getattr__(self, name):  # close(), set_description(), ... -> no-op
                return lambda *a, **k: None

        return Bar

    def _patch_whisper(self):
        if self._patched:
            return
        bar = self._make_bar_class()
        for name in ("whisper", "whisper.transcribe"):
            mod = importlib.import_module(name)
            t = getattr(mod, "tqdm", None)
            if inspect.ismodule(t):          # `import tqdm` style
                mod.tqdm = types.SimpleNamespace(tqdm=bar)
            elif t is not None:              # `from tqdm import tqdm` style
                mod.tqdm = bar
        self._patched = True

    # ---------- work ----------
    def _worker(self, path, model_name):
        try:
            self._events.put(("status", "正在读取音频……"))
            # Whisper wants 16 kHz mono float32; decoding here means it never calls ffmpeg
            audio = intensive_listening.load_audio(path, layout="mono", rate=16000)[0][:, 0]
            if self._cancel.is_set():
                raise Cancelled()
            try:
                segments = self._transcribe_here(audio, model_name)
            except ImportError:
                # the packaged .app has no Whisper (torch alone is 1GB+):
                # hand the audio to a Python on this computer that has it
                segments = self._transcribe_external(audio, model_name)
            for seg in segments:
                self._events.put(("seg", seg))
            self._events.put(("done", f"转录完成：{os.path.basename(path)}"))
        except Cancelled:
            self._events.put(("cancelled", "已取消"))
        except Exception as e:  # show any failure (no Whisper, bad file...)
            self._events.put(("error", str(e)))

    def _transcribe_here(self, audio, model_name):
        """Whisper in this process (raises ImportError when it isn't installed)."""
        import torch
        import whisper

        self._patch_whisper()
        if model_name not in self._models:
            self._events.put(("status", f"正在加载模型 {model_name}"
                                        "（第一次使用会先下载）……"))
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._models[model_name] = whisper.load_model(model_name, device=device)
        model = self._models[model_name]
        if self._cancel.is_set():
            raise Cancelled()

        self._events.put(("progress", ("转录中", 0)))
        result = model.transcribe(audio, language="en", task="transcribe",
                                  verbose=False, fp16=(model.device.type == "cuda"))
        return [(s["start"], s["end"], s["text"].strip()) for s in result["segments"]]

    def _transcribe_external(self, audio, model_name):
        """Run TRANSCRIBE_HELPER in another Python; it streams JSON lines back on stdout."""
        self._events.put(("status", "这个程序里没有 Whisper，正在找装了 Whisper 的 Python……"))
        py = intensive_listening.find_external_python()
        if not py:
            pip = "pip3" if sys.platform == "darwin" else "pip"
            raise RuntimeError("转录需要 Whisper：在终端运行 "
                               f"{pip} install openai-whisper，然后再点「开始转录」")
        if self._cancel.is_set():
            raise Cancelled()
        self._events.put(("status", f"正在加载模型 {model_name}（用 {py}；"
                                    "第一次使用会先下载）……"))
        segments = []
        with tempfile.TemporaryDirectory() as d:
            audio_path = os.path.join(d, "audio.npy")
            np.save(audio_path, audio.astype(np.float32))
            with open(os.path.join(d, "stderr.txt"), "w+b") as err:
                proc = subprocess.Popen([py, "-c", TRANSCRIBE_HELPER, audio_path, model_name],
                                        stdout=subprocess.PIPE, stderr=err,
                                        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)

                def kill_on_cancel():   # readline() below blocks, so cancel from here
                    while proc.poll() is None:
                        if self._cancel.wait(0.1):
                            proc.kill()
                            return
                threading.Thread(target=kill_on_cancel, daemon=True).start()

                for line in proc.stdout:
                    try:
                        kind, *data = json.loads(line)
                    except ValueError:
                        continue        # something else printed to stdout
                    if kind == "progress":
                        self._events.put(("progress", tuple(data)))
                    elif kind == "seg":
                        segments.append(tuple(data))
                proc.wait()
                if self._cancel.is_set():
                    raise Cancelled()
                if proc.returncode != 0:
                    err.seek(0)
                    lines = err.read().decode("utf-8", "replace").strip().splitlines()
                    raise RuntimeError(f"{os.path.basename(py)}: "
                                       f"{lines[-1] if lines else '未知错误'}")
        return segments

    def _poll(self):
        try:
            while True:
                kind, msg = self._events.get_nowait()
                if kind == "status":
                    self.status.set(msg)
                elif kind == "alert":
                    self.status.set("出错了")
                    messagebox.showerror("错误", msg)
                elif kind == "saved":
                    self._switch_to_saved(msg)
                elif kind == "progress":
                    phase, pct = msg
                    self._set_progress(pct)
                    if not self._cancel.is_set():
                        self.status.set(f"{phase}…… {pct:.0f}%")
                elif kind == "seg":
                    self.segments.append(msg)
                    self.insert_segment(msg)
                    self.text.see("end")
                else:  # done / cancelled / error
                    self._busy = False
                    if kind == "done":
                        self._set_progress(100)
                        self._show_file("已转录")
                        if not self.player.duration and self.segments:
                            self.player.duration = self.segments[-1][1]
                    elif kind == "cancelled":
                        self._set_progress(0)
                    self.status.set(msg if kind != "error" else "出错了")
                    self._update_buttons()
                    if kind == "error":
                        messagebox.showerror("转录失败", msg)
        except queue.Empty:
            pass
        if self.recorder.time_up:
            self._finish_record()       # auto-stop at 60s / 90s
        elif self.recorder.active:
            self._refresh_recorder()
        self._refresh_player()
        self.root.after(100, self._poll)

    # ---------- output ----------
    def copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.text.get("1.0", "end").strip())
        self.status.set("文字已复制到剪贴板")

    def save(self):
        content = self.text.get("1.0", "end").strip()
        if not content:
            return
        initial = (os.path.splitext(os.path.basename(self.current_file))[0] + ".txt"
                   if self.current_file else "transcript.txt")
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                            initialfile=initial,
                                            filetypes=[("文本", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content + "\n")
            self.status.set(f"文字已保存：{path}")

    def save_audio(self):
        """Save the current audio (e.g. a recording) - copied, or converted by extension."""
        src = self.current_file
        if not src or self.recorder.active:
            return
        ext = os.path.splitext(src)[1].lower()
        types_ = [("WAV", "*.wav"), ("MP3", "*.mp3"), ("M4A / AAC", "*.m4a"),
                  ("FLAC", "*.flac")]
        types_.sort(key=lambda t: t[1] != "*" + ext)   # original format first
        dst = filedialog.asksaveasfilename(initialfile=os.path.basename(src),
                                           defaultextension=ext, filetypes=types_)
        if not dst:
            return
        if os.path.abspath(dst).lower() == os.path.abspath(src).lower():
            self.status.set("这就是原文件，不用再存")
            return
        self.status.set(f"正在保存音频 → {os.path.basename(dst)}……")
        threading.Thread(target=self._save_audio_worker, args=(src, dst),
                         daemon=True).start()

    def _save_audio_worker(self, src, dst):
        try:
            if os.path.splitext(dst)[1].lower() == os.path.splitext(src)[1].lower():
                shutil.copy2(src, dst)
            else:   # different format -> convert it with PyAV
                convert_audio(src, dst)
            self._events.put(("saved", dst))
        except Exception as e:
            self._events.put(("alert", f"音频没能保存：\n{e}"))

    def _switch_to_saved(self, dst):
        """After Save audio, the saved copy becomes the current file (transcript kept)."""
        p = self.player
        pos, was_playing = p.position, p.playing
        self.current_file = dst
        p.load(dst)
        if was_playing:
            p.play(pos)
        else:
            p.seek(pos)
        self._show_file("已另存" + ("，已转录" if self.segments else ""))
        self.status.set(f"音频已保存：{dst}")
        self._update_buttons()

    def _show_file(self, note):
        """Drop zone shows the current file name, its folder, and a short state note."""
        path = self.current_file
        folder = os.path.dirname(os.path.abspath(path))
        if len(folder) > 60:   # 太深的路径只留结尾，免得撑满拖放区
            folder = "…" + folder[-59:]
        self.drop.config(text=f"{os.path.basename(path)}\n{folder}\n（{note}）")
