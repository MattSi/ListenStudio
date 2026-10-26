#!/bin/bash
# Package app.py (ListenStudio: transcribe / record + intensive listening + read aloud)
# into ListenStudio.app on a Mac (packaging only works on a Mac, not on Windows).
#
# Needs: Python 3 from python.org (it includes Tk); with Homebrew's Python, also install python-tk.
# Usage: bash build_mac.sh
# Output: dist/ListenStudio.app and dist/ListenStudio-mac-<chip>.zip
#
# Note: Whisper (torch is 1GB+) and the ffmpeg command-line tools are NOT put in the .app.
#   Reading, playback, waveforms and saving audio (wav/mp3/m4a/flac) all use the bundled
#   PyAV (av), so they don't need them; recording only uses sounddevice.
#   Subtitles and the transcribe tab's "开始转录" look for a Python on the computer that
#   has Whisper (pip3 install openai-whisper; or set it with the JINGTING_PYTHON
#   environment variable, see intensive_listening.py).
#   The .app decodes the audio with PyAV before handing it over, so that Python doesn't need ffmpeg either.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv-build
source .venv-build/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt pyinstaller

python -c "import tkinter; tkinter.Tk().destroy()" || {
    echo "这个 Python 没有可用的 Tk，请装 python.org 的 Python，或 brew install python-tk"; exit 1; }

# --collect-all: bundle binaries such as the tkdnd drag-and-drop library, PortAudio, the ffmpeg
# libraries, and espeak-ng / onnxruntime for reading aloud; certifi's certificates make the
# HTTPS connection for Edge online voices work inside the .app
pyinstaller --noconfirm --clean --windowed \
    --name ListenStudio \
    --osx-bundle-identifier local.listenstudio \
    --collect-all tkinterdnd2 \
    --collect-all sounddevice \
    --collect-all av \
    --collect-all espeakng_loader \
    --collect-all kokoro_onnx \
    --collect-all onnxruntime \
    --collect-data certifi \
    app.py

# Recording uses the mic: macOS requires the reason in Info.plist, or it simply denies access
PLIST=dist/ListenStudio.app/Contents/Info.plist
plutil -replace NSMicrophoneUsageDescription \
    -string "录音功能需要使用麦克风（录音转录、精听，以及朗读页录下自己的朗读来对比发音）。" "$PLIST"
# Turn off App Nap: when the window isn't in front, macOS puts the program to "nap" (timers
# slow down or are suspended). The auto-next countdown and playback progress rely on timers,
# so after a nap the program would stutter or not respond
plutil -replace NSAppSleepDisabled -bool YES "$PLIST"
codesign --force --deep --sign - dist/ListenStudio.app   # Info.plist changed, so sign again (local ad-hoc signature)

ARCH="$(uname -m)"   # arm64 = M-series chip, x86_64 = Intel
# Zip with ditto, which keeps the symlinks and permissions inside the .app (plain zip breaks them)
ditto -c -k --keepParent dist/ListenStudio.app "dist/ListenStudio-mac-$ARCH.zip"
echo "完成：dist/ListenStudio.app  和  dist/ListenStudio-mac-$ARCH.zip"
