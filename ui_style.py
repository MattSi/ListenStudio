"""Shared look for all tabs: fonts, colors, ttk widget styles, the drop zone.

Every tab (whether run alone or together in app.py) calls apply(root) and creates its
drop zone with drop_zone(), so they all look like one program.
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
TEXT_FONT = "Segoe UI" if sys.platform == "win32" else FONT  # English body text (transcripts, subtitles)

PAD = 10                 # Page left/right margin / row spacing
ACCENT = "#2a6fdb"       # Main color: timestamps, waveform, current sentence
DROP_BG = "#eef3fb"      # Drop zone background
DROP_BORDER = "#b9cbe8"  # Drop zone border
MUTED = "#888888"        # Hint text
OK = "#22a06b"           # Status (normal)
REC = "#e02424"          # Recording red dot


def apply(root):
    """Set the default font and ttk styles for the whole window; safe to call more than once."""
    root.option_add("*Font", (FONT, 10))
    st = ttk.Style(root)
    st.configure(".", font=(FONT, 10))
    st.configure("TButton", padding=(10, 4))
    st.configure("Big.TButton", font=(FONT, 11), padding=(12, 5))
    st.configure("Hint.TLabel", foreground=MUTED, font=(FONT, 9))
    st.configure("TNotebook.Tab", font=(FONT, 11), padding=(16, 5))
    return st


def drop_zone(parent, text, height=4):
    """A drop zone in the shared style (light blue background, thin border, hand cursor)."""
    lbl = tk.Label(parent, text=text, height=height, bg=DROP_BG, fg="#333",
                   font=(FONT, 12), cursor="hand2", bd=0, highlightthickness=1,
                   highlightbackground=DROP_BORDER, highlightcolor=DROP_BORDER,
                   justify="center")
    # Wrap long text (e.g. a deep folder path) instead of stretching the window
    lbl.bind("<Configure>", lambda e: lbl.config(wraplength=max(200, e.width - 40)))
    return lbl
