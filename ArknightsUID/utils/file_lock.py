"""跨进程文件锁 + 唯一临时文件（审计 #10：多进程状态写入协调）。

多 worker / 多容器共享数据目录部署时，asyncio.Lock 只保护单进程内的
读-改-写窗口；磁盘状态文件仍会被另一进程的旧快照整文件覆盖。本模块：

- ``file_locked(path)``：以 path 同目录的 ``<name>.lock`` 做进程间互斥
  （POSIX flock；Windows 用 msvcrt.locking；两者都不可用的平台退化为
  无锁——与现状一致，不劣化）。锁文件本身不删除（复用无副作用）。
- ``unique_tmp(path)``：带 pid+线程 id 的临时文件名，防并发写者互踩
  同一 tmp 后 os.replace 到一半被覆盖。

flock 等待自适应超时（审计 #锁超时）：flock 随持锁进程退出自动释放，
正常临界区（小 JSON 读-改-写）毫秒级完成；等待只会出现在多进程撞车
瞬间或对端异常挂起。为避免"外进程持锁不放 → 本进程事件循环被同步
flock 无限期冻死"，加锁改为非阻塞轮询：

- 工作线程内等锁上限 2s；
- 事件循环线程内上限 0.2s（阻塞循环的代价远高于降级）；
- 超时降级为无锁继续并告警——与无锁平台同档"尽力而为"：写侧仍是
  唯一 tmp + os.replace 原子写，最坏丢失一次并发合并，不会撕裂文件。

用法（同步上下文，状态写路径三步全在锁内）：

    with file_locked(state_path):
        state = json.loads(state_path.read_text(...))
        ...merge...
        tmp = unique_tmp(state_path)
        tmp.write_text(json.dumps(state))
        os.replace(tmp, state_path)
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

from gsuid_core.logger import logger

# 工作线程 / 事件循环线程各自的等锁上限与轮询间隔（秒）
_LOCK_TIMEOUT_WORKER = 2.0
_LOCK_TIMEOUT_EVENT_LOOP = 0.2
_LOCK_POLL_INTERVAL = 0.02

_held = threading.local()


def _lock_timeout() -> float:
    """当前线程的等锁上限：跑在事件循环线程里时尽快放弃。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _LOCK_TIMEOUT_WORKER
    return _LOCK_TIMEOUT_EVENT_LOOP


@contextmanager
def file_locked(state_path: Path) -> Iterator[None]:
    """对 state_path 的跨进程写锁（flock / msvcrt，尽力而为）。

    同线程可重入：嵌套加锁（如外层段落锁 + 内部写函数再锁）不会
    自锁——flock 对同进程的不同 fd 仍然互斥，靠线程本地持有集合规避。
    """
    key = str(state_path)
    held = getattr(_held, "keys", None)
    if held is None:
        held = set()
        _held.keys = held
    if key in held:
        yield
        return
    lock_path = state_path.with_name(state_path.name + ".lock")
    handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        acquired = _acquire(handle, _lock_timeout())
        held.add(key)
        if not acquired:
            logger.warning(
                f"[ArknightsUID·文件锁] {lock_path.name} 等锁超时，"
                "降级为无锁写入（多进程部署下可能丢失一次并发合并）"
            )
        yield
    finally:
        held.discard(key)
        if handle is not None:
            try:
                _release(handle)
            except Exception:
                pass
            handle.close()


def _acquire(handle, timeout: float) -> bool:
    """非阻塞轮询加锁，超时放弃；返回是否真正持锁。"""
    try:
        import fcntl
    except ImportError:
        fcntl = None
    if fcntl is not None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_LOCK_POLL_INTERVAL)
    try:
        import msvcrt

        # LK_LOCK 自带 ~10s 重试语义；Windows 路径维持旧行为
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return True
    except (ImportError, OSError):
        # 无锁可用平台：退化为无进程间互斥（不劣化现状）
        return False


def _release(handle) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    except ImportError:
        pass
    try:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except (ImportError, OSError):
        pass


def unique_tmp(state_path: Path) -> Path:
    """带 pid+线程 id 的临时文件路径（并发写者不互踩 tmp）。"""
    return state_path.with_name(
        f"{state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
