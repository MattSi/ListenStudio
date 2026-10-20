"""听写 & 精听（ListenStudio）—— 程序的唯一入口。

两个标签页：
  转录 / 录音  transcriber.py：拖入音频 → Whisper 转文字（带时间戳）、播放、60/90 秒录音、另存音频
  精听        intensive_listening.py：自动断句、每句播完暂停、波形、视频、字幕
两边可以互相送文件：转录页「送去精听 ▸」，精听页「◂ 送去转录」。
切换标签页时，离开的那一页会自动停止播放。
设置存在 settings.json，录音存在 recordings/（打包后的位置见 app_paths.py）。

用法：
  python app.py                                  # 打开窗口，把音频拖进去
  python app.py 听力2-1_part_6.m4a               # 打开时直接载入这个文件
  精听的断句参数平时在「⚙ 设置」里改；命令行写了的只对这一次生效：
  python app.py 文件.m4a --min-silence 0.25      # 断得更细（按意群）
  python app.py 文件.m4a --max-len 10            # 允许更长的句子（默认超过 6 秒再切）
  python app.py 文件.m4a --threshold -30         # 手动指定静音阈值（默认按底噪自动定）

依赖：pip install tkinterdnd2 sounddevice av numpy pillow
     转录和精听字幕另需 faster-whisper（openai-whisper 也行）：装在这个 Python 里，或者装在电脑上另一个 Python 里
     （打包的 .app 就是这样：会自动去找，见 intensive_listening.find_external_python）。
     不需要 ffmpeg / ffprobe 命令行工具（音频都用 PyAV 处理）。
"""
import argparse
import os
import tkinter as tk
from tkinter import messagebox, ttk

from tkinterdnd2 import TkinterDnD

import intensive_listening
import ui_style
import transcriber

TITLE = "听写 & 精听"


class Tooltip:
    """鼠标停在控件上 0.5 秒后显示一个小说明框，移开就消失。"""

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
        """overrides：命令行给的精听断句参数，只对这一次生效。"""
        self.root = root
        root.title(TITLE)
        root.geometry("1000x800")
        ui_style.apply(root)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        self.tx_tab, self.jt_tab = tk.Frame(self.nb), tk.Frame(self.nb)
        self.nb.add(self.tx_tab, text="  转录 / 录音  ")
        self.nb.add(self.jt_tab, text="  精听  ")

        # 两页共用一份设置（settings.json）：识别模型在「⚙ 设置」里选，录音时长在转录页选
        opts = intensive_listening.load_settings()
        opts.update(overrides or {})
        self.tx = transcriber.App(root, parent=self.tx_tab, settings=opts)
        self.jt = intensive_listening.Player(root, opts, parent=self.jt_tab)
        self.jt.active = False  # 先显示的是转录页

        # 转录页也能打开设置（同一个窗口；麦克风 / 扬声器在里面选）
        ttk.Button(self.tx.top, text="⚙ 设置", command=self.jt.open_settings).pack(
            side="right", padx=(4, 0), before=self.tx.save_audio_btn)

        # 互相送文件的按钮（没有文件时变灰）
        self.to_jt_btn = ttk.Button(self.tx.top, text="送去精听 ▸",
                                    command=self.send_to_listening)
        self.to_jt_btn.pack(side="right", padx=(0, 4))
        Tooltip(self.to_jt_btn, "把当前音频（拖入的文件或刚录的音）\n送到「精听」页，逐句播放、自动暂停")
        self.to_tx_btn = ttk.Button(self.jt.top, text="◂ 送去转录", takefocus=False,
                                    command=self.send_to_transcribe)
        self.to_tx_btn.pack(side="right", padx=(0, 12))
        Tooltip(self.to_tx_btn, "把精听页当前的文件送到「转录 / 录音」页，\n用 Whisper 转成带时间戳的文字")

        self.nb.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.update_send_buttons()

    def update_send_buttons(self):
        """有文件才能送；转录页在录音时、或转录页正忙时不能收发。"""
        tx = self.tx
        ok_jt = bool(tx.current_file) and not tx.recorder.active
        ok_tx = bool(self.jt.path) and not tx._busy and not tx.recorder.active
        self.to_jt_btn.config(state="normal" if ok_jt else "disabled")
        self.to_tx_btn.config(state="normal" if ok_tx else "disabled")
        self.root.after(300, self.update_send_buttons)

    # ---------- 标签页切换 ----------
    def on_tab_changed(self, event=None):
        on_jt = self.nb.select() == str(self.jt_tab)
        self.jt.active = on_jt
        if on_jt:
            if self.tx.player.playing:   # 离开转录页：暂停那边的播放
                self.tx.player.pause()
            self.jt_tab.focus_set()      # 焦点离开转录文字框，空格才不会打进去
        else:
            if self.jt.playing or self.jt.pending:  # 离开精听页：停止那边的播放
                self.jt.stop_all()
                self.jt.refresh("已停止")
            self.tx_tab.focus_set()

    # ---------- 互送文件 ----------
    def send_to_listening(self):
        path = self.tx.current_file
        if not path:
            self.tx.status.set("先拖入或录一段音频，再送去精听。")
            return
        if self.tx.recorder.active:
            self.tx.status.set("正在录音 - 录完再送去精听。")
            return
        self.nb.select(self.jt_tab)
        self.jt.open_file(path)

    def send_to_transcribe(self):
        path = self.jt.path
        if not path:
            return
        if self.tx._busy or self.tx.recorder.active:
            messagebox.showinfo(TITLE, "转录页正在转录或录音，等它结束再送过去。")
            return
        self.nb.select(self.tx_tab)
        self.tx.select(path)
        self.tx.status.set(f"从精听送来：{os.path.basename(path)} - 点 Start 开始转录")

    def quit(self):
        self.tx.shutdown()   # 停止播放；录了一半的音也会存下来
        self.jt.stop_all()
        self.root.destroy()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="听写 & 精听：Whisper 转录 / 录音 + 自动断句精听")
    p.add_argument("file", nargs="?",
                   help="音频或视频文件（m4a / mp3 / wav / mp4 等）；不填就打开窗口拖文件进去")
    # 精听的断句参数平时在窗口的"设置"里改；命令行写了的只对这一次生效
    S = argparse.SUPPRESS
    p.add_argument("--threshold", type=float, default=S,
                   help="静音阈值 dB（相对峰值）。默认按本底噪声自动定；"
                        "手动指定时，呼吸声被当成说话就调高（如 -25），吞字就调低（如 -40）")
    p.add_argument("--max-len", type=float, default=S,
                   help="超过这么长（秒）的句子在句内最长的停顿处再切，默认 6；0 = 不再切")
    p.add_argument("--min-silence", type=float, default=S,
                   help="多长的静音算断句（秒），默认 0.35；调小断得更细")
    p.add_argument("--pad", type=float, default=S,
                   help="每句前后多留的时间（秒），防止吞音，默认 0.15")
    args = vars(p.parse_args(argv))
    return args.pop("file"), args


def main(argv=None):
    file, overrides = parse_args(argv)
    root = TkinterDnD.Tk()
    app = CombinedApp(root, overrides)
    if file:
        # 两页都载入：转录页选中它，精听页开始断句（命令行参数主要是给精听用的）
        app.tx.select(file)
        app.nb.select(app.jt_tab)
        app.jt.open_file(file)
    root.mainloop()


if __name__ == "__main__":
    main()
