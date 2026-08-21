"""统一磁盘 JSON 缓存（包装格式 {"_cache_time", "payload"}）。

此前 prts / yituliu / gamedata 三处各自复制了一整套「包装格式读写 + TTL 判定」，
且都在 async 路径上做同步文件 I/O——roguelike_topic_table.json（~18MB）的
read_text/write_text 会阻塞事件循环数百毫秒，写入也非原子。

本模块收口为：
- 读写走 asyncio.to_thread，不阻塞事件循环
- 原子写：先写临时文件再 os.replace（崩溃不留半个文件）
- 包装格式与旧缓存文件兼容（prts/yituliu/gamedata 历史缓存可直接读取）
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from gsuid_core.logger import logger


def _read_sync(path: Path) -> tuple[Any | None, float]:
    """同步读包装格式：返回 (payload, saved_ts)；损坏/缺失返回 (None, 0)。"""
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, 0.0
    if not isinstance(meta, dict):
        return None, 0.0
    payload = meta.get("payload")
    if payload is None:
        return None, 0.0
    try:
        saved_ts = float(meta.get("_cache_time", 0))
    except (TypeError, ValueError):
        saved_ts = 0.0
    return payload, saved_ts


def _write_sync(path: Path, payload: Any) -> None:
    """同步原子写：tmp + os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名必须每次唯一：同进程内并发写同一路径（to_thread 并行）时，
    # 只按 pid 命名会让两个线程共用同一 tmp，互相截断/删除对方半成品
    tmp_path = path.with_name(
        f"{path.name}.tmp{os.getpid()}.{uuid4().hex[:8]}"
    )
    try:
        tmp_path.write_text(
            json.dumps(
                {"_cache_time": time.time(), "payload": payload},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        # 写失败时清掉残留 tmp（replace 成功后 tmp 已不存在）
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


async def cache_read(path: Path) -> tuple[Any | None, float]:
    """读缓存 → (payload, saved_ts)。损坏/缺失返回 (None, 0)。

    payload 为 dict 的历史缓存（prts/yituliu/gamedata 同格式）可直接读取。
    """
    if not path.exists():
        return None, 0.0
    try:
        return await asyncio.to_thread(_read_sync, path)
    except Exception as exc:
        # 缓存层拿不到业务敏感值做脱敏，异常 message 一律不进日志（只打类名）
        logger.debug(
            f"[ArknightsUID·缓存] 读取失败 {path.name}: "
            f"{exc.__class__.__name__}"
        )
        return None, 0.0


async def cache_write(path: Path, payload: Any) -> None:
    """原子写缓存；失败仅 debug 日志（缓存失败不影响主流程）。"""
    try:
        await asyncio.to_thread(_write_sync, path, payload)
    except Exception as exc:
        logger.debug(
            f"[ArknightsUID·缓存] 写入失败 {path.name}: "
            f"{exc.__class__.__name__}"
        )


def is_fresh(saved_ts: float, ttl: float) -> bool:
    """TTL 判定（saved_ts=0 视为不新鲜）。"""
    return saved_ts > 0 and time.time() - saved_ts <= ttl
