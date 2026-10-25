"""声音输出 / 输入的"保险"，三个页面共用；外加一份错误日志。

为什么要有它：sounddevice 底下的 PortAudio 只在启动时扫描一次声卡。
程序闲置一阵子之后（Mac 上尤其常见：蓝牙耳机 / AirPods 自动断开、显示器休眠、
系统切换了默认输出），它手里的设备已经不存在了，这时再播放要么报错、要么直接卡住。

  call(fn, ...)  调用 sounddevice（sd.play、打开音频流……）：出错就重新初始化音频再试一次，
                 第二次还失败才抛出 AudioError（带中文说明）。
  prepare()      闲置超过 IDLE_REFRESH 秒后第一次用声音前，先主动重新初始化——
                 对付"卡住"的情况（卡住是抓不到异常的，只能提前避开）。
  in_use()       正在长时间播放 / 录音时套上它，这期间不会去重新初始化，免得把正在用的流掐断。
  log            日志（listenstudio.log，放在设置文件旁边），install_tk_hook 让按钮回调里
                 没被处理的错误也写进日志、并显示在状态行，而不是悄悄消失。
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

IDLE_REFRESH = 300  # 闲置超过 5 分钟，下次用声音前先重新初始化
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
        log.addHandler(logging.NullHandler())  # 写不了日志也不影响用


class AudioError(RuntimeError):
    pass


_lock = threading.RLock()
_last_use = time.monotonic()
_busy = 0
_after_reinit = []  # 重新初始化后要做的事（例如把设置里选的扬声器 / 麦克风再设一遍）


def on_reinit(fn):
    _after_reinit.append(fn)


def reinit(reason):
    """关掉 PortAudio 再重新打开：重新扫描声卡，丢掉失效的设备句柄。"""
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
    """闲置太久了就先重新初始化；有流正在用时不动。"""
    with _lock:
        idle = time.monotonic() - _last_use
        if idle > IDLE_REFRESH and _busy == 0:
            reinit(f"闲置了 {idle / 60:.0f} 分钟")
        touch()


def call(fn, *args, **kwargs):
    """调用 sounddevice：失败就重新初始化音频再试一次。"""
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
    """录音这种开始 / 结束不在同一个函数里的，用这一对代替 in_use()。"""
    global _busy
    with _lock:
        _busy += 1


def busy_end():
    global _busy
    with _lock:
        _busy = max(0, _busy - 1)
    touch()


def install_tk_hook(root, show):
    """Tk 回调（按钮、快捷键、定时器）里没被处理的错误：写进日志，再用 show(文字) 显示出来。
    打包的 .app 没有终端，不这样做的话错误会悄悄消失，看起来就是"点了没反应"。"""
    def report(exc, val, tb):
        log.error("界面回调出错：\n%s", "".join(traceback.format_exception(exc, val, tb)))
        try:
            show(f"出错了：{val}（详情见日志 {LOG_FILE}）")
        except Exception:
            pass
    root.report_callback_exception = report
