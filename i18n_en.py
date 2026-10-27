"""English UI text. Key = the Chinese text used in the code (see i18n.py).

To add a string: write tr("中文") in the code and add "中文": "English" here.
check_i18n.py lists any tr() text that has no entry yet."""

EN = {
    "\n鼠标：点波形/画面重听，点总览跳到那一句；在波形/画面上滚轮 = 上下句，在音量上滚轮 = 调音量":
        "\nMouse: click the waveform/picture to replay, click the overview to jump to a sentence; wheel over the waveform/picture = prev / next, wheel over the volume = volume",
    "    Q 退出":
        "    Q quit",
    "  朗读  ":
        "  Read Aloud  ",
    "  精听  ":
        "  Listening  ",
    "  转录 / 录音  ":
        "  Transcribe / Record  ",
    " · 你的录音里没找到这句":
        " · not in your recording",
    " · 正在放：{part}":
        " · now: {part}",
    "Edge 在线语音":
        "Edge online voice",
    "Edge 在线语音没有返回声音":
        "The Edge online voice returned no audio",
    "Kokoro 离线语音":
        "Kokoro offline voice",
    "Whisper 听到的：":
        "Whisper heard: ",
    "{a} – {b}（{d:.1f} 秒）":
        "{a} – {b} ({d:.1f} s)",
    "{dot} 录音中":
        "{dot} Recording",
    "{engine} 合成中…… 第 {i} / {n} 句":
        "{engine}: synthesizing… sentence {i} / {n}",
    "{msg}\n\n要下载离线语音（约 {mb} MB）吗？下载后没网也能朗读。":
        "{msg}\n\nDownload the offline voice (about {mb} MB)? After that, reading aloud works without internet.",
    "{name}\n{folder}\n（{note}）":
        "{name}\n{folder}\n({note})",
    "{name}\n共 {n} 句{thr}（拖入其他文件可替换）":
        "{name}\n{n} sentences{thr} (drop another file to replace it)",
    "{name}\n正在读取并断句……":
        "{name}\nReading and splitting into sentences…",
    "{name}\n（{err}）\n可以再拖一个文件进来":
        "{name}\n({err})\nYou can drop another file in",
    "{n} 秒":
        "{n} s",
    "{phase}…… {pct:.0f}%":
        "{phase}… {pct:.0f}%",
    "↔ 逐句对比":
        "↔ Compare",
    "↔ 逐句对比：第 {i} / {n} 句，先标准，再你的{note}":
        "↔ Comparing sentence {i} / {n}: reference first, then yours{note}",
    "↻ 重听":
        "↻ Replay",
    "↻ 重听这句":
        "↻ Replay",
    "⏮ 上一句":
        "⏮ Prev",
    "⏸ {left:.1f} 秒后自动播放下一句（空格 = 取消）":
        "⏸ Next sentence in {left:.1f} s (Space = cancel)",
    "⏸ 已暂停，按空格继续":
        "⏸ Paused - press Space to continue",
    "① 写好英文（或粘贴）  ② ▶ 朗读，听标准读法  ③ ● 录下自己读一遍  ④ ✓ 检查发音 / ↔ 逐句对比":
        "1. Write (or paste) English   2. ▶ Read aloud to hear it   3. ● Record yourself reading it   4. ✓ Check / ↔ Compare",
    "■ 停止":
        "■ Stop",
    "■ 停止录音":
        "■ Stop rec",
    "▶ 准备画面……":
        "▶ Preparing video…",
    "▶ 听我的":
        "▶ Play mine",
    "▶ 开始":
        "▶ Start",
    "▶ 播放":
        "▶ Play",
    "▶ 播放中":
        "▶ Playing",
    "▶ 播放中（{s}）":
        "▶ Playing ({s})",
    "▶ 播放我的录音":
        "▶ Playing my recording",
    "▶ 朗读":
        "▶ Read aloud",
    "▶ 标准朗读中":
        "▶ Reading the reference",
    "▶ 继续":
        "▶ Continue",
    "◂ 送去转录":
        "◂ To Transcribe",
    "● 录我的朗读":
        "● Record me",
    "● 录音":
        "● Record",
    "● 录音中 - {title}":
        "● Recording - {title}",
    "⚙ 设置":
        "⚙ Settings",
    "✎ 返回编辑":
        "✎ Back to editing",
    "✓ 已播完最后一句":
        "✓ Finished the last sentence",
    "✓ 检查发音":
        "✓ Check pronunciation",
    "❚❚ 暂停":
        "❚❚ Pause",
    "一直显示":
        "Always show",
    "下一句 ⏭":
        "Next ⏭",
    "下载模型":
        "Downloading model",
    "下载离线语音":
        "Download offline voice",
    "下载离线语音…… {done:.0f} / {total:.0f} MB":
        "Downloading offline voice… {done:.0f} / {total:.0f} MB",
    "下载（约 {mb} MB）":
        "Download (about {mb} MB)",
    "不支持的格式：{ext}，请用 wav / mp3 / m4a / flac":
        "Unsupported format: {ext}; use wav / mp3 / m4a / flac",
    "不是文件：\n{path}":
        "Not a file:\n{path}",
    "从精听送来：{name} - 点 Start 开始转录":
        "From Listening: {name} - click \"Transcribe\" to start",
    "你的":
        "yours",
    "保存失败":
        "Save failed",
    "保存文字…":
        "Save text…",
    "保存文本…":
        "Save text…",
    "保存朗读音频…":
        "Save reading audio…",
    "保存音频…":
        "Save audio…",
    "倍":
        "×",
    "先「● 录我的朗读」，把上面的文字读一遍，再逐句对比":
        "First click \"● Record me\" and read the text above, then compare",
    "先在上面写一段英文":
        "Write some English above first",
    "先拖入或录一段音频，再送去精听。":
        "Drop in or record some audio first, then send it to Listening.",
    "全部 {n} 句对比完了：可以「↻ 重听这句」，或「⏮ 上一句」往回听":
        "All {n} sentences compared: click \"↻ Replay\", or \"⏮ Prev\" to go back",
    "共 {t}":
        "Total {t}",
    "关闭":
        "Close",
    "关闭##button":
        "Close",
    "写好英文后点「▶ 朗读」":
        "Write some English, then click \"▶ Read aloud\"",
    "出错了":
        "Something went wrong",
    "出错了：{err}（详情见日志 {log}）":
        "Error: {err} (details in the log: {log})",
    "分钟":
        "min",
    "刷新":
        "Refresh",
    "前后留白 (秒)":
        "Padding (s)",
    "加拿大 · 女 · Clara":
        "Canadian · female · Clara",
    "加拿大 · 男 · Liam":
        "Canadian · male · Liam",
    "取消":
        "Cancel",
    "句间停顿":
        "Pause",
    "只用在线":
        "Online only",
    "只用离线":
        "Offline only",
    "合成好了{how}，开始朗读":
        "Ready{how}; reading aloud",
    "听写 & 精听":
        "ListenStudio",
    "听写 & 精听：Whisper 转录 / 录音 + 自动断句精听":
        "ListenStudio: Whisper transcription / recording + sentence-by-sentence intensive listening",
    "听完一句再显示":
        "Show after each sentence",
    "呼吸声被当成说话 → 调高（如 -25）；吞字 → 调低（如 -40）":
        "Breathing counts as speech → raise it (e.g. -25); word endings cut → lower it (e.g. -40)",
    "声音设备出错（已自动重试一次）：{e}":
        "Sound device error (already retried once): {e}",
    "复制":
        "Copy",
    "复制整句":
        "Copy whole sentence",
    "复制文字":
        "Copy text",
    "多长的静音算一句结束；调小断得更细":
        "How long a silence ends a sentence; smaller = finer splitting",
    "多长的静音算断句（秒），默认 0.35；调小断得更细":
        "How long a silence ends a sentence (seconds), default 0.35; smaller = finer splitting",
    "字幕":
        "Subtitles",
    "字幕识别中……（{model}，第一次会慢一些）":
        "Recognizing subtitles… ({model}; slower the first time)",
    "字幕识别失败：{ex}":
        "Subtitle recognition failed: {ex}",
    "已停止":
        "Stopped",
    "已取消":
        "Cancelled",
    "已取消下载":
        "Download cancelled",
    "已另存":
        "Saved as a copy",
    "已打开：{name}":
        "Opened: {name}",
    "已按新设置重新断句":
        "Split again with the new settings",
    "已经是最后一句（← 返回，R 重听）":
        "This is the last sentence (← back, R replay)",
    "已转录":
        "Transcribed",
    "应用并保存":
        "Apply & save",
    "开始转录":
        "Transcribe",
    "引擎":
        "Engine",
    "录好了 {t}，已存到 {path}":
        "Recorded {t}, saved to {path}",
    "录好了 {t}：可以「▶ 听我的」，或「✓ 检查发音」":
        "Recorded {t}: click \"▶ Play mine\" or \"✓ Check pronunciation\"",
    "录好自己的朗读后，可以逐句对比":
        "Record yourself, then compare",
    "录完自动转录":
        "Transcribe when done",
    "录音、两页的播放都用这里选的设备；刚插上耳机就点「刷新」":
        "Recording and playback on all tabs use these devices; click \"Refresh\" after plugging in headphones",
    "录音失败":
        "Recording failed",
    "录音已停止，没有录到声音":
        "Recording stopped; no sound was recorded",
    "恢复默认":
        "Reset",
    "我的录音 {t}":
        "My recording {t}",
    "所有文件":
        "All files",
    "手动":
        "manual",
    "手动显示":
        "Show manually",
    "打不开麦克风：\n{e}":
        "Can't open the microphone:\n{e}",
    "打开文件…":
        "Open file…",
    "打开文本…":
        "Open text…",
    "扬声器":
        "Speaker",
    "把当前音频（拖入的文件或刚录的音）\n送到「精听」页，逐句播放、自动暂停":
        "Send the current audio (a dropped file or a new recording)\nto the Listening tab: plays sentence by sentence, pausing after each",
    "把精听页当前的文件送到「转录 / 录音」页，\n用 Whisper 转成带时间戳的文字":
        "Send the current file on the Listening tab to the Transcribe / Record tab,\nwhere Whisper turns it into text with timestamps",
    "把音频或视频文件拖到这里（或点这里选择），然后点「开始转录」":
        "Drop an audio or video file here (or click to choose), then click \"Transcribe\"",
    "把音频或视频文件拖到这里（或点这里选择），自动断句后逐句精听":
        "Drop an audio or video file here (or click to choose); it is split into sentences for intensive listening",
    "拖入或录一段音频开始":
        "Drop in or record some audio to start",
    "拖入音频文件开始":
        "Drop in an audio file to start",
    "按本底噪声自动":
        "Auto (from noise floor)",
    "按空格开始":
        "Press Space to start",
    "播放出错：{err}":
        "Playback error: {err}",
    "播放完了":
        "Finished playing",
    "文件已就绪，可以播放，或点「开始转录」":
        "File ready: play it, or click \"Transcribe\"",
    "文字已保存：{path}":
        "Text saved: {path}",
    "文字已复制到剪贴板":
        "Text copied to the clipboard",
    "文字框是空的，没法对比":
        "The text box is empty; nothing to compare",
    "文本":
        "Text",
    "断句静音 (秒)":
        "Sentence-end silence (s)",
    "无法读取：{ex}":
        "Can't read the file: {ex}",
    "显示字幕":
        "Show subs",
    "显示时间戳":
        "Show timestamps",
    "最长一句 (秒)":
        "Longest sentence (s)",
    "朗读失败":
        "Reading aloud failed",
    "朗读音频已保存：{dst}":
        "Reading audio saved: {dst}",
    "未知类型":
        "Unknown type",
    "未知错误":
        "Unknown error",
    "标准":
        "reference",
    "标准朗读合成好了{how}，接着检查发音……":
        "Reference reading ready{how}; now checking pronunciation…",
    "检查完了：可以「↔ 逐句对比」，一句一句听差别":
        "Check done: click \"↔ Compare\" to hear the differences sentence by sentence",
    "检查没能完成：{e}":
        "The check couldn't finish: {e}",
    "模型 {m}":
        "Model {m}",
    "正在保存音频 → {name}……":
        "Saving audio → {name}…",
    "正在加载模型 {model}（用 {py}；第一次使用会先下载）……":
        "Loading model {model} (with {py}; downloaded on first use)…",
    "正在加载模型 {model}（第一次使用会先下载）……":
        "Loading model {model} (downloaded on first use)…",
    "正在取消……":
        "Cancelling…",
    "正在录音 - 录完再送去精听。":
        "Recording - finish recording before sending it to Listening.",
    "正在录音……对着麦克风把上面的文字读一遍，读完点「■ 停止录音」":
        "Recording… read the text above into the mic, then click \"■ Stop rec\"",
    "正在录音（{t}，到时间自动停止；点「停止录音」可提前结束）":
        "Recording ({t}; stops automatically; click \"Stop rec\" to finish early)",
    "正在录音，先停止录音再换文件":
        "Recording - stop recording before choosing another file",
    "正在读取音频……":
        "Reading audio…",
    "正在转录，先点「取消」再换文件":
        "Transcribing - click \"Cancel\" before choosing another file",
    "每句前后多播一点，防止吞音":
        "Play a little extra before and after each sentence so word edges aren't cut",
    "每句前后多留的时间（秒），防止吞音，默认 0.15":
        "Extra time kept before and after each sentence (seconds) so word edges aren't cut, default 0.15",
    "没有可以朗读的文字":
        "There is no text to read",
    "没有安装 sounddevice，请运行：pip install sounddevice":
        "sounddevice is not installed; run: pip install sounddevice",
    "没有检测到句子，试试在设置里调低静音阈值":
        "No sentences found; try lowering the silence threshold in Settings",
    "没网时用的离线语音：":
        "Offline voice for when there's no internet:",
    "清空":
        "Clear",
    "清空文字框里的内容吗？":
        "Clear the text box?",
    "点「↔ 逐句对比」开始（缺朗读 / 检查会自动先做）":
        "Click \"↔ Compare\" to start",
    "点「开始转录」把它转成文字":
        "Click \"Transcribe\" to turn it into text",
    "点击这里选择音频或视频文件":
        "Click here to choose an audio or video file",
    "用 Whisper（{model}）听你的录音……第一次会慢一些":
        "Listening to your recording with Whisper ({model})… slower the first time",
    "用这组参数没有检测到句子，试试调低静音阈值":
        "No sentences found with these settings; try lowering the silence threshold",
    "界面语言改好了，下次打开程序时生效。":
        "The new language takes effect the next time you open the program.",
    "界面语言改好了，重启程序后生效。现在重启吗？":
        "The language will change after restarting the program. Restart now?",
    "离线语音 ✓ 已下载":
        "Offline voice ✓ downloaded",
    "离线语音下载好了，没网时也能朗读":
        "Offline voice downloaded; reading aloud now works without internet",
    "离线语音没能下载：{e}":
        "Couldn't download the offline voice: {e}",
    "离线语音还没下载：在「朗读」页点「下载离线语音」":
        "The offline voice isn't downloaded yet: on the Read Aloud tab, click \"Download\"",
    "秒":
        "s",
    "秒后":
        "s",
    "空格 下一句 / 停止    R 重听    ← → 上下句    T 显示/隐藏字幕    + - 音量    [ ] 速度":
        "Space next / stop    R replay    ← → prev / next    T show/hide subtitles    + - volume    [ ] speed",
    "第 {i} / {n} 句":
        "Sentence {i} / {n}",
    "第 {i} / {n} 句##cmp":
        "{i} / {n}",
    "第 {i} 句对比完，马上放下一句……":
        "Sentence {i} done; playing the next one…",
    "第 {i} 句对比完：「↻ 重听这句」再听一遍，或者「下一句 ⏭」":
        "Sentence {i} done: \"↻ Replay\" to hear it again, or \"Next ⏭\"",
    "精听":
        "Listening",
    "精听 - {name}":
        "Listening - {name}",
    "精听字幕正在用识别模型，等它识别完就开始……":
        "The subtitles on the Listening tab are using the model; starting when they finish…",
    "美国 · 女 · Aria":
        "American · female · Aria",
    "美国 · 女 · Jenny":
        "American · female · Jenny",
    "美国 · 男 · Guy":
        "American · male · Guy",
    "自动":
        "auto",
    "自动下一句":
        "Auto-next after",
    "自动（在线优先）":
        "Auto (online first)",
    "自定":
        "Custom",
    "英国 · 女 · Sonia":
        "British · female · Sonia",
    "英国 · 男 · Ryan":
        "British · male · Ryan",
    "要从 GitHub 下载 Kokoro 离线语音模型吗？\n大约 {mb} MB，只需下载一次，存在：\n{folder}\n\n下载后，没网时也能朗读。":
        "Download the Kokoro offline voice model from GitHub?\nAbout {mb} MB, downloaded only once, stored in:\n{folder}\n\nAfter that, reading aloud works without internet.",
    "要显示字幕需要先安装 Whisper：在终端运行 {cmd}":
        "Subtitles need Whisper: run {cmd} in a terminal",
    "设置":
        "Settings",
    "设置没能保存：\n{ex}":
        "Couldn't save the settings:\n{ex}",
    "识别模型":
        "Recognition model",
    "语言 / Language":
        "语言 / Language",
    "语速":
        "Rate",
    "语音":
        "Voice",
    "请填数字":
        "Please enter a number",
    "读取中，请稍候":
        "Reading, please wait",
    "读对 {ok} / {total} 个词（{pct:.0f}%）   漏读 {miss}   读错 {wrong}   多读 {extra}      红色 = 漏读，橙色 = 被听成了别的词":
        "{ok} / {total} words right ({pct:.0f}%)   skipped {miss}   wrong {wrong}   extra {extra}      red = skipped, orange = heard as another word",
    "超过就在句内最长停顿处再切；0 = 不再切":
        "Longer sentences are split again at their longest pause; 0 = don't",
    "超过这么长（秒）的句子在句内最长的停顿处再切，默认 6；0 = 不再切":
        "Sentences longer than this (seconds) are split again at their longest pause; default 6; 0 = don't split",
    "跟随系统默认":
        "System default",
    "转录 / 录音 - Whisper":
        "Transcribe / Record - Whisper",
    "转录中":
        "Transcribing",
    "转录失败":
        "Transcription failed",
    "转录完成：{name}":
        "Transcription done: {name}",
    "转录需要 Whisper：在终端运行 {cmd}，然后再点「开始转录」":
        "Transcription needs Whisper: run {cmd} in a terminal, then click \"Transcribe\" again",
    "转录页和精听字幕共用这个模型；字幕每个文件只识别一次（结果会缓存）\n模型越大越准越慢：tiny / base 快，small 平衡，medium / large 准":
        "The Transcribe tab and the subtitles share this model; each file is recognized only once (results are cached)\nBigger models are more accurate but slower: tiny / base fast, small balanced, medium / large accurate",
    "转录页正在录音，等它录完再录":
        "The Transcribe tab is recording; wait until it finishes",
    "转录页正在转录或录音，等它结束再送过去。":
        "The Transcribe tab is transcribing or recording; wait until it finishes, then send.",
    "这个程序里没有 Whisper，正在找装了 Whisper 的 Python……":
        "Whisper is not in this program; looking for a Python that has Whisper…",
    "这就是原文件，不用再存":
        "This is the original file; no need to save it again",
    "这看起来不像音频文件，还是要用它吗？":
        "This doesn't look like an audio file. Use it anyway?",
    "连不上 Edge 在线语音（{e}）":
        "Can't reach the Edge online voice ({e})",
    "连续播放":
        "Continuous",
    "送去精听 ▸":
        "To Listening ▸",
    "逐句视图：一句一行，点哪一行就对比哪一句   红色 = 漏读，橙色 = 读错，行尾 = 这句读对的词数":
        "Sentence view: one sentence per line, click a line to compare it   red = skipped, orange = wrong, line end = words read right",
    "速度":
        "Speed",
    "错误":
        "Error",
    "隐藏字幕":
        "Hide subs",
    "静音阈值 (dB)":
        "Silence threshold (dB)",
    "静音阈值 dB（相对峰值）。默认按本底噪声自动定；手动指定时，呼吸声被当成说话就调高（如 -25），吞字就调低（如 -40）":
        "Silence threshold in dB (relative to the peak). Default: set from the noise floor. When set by hand: raise it (e.g. -25) if breathing counts as speech, lower it (e.g. -40) if word endings get cut",
    "音量 {v:.0f}%":
        "Volume {v:.0f}%",
    "音频/视频":
        "Audio/Video",
    "音频已保存：{dst}":
        "Audio saved: {dst}",
    "音频或视频文件（m4a / mp3 / wav / mp4 等）；不填就打开窗口拖文件进去":
        "Audio or video file (m4a / mp3 / wav / mp4, etc.); leave it out to open the window and drop a file in",
    "音频没能保存：\n{e}":
        "Couldn't save the audio:\n{e}",
    "麦克风":
        "Microphone",
    "（你的录音里没找到这句，只放标准）":
        " (this sentence wasn't found in your recording; playing the reference only)",
    "（听完这句显示字幕，按 T 立即显示）":
        "(Subtitle shows after this sentence plays; press T to show it now)",
    "（字幕已隐藏，按 T 显示）":
        "(Subtitles hidden; press T to show)",
    "（字幕已隐藏，点“显示字幕”或按 T）":
        "(Subtitle hidden; click \"Show subs\" or press T)",
    "（没有扩展名）":
        "(no extension)",
    "（离线）":
        " (offline)",
    "（这句没识别出文字）":
        "(No text recognized in this sentence)",
    "（重启后生效）":
        "(after restart)",
    "，已转录":
        ", transcribed",
    "，静音阈值 {thr:.0f} dB（{how}）":
        ", silence threshold {thr:.0f} dB ({how})",
}
