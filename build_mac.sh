#!/bin/bash
# 在 Mac 上把 app.py（听写 & 精听：转录 / 录音 + 精听）打包成 ListenStudio.app
# （打包只能在 Mac 上做，Windows 上做不了）。
#
# 前提：装好 python.org 的 Python 3（自带 Tk）；用 Homebrew 的 Python 需要另装 python-tk。
# 用法：bash build_mac.sh
# 产物：dist/ListenStudio.app 和 dist/ListenStudio-mac-<芯片>.zip
#
# 注意：Whisper（torch 有 1GB+）和 ffmpeg 命令行工具不打进 .app。
#   两页的读取、播放、波形、另存音频（wav/mp3/m4a/flac）都用打进去的 PyAV（av），不需要它们；
#   录音只用 sounddevice；
#   精听字幕和转录页的「开始转录」会去找电脑上装了 Whisper 的 Python 来识别
#   （pip3 install openai-whisper；也可以用环境变量 JINGTING_PYTHON 指定，见 intensive_listening.py）。
#   音频由 .app 用 PyAV 解码好再交给那个 Python，所以它也不需要 ffmpeg。
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv-build
source .venv-build/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller

python -c "import tkinter; tkinter.Tk().destroy()" || {
    echo "这个 Python 没有可用的 Tk，请装 python.org 的 Python，或 brew install python-tk"; exit 1; }

# --collect-all：把拖放库 tkdnd、PortAudio、ffmpeg 动态库这些二进制一起打进去
pyinstaller --noconfirm --clean --windowed \
    --name ListenStudio \
    --osx-bundle-identifier local.listenstudio \
    --collect-all tkinterdnd2 \
    --collect-all sounddevice \
    --collect-all av \
    app.py

# 录音要用麦克风：macOS 要求 Info.plist 里写明用途，否则会直接拒绝访问
PLIST=dist/ListenStudio.app/Contents/Info.plist
plutil -replace NSMicrophoneUsageDescription \
    -string "录音功能需要使用麦克风（录 60 / 90 秒音频用于转录和精听）。" "$PLIST"
codesign --force --deep --sign - dist/ListenStudio.app   # 改过 Info.plist 要重新签名（本机自签）

ARCH="$(uname -m)"   # arm64 = M 系列芯片，x86_64 = Intel
# 用 ditto 压缩，保留 .app 里的符号链接和权限（用 zip 会弄坏）
ditto -c -k --keepParent dist/ListenStudio.app "dist/ListenStudio-mac-$ARCH.zip"
echo "完成：dist/ListenStudio.app  和  dist/ListenStudio-mac-$ARCH.zip"
