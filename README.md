# ListenStudio

A desktop app for practising English listening and speaking. Drop in any audio or video file to transcribe it with Whisper, listen to it sentence by sentence, or write your own text, hear it read aloud and compare your pronunciation with the reference, sentence by sentence.

> The user interface is in Chinese (中文); this README is in English.

<!-- Screenshots: add one per tab, e.g. docs/transcribe.png, docs/listening.png, docs/read-aloud.png -->

## Features

### 1. Transcribe / Record (转录 / 录音)
- Drag and drop audio or video (mp3, m4a, wav, flac, ogg, mp4, mkv, mov, …) and transcribe it to timestamped text with **Whisper** (faster-whisper or openai-whisper; uses an NVIDIA GPU when available).
- Transcription runs in a background thread, so the window stays responsive; it can be paused and cancelled.
- Record from a chosen microphone (60 s, 90 s or a custom length up to 60 min). Audio is written to disk while recording, so long recordings don't fill memory.
- Save audio as WAV, MP3, M4A or FLAC.

### 2. Intensive listening (精听)
- Splits the audio into sentences at the silences. The silence threshold is set automatically from the recording's noise floor; long sentences are split again.
- Plays one sentence at a time and pauses, with an optional auto-advance after N seconds.
- Zoomed waveform of the current sentence plus a clickable overview of the whole file; video files show the picture in sync.
- Optional Whisper subtitles, cached so each file is only recognised once.
- Speed 0.5×–1.5× **without changing pitch**, and volume boost with a limiter so loud parts never clip.
- Keyboard driven: `Space` play / stop, `R` replay, `←` / `→` previous / next, `T` subtitles, `[` / `]` speed, `+` / `-` volume.

### 3. Read aloud (朗读)
- Write or paste your own English; it is read aloud with neural voices (Canadian, US and UK accents), highlighting the sentence being read.
- Two text-to-speech engines: **Microsoft Edge online voices**, with **Kokoro** as an offline fallback (≈120 MB model, downloaded on first use).
- Record yourself reading the text, then **check pronunciation**: Whisper transcribes your recording and it is aligned word by word with the text. Skipped words are marked red, misread words orange, and you get the share of words read correctly.
- **Compare sentence by sentence**: plays the reference sentence, then yours, with previous / replay / next / stop and a continuous-play option.

The Transcribe and Listening tabs can send the current file to each other, and switching tabs stops playback on the tab you leave.

## Installation

### Run from source (Windows, macOS)

Requires **Python 3.10+** with Tk (included in the python.org installers).

```bash
git clone https://github.com/MattSi/ListenStudio.git
cd ListenStudio
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install faster-whisper          # needed for transcription, subtitles and the pronunciation check
python app.py
```

Optionally open a file straight away:

```bash
python app.py lecture.m4a
```

Notes:
- No ffmpeg command-line tools are needed: all decoding, playback and conversion go through **PyAV**.
- `openai-whisper` also works in place of `faster-whisper`. With an NVIDIA GPU and a CUDA build of PyTorch it runs on the GPU.
- The Whisper model (tiny.en … large-v3, turbo) is chosen in **⚙ 设置** (Settings) and shared by all tabs.
- Settings are saved to `settings.json` and recordings to `recordings/`, both next to `app.py` and ignored by git.

### macOS app

Download a build from the [Actions page](../../actions/workflows/build-mac.yml) (Apple silicon and Intel), or build it on a Mac:

```bash
bash build_mac.sh      # outputs dist/ListenStudio.app and a zip
```

The `.app` does not bundle Whisper (PyTorch is over 1 GB). For transcription it looks for a Python on the computer that has Whisper installed (`pip3 install faster-whisper`), or the one named in the `JINGTING_PYTHON` environment variable. In the packaged app, settings go to `~/Library/Application Support/ListenStudio/` and recordings to `~/Music/ListenStudio/`.

## Project structure

| File | What it does |
|---|---|
| `app.py` | Entry point: the window, the three tabs and command-line options |
| `transcriber.py` | Transcribe / Record tab: Whisper worker thread, recording, audio conversion |
| `intensive_listening.py` | Listening tab: silence-based sentence splitting, waveform, video, subtitles, speed and volume |
| `reader.py` | Read-aloud tab: recording, word-level alignment, sentence-by-sentence comparison |
| `tts.py` | Text to speech: Edge online voices, Kokoro offline fallback, per-sentence timing, cache |
| `audio_io.py` | Audio device selection |
| `app_paths.py` | Where settings, recordings, models and cache are stored |
| `ui_style.py` | Shared fonts and colours |
| `build_mac.sh`, `.github/workflows/build-mac.yml` | macOS packaging with PyInstaller, and the CI job that runs it |

## How I built this with Claude Code

I built ListenStudio in about a week (October 18–25, 2026) with [Claude Code](https://claude.com/claude-code) as a pair programmer. About 4,200 lines of Python went in over 17 commits and 4 merged pull requests.

**How we split the work.** I decided what to build and how it should behave: which features help a learner, how each screen should work, and what "done" looks like. Claude Code wrote most of the code from those descriptions. I tested every change in the app and reviewed the diff before merging.

**Workflow.** After the first prototype, each feature went on its own branch and was merged through a pull request:

| PR | Feature |
|---|---|
| #1 | Auto-play the next sentence after N seconds |
| #2 | One shared Whisper model for all tabs, loaded once, with a lock so two tabs take turns |
| #3 | The Read-aloud tab: TTS, recording, pronunciation check, sentence-by-sentence comparison |
| #4 | Highlight the current sentence in the listening overview |

Each commit message explains what changed and why, so the history can be read as a changelog.

**Where human review mattered.**
- *A bug in generated code.* While adding the comparison controls, part of a `refresh()` method ended up inside another function, after an early `return`. In the app this showed up as a blank button and an error when the offline voice wasn't downloaded. Testing caught it, and the fix commit records the cause, not just the symptom.
- *Design choices.* Some decisions came from using the app, not from the code: decoding everything with PyAV so users don't need ffmpeg; writing recordings to disk as they happen so long recordings don't fill memory; sharing one Whisper model between tabs instead of loading it twice; turning off macOS App Nap so timers keep working when the window is in the background.
- *Edge cases.* Short sentences, the first and last sentence in the waveform overview, a non-number typed in a number box, running offline. I asked for each to be handled and checked it.

**What I learned.** Claude Code is fastest when the request is specific and has one goal: "add a progress bar and previous / replay / next buttons to the comparison row" works better than "improve the comparison". The developer's job moves towards writing clear requirements, reviewing diffs and testing behaviour, and that is where the bugs get caught.
