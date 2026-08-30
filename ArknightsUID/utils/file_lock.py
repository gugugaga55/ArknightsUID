"""跨进程文件锁 + 唯一临时文件（审计 #10：多进程状态写入协调）。

多 worker / 多容器共享数据目录部署时，asyncio.Lock 只保护单进程内的
读-改-写窗口；磁盘状态文件仍会被另一进程的旧快照整文件覆盖。本模块：

- ``file_locked(path)``：以 path 同目录的 ``<name>.lock`` 做进程间互斥
  （POSIX flock；Windows 用 msvcrt.locking；两者都不可用的平台退化为
  无锁——与现状一致，不劣化）。锁文件本身不删除（复用无副作用）。
- ``unique_tmp(path)``：带 pid+线程 id 的临时文件名，防并发写者互踩
  同一 tmp 后 os.replace 到一半被覆盖。

用法（同步上下文，状态写路径三步全在锁内）：

    with file_locked(state_path):
        state = json.loads(state_path.read_text(...))
        ...merge...
        tmp = unique_tmp(state_path)
        tmp.write_text(json.dumps(state))
        os.replace(tmp, state_path)
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_held = threading.local()


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
        _acquire(handle)
        held.add(key)
        yield
    finally:
        held.discard(key)
        if handle is not None:
            try:
                _release(handle)
            except Exception:
                pass
            handle.close()


def _acquire(handle) -> None:
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    except ImportError:
        pass
    try:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    except (ImportError, OSError):
        pass  # 无锁可用平台：退化为无进程间互斥（不劣化现状）


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
