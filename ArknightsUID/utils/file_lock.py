"""跨进程文件锁 + 唯一临时文件（审计 #10：多进程状态写入协调）。

多 worker / 多容器共享数据目录部署时，asyncio.Lock 只保护单进程内的
读-改-写窗口；磁盘状态文件仍会被另一进程的旧快照整文件覆盖。本模块：

- ``file_locked(path)``：以 path 同目录的 ``<name>.lock`` 做进程间互斥
  （POSIX flock；Windows 用 msvcrt.locking；两者都不可用的平台退化为
  无锁——与现状一致，不劣化）。锁文件本身不删除（复用无副作用）。
- ``unique_tmp(path)``：带 pid+线程 id 的临时文件名，防并发写者互踩
  同一 tmp 后 os.replace 到一半被覆盖。

加锁结果三态：

- 竞争超时 → 抛 :class:`LockTimeoutError`，**不进入写临界区**：旧实现
  "超时降级为无锁继续"会让本进程旧快照整文件覆盖另一进程的更新并照常
  报成功；现在事务直接失败，由调用方按失败处理（不改文件、不报成功）。
- 平台无原生锁（flock / msvcrt 均不可用）→ 保持既有明确兼容退化：
  无进程间互斥继续执行，每进程每锁文件告警一次（不宣称跨进程互斥）。
- 权限等其他 OSError → 原样上抛，不与上面两态混淆。

flock 等待自适应超时：正常临界区（小 JSON 读-改-写）毫秒级完成；等待
只出现在多进程撞车瞬间或对端异常挂起。非阻塞轮询避免同步 flock 无限期
冻死线程：

- 工作线程内等锁上限 2s；
- 事件循环线程内上限 0.2s（调用方应尽量经 asyncio.to_thread 把完整
  读-改-写事务下放线程，避免走到这一档）。

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
import errno
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

# “字节区被占用”类 errno：竞争，轮询/超时处理（其余 OSError 是真实错误）
_POSIX_CONTEND_ERRNOS = frozenset(
    e for e in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK) if e
)
_WINDOWS_CONTEND_ERRNOS = frozenset(
    e for e in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK) if e
)

_held = threading.local()
# 平台不支持时每个锁文件只告警一次（避免每轮写入刷屏）
_unsupported_warned: set[str] = set()


class LockTimeoutError(RuntimeError):
    """文件锁竞争超时：未获得锁，调用方不得进入写临界区。"""


def _lock_timeout() -> float:
    """当前线程的等锁上限：跑在事件循环线程里时尽快放弃。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _LOCK_TIMEOUT_WORKER
    return _LOCK_TIMEOUT_EVENT_LOOP


def _warn_unsupported_once(lock_path: Path) -> None:
    key = str(lock_path)
    if key in _unsupported_warned:
        return
    _unsupported_warned.add(key)
    logger.warning(
        f"[ArknightsUID·文件锁] {lock_path.name} 当前平台无原生文件锁"
        "（flock/msvcrt 均不可用），退化为无进程间互斥——多进程共享数据目录"
        "部署需外部协调，写入仅有唯一 tmp + os.replace 的防撕裂保护"
    )


@contextmanager
def file_locked(state_path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """对 state_path 的跨进程写锁（flock / msvcrt）。

    同线程可重入：嵌套加锁（如外层段落锁 + 内部写函数再锁）不会
    自锁——flock 对同进程的不同 fd 仍然互斥，靠线程本地持有集合规避。

    竞争超时抛 LockTimeoutError（事务失败，不进临界区）；无原生锁平台
    告警一次后无锁继续（与历史行为一致，不劣化）。
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
    acquired = False
    effective_timeout = _lock_timeout() if timeout is None else timeout
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        acquired = _acquire(handle, effective_timeout)
        if acquired:
            held.add(key)
        else:
            _warn_unsupported_once(lock_path)
        yield
    finally:
        # 只在确实拿锁时记录持有与解锁
        if acquired:
            held.discard(key)
            try:
                _release(handle)
            except Exception:
                # 释放失败由随后的 close 兜底（fd 关闭即释放 OS 锁）
                logger.debug(
                    f"[ArknightsUID·文件锁] {lock_path.name} 解锁异常", exc_info=True
                )
        if handle is not None:
            handle.close()


def _acquire(handle, timeout: float) -> bool:
    """非阻塞轮询加锁。

    返回 True=已持锁；False 仅表示平台无原生锁（明确退化）；竞争超时抛
    LockTimeoutError；权限等其他 OSError 原样上抛，不混入前两态。
    """
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
            except OSError as exc:
                if exc.errno not in _POSIX_CONTEND_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"lock contention timeout after {timeout}s"
                    ) from exc
                time.sleep(_LOCK_POLL_INTERVAL)
    try:
        import msvcrt
    except ImportError:
        return False

    # LK_NBLCK 非阻塞、按 timeout 轮询（不沿用 LK_LOCK 的内置 ~10s 等待）。
    # 锁文件以 a+ 打开、初始位置在 EOF，而 msvcrt.locking 从当前文件位置
    # 锁字节——每次尝试前 seek(0)，与 _release 统一锁定/解锁第 0 字节。
    deadline = time.monotonic() + timeout
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno not in _WINDOWS_CONTEND_ERRNOS:
                raise
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    f"lock contention timeout after {timeout}s"
                ) from exc
            time.sleep(_LOCK_POLL_INTERVAL)


def _release(handle) -> None:
    try:
        import fcntl
    except ImportError:
        pass
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    import msvcrt

    # 解锁第 0 字节：先回文件起点再 LK_UNLCK（与 _acquire 同一偏移）
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


_tmp_seq = 0
_tmp_seq_lock = threading.Lock()


def unique_tmp(state_path: Path) -> Path:
    """带 pid+线程 id+自增序号的临时文件路径（同线程多协程并发也不互踩 tmp）。"""
    global _tmp_seq
    with _tmp_seq_lock:
        _tmp_seq += 1
        seq = _tmp_seq
    return state_path.with_name(
        f"{state_path.name}.{os.getpid()}.{threading.get_ident()}.{seq}.tmp"
    )
