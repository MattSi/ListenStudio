"""ListenStudio (听写 & 精听) - the single entry point of the program.

Three tabs:
  转录 / 录音 (Transcribe / Record)  transcriber.py: drop in audio -> Whisper turns it into
               text with timestamps; play, record, save audio as another format
  精听 (Intensive listening)  intensive_listening.py: splits into sentences, pauses after
               each one; waveform, video, subtitles
  朗读 (Read aloud)  reader.py: reads your own English text aloud (tts.py), records you
               reading it, checks pronunciation, compares sentence by sentence
Transcribe and Intensive listening can send files to each other: the "送去精听 ▸" button
on the transcribe tab and the "◂ 送去转录" button on the listening tab.
When you switch tabs, the tab you leave stops playing.
Settings are stored in settings.json, recordings in recordings/
(where they go in the packaged app: see app_paths.py).

Usage:
  python app.py                                  # open the window, then drop audio in
  python app.py 听力2-1_part_6.m4a               # open and load this file right away
  Sentence-split options are normally changed in "⚙ 设置" (Settings);
  options given on the command line only apply to this run:
  python app.py 文件.m4a --min-silence 0.25      # split finer (by phrase)
  python app.py 文件.m4a --max-len 10            # allow longer sentences (default: split above 6 s)
  python app.py 文件.m4a --threshold -30         # set the silence threshold by hand (default: from the noise floor)

Dependencies: pip install tkinterdnd2 sounddevice av numpy pillow
     Transcription and subtitles also need faster-whisper (openai-whisper also works),
     either in this Python or in another Python on the computer
     (the packaged .app works this way: it finds it by itself, see
     intensive_listening.find_external_python).
     The ffmpeg / ffprobe command-line tools are not needed (all audio goes through PyAV).
"""
import argparse
import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from tkinterdnd2 import TkinterDnD

import audio_io
import intensive_listening
import reader
import ui_style
from i18n import tr
import transcriber

TITLE = tr("听写 & 精听")


class Tooltip:
    """Show a small help box after the mouse rests on a widget for 0.5 s; hide it when the mouse leaves."""

    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.tip, self.job = None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._hide()
        self.job = self.widget.after(500, self._show)

    def _show(self):
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, justify="left", bg="#ffffe0", relief="solid",
                 bd=1, padx=6, pady=3, font=(ui_style.FONT, 9)).pack()

    def _hide(self, event=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


class CombinedApp:
    def __init__(self, root, overrides=None):
        """overrides: sentence-split options from the command line; they only apply to this run."""
        self.root = root
        root.title(TITLE)
        root.geometry("1000x800")
        ui_style.apply(root)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.tx_tab, self.jt_tab, self.rd_tab = (tk.Frame(self.nb) for _ in range(3))
        self.nb.add(self.tx_tab, text=tr("  转录 / 录音  "))
        self.nb.add(self.jt_tab, text=tr("  精听  "))
        self.nb.add(self.rd_tab, text=tr("  朗读  "))

        # Both tabs share one settings dict (settings.json): the recognition model is chosen in "⚙ 设置", the recording length on the transcribe tab
        opts = intensive_listening.load_settings()
        opts.update(overrides or {})
        self.tx = transcriber.App(root, parent=self.tx_tab, settings=opts)
        self.jt = intensive_listening.Player(root, opts, parent=self.jt_tab)
        self.jt.active = False  # The transcribe tab is shown first
        self.jt.restart = self.restart  # Settings can restart the program to switch language
        self.rd = reader.Reader(root, self.rd_tab, opts,
                                mic_busy=lambda: self.tx.recorder.active)

        # The transcribe tab can open the settings too (same window; mic / speaker are chosen there)
        ttk.Button(self.tx.top, text=tr("⚙ 设置"), command=self.jt.open_settings).pack(
            side="right", padx=(4, 0), before=self.tx.save_audio_btn)

        # Buttons that send the file to the other tab (greyed out when there is no file)
        self.to_jt_btn = ttk.Button(self.tx.top, text=tr("送去精听 ▸"),
                                    command=self.send_to_listening)
        self.to_jt_btn.pack(side="right", padx=(0, 4))
        Tooltip(self.to_jt_btn, tr("把当前音频（拖入的文件或刚录的音）\n送到「精听」页，逐句播放、自动暂停"))
        self.to_tx_btn = ttk.Button(self.jt.top, text=tr("◂ 送去转录"), takefocus=False,
                                    command=self.send_to_transcribe)
        self.to_tx_btn.pack(side="right", padx=(0, 12))
        Tooltip(self.to_tx_btn, tr("把精听页当前的文件送到「转录 / 录音」页，\n用 Whisper 转成带时间戳的文字"))

        self.nb.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        # Errors not handled in buttons / shortcuts / timers: write them to the log and show them in the current tab's status line
        audio_io.install_tk_hook(root, self.show_error)
        audio_io.log.info("Started %s", TITLE)
        self.update_send_buttons()

    def show_error(self, msg):
        cur = self.nb.select()
        if cur == str(self.jt_tab):
            self.jt.show_error(msg)
        elif cur == str(self.rd_tab):
            self.rd.status.set(msg)
        else:
            self.tx.status.set(msg)

    def update_send_buttons(self):
        """Sending needs a file; nothing can be sent while the transcribe tab is recording or busy."""
        tx = self.tx
        ok_jt = bool(tx.current_file) and not tx.recorder.active
        ok_tx = bool(self.jt.path) and not tx._busy and not tx.recorder.active
        self.to_jt_btn.config(state="normal" if ok_jt else "disabled")
        self.to_tx_btn.config(state="normal" if ok_tx else "disabled")
        self.root.after(300, self.update_send_buttons)

    # ---------- Switching tabs ----------
    def on_tab_changed(self, event=None):
        """On tab switch: stop / pause playback on the other tabs, so two tabs never play at once."""
        cur = self.nb.select()
        self.jt.active = cur == str(self.jt_tab)
        self.rd.active = cur == str(self.rd_tab)
        if cur != str(self.tx_tab) and self.tx.player.playing:
            self.tx.player.pause()
        if not self.jt.active and (self.jt.playing or self.jt.pending or self.jt.auto_job):
            self.jt.stop_all()
            self.jt.refresh(tr("已停止"))
        if not self.rd.active:
            self.rd.shutdown()   # Stop playback; if "my reading" is being recorded, stop and save it
        if self.jt.active:
            self.jt_tab.focus_set()  # Move focus out of the text box, so the listening tab's Space etc. are not typed into it

    # ---------- Sending files between tabs ----------
    def send_to_listening(self):
        path = self.tx.current_file
        if not path:
            self.tx.status.set(tr("先拖入或录一段音频，再送去精听。"))
            return
        if self.tx.recorder.active:
            self.tx.status.set(tr("正在录音 - 录完再送去精听。"))
            return
        self.nb.select(self.jt_tab)
        self.jt.open_file(path)

    def send_to_transcribe(self):
        path = self.jt.path
        if not path:
            return
        if self.tx._busy or self.tx.recorder.active:
            messagebox.showinfo(TITLE, tr("转录页正在转录或录音，等它结束再送过去。"))
            return
        self.nb.select(self.tx_tab)
        self.tx.select(path)
        self.tx.status.set(tr("从精听送来：{name} - 点 Start 开始转录", name=os.path.basename(path)))

    def quit(self):
        self.tx.shutdown()   # Stop playback; a half-finished recording is saved too
        self.jt.stop_all()
        self.rd.shutdown()   # Same as above, and save the draft on the 朗读 (Read aloud) tab
        self.root.destroy()

    def restart(self):
        """Start a new copy of the program, then quit this one (used after changing the UI language).
        The packaged app is its own executable; a script is run again with the same Python."""
        args = [sys.executable] if getattr(sys, "frozen", False) else \
            [sys.executable, os.path.abspath(sys.argv[0])]
        self.quit()
        subprocess.Popen(args, cwd=os.getcwd())


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=tr("听写 & 精听：Whisper 转录 / 录音 + 自动断句精听"))
    p.add_argument("file", nargs="?",
                   help=tr("音频或视频文件（m4a / mp3 / wav / mp4 等）；不填就打开窗口拖文件进去"))
    # Sentence-split options are normally changed in the window's Settings; options given on the command line only apply to this run
    S = argparse.SUPPRESS
    p.add_argument("--threshold", type=float, default=S,
                   help=tr("静音阈值 dB（相对峰值）。默认按本底噪声自动定；"
                        "手动指定时，呼吸声被当成说话就调高（如 -25），吞字就调低（如 -40）"))
    p.add_argument("--max-len", type=float, default=S,
                   help=tr("超过这么长（秒）的句子在句内最长的停顿处再切，默认 6；0 = 不再切"))
    p.add_argument("--min-silence", type=float, default=S,
                   help=tr("多长的静音算断句（秒），默认 0.35；调小断得更细"))
    p.add_argument("--pad", type=float, default=S,
                   help=tr("每句前后多留的时间（秒），防止吞音，默认 0.15"))
    args = vars(p.parse_args(argv))
    return args.pop("file"), args


def main(argv=None):
    file, overrides = parse_args(argv)
    root = TkinterDnD.Tk()
    app = CombinedApp(root, overrides)
    if file:
        # Load the file in both tabs: select it on the transcribe tab, start splitting it on the listening tab (command-line options are mainly for listening)
        app.tx.select(file)
        app.nb.select(app.jt_tab)
        app.jt.open_file(file)
    root.mainloop()


if __name__ == "__main__":
    main()
