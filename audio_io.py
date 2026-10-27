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
from i18n import tr

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
        log.info("Re-initializing audio: %s", reason)
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
                log.exception("Failed to restore device settings after re-initializing")


def touch():
    global _last_use
    _last_use = time.monotonic()


def prepare():
    """Re-initialize first if idle for too long; do nothing while a stream is in use."""
    with _lock:
        idle = time.monotonic() - _last_use
        if idle > IDLE_REFRESH and _busy == 0:
            reinit(f"idle for {idle / 60:.0f} min")
        touch()


def call(fn, *args, **kwargs):
    """Call sounddevice: if it fails, re-initialize audio and try once more."""
    with _lock:
        prepare()
        try:
            return fn(*args, **kwargs)
        except Exception as first:
            log.warning("Audio call %s failed: %r; re-initializing and trying again",
                        getattr(fn, "__name__", fn), first)
            reinit(f"{getattr(fn, '__name__', fn)} failed")
            try:
                return fn(*args, **kwargs)
            except Exception as second:
                log.error("Retry failed too: %r", second)
                raise AudioError(tr("声音设备出错（已自动重试一次）：{e}", e=second)) from second
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
        log.error("Error in a UI callback:\n%s", "".join(traceback.format_exception(exc, val, tb)))
        try:
            show(tr("出错了：{err}（详情见日志 {log}）", err=val, log=LOG_FILE))
        except Exception:
            pass
    root.report_callback_exception = report
