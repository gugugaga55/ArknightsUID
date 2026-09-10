"""开发入口互斥锁：串行化 gsuid_core 配置初始化的写回窗口。

gsuid_core 的配置模块（plugins_config.gs_config）在进程首次导入时会
reconcile 默认值并 atomic_save 写回 core_config.json / 各插件配置。
boltons 的 atomic_save 以 O_EXCL 打开排他 ``.part`` 临时文件：两个进程
（如 render_previews 与 pytest）同时进入该窗口时，后到者 FileExistsError
直接崩溃，且崩溃残留的 ``.part`` 会阻塞之后所有运行（本库不自清）。

框架层不改（gsuid_core 是外部仓库），本锁在两个开发入口
（tools/render_previews.py、tests/conftest.py）启动时获取并持有到进程
退出——运行期后续的惰性插件导入（各自的配置写回）也被覆盖。等待超时
降级为无锁继续并告警（与 utils/file_lock 的"尽力而为"哲学一致），
不阻塞任务本身。

纯 stdlib 且不 import gsuid_core：锁自身不能触发它要保护的那个写。
Windows 无 fcntl 时退化为无锁（开发环境为 Linux/WSL，同 file_lock 口径）。
"""
from __future__ import annotations

import atexit
import sys
import time
from pathlib import Path

try:  # Windows 退化为无锁
    import fcntl

    _HAVE_FLOCK = True
except ImportError:  # pragma: no cover
    _HAVE_FLOCK = False

_LOCK_NAME = "arkuid_dev_init.lock"
# 渲染全量 ~80s 是常态持有上限，留足余量；超时降级而非失败
_WAIT_TIMEOUT = 600.0

_held = False
_handle = None


def _lock_path() -> Path:
    """锁文件落 .git/（开发副本必有、天然不入库）；非 git 环境退 tmp。"""
    root = Path(__file__).resolve().parents[1]
    git = root / ".git"
    if git.exists():
        return git / _LOCK_NAME
    import tempfile

    return Path(tempfile.gettempdir()) / _LOCK_NAME


def acquire_dev_init_lock(timeout: float = _WAIT_TIMEOUT) -> bool:
    """取进程级初始化锁（可重入；持有到进程退出，atexit 释放）。

    返回是否真正持锁——False 表示不可用平台或等锁超时（已告警降级）。
    """
    global _held, _handle
    if _held:
        return True
    if not _HAVE_FLOCK:  # pragma: no cover
        print("⚠️ [dev_init_lock] 平台无 flock，降级为无锁初始化", file=sys.stderr)
        return False
    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
    except OSError as e:
        print(f"⚠️ [dev_init_lock] 锁文件不可用({e})，降级为无锁初始化", file=sys.stderr)
        return False
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _held, _handle = True, handle
            atexit.register(_release)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                print(
                    f"⚠️ [dev_init_lock] 等锁超时({timeout:.0f}s)，降级为无锁初始化"
                    "（并发进程可能撞 .part 文件）",
                    file=sys.stderr,
                )
                return False
            time.sleep(0.05)


def _release() -> None:
    global _held, _handle
    if _handle is not None:
        try:
            fcntl.flock(_handle, fcntl.LOCK_UN)
        except OSError:
            pass
        _handle.close()
        _handle = None
    _held = False
