"""The 朗读 (Read aloud) tab: hear your own English answer read aloud, then record yourself reading it and compare pronunciation.

How to use:
  1. Write or paste English into the text box (the draft is saved automatically and is still there next time).
  2. "▶ 朗读": read it with the chosen voice / rate; the sentence being read is highlighted.
  3. "● 录我的朗读": record yourself reading it (up to 10 minutes; click "停止录音" to finish).
  4. "✓ 检查发音": Whisper listens to your recording and compares it word by word with the text:
       skipped words are marked red, wrong words (heard as a different word) orange,
       plus the share of words read correctly.
     "↔ 逐句对比" (compare by sentence): plays the reference sentence, then yours, one by one
       (if not checked yet, it checks first, because the check result tells where each
       sentence is in your recording). The compare row has
       ⏮ previous / ↻ replay this one / next ⏭ / ■ stop, and a progress bar for the sentence number;
       with "连续播放" (continuous) ticked it moves on by itself, otherwise it stops and waits;
       you can also double-click a sentence in the text box to compare that sentence.
       While comparing, the text switches to a one-sentence-per-line view (the end of each
       line shows how many words of it were right; click a line to compare that sentence);
       "✎ 返回编辑" goes back to the editable text box.
Speech synthesis: see tts.py (Edge online voices first, Kokoro offline voices as backup).
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
from i18n import tr

REC_LIMIT = 600   # "录我的朗读" (record my reading) is limited to 10 minutes
MY_REC_DIR = os.path.join(app_paths.RECORDINGS_DIR, "reader")
GUIDE = tr("① 写好英文（或粘贴）  ② ▶ 朗读，听标准读法  ③ ● 录下自己读一遍  ④ ✓ 检查发音 / ↔ 逐句对比")

_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")


def _norm(w):
    return w.lower().replace("’", "'")


def compare_reading(text, heard):
    """Align the words Whisper heard ([(start s, end s, word), ...]) with the text, word by word.
    Returns a dict:
      ref:     each word of the text (start, end char, state); state ok / wrong (read as another word) / miss (skipped)
      hyp:     each word heard (word, state); state ok / wrong / extra (not in the text)
      ref_time:each text word's rough (start, end) seconds in the recording; None if it didn't match
      correct / total / miss / wrong / extra: counts"""
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
            span = (hyp[j1][0], hyp[j2 - 1][1])   # These words are roughly in this part of the recording
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
    """Each text sentence's (start, end) seconds in my recording (from the words that matched); None if no word matched."""
    out = []
    for a, b in spans:
        ts = [t for (s, e, _), t in zip(result["ref"], result["ref_time"])
              if t and s >= a and e <= b]
        out.append((max(0.0, min(t[0] for t in ts) - pad), max(t[1] for t in ts) + pad)
                   if ts else None)
    return out


class Reader:
    def __init__(self, root, parent, settings, mic_busy=lambda: False):
        """settings: the settings dict shared with the other two tabs (settings.json);
        mic_busy(): returns True while the transcribe tab is recording (only one side can use the mic)."""
        self.root, self.settings, self.mic_busy = root, settings, mic_busy
        self.active = False
        self.player = transcriber.Player()
        self.recorder = transcriber.Recorder()
        self.speech = None      # tts.Speech: the reference reading of the current text
        self.mine = None        # My recording file
        self.result = None      # Result of compare_reading (matches self.mine and the text at that time)
        self.playlist = []      # [(file, start s, end s, sentence no.)]: compare by sentence / play the whole thing
        self.pl_idx = -1
        self.pl_wait = None     # after() job for the short pause between two parts
        self.now_sentence = None
        self.job = None         # What is running in the background: "synth" / "check" / "download"
        self.cancel_ev = threading.Event()
        self.events = queue.Queue()
        self.draft_job = None

        ui_style.apply(root)
        P = ui_style.PAD
        BW = 13

        # --- Toolbar: text file + offline voice ---
        top = self.top = ttk.Frame(parent, padding=(P, P, P, 0))
        top.pack(fill="x")
        ttk.Button(top, text=tr("打开文本…"), command=self.open_text).pack(side="left")
        ttk.Button(top, text=tr("保存文本…"), command=self.save_text).pack(side="left", padx=4)
        ttk.Button(top, text=tr("清空"), command=self.clear_text).pack(side="left")
        self.offline_btn = ttk.Button(top, command=self.download_offline)
        self.offline_btn.pack(side="right")
        self.offline_lbl = ttk.Label(top, style="Hint.TLabel")
        self.offline_lbl.pack(side="right", padx=(0, 6))

        # --- Voice options ---
        opt = ttk.Frame(parent, padding=(P, 8, P, 0))
        opt.pack(fill="x")
        ttk.Label(opt, text=tr("语音")).pack(side="left")
        voice = settings["tts_voice"] if settings["tts_voice"] in tts.VOICE_NAMES else tts.VOICE_IDS[0]
        self.voice_var = tk.StringVar(value=tts.VOICE_NAMES[voice])
        vbox = ttk.Combobox(opt, textvariable=self.voice_var, state="readonly", width=18,
                            values=[tts.VOICE_NAMES[v] for v in tts.VOICE_IDS])
        vbox.pack(side="left", padx=(6, 14))
        vbox.bind("<<ComboboxSelected>>", lambda e: (vbox.selection_clear(), self.on_option()))
        ttk.Label(opt, text=tr("语速")).pack(side="left")
        self.speed_var = tk.StringVar(value=f"{settings['tts_speed']:g}")
        sp = ttk.Spinbox(opt, from_=tts.SPEED_RANGE[0], to=tts.SPEED_RANGE[1], increment=0.1,
                         width=5, textvariable=self.speed_var, command=self.on_option)
        sp.pack(side="left", padx=(6, 2))
        ttk.Label(opt, text=tr("倍")).pack(side="left", padx=(0, 14))
        ttk.Label(opt, text=tr("句间停顿")).pack(side="left")
        self.pause_var = tk.StringVar(value=f"{settings['tts_pause']:g}")
        ps = ttk.Spinbox(opt, from_=tts.PAUSE_RANGE[0], to=tts.PAUSE_RANGE[1], increment=0.1,
                         width=5, textvariable=self.pause_var, command=self.on_option)
        ps.pack(side="left", padx=(6, 2))
        ttk.Label(opt, text=tr("秒")).pack(side="left", padx=(0, 14))
        for w in (sp, ps):
            w.bind("<Return>", lambda e: self.on_option())
            w.bind("<FocusOut>", lambda e: self.on_option())
        ttk.Label(opt, text=tr("引擎")).pack(side="left")
        self.engine_var = tk.StringVar(value=tts.ENGINES.get(settings["tts_engine"],
                                                              tts.ENGINES["auto"]))
        ebox = ttk.Combobox(opt, textvariable=self.engine_var, state="readonly", width=14,
                            values=list(tts.ENGINES.values()))
        ebox.pack(side="left", padx=(6, 0))
        ebox.bind("<<ComboboxSelected>>", lambda e: (ebox.selection_clear(), self.on_option()))

        # --- Text box: normally editable text; while comparing, switches to a one-sentence-per-line view ---
        guide = ttk.Frame(parent, padding=(P, 8, P, 2))
        guide.pack(fill="x")
        self.guide_lbl = ttk.Label(guide, text=GUIDE, style="Hint.TLabel")
        self.guide_lbl.pack(side="left")
        self.edit_btn = ttk.Button(guide, text=tr("✎ 返回编辑"), command=self.show_editor)
        tf = ttk.Frame(parent, padding=(P, 0, P, 0))
        tf.pack(fill="both", expand=True)
        self.edit_frame = ttk.Frame(tf)
        self.edit_frame.pack(fill="both", expand=True)
        self.list_frame = ttk.Frame(tf)   # Sentence view, only shown while comparing by sentence
        box = dict(wrap="word", font=(ui_style.TEXT_FONT, 13), relief="flat", padx=10, pady=8,
                   height=8, highlightthickness=1, highlightbackground=ui_style.DROP_BORDER,
                   highlightcolor=ui_style.ACCENT)
        self.text = tk.Text(self.edit_frame, undo=True, spacing1=3, spacing3=3, **box)
        self.lines = tk.Text(self.list_frame, spacing1=5, spacing3=5, cursor="hand2",
                             state="disabled", **box)
        for t, frame in ((self.text, self.edit_frame), (self.lines, self.list_frame)):
            sb = ttk.Scrollbar(frame, command=t.yview)
            t.configure(yscrollcommand=sb.set)
            t.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            t.tag_configure("now", background="#fff3b0")
            t.tag_configure("miss", background="#ffd6d6", foreground="#b42318", overstrike=True)
            t.tag_configure("wrong", background="#ffe2b8", foreground="#9a4b00")
            t.tag_raise("miss")
            t.tag_raise("wrong")
        self.lines.tag_configure("num", foreground=ui_style.MUTED, font=(ui_style.TEXT_FONT, 10))
        self.lines.tag_configure("score", foreground="#9a4b00", font=(ui_style.TEXT_FONT, 10))
        self.lines.tag_configure("score_ok", foreground=ui_style.OK, font=(ui_style.TEXT_FONT, 10))
        self.lines.bind("<Button-1>", self.on_line_click)
        self.view = "edit"   # "edit" = editing text / "list" = sentence view
        self.text.bind("<<Modified>>", self.on_text_modified)

        # --- Reference reading ---
        std = ttk.Frame(parent, padding=(P, P, P, 0))
        std.pack(fill="x")
        self.read_btn = ttk.Button(std, text=tr("▶ 朗读"), width=BW, command=self.read_aloud)
        self.read_btn.pack(side="left")
        self.stop_btn = ttk.Button(std, text=tr("■ 停止"), width=8, command=self.stop)
        self.stop_btn.pack(side="left", padx=(4, 12))
        self.save_audio_btn = ttk.Button(std, text=tr("保存朗读音频…"), command=self.save_audio)
        self.save_audio_btn.pack(side="right")
        self.std_time = tk.StringVar(value="")
        ttk.Label(std, textvariable=self.std_time, width=16, anchor="e").pack(side="right")
        self.bar = ttk.Progressbar(std, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True, padx=(0, 6))

        # --- My reading ---
        me = ttk.Frame(parent, padding=(P, 8, P, 0))
        me.pack(fill="x")
        self.rec_btn = ttk.Button(me, text=tr("● 录我的朗读"), width=BW, command=self.toggle_record)
        self.rec_btn.pack(side="left")
        self.rec_dot = tk.Label(me, text="", fg=ui_style.REC, width=7, anchor="w",
                                font=(ui_style.FONT, 10, "bold"))
        self.rec_dot.pack(side="left", padx=(6, 0))
        self.play_me_btn = ttk.Button(me, text=tr("▶ 听我的"), command=self.play_mine)
        self.play_me_btn.pack(side="left", padx=(0, 4))
        self.cmp_btn = ttk.Button(me, text=tr("↔ 逐句对比"), command=self.compare_play)
        self.cmp_btn.pack(side="left", padx=(0, 4))
        self.check_btn = ttk.Button(me, text=tr("✓ 检查发音"), command=self.check)
        self.check_btn.pack(side="left")
        self.me_time = tk.StringVar(value="")
        ttk.Label(me, textvariable=self.me_time, width=16, anchor="e").pack(side="right")

        # --- Compare by sentence: one reference sentence, then yours; can replay, change sentence, play continuously ---
        cmp = ttk.Frame(parent, padding=(P, 8, P, 0))
        cmp.pack(fill="x")
        self.cmp_buttons = {}
        for key, text, cmd, w in (("prev", tr("⏮ 上一句"), lambda: self.play_pair(self.cmp_idx - 1), 9),
                                  ("again", tr("↻ 重听这句"), lambda: self.play_pair(self.cmp_idx), 10),
                                  ("next", tr("下一句 ⏭"), lambda: self.play_pair(self.cmp_idx + 1), 9),
                                  ("stop", tr("■ 停止"), self.stop, 7)):
            b = ttk.Button(cmp, text=text, width=w, command=cmd)
            b.pack(side="left", padx=(0, 4))
            self.cmp_buttons[key] = b
        self.cmp_auto = tk.BooleanVar(value=settings["cmp_auto"])
        ttk.Checkbutton(cmp, text=tr("连续播放"), variable=self.cmp_auto,
                        command=self.on_cmp_auto).pack(side="right")
        self.cmp_info = ttk.Label(cmp, width=30, anchor="w")
        self.cmp_info.pack(side="right", padx=(8, 8))
        self.cmp_bar = ttk.Progressbar(cmp, mode="determinate", maximum=1)
        self.cmp_bar.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.cmp_idx = 0          # Which sentence the comparison is on now
        self.cmp_playing = False  # Playing this sentence's "reference -> yours"
        self.cmp_pending = False  # Compare was clicked and synthesis / checking is being done first: start comparing when it's done
        self.text.bind("<Double-Button-1>", self.on_text_double_click, add="+")

        # --- Check results ---
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

        self.status = tk.StringVar(value=tr("写好英文后点「▶ 朗读」"))
        ttk.Label(parent, textvariable=self.status, foreground=ui_style.OK,
                  padding=(P, 6, P, P)).pack(fill="x")

        self.load_draft()
        self.refresh()
        root.after(100, self.poll)

    # ---------- Text ----------
    def get_text(self):
        return self.text.get("1.0", "end-1c")

    def set_text(self, s):
        self.show_editor()   # The text changed: go back to the edit view
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
        # The text was edited: the reference reading and the check result no longer match it
        if self.speech or self.result:
            self.stop()
            self.speech = None
            self.clear_result()
        if self.draft_job:
            self.root.after_cancel(self.draft_job)
        self.draft_job = self.root.after(800, self.save_draft)
        self.refresh()

    def open_text(self):
        path = filedialog.askopenfilename(filetypes=[(tr("文本"), "*.txt"), (tr("所有文件"), "*.*")])
        if path:
            with open(path, encoding="utf-8", errors="replace") as f:
                self.set_text(f.read())
            self.status.set(tr("已打开：{name}", name=os.path.basename(path)))

    def save_text(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="answer.txt",
                                            filetypes=[(tr("文本"), "*.txt")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.get_text().rstrip() + "\n")
            self.status.set(tr("文字已保存：{path}", path=path))

    def clear_text(self):
        if self.get_text().strip() and not messagebox.askyesno(tr("清空"), tr("清空文字框里的内容吗？")):
            return
        self.set_text("")

    # ---------- Options ----------
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
            v = self.settings[key]   # Not a number: restore the old value
        var.set(f"{v:g}")
        return v

    def on_option(self):
        voice, speed, pause, engine = self.options()
        new = {"tts_voice": voice, "tts_speed": speed, "tts_pause": pause, "tts_engine": engine}
        if any(self.settings[k] != v for k, v in new.items()):
            self.settings.update(new)
            intensive_listening.save_settings(self.settings)
            if self.speech:   # Voice / rate changed: synthesize again on the next reading
                self.stop()
                self.speech = None
        self.refresh()

    # ---------- Reference reading ----------
    def read_aloud(self):
        if self.job or self.recorder.active:
            return
        text = self.get_text()
        if not text.strip():
            self.status.set(tr("先在上面写一段英文"))
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
                                          tr("{engine} 合成中…… 第 {i} / {n} 句", engine=tts.ENGINE_NAMES[eng], i=min(i + 1, n), n=n))))
        try:
            sp = tts.synthesize(text, voice, speed, pause, engine, progress, self.cancel_ev)
            self.events.put(("synth_done", (text, sp)))
        except (tts.Cancelled, transcriber.Cancelled):
            self.events.put(("job_end", tr("已取消")))
        except Exception as e:
            self.events.put(("synth_error", str(e)))

    def play_speech(self):
        sp = self.speech
        self.start_playlist([(sp.path, 0.0, None, None)], tr("▶ 标准朗读中"))

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
            self.status.set(tr("朗读音频已保存：{dst}", dst=dst))
        except Exception as e:
            messagebox.showerror(tr("保存失败"), str(e))

    # ---------- Offline voice ----------
    def download_offline(self):
        if self.job or tts.offline_ready():
            return
        if not messagebox.askyesno(
                tr("下载离线语音"),
                tr("要从 GitHub 下载 Kokoro 离线语音模型吗？\n大约 {mb} MB，只需下载一次，存在：\n{folder}\n\n下载后，没网时也能朗读。", mb=tts.OFFLINE_SIZE_MB, folder=tts.KOKORO_DIR)):
            return
        self.start_job("download", self._download_worker)

    def _download_worker(self):
        def progress(done, total):
            self.events.put(("progress", (100 * done / total if total else 0,
                                          tr("下载离线语音…… {done:.0f} / {total:.0f} MB", done=done / 1e6, total=total / 1e6))))
        try:
            tts.download_offline(progress, self.cancel_ev)
            self.events.put(("job_end", tr("离线语音下载好了，没网时也能朗读")))
        except tts.Cancelled:
            self.events.put(("job_end", tr("已取消下载")))
        except Exception as e:
            self.events.put(("job_error", tr("离线语音没能下载：{e}", e=e)))

    # ---------- My reading ----------
    def toggle_record(self):
        if self.recorder.active:
            self.finish_record()
            return
        if self.job:
            return
        if self.mic_busy():
            self.status.set(tr("转录页正在录音，等它录完再录"))
            return
        self.stop()   # Don't record the sound from the speakers too
        path = os.path.join(MY_REC_DIR, time.strftime("me_%Y%m%d_%H%M%S.wav"))
        try:
            self.recorder.start(REC_LIMIT, path)
        except Exception as e:
            messagebox.showerror(tr("录音失败"), tr("打不开麦克风：\n{e}", e=e))
            return
        self.status.set(tr("正在录音……对着麦克风把上面的文字读一遍，读完点「■ 停止录音」"))
        self.refresh()

    def finish_record(self):
        try:
            path = self.recorder.finish()
        except Exception as e:
            messagebox.showerror(tr("录音失败"), str(e))
            path = None
        if path:
            self.mine = path
            self.clear_result()
            self.status.set(tr("录好了 {t}：可以「▶ 听我的」，或「✓ 检查发音」", t=transcriber.App.fmt_time(self.recorder.elapsed)))
        else:
            self.status.set(tr("录音已停止，没有录到声音"))
        self.refresh()

    def play_mine(self):
        if self.mine and not self.recorder.active:
            self.start_playlist([(self.mine, 0.0, None, None)], tr("▶ 播放我的录音"))

    # ---------- Compare by sentence ----------
    def pairs(self):
        """Each sentence's (reference start s, reference end s, mine (start, end) or None); None if comparing isn't possible yet."""
        if not (self.speech and self.mine and self.result):
            return None
        mine = sentence_ranges(self.speech.spans, self.result)
        return [(a, b, m) for (a, b), m in zip(self.speech.times, mine)]

    def compare_play(self):
        """"↔ 逐句对比" (compare by sentence): first fill in whatever is missing - synthesize the reference
        reading if there is none, check pronunciation if not checked yet - then compare from the current sentence."""
        if self.recorder.active or self.job:
            return
        text = self.get_text()
        if not self.mine:
            self.status.set(tr("先「● 录我的朗读」，把上面的文字读一遍，再逐句对比"))
            return
        if not text.strip():
            self.status.set(tr("文字框是空的，没法对比"))
            return
        if not self.speech:   # Step 1: synthesize the reference reading (don't play it when done; go on to the next step)
            self.cmp_pending = True
            self.stop()
            voice, speed, pause, engine = self.options()
            self.start_job("synth", self._synth_worker, text, voice, speed, pause, engine)
            return
        if not self.result:   # Step 2: check pronunciation (needed to know where each sentence is in your recording)
            self.cmp_pending = True
            self.check()
            return
        self.cmp_pending = False
        n = len(self.pairs())
        self.play_pair(0 if self.cmp_idx >= n - 1 and not self.cmp_playing else self.cmp_idx)

    def play_pair(self, i):
        """Play sentence i: reference first, then yours."""
        pairs = self.pairs()
        if not pairs or self.recorder.active or self.job:
            return
        i = max(0, min(len(pairs) - 1, i))
        a, b, m = pairs[i]
        items = [(self.speech.path, a, b, i)]
        if m:
            items.append((self.mine, m[0], m[1], i))
        self.cmp_idx = i
        if self.view != "list":
            self.show_list()   # While comparing, view one sentence per line
        note = "" if m else tr("（你的录音里没找到这句，只放标准）")
        self.start_playlist(items, tr("↔ 逐句对比：第 {i} / {n} 句，先标准，再你的{note}", i=i + 1, n=len(pairs), note=note))
        self.cmp_playing = True
        self.refresh()

    def pair_done(self):
        """This sentence's "reference -> yours" has finished: with continuous play go to the next one, otherwise stop here and wait."""
        self.cmp_playing = False
        n = len(self.pairs() or [])
        i = self.cmp_idx
        self.set_now(i)   # Also mark which sentence it is when stopped
        if self.cmp_auto.get() and i < n - 1:
            self.status.set(tr("第 {i} 句对比完，马上放下一句……", i=i + 1))
            self.pl_wait = self.root.after(800, lambda: self.play_pair(i + 1))
        elif i >= n - 1:
            self.status.set(tr("全部 {n} 句对比完了：可以「↻ 重听这句」，或「⏮ 上一句」往回听", n=n))
        else:
            self.status.set(tr("第 {i} 句对比完：「↻ 重听这句」再听一遍，或者「下一句 ⏭」", i=i + 1))
        self.refresh()

    def on_cmp_auto(self):
        self.settings["cmp_auto"] = self.cmp_auto.get()
        intensive_listening.save_settings(self.settings)

    def on_text_double_click(self, event):
        """After checking pronunciation, double-click a sentence in the text box to start comparing from it."""
        if not self.pairs():
            return
        pos = len(self.text.get("1.0", f"@{event.x},{event.y}"))
        for i, (a, b) in enumerate(self.speech.spans):
            if a <= pos <= b:
                self.root.after(10, lambda: self.play_pair(i))   # Wait until the default double-click action (selecting a word) is done
                return

    # ---------- Sentence view (one sentence per line) ----------
    LIST_GUIDE = tr("逐句视图：一句一行，点哪一行就对比哪一句   红色 = 漏读，橙色 = 读错，行尾 = 这句读对的词数")

    def show_list(self):
        """Show the text one sentence per line (only the display changes; the original paragraphs and line breaks are kept)."""
        text = self.get_text()
        r = self.result
        L = self.lines
        L.config(state="normal")
        L.delete("1.0", "end")
        for i, (a, b) in enumerate(tts.split_sentences(text)):
            L.insert("end", f"{i + 1:>2}   ", "num")
            start = L.index("end-1c")
            L.insert("end", text[a:b].replace("\n", " "))   # Same length, so the character positions below still match
            if r:
                ok = total = 0
                for s, e, st in r["ref"]:
                    if s >= a and e <= b:
                        total += 1
                        ok += st == "ok"
                        if st != "ok":
                            L.tag_add(st, f"{start}+{s - a}c", f"{start}+{e - a}c")
                L.insert("end", f"   {ok}/{total}", "score_ok" if ok == total else "score")
            L.insert("end", "\n")
        L.config(state="disabled")
        if self.view != "list":
            self.view = "list"
            self.edit_frame.pack_forget()
            self.list_frame.pack(fill="both", expand=True)
            self.guide_lbl.config(text=self.LIST_GUIDE)
            self.edit_btn.pack(side="right")
        self._mark(self.now_sentence)

    def show_editor(self):
        """"✎ 返回编辑": go back to the editable text box."""
        if self.view == "edit":
            return
        self.view = "edit"
        self.list_frame.pack_forget()
        self.edit_frame.pack(fill="both", expand=True)
        self.guide_lbl.config(text=GUIDE)
        self.edit_btn.pack_forget()
        self._mark(self.now_sentence)
        self.text.focus_set()

    def on_line_click(self, event):
        line = int(self.lines.index(f"@{event.x},{event.y}").split(".")[0])
        if self.pairs() and 1 <= line <= len(self.speech.spans):
            self.play_pair(line - 1)
        return "break"   # Don't put the cursor in the read-only sentence view

    # ---------- Check pronunciation ----------
    def check(self):
        if self.job or not self.mine or self.recorder.active:
            return
        text = self.get_text()
        if not text.strip():
            self.status.set(tr("文字框是空的，没法对比"))
            return
        self.stop()
        self.start_job("check", self._check_worker, self.mine, text, self.settings["whisper_model"])

    def _check_worker(self, path, text, model):
        self.events.put(("progress", (0, tr("用 Whisper（{model}）听你的录音……第一次会慢一些", model=model))))
        try:
            words = intensive_listening.transcribe_words(path, model)
            self.events.put(("check_done", (path, text, compare_reading(text, words))))
        except Exception as e:
            self.events.put(("job_error", tr("检查没能完成：{e}", e=e)))

    def show_result(self, r):
        self.result = r
        self.text.tag_remove("miss", "1.0", "end")
        self.text.tag_remove("wrong", "1.0", "end")
        for a, b, st in r["ref"]:
            if st != "ok":
                self.text.tag_add(st, f"1.0+{a}c", f"1.0+{b}c")
        pct = 100 * r["correct"] / r["total"] if r["total"] else 0
        self.score.config(text=tr("读对 {ok} / {total} 个词（{pct:.0f}%）   漏读 {miss}   读错 {wrong}   多读 {extra}      红色 = 漏读，橙色 = 被听成了别的词", ok=r['correct'], total=r['total'], pct=pct, miss=r['miss'], wrong=r['wrong'], extra=r['extra']))
        self.heard.config(state="normal")
        self.heard.delete("1.0", "end")
        self.heard.insert("end", tr("Whisper 听到的："), "label")
        for w, st in r["hyp"]:
            self.heard.insert("end", " ")
            self.heard.insert("end", w, () if st == "ok" else st)
        self.heard.config(state="disabled")
        if self.view == "list":
            self.show_list()   # Update the red / orange marks and the word count on each line

    def clear_result(self):
        self.result = None
        self.cmp_idx = 0
        self.text.tag_remove("miss", "1.0", "end")
        self.text.tag_remove("wrong", "1.0", "end")
        self.score.config(text="")
        self.heard.config(state="normal")
        self.heard.delete("1.0", "end")
        self.heard.config(state="disabled")
        if self.view == "list":
            self.show_list()

    # ---------- Playlist ----------
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
            self.status.set(tr("播放完了"))
            self.refresh()
            return
        path, start, _, _ = self.playlist[self.pl_idx]
        if self.player.path != path:
            self.player.load(path)
        self.player.play(start)
        self.refresh()

    def stop(self):
        """Stop playback / comparing; also cancel synthesis or checking in progress."""
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
        """Highlight sentence i being read (None = no highlight)."""
        if i == self.now_sentence:
            return
        self.now_sentence = i
        self._mark(i)

    def _mark(self, i):
        """Mark sentence i in whichever view is shown (in the sentence view, the whole line)."""
        self.text.tag_remove("now", "1.0", "end")
        self.lines.tag_remove("now", "1.0", "end")
        if i is None or not self.speech or i >= len(self.speech.spans):
            return
        if self.view == "list":
            self.lines.tag_add("now", f"{i + 1}.0", f"{i + 2}.0")
            self.lines.see(f"{i + 1}.0")
        else:
            a, b = self.speech.spans[i]
            self.text.tag_add("now", f"1.0+{a}c", f"1.0+{b}c")
            self.text.see(f"1.0+{a}c")

    # ---------- Background jobs ----------
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
                # Comparing is waiting for this step: if it worked, go on to the next step, otherwise give up
                pending, self.cmp_pending = self.cmp_pending, False
                if kind == "synth_done":
                    text, sp = msg
                    if text == self.get_text():   # The text wasn't edited during synthesis
                        self.speech = sp
                        how = tr("（离线）") if sp.engine == "kokoro" else ""
                        if pending:
                            self.status.set(tr("标准朗读合成好了{how}，接着检查发音……", how=how))
                            self.root.after(10, self.compare_play)
                        else:
                            self.status.set(tr("合成好了{how}，开始朗读", how=how))
                            self.play_speech()
                elif kind == "synth_error":
                    self.status.set(tr("朗读失败"))
                    if tts.offline_ready():
                        messagebox.showerror(tr("朗读失败"), msg)
                    elif messagebox.askyesno(
                            tr("朗读失败"), tr("{msg}\n\n要下载离线语音（约 {mb} MB）吗？下载后没网也能朗读。", msg=msg, mb=tts.OFFLINE_SIZE_MB)):
                        self.root.after(10, self.download_offline_now)
                elif kind == "check_done":
                    path, text, r = msg
                    if path == self.mine and text == self.get_text():
                        self.show_result(r)
                        self.status.set(tr("检查完了：可以「↔ 逐句对比」，一句一句听差别"))
                        if pending:   # This check was started by "逐句对比": start comparing next
                            self.root.after(10, self.compare_play)
                elif kind == "job_error":
                    self.status.set(tr("出错了"))
                    messagebox.showerror(tr("出错了"), msg)
                else:   # job_end
                    self.status.set(msg)
                self.refresh()
        except queue.Empty:
            pass
        self.tick()
        self.root.after(80, self.poll)

    def download_offline_now(self):
        """The user already agreed to the download after reading failed; don't ask again."""
        if not self.job and not tts.offline_ready():
            self.start_job("download", self._download_worker)

    def tick(self):
        """Playback progress, the sentence being read, moving to the next part while comparing, recording timer."""
        p = self.player
        if p.error:
            self.status.set(tr("播放出错：{err}", err=p.error))
            p.error = None
        if self.playlist and self.pl_idx >= 0 and not self.pl_wait:
            path, start, end, sent = self.playlist[self.pl_idx]
            pos = p.position
            on_std = self.speech and path == self.speech.path
            if on_std:
                self.set_now(sent if sent is not None else self._sentence_at(pos))
            else:
                self.set_now(sent)
            done = not p.playing or (end is not None and pos >= end)   # Finished playing, or reached the end of this sentence
            if done:
                p.stop()
                gap = 400 if self.pl_idx + 1 < len(self.playlist) else 0
                self.pl_wait = self.root.after(gap, self.next_item)
            if self.job is None:
                self.bar["value"] = 100 * pos / p.duration if p.duration else 0
            if self.cmp_playing:
                self.update_cmp_info()   # "正在放：标准 / 你的" (now playing: reference / yours)
        fmt = transcriber.App.fmt_time
        if self.speech and self.player.path == self.speech.path and self.playlist:
            self.std_time.set(f"{fmt(p.position)} / {fmt(p.duration)}")
        elif self.speech:
            self.std_time.set(tr("共 {t}", t=fmt(self.speech.times[-1][1])))
        else:
            self.std_time.set("")
        r = self.recorder
        if r.active:
            if r.time_up:
                self.finish_record()
            else:
                dot = "●" if int(time.time() * 2) % 2 == 0 else "  "
                self.rec_dot.config(text=tr("{dot} 录音中", dot=dot))
                self.me_time.set(f"{fmt(r.elapsed)} / {fmt(REC_LIMIT)}")
        else:
            if self.rec_dot.cget("text"):
                self.rec_dot.config(text="")
            self.me_time.set(tr("我的录音 {t}", t=fmt(self.recorder.elapsed)) if self.mine else "")

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
        self.rec_btn.config(text=tr("■ 停止录音") if rec else tr("● 录我的朗读"),
                            state=st(rec or not busy))
        self.play_me_btn.config(state=st(self.mine and not rec))
        self.cmp_btn.config(state=st(self.mine and has_text and not rec and not busy))
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
        if tts.offline_ready():
            self.offline_lbl.config(text=tr("离线语音 ✓ 已下载"))
            self.offline_btn.pack_forget()
        else:
            self.offline_lbl.config(text=tr("没网时用的离线语音："))
            if not self.offline_btn.winfo_manager():
                self.offline_btn.pack(side="right")
            self.offline_btn.config(text=tr("下载（约 {mb} MB）", mb=tts.OFFLINE_SIZE_MB),
                                    state=st(not busy))

    def update_cmp_info(self):
        pairs = self.pairs()
        if not pairs:
            hint = (tr("点「↔ 逐句对比」开始（缺朗读 / 检查会自动先做）") if self.mine
                    else tr("录好自己的朗读后，可以逐句对比"))
            self.cmp_info.config(text=hint, style="Hint.TLabel")
            return
        i, n = self.cmp_idx, len(pairs)
        text = tr("第 {i} / {n} 句##cmp", i=i + 1, n=n)
        if self.cmp_playing and 0 <= self.pl_idx < len(self.playlist):
            part = tr("标准") if self.playlist[self.pl_idx][0] == self.speech.path else tr("你的")
            text += tr(" · 正在放：{part}", part=part)
        elif pairs[i][2] is None:
            text += tr(" · 你的录音里没找到这句")
        self.cmp_info.config(text=text, style="TLabel")

    def shutdown(self):
        """Leaving the tab / closing the program: stop playback; a half-finished recording is saved too."""
        self.stop()
        if self.recorder.active:
            self.finish_record()
        if self.draft_job:
            self.root.after_cancel(self.draft_job)
            self.save_draft()
