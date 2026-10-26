"""A safety layer for sound output / input, shared by all three tabs; plus an error log.

Why it exists: PortAudio (under sounddevice) scans the sound devices only once, at start.
After the program has been idle for a while (common on Mac: Bluetooth headphones / AirPods
disconnect by themselves, the display sleeps, the system switches the default output),
the device it holds no longer exists, and the next playback either fails or simply hangs.

  call(fn, ...)  Call sounddevice (sd.play, opening a stream, ...): on error, re-initialize
                 audio and try once more; only if the second try also fails, raise
                 AudioError (with a Chinese message for the status line).
  prepare()      Before the first use of sound after more than IDLE_REFRESH seconds idle,
                 re-initialize first. This is for the "hang" case: a hang can't be caught
                 as an exception, so we can only avoid it in advance.
  in_use()       Wrap long playback / recording in this; no re-initialization happens
                 meanwhile, so a stream in use is not cut off.
  log            The log (listenstudio.log, next to the settings file). install_tk_hook makes
                 unhandled errors in button callbacks go to the log and the status line
                 instead of disappearing silently.
"""
import contextlib
import logging
import logging.handlers
import os
import threading
import time
import traceback

import sounddevice as sd

import app_paths

IDLE_REFRESH = 300  # Idle for more than 5 minutes: re-initialize before the next use of sound
LOG_FILE = os.path.join(app_paths.data_dir(), "listenstudio.log")

log = logging.getLogger("listenstudio")
if not log.handlers:
    log.setLevel(logging.INFO)
    try:
        h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2,
                                                 encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    except OSError:
        log.addHandler(logging.NullHandler())  # If the log can't be written, everything still works


class AudioError(RuntimeError):
    pass


_lock = threading.RLock()
_last_use = time.monotonic()
_busy = 0
_after_reinit = []  # Things to do after re-initializing (e.g. set the chosen speaker / mic again)


def on_reinit(fn):
    _after_reinit.append(fn)


def reinit(reason):
    """Close PortAudio and open it again: rescans the sound devices and drops dead device handles."""
    with _lock:
        log.info("重新初始化音频：%s", reason)
        try:
            sd.stop()
        except Exception:
            pass
        sd._terminate()
        sd._initialize()
        for fn in _after_reinit:
            try:
                fn()
            except Exception:
                log.exception("重新初始化后恢复设备设置失败")


def touch():
    global _last_use
    _last_use = time.monotonic()


def prepare():
    """Re-initialize first if idle for too long; do nothing while a stream is in use."""
    with _lock:
        idle = time.monotonic() - _last_use
        if idle > IDLE_REFRESH and _busy == 0:
            reinit(f"闲置了 {idle / 60:.0f} 分钟")
        touch()


def call(fn, *args, **kwargs):
    """Call sounddevice: if it fails, re-initialize audio and try once more."""
    with _lock:
        prepare()
        try:
            return fn(*args, **kwargs)
        except Exception as first:
            log.warning("音频调用 %s 失败：%r，重新初始化后再试一次",
                        getattr(fn, "__name__", fn), first)
            reinit(f"{getattr(fn, '__name__', fn)} 出错")
            try:
                return fn(*args, **kwargs)
            except Exception as second:
                log.error("重试仍然失败：%r", second)
                raise AudioError(f"声音设备出错（已自动重试一次）：{second}") from second
        finally:
            touch()


@contextlib.contextmanager
def in_use():
    global _busy
    with _lock:
        _busy += 1
    try:
        yield
    finally:
        with _lock:
            _busy -= 1
        touch()


def busy_begin():
    """For things like recording, where start and end are in different functions, use this pair instead of in_use()."""
    global _busy
    with _lock:
        _busy += 1


def busy_end():
    global _busy
    with _lock:
        _busy = max(0, _busy - 1)
    touch()


def install_tk_hook(root, show):
    """Errors not handled in Tk callbacks (buttons, shortcuts, timers): write them to the log, then show them with show(text).
    The packaged .app has no terminal; without this, errors disappear silently and it looks like "clicking does nothing"."""
    def report(exc, val, tb):
        log.error("界面回调出错：\n%s", "".join(traceback.format_exception(exc, val, tb)))
        try:
            show(f"出错了：{val}（详情见日志 {LOG_FILE}）")
        except Exception:
            pass
    root.report_callback_exception = report
