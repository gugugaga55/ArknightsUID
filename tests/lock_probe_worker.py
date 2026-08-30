"""跨进程文件锁验证的子进程体（模块级、仅 stdlib）。

spawn 子进程会 import 本模块：顶层绝不能 import ArknightsUID/gsuid_core
（包初始化的配置写盘会在并发子进程间撞 .part 临时文件）。file_lock 本身
是纯 stdlib 模块，按文件路径直接加载。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

_FILE_LOCK_PATH = (
    Path(__file__).resolve().parents[1] / "ArknightsUID" / "utils" / "file_lock.py"
)


def _load_file_lock():
    spec = importlib.util.spec_from_file_location("ark_file_lock", _FILE_LOCK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lock_write_worker(state_path: str, key: str) -> None:
    """锁内 读→update→唯一 tmp→replace（真实多进程读改写）。"""
    file_locked, unique_tmp = _load_file_lock().file_locked, _load_file_lock().unique_tmp
    target = Path(state_path)
    with file_locked(target):
        try:
            state = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        state[key] = 1
        tmp = unique_tmp(target)
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, target)
