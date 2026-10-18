"""转录页和精听页共用的界面风格：字体、颜色、ttk 控件样式、拖放区。

两边（无论单独运行还是在 app.py 里合在一起）都调用 apply(root)，
拖放区都用 drop_zone() 创建，这样看起来是同一个程序。
"""
import sys
import tkinter as tk
from tkinter import ttk

if sys.platform == "darwin":
    FONT = "PingFang SC"
elif sys.platform == "win32":
    FONT = "Microsoft YaHei UI"
else:
    FONT = "Noto Sans CJK SC"
TEXT_FONT = "Segoe UI" if sys.platform == "win32" else FONT  # 英文正文（转录文字、字幕）

PAD = 10                 # 页面左右边距 / 行间距
ACCENT = "#2a6fdb"       # 主色：时间戳、波形、当前句
DROP_BG = "#eef3fb"      # 拖放区底色
DROP_BORDER = "#b9cbe8"  # 拖放区边框
MUTED = "#888888"        # 提示文字
OK = "#22a06b"           # 状态（正常）
REC = "#e02424"          # 录音红点


def apply(root):
    """设置整个窗口的默认字体和 ttk 样式；重复调用没关系。"""
    root.option_add("*Font", (FONT, 10))
    st = ttk.Style(root)
    st.configure(".", font=(FONT, 10))
    st.configure("TButton", padding=(10, 4))
    st.configure("Big.TButton", font=(FONT, 11), padding=(12, 5))
    st.configure("Hint.TLabel", foreground=MUTED, font=(FONT, 9))
    st.configure("TNotebook.Tab", font=(FONT, 11), padding=(16, 5))
    return st


def drop_zone(parent, text, height=4):
    """统一样式的拖放区（浅蓝底、细边框、手形光标）。"""
    lbl = tk.Label(parent, text=text, height=height, bg=DROP_BG, fg="#333",
                   font=(FONT, 12), cursor="hand2", bd=0, highlightthickness=1,
                   highlightbackground=DROP_BORDER, highlightcolor=DROP_BORDER,
                   justify="center")
    # 文字太长（比如很深的文件夹路径）时自动换行，不撑出窗口
    lbl.bind("<Configure>", lambda e: lbl.config(wraplength=max(200, e.width - 40)))
    return lbl
