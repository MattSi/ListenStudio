"""「朗读」页：把自己写的英文答案读出来听，再录自己读一遍来对比发音。

用法：
  ① 在文字框里写或粘贴英文（自动保存草稿，下次打开还在）
  ② 「▶ 朗读」：用选好的语音 / 语速读出来，正在读的那句会高亮
  ③ 「● 录我的朗读」：自己读一遍（最长 10 分钟，点「停止录音」结束）
  ④ 「✓ 检查发音」：用 Whisper 听你的录音，和原文逐词对比：
       漏读的词标红，读得不对（被听成别的词）的标橙，再给一个读对的比例；
     「↔ 逐句对比」：一句一句地先放标准、再放你的（还没检查过会先自动检查，
       因为要靠检查结果才知道你的录音里每句在哪）。对比那一行可以
       ⏮ 上一句 / ↻ 重听这句 / 下一句 ⏭ / ■ 停止，进度条显示到第几句；
       勾「连续播放」一句完了自动接下一句，不勾就停在这句等你选；
       也可以双击文字框里的某一句，直接对比那一句
语音合成见 tts.py（Edge 在线语音为主，Kokoro 离线语音备用）。
"""
import difflib
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import app_paths
import intensive_listening
import transcriber
import tts
import ui_style

REC_LIMIT = 600   # 「录我的朗读」最长 10 分钟
MY_REC_DIR = os.path.join(app_paths.RECORDINGS_DIR, "reader")
GUIDE = "① 写好英文（或粘贴）  ② ▶ 朗读，听标准读法  ③ ● 录下自己读一遍  ④ ✓ 检查发音 / ↔ 逐句对比"

_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")


def _norm(w):
    return w.lower().replace("’", "'")


def compare_reading(text, heard):
    """把 Whisper 听到的词（[(开始秒, 结束秒, 词), ...]）和原文逐词对齐。
    返回 dict：
      ref:     原文每个词 (起, 止字符, 状态)，状态 ok / wrong（读成了别的词）/ miss（漏读）
      hyp:     听到的每个词 (词, 状态)，状态 ok / wrong / extra（原文里没有）
      ref_time:原文每个词在录音里大概的 (起, 止) 秒；对不上的是 None
      correct / total / miss / wrong / extra：统计"""
    ref = [(m.start(), m.end(), _norm(m.group())) for m in _WORD.finditer(text)]
    hyp = []
    for s, e, w in heard:
        for m in _WORD.finditer(w):
            hyp.append((s, e, _norm(m.group()), m.group()))
    ref_state = ["miss"] * len(ref)
    hyp_state = ["extra"] * len(hyp)
    ref_time = [None] * len(ref)
    sm = difflib.SequenceMatcher(None, [r[2] for r in ref], [h[2] for h in hyp], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                ref_state[i1 + k] = hyp_state[j1 + k] = "ok"
                ref_time[i1 + k] = hyp[j1 + k][:2]
        elif op == "replace":
            span = (hyp[j1][0], hyp[j2 - 1][1])   # 这几个词大概在录音的这一段
            for i in range(i1, i2):
                ref_state[i], ref_time[i] = "wrong", span
            for j in range(j1, j2):
                hyp_state[j] = "wrong"
    return {
        "ref": [(a, b, st) for (a, b, _), st in zip(ref, ref_state)],
        "hyp": [(h[3], st) for h, st in zip(hyp, hyp_state)],
        "ref_time": ref_time,
        "correct": ref_state.count("ok"), "total": len(ref),
        "miss": ref_state.count("miss"), "wrong": ref_state.count("wrong"),
        "extra": hyp_state.count("extra"),
    }


def sentence_ranges(spans, result, pad=0.15):
    """每句原文在我的录音里的 (起, 止) 秒（按这句里对上的词算）；一个词都没对上就是 None。"""
    out = []
    for a, b in spans:
        ts = [t for (s, e, _), t in zip(result["ref"], result["ref_time"])
              if t and s >= a and e <= b]
        out.append((max(0.0, min(t[0] for t in ts) - pad), max(t[1] for t in ts) + pad)
                   if ts else None)
    return out


class Reader:
    def __init__(self, root, parent, settings, mic_busy=lambda: False):
        """settings：和另外两页共用的设置字典（settings.json）；
        mic_busy()：转录页正在录音时返回 True（麦克风只能给一边用）。"""
        self.root, self.settings, self.mic_busy = root, settings, mic_busy
        self.active = False
        self.player = transcriber.Player()
        self.recorder = transcriber.Recorder()
        self.speech = None      # tts.Speech：当前文字的标准朗读
        self.mine = None        # 我的录音文件
        self.result = None      # compare_reading 的结果（对应 self.mine 和当时的文字）
        self.playlist = []      # [(文件, 起秒, 止秒, 第几句)]：逐句对比 / 整段播放
        self.pl_idx = -1
        self.pl_wait = None     # 两段之间停一下的 after 任务
        self.now_sentence = None
        self.job = None         # 正在后台做的事："synth" / "check" / "download"
        self.cancel_ev = threading.Event()
        self.events = queue.Queue()
        self.draft_job = None

        ui_style.apply(root)
        P = ui_style.PAD
        BW = 13

        # --- 工具栏：文字文件 + 离线语音 ---
        top = self.top = ttk.Frame(parent, padding=(P, P, P, 0))
        top.pack(fill="x")
        ttk.Button(top, text="打开文本…", command=self.open_text).pack(side="left")
        ttk.Button(top, text="保存文本…", command=self.save_text).pack(side="left", padx=4)
        ttk.Button(top, text="清空", command=self.clear_text).pack(side="left")
        self.offline_btn = ttk.Button(top, command=self.download_offline)
        self.offline_btn.pack(side="right")
        self.offline_lbl = ttk.Label(top, style="Hint.TLabel")
        self.offline_lbl.pack(side="right", padx=(0, 6))

        # --- 语音选项 ---
        opt = ttk.Frame(parent, padding=(P, 8, P, 0))
        opt.pack(fill="x")
        ttk.Label(opt, text="语音").pack(side="left")
        voice = settings["tts_voice"] if settings["tts_voice"] in tts.VOICE_NAMES else tts.VOICE_IDS[0]
        self.voice_var = tk.StringVar(value=tts.VOICE_NAMES[voice])
        vbox = ttk.Combobox(opt, textvariable=self.voice_var, state="readonly", width=18,
                            values=[tts.VOICE_NAMES[v] for v in tts.VOICE_IDS])
        vbox.pack(side="left", padx=(6, 14))
        vbox.bind("<<ComboboxSelected>>", lambda e: (vbox.selection_clear(), self.on_option()))
        ttk.Label(opt, text="语速").pack(side="left")
        self.speed_var = tk.StringVar(value=f"{settings['tts_speed']:g}")
        sp = ttk.Spinbox(opt, from_=tts.SPEED_RANGE[0], to=tts.SPEED_RANGE[1], increment=0.1,
                         width=5, textvariable=self.speed_var, command=self.on_option)
        sp.pack(side="left", padx=(6, 2))
        ttk.Label(opt, text="倍").pack(side="left", padx=(0, 14))
        ttk.Label(opt, text="句间停顿").pack(side="left")
        self.pause_var = tk.StringVar(value=f"{settings['tts_pause']:g}")
        ps = ttk.Spinbox(opt, from_=tts.PAUSE_RANGE[0], to=tts.PAUSE_RANGE[1], increment=0.1,
                         width=5, textvariable=self.pause_var, command=self.on_option)
        ps.pack(side="left", padx=(6, 2))
        ttk.Label(opt, text="秒").pack(side="left", padx=(0, 14))
        for w in (sp, ps):
            w.bind("<Return>", lambda e: self.on_option())
            w.bind("<FocusOut>", lambda e: self.on_option())
        ttk.Label(opt, text="引擎").pack(side="left")
        self.engine_var = tk.StringVar(value=tts.ENGINES.get(settings["tts_engine"],
                                                              tts.ENGINES["auto"]))
        ebox = ttk.Combobox(opt, textvariable=self.engine_var, state="readonly", width=14,
                            values=list(tts.ENGINES.values()))
        ebox.pack(side="left", padx=(6, 0))
        ebox.bind("<<ComboboxSelected>>", lambda e: (ebox.selection_clear(), self.on_option()))

        # --- 文字框 ---
        ttk.Label(parent, text=GUIDE, style="Hint.TLabel",
                  padding=(P, 8, P, 2)).pack(fill="x")
        tf = ttk.Frame(parent, padding=(P, 0, P, 0))
        tf.pack(fill="both", expand=True)
        self.text = tk.Text(tf, wrap="word", font=(ui_style.TEXT_FONT, 13), undo=True,
                            relief="flat", padx=10, pady=8, spacing1=3, spacing3=3, height=8,
                            highlightthickness=1, highlightbackground=ui_style.DROP_BORDER,
                            highlightcolor=ui_style.ACCENT)
        sb = ttk.Scrollbar(tf, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.text.tag_configure("now", background="#fff3b0")
        self.text.tag_configure("miss", background="#ffd6d6", foreground="#b42318",
                                overstrike=True)
        self.text.tag_configure("wrong", background="#ffe2b8", foreground="#9a4b00")
        self.text.tag_raise("miss")
        self.text.tag_raise("wrong")
        self.text.bind("<<Modified>>", self.on_text_modified)

        # --- 标准朗读 ---
        std = ttk.Frame(parent, padding=(P, P, P, 0))
        std.pack(fill="x")
        self.read_btn = ttk.Button(std, text="▶ 朗读", width=BW, command=self.read_aloud)
        self.read_btn.pack(side="left")
        self.stop_btn = ttk.Button(std, text="■ 停止", width=8, command=self.stop)
        self.stop_btn.pack(side="left", padx=(4, 12))
        self.save_audio_btn = ttk.Button(std, text="保存朗读音频…", command=self.save_audio)
        self.save_audio_btn.pack(side="right")
        self.std_time = tk.StringVar(value="")
        ttk.Label(std, textvariable=self.std_time, width=16, anchor="e").pack(side="right")
        self.bar = ttk.Progressbar(std, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        # --- 我的朗读 ---
        me = ttk.Frame(parent, padding=(P, 8, P, 0))
        me.pack(fill="x")
        self.rec_btn = ttk.Button(me, text="● 录我的朗读", width=BW, command=self.toggle_record)
        self.rec_btn.pack(side="left")
        self.rec_dot = tk.Label(me, text="", fg=ui_style.REC, width=7, anchor="w",
                                font=(ui_style.FONT, 10, "bold"))
        self.rec_dot.pack(side="left", padx=(6, 0))
        self.play_me_btn = ttk.Button(me, text="▶ 听我的", command=self.play_mine)
        self.play_me_btn.pack(side="left", padx=(0, 4))
        self.cmp_btn = ttk.Button(me, text="↔ 逐句对比", command=self.compare_play)
        self.cmp_btn.pack(side="left", padx=(0, 4))
        self.check_btn = ttk.Button(me, text="✓ 检查发音", command=self.check)
        self.check_btn.pack(side="left")
        self.me_time = tk.StringVar(value="")
        ttk.Label(me, textvariable=self.me_time, width=16, anchor="e").pack(side="right")

        # --- 逐句对比：标准一句、你的一句；可以重听、换句、连续播放 ---
        cmp = ttk.Frame(parent, padding=(P, 8, P, 0))
        cmp.pack(fill="x")
        self.cmp_buttons = {}
        for key, text, cmd, w in (("prev", "⏮ 上一句", lambda: self.play_pair(self.cmp_idx - 1), 9),
                                  ("again", "↻ 重听这句", lambda: self.play_pair(self.cmp_idx), 10),
                                  ("next", "下一句 ⏭", lambda: self.play_pair(self.cmp_idx + 1), 9),
                                  ("stop", "■ 停止", self.stop, 7)):
            b = ttk.Button(cmp, text=text, width=w, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.cmp_buttons[key] = b
        self.cmp_auto = tk.BooleanVar(value=settings["cmp_auto"])
        ttk.Checkbutton(cmp, text="连续播放", variable=self.cmp_auto,
                        command=self.on_cmp_auto).pack(side="right")
        self.cmp_info = ttk.Label(cmp, width=30, anchor="w")
        self.cmp_info.pack(side="right", padx=(8, 8))
        self.cmp_bar = ttk.Progressbar(cmp, mode="determinate", maximum=1)
        self.cmp_bar.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.cmp_idx = 0          # 逐句对比现在在第几句
        self.cmp_playing = False  # 正在放这一句的「标准 → 你的」
        self.cmp_after_check = False  # 点了逐句对比但还没检查：检查完自动开始
        self.text.bind("<Double-Button-1>", self.on_text_double_click, add="+")

        # --- 检查结果 ---
        res = ttk.Frame(parent, padding=(P, 8, P, 0))
        res.pack(fill="x")
        self.score = ttk.Label(res, text="", font=(ui_style.FONT, 11, "bold"))
        self.score.pack(anchor="w")
        self.heard = tk.Text(res, wrap="word", height=3, font=(ui_style.TEXT_FONT, 11),
                             relief="flat", padx=8, pady=4, bg="#f7f9fc", highlightthickness=1,
                             highlightbackground=ui_style.DROP_BORDER, state="disabled")
        self.heard.pack(fill="x", pady=(2, 0))
        self.heard.tag_configure("wrong", background="#ffe2b8", foreground="#9a4b00")
        self.heard.tag_configure("extra", foreground="#888888", overstrike=True)
        self.heard.tag_configure("label", foreground=ui_style.MUTED)

        self.status = tk.StringVar(value="写好英文后点「▶ 朗读」")
        ttk.Label(parent, textvariable=self.status, foreground=ui_style.OK,
                  padding=(P, 6, P, P)).pack(fill="x")

        self.load_draft()
        self.refresh()
        root.after(100, self.poll)

    # ---------- 文字 ----------
    def get_text(self):
        return self.text.get("1.0", "end-1c")

    def set_text(self, s):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", s)
        self.text.edit_reset()

    def load_draft(self):
        try:
            with open(app_paths.READER_DRAFT, encoding="utf-8") as f:
                self.set_text(f.read())
        except OSError:
            pass
        self.text.edit_modified(False)

    def save_draft(self):
        self.draft_job = None
        try:
            with open(app_paths.READER_DRAFT, "w", encoding="utf-8") as f:
                f.write(self.get_text())
        except OSError:
            pass

    def on_text_modified(self, event=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        # 文字改了：标准朗读和检查结果都对不上了
        if self.speech or self.result:
            self.stop()
            self.speech = None
            self.clear_result()
        if self.draft_job:
            self.root.after_cancel(self.draft_job)
        self.draft_job = self.root.after(800, self.save_draft)
        self.refresh()

    def open_text(self):
        path = filedialog.askopenfilename(filetypes=[("文本", "*.txt"), ("所有文件", "*.*")])
        if path:
            with open(path, encoding="utf-8", errors="replace") as f:
                self.set_text(f.read())
            self.status.set(f"已打开：{os.path.basename(path)}")

    def save_text(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="answer.txt",
                                            filetypes=[("文本", "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.get_text().rstrip() + "\n")
            self.status.set(f"文字已保存：{path}")

    def clear_text(self):
        if self.get_text().strip() and not messagebox.askyesno("清空", "清空文字框里的内容吗？"):
            return
        self.set_text("")

    # ---------- 选项 ----------
    def options(self):
        voice = next((v for v in tts.VOICE_IDS if tts.VOICE_NAMES[v] == self.voice_var.get()),
                     tts.VOICE_IDS[0])
        engine = next((k for k, v in tts.ENGINES.items() if v == self.engine_var.get()), "auto")
        return voice, self._num(self.speed_var, "tts_speed", tts.SPEED_RANGE), \
            self._num(self.pause_var, "tts_pause", tts.PAUSE_RANGE), engine

    def _num(self, var, key, rng):
        try:
            v = round(min(rng[1], max(rng[0], float(var.get()))), 2)
        except ValueError:
            v = self.settings[key]   # 不是数字：恢复原来的值
        var.set(f"{v:g}")
        return v

    def on_option(self):
        voice, speed, pause, engine = self.options()
        new = {"tts_voice": voice, "tts_speed": speed, "tts_pause": pause, "tts_engine": engine}
        if any(self.settings[k] != v for k, v in new.items()):
            self.settings.update(new)
            intensive_listening.save_settings(self.settings)
            if self.speech:   # 换了声音 / 语速，下次朗读重新合成
                self.stop()
                self.speech = None
        self.refresh()

    # ---------- 标准朗读 ----------
    def read_aloud(self):
        if self.job or self.recorder.active:
            return
        text = self.get_text()
        if not text.strip():
            self.status.set("先在上面写一段英文")
            return
        self.stop()
        if self.speech:
            self.play_speech()
            return
        voice, speed, pause, engine = self.options()
        self.start_job("synth", self._synth_worker, text, voice, speed, pause, engine)

    def _synth_worker(self, text, voice, speed, pause, engine):
        def progress(i, n, eng):
            self.events.put(("progress", (100 * i / n,
                                          f"{tts.ENGINE_NAMES[eng]} 合成中…… 第 {min(i + 1, n)} / {n} 句")))
        try:
            sp = tts.synthesize(text, voice, speed, pause, engine, progress, self.cancel_ev)
            self.events.put(("synth_done", (text, sp)))
        except (tts.Cancelled, transcriber.Cancelled):
            self.events.put(("job_end", "已取消"))
        except Exception as e:
            self.events.put(("synth_error", str(e)))

    def play_speech(self):
        sp = self.speech
        self.start_playlist([(sp.path, 0.0, None, None)], "▶ 标准朗读中")

    def save_audio(self):
        if not self.speech:
            return
        dst = filedialog.asksaveasfilename(initialfile="reading.wav", defaultextension=".wav",
                                           filetypes=[("WAV", "*.wav"), ("MP3", "*.mp3"),
                                                      ("M4A / AAC", "*.m4a"), ("FLAC", "*.flac")])
        if not dst:
            return
        try:
            transcriber.convert_audio(self.speech.path, dst)
            self.status.set(f"朗读音频已保存：{dst}")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    # ---------- 离线语音 ----------
    def download_offline(self):
        if self.job or tts.offline_ready():
            return
        if not messagebox.askyesno(
                "下载离线语音",
                f"要从 GitHub 下载 Kokoro 离线语音模型吗？\n大约 {tts.OFFLINE_SIZE_MB} MB，"
                f"只需下载一次，存在：\n{tts.KOKORO_DIR}\n\n下载后，没网时也能朗读。"):
            return
        self.start_job("download", self._download_worker)

    def _download_worker(self):
        def progress(done, total):
            self.events.put(("progress", (100 * done / total if total else 0,
                                          f"下载离线语音…… {done / 1e6:.0f} / {total / 1e6:.0f} MB")))
        try:
            tts.download_offline(progress, self.cancel_ev)
            self.events.put(("job_end", "离线语音下载好了，没网时也能朗读"))
        except tts.Cancelled:
            self.events.put(("job_end", "已取消下载"))
        except Exception as e:
            self.events.put(("job_error", f"离线语音没能下载：{e}"))

    # ---------- 我的朗读 ----------
    def toggle_record(self):
        if self.recorder.active:
            self.finish_record()
            return
        if self.job:
            return
        if self.mic_busy():
            self.status.set("转录页正在录音，等它录完再录")
            return
        self.stop()   # 不要把喇叭里的声音也录进去
        path = os.path.join(MY_REC_DIR, time.strftime("me_%Y%m%d_%H%M%S.wav"))
        try:
            self.recorder.start(REC_LIMIT, path)
        except Exception as e:
            messagebox.showerror("录音失败", f"打不开麦克风：\n{e}")
            return
        self.status.set("正在录音……对着麦克风把上面的文字读一遍，读完点「■ 停止录音」")
        self.refresh()

    def finish_record(self):
        try:
            path = self.recorder.finish()
        except Exception as e:
            messagebox.showerror("录音失败", str(e))
            path = None
        if path:
            self.mine = path
            self.clear_result()
            self.status.set(f"录好了 {transcriber.App.fmt_time(self.recorder.elapsed)}："
                            "可以「▶ 听我的」，或「✓ 检查发音」")
        else:
            self.status.set("录音已停止，没有录到声音")
        self.refresh()

    def play_mine(self):
        if self.mine and not self.recorder.active:
            self.start_playlist([(self.mine, 0.0, None, None)], "▶ 播放我的录音")

    # ---------- 逐句对比 ----------
    def pairs(self):
        """每句的 (标准起秒, 标准止秒, 我的 (起, 止) 或 None)；还不能逐句对比时返回 None。"""
        if not (self.speech and self.mine and self.result):
            return None
        mine = sentence_ranges(self.speech.spans, self.result)
        return [(a, b, m) for (a, b), m in zip(self.speech.times, mine)]

    def compare_play(self):
        """「↔ 逐句对比」：从当前这句开始；还没检查过发音就先检查，检查完自动开始。"""
        if not (self.speech and self.mine) or self.recorder.active or self.job:
            if not self.speech:
                self.status.set("先点「▶ 朗读」生成标准读法，再对比")
            return
        if not self.result:
            self.cmp_after_check = True
            self.check()
            return
        n = len(self.pairs())
        self.play_pair(0 if self.cmp_idx >= n - 1 and not self.cmp_playing else self.cmp_idx)

    def play_pair(self, i):
        """放第 i 句：先标准，再你的。"""
        pairs = self.pairs()
        if not pairs or self.recorder.active or self.job:
            return
        i = max(0, min(len(pairs) - 1, i))
        a, b, m = pairs[i]
        items = [(self.speech.path, a, b, i)]
        if m:
            items.append((self.mine, m[0], m[1], i))
        self.cmp_idx = i
        note = "" if m else "（你的录音里没找到这句，只放标准）"
        self.start_playlist(items, f"↔ 逐句对比：第 {i + 1} / {len(pairs)} 句，先标准，再你的{note}")
        self.cmp_playing = True
        self.refresh()

    def pair_done(self):
        """这一句的「标准 → 你的」放完了：连续播放就接着下一句，否则停在这句等你选。"""
        self.cmp_playing = False
        n = len(self.pairs() or [])
        i = self.cmp_idx
        self.set_now(i)   # 停下来时也标出是哪一句
        if self.cmp_auto.get() and i < n - 1:
            self.status.set(f"第 {i + 1} 句对比完，马上放下一句……")
            self.pl_wait = self.root.after(800, lambda: self.play_pair(i + 1))
        elif i >= n - 1:
            self.status.set(f"全部 {n} 句对比完了：可以「↻ 重听这句」，或「⏮ 上一句」往回听")
        else:
            self.status.set(f"第 {i + 1} 句对比完：「↻ 重听这句」再听一遍，或者「下一句 ⏭」")
        self.refresh()

    def on_cmp_auto(self):
        self.settings["cmp_auto"] = self.cmp_auto.get()
        intensive_listening.save_settings(self.settings)

    def on_text_double_click(self, event):
        """检查过发音后，双击文字框里的某一句，就从那一句开始对比。"""
        if not self.pairs():
            return
        pos = len(self.text.get("1.0", f"@{event.x},{event.y}"))
        for i, (a, b) in enumerate(self.speech.spans):
            if a <= pos <= b:
                self.root.after(10, lambda: self.play_pair(i))   # 等双击选中单词的默认动作做完
                return

    # ---------- 检查发音 ----------
    def check(self):
        if self.job or not self.mine or self.recorder.active:
            return
        text = self.get_text()
        if not text.strip():
            self.status.set("文字框是空的，没法对比")
            return
        self.stop()
        self.start_job("check", self._check_worker, self.mine, text, self.settings["whisper_model"])

    def _check_worker(self, path, text, model):
        self.events.put(("progress", (0, f"用 Whisper（{model}）听你的录音……第一次会慢一些")))
        try:
            words = intensive_listening.transcribe_words(path, model)
            self.events.put(("check_done", (path, text, compare_reading(text, words))))
        except Exception as e:
            self.events.put(("job_error", f"检查没能完成：{e}"))

    def show_result(self, r):
        self.result = r
        self.text.tag_remove("miss", "1.0", "end")
        self.text.tag_remove("wrong", "1.0", "end")
        for a, b, st in r["ref"]:
            if st != "ok":
                self.text.tag_add(st, f"1.0+{a}c", f"1.0+{b}c")
        pct = 100 * r["correct"] / r["total"] if r["total"] else 0
        self.score.config(text=f"读对 {r['correct']} / {r['total']} 个词（{pct:.0f}%）"
                               f"   漏读 {r['miss']}   读错 {r['wrong']}   多读 {r['extra']}"
                               "      红色 = 漏读，橙色 = 被听成了别的词")
        self.heard.config(state="normal")
        self.heard.delete("1.0", "end")
        self.heard.insert("end", "Whisper 听到的：", "label")
        for w, st in r["hyp"]:
            self.heard.insert("end", " ")
            self.heard.insert("end", w, () if st == "ok" else st)
        self.heard.config(state="disabled")

    def clear_result(self):
        self.result = None
        self.cmp_idx, self.cmp_after_check = 0, False
        self.text.tag_remove("miss", "1.0", "end")
        self.text.tag_remove("wrong", "1.0", "end")
        self.score.config(text="")
        self.heard.config(state="normal")
        self.heard.delete("1.0", "end")
        self.heard.config(state="disabled")

    # ---------- 播放列表 ----------
    def start_playlist(self, items, msg):
        self.stop()
        self.playlist, self.pl_idx = items, -1
        self.status.set(msg)
        self.next_item()

    def next_item(self):
        self.pl_wait = None
        self.pl_idx += 1
        if self.pl_idx >= len(self.playlist):
            self.playlist, self.pl_idx = [], -1
            if self.cmp_playing:
                self.pair_done()
                return
            self.set_now(None)
            self.status.set("播放完了")
            self.refresh()
            return
        path, start, _, _ = self.playlist[self.pl_idx]
        if self.player.path != path:
            self.player.load(path)
        self.player.play(start)
        self.refresh()

    def stop(self):
        """停掉播放 / 逐句对比；正在合成或检查的话也取消。"""
        if self.job in ("synth", "check"):
            self.cancel_ev.set()
        if self.pl_wait:
            self.root.after_cancel(self.pl_wait)
            self.pl_wait = None
        self.playlist, self.pl_idx = [], -1
        self.cmp_playing = False
        self.player.stop()
        self.set_now(None)
        self.refresh()

    def set_now(self, i):
        """高亮正在读的第 i 句（None = 不高亮）。"""
        if i == self.now_sentence:
            return
        self.now_sentence = i
        self.text.tag_remove("now", "1.0", "end")
        if i is not None and self.speech and i < len(self.speech.spans):
            a, b = self.speech.spans[i]
            self.text.tag_add("now", f"1.0+{a}c", f"1.0+{b}c")
            self.text.see(f"1.0+{a}c")

    # ---------- 后台任务 ----------
    def start_job(self, kind, fn, *args):
        self.job = kind
        self.cancel_ev = threading.Event()
        self.bar["value"] = 0
        threading.Thread(target=fn, args=args, daemon=True).start()
        self.refresh()

    def poll(self):
        try:
            while True:
                kind, msg = self.events.get_nowait()
                if kind == "progress":
                    self.bar["value"], text = msg
                    self.status.set(text)
                    continue
                self.job = None
                self.bar["value"] = 0
                if kind == "synth_done":
                    text, sp = msg
                    if text == self.get_text():   # 合成途中文字没被改过
                        self.speech = sp
                        how = "（离线）" if sp.engine == "kokoro" else ""
                        self.status.set(f"合成好了{how}，开始朗读")
                        self.play_speech()
                elif kind == "synth_error":
                    self.status.set("朗读失败")
                    if tts.offline_ready():
                        messagebox.showerror("朗读失败", msg)
                    elif messagebox.askyesno(
                            "朗读失败", f"{msg}\n\n要下载离线语音（约 {tts.OFFLINE_SIZE_MB} MB）吗？"
                                        "下载后没网也能朗读。"):
                        self.root.after(10, self.download_offline_now)
                elif kind == "check_done":
                    path, text, r = msg
                    if path == self.mine and text == self.get_text():
                        self.show_result(r)
                        self.status.set("检查完了：可以「↔ 逐句对比」，一句一句听差别")
                        if self.cmp_after_check:   # 是点「逐句对比」触发的检查：接着开始对比
                            self.cmp_after_check = False
                            self.root.after(10, lambda: self.play_pair(0))
                    self.cmp_after_check = False
                elif kind == "job_error":
                    self.cmp_after_check = False
                    self.status.set("出错了")
                    messagebox.showerror("出错了", msg)
                else:   # job_end
                    self.status.set(msg)
                self.refresh()
        except queue.Empty:
            pass
        self.tick()
        self.root.after(80, self.poll)

    def download_offline_now(self):
        """朗读失败后用户已经同意下载，不再重复问。"""
        if not self.job and not tts.offline_ready():
            self.start_job("download", self._download_worker)

    def tick(self):
        """播放进度、正在读的句子、逐句对比换下一段、录音计时。"""
        p = self.player
        if p.error:
            self.status.set(f"播放出错：{p.error}")
            p.error = None
        if self.playlist and self.pl_idx >= 0 and not self.pl_wait:
            path, start, end, sent = self.playlist[self.pl_idx]
            pos = p.position
            on_std = self.speech and path == self.speech.path
            if on_std:
                self.set_now(sent if sent is not None else self._sentence_at(pos))
            else:
                self.set_now(sent)
            done = not p.playing or (end is not None and pos >= end)   # 放完了，或到了这句的结尾
            if done:
                p.stop()
                gap = 400 if self.pl_idx + 1 < len(self.playlist) else 0
                self.pl_wait = self.root.after(gap, self.next_item)
            if self.job is None:
                self.bar["value"] = 100 * pos / p.duration if p.duration else 0
            if self.cmp_playing:
                self.update_cmp_info()   # 「正在放：标准 / 你的」
        fmt = transcriber.App.fmt_time
        if self.speech and self.player.path == self.speech.path and self.playlist:
            self.std_time.set(f"{fmt(p.position)} / {fmt(p.duration)}")
        elif self.speech:
            self.std_time.set(f"共 {fmt(self.speech.times[-1][1])}")
        else:
            self.std_time.set("")
        r = self.recorder
        if r.active:
            if r.time_up:
                self.finish_record()
            else:
                dot = "●" if int(time.time() * 2) % 2 == 0 else "  "
                self.rec_dot.config(text=f"{dot} 录音中")
                self.me_time.set(f"{fmt(r.elapsed)} / {fmt(REC_LIMIT)}")
        else:
            if self.rec_dot.cget("text"):
                self.rec_dot.config(text="")
            self.me_time.set(f"我的录音 {fmt(self.recorder.elapsed)}" if self.mine else "")

    def _sentence_at(self, pos):
        for i, (a, b) in enumerate(self.speech.times):
            if a <= pos <= b + 0.05:
                return i
        return None

    def refresh(self):
        has_text = bool(self.get_text().strip())
        busy = self.job is not None
        rec = self.recorder.active
        playing = bool(self.playlist)
        st = lambda ok: "normal" if ok else "disabled"
        self.read_btn.config(state=st(has_text and not busy and not rec))
        self.stop_btn.config(state=st(playing or self.job in ("synth", "check")))
        self.save_audio_btn.config(state=st(self.speech is not None and not busy))
        self.rec_btn.config(text="■ 停止录音" if rec else "● 录我的朗读",
                            state=st(rec or not busy))
        self.play_me_btn.config(state=st(self.mine and not rec))
        self.cmp_btn.config(state=st(self.mine and self.speech and not rec and not busy))
        self.check_btn.config(state=st(self.mine and has_text and not rec and not busy))
        pairs = self.pairs()
        ok = bool(pairs) and not rec and not busy
        n, i = len(pairs or []), self.cmp_idx
        b = self.cmp_buttons
        b["prev"].config(state=st(ok and i > 0))
        b["again"].config(state=st(ok))
        b["next"].config(state=st(ok and i < n - 1))
        b["stop"].config(state=st(self.cmp_playing or (pairs and self.pl_wait)))
        self.cmp_bar.config(maximum=max(n, 1), value=(i + 1) if pairs else 0)
        self.update_cmp_info()

    def update_cmp_info(self):
        pairs = self.pairs()
        if not pairs:
            hint = ("点「↔ 逐句对比」开始（会先检查发音）" if self.mine and self.speech
                    else "朗读 + 录好自己的之后，可以逐句对比")
            self.cmp_info.config(text=hint, style="Hint.TLabel")
            return
        i, n = self.cmp_idx, len(pairs)
        text = f"第 {i + 1} / {n} 句"
        if self.cmp_playing and 0 <= self.pl_idx < len(self.playlist):
            part = "标准" if self.playlist[self.pl_idx][0] == self.speech.path else "你的"
            text += f" · 正在放：{part}"
        elif pairs[i][2] is None:
            text += " · 你的录音里没找到这句"
        self.cmp_info.config(text=text, style="TLabel")
        if tts.offline_ready():
            self.offline_lbl.config(text="离线语音 ✓ 已下载")
            self.offline_btn.pack_forget()
        else:
            self.offline_lbl.config(text="没网时用的离线语音：")
            if not self.offline_btn.winfo_manager():
                self.offline_btn.pack(side="right")
            self.offline_btn.config(text=f"下载（约 {tts.OFFLINE_SIZE_MB} MB）",
                                    state=st(not busy))

    def shutdown(self):
        """离开页面 / 关程序：停播放；录了一半的也存下来。"""
        self.stop()
        if self.recorder.active:
            self.finish_record()
        if self.draft_job:
            self.root.after_cancel(self.draft_job)
            self.save_draft()
