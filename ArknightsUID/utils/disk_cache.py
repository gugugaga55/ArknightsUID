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
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from gsuid_core.logger import logger

# 包装格式写侧固定 _cache_time 在首位（json.dumps 保序），判鲜时
# 只读文件头即可取到时间戳，避免为一个 float 解析 15-18MB payload
_HEAD_TS_RE = re.compile(rb'"_cache_time"\s*:\s*([0-9][0-9.eE+-]*)')
_HEAD_READ_BYTES = 256


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
    """包装缓存沿用同一原子 JSON 写入实现。"""
    _write_json_sync(path, {"_cache_time": time.time(), "payload": payload})


def _write_json_sync(path: Path, payload: Any) -> None:
    """唯一临时文件 + 原子替换，失败不破坏旧 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名必须每次唯一：同进程内并发写同一路径（to_thread 并行）时，
    # 只按 pid 命名会让两个线程共用同一 tmp，互相截断/删除对方半成品
    tmp_path = path.with_name(f"{path.name}.tmp{os.getpid()}.{uuid4().hex[:8]}")
    try:
        tmp_path.write_text(
            json.dumps(
                payload,
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


def _read_json_sync(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def read_json(path: Path) -> Any | None:
    """异步读取纯 JSON（派生索引格式）；缺失/损坏返回 None。"""
    return await asyncio.to_thread(_read_json_sync, path)


async def write_json(path: Path, payload: Any) -> None:
    """异步原子写入纯 JSON；缓存写失败不影响本次业务结果。"""
    try:
        await asyncio.to_thread(_write_json_sync, path, payload)
    except (OSError, TypeError, ValueError) as exc:
        logger.debug(f"[ArknightsUID·缓存] 写入失败 {path.name}: {exc.__class__.__name__}")


def _saved_ts_sync(path: Path) -> float:
    """同步轻量读时间戳：头部命中直接用；未命中退回全量解析（语义与 cache_read 一致）。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_READ_BYTES)
    except OSError:
        return 0.0
    match = _HEAD_TS_RE.search(head)
    if match is None:
        _payload, saved_ts = _read_sync(path)
        return saved_ts
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


async def cache_saved_ts(path: Path) -> float:
    """读缓存时间戳（不解析 payload）；缺失/损坏返回 0。判鲜专用。"""
    if not path.exists():
        return 0.0
    try:
        return await asyncio.to_thread(_saved_ts_sync, path)
    except Exception as exc:
        # 同 cache_read：异常 message 一律不进日志（只打类名）
        logger.debug(
            f"[ArknightsUID·缓存] 时间戳读取失败 {path.name}: {exc.__class__.__name__}"
        )
        return 0.0


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
            f"[ArknightsUID·缓存] 读取失败 {path.name}: {exc.__class__.__name__}"
        )
        return None, 0.0


async def cache_write(path: Path, payload: Any, validators: dict | None = None) -> None:
    """原子写缓存；失败仅 debug 日志（缓存失败不影响主流程）。

    validators（etag/last_modified/source_url，条件 GET 复核用）写旁车
    文件 ``<name>.validators.json``——不进信封：18MB 大表判鲜只读文件头，
    信封塞校验器会破坏头部命中；旁车独立损坏也不影响主缓存读取。
    """
    try:
        await asyncio.to_thread(_write_sync, path, payload)
        sidecar = _validators_path(path)
        if validators is not None:
            await asyncio.to_thread(
                _write_validators_sync, sidecar, validators
            )
        else:
            # 新 payload 无校验器时，旧 ETag 不再描述当前缓存。
            await asyncio.to_thread(sidecar.unlink, missing_ok=True)
    except Exception as exc:
        logger.debug(
            f"[ArknightsUID·缓存] 写入失败 {path.name}: {exc.__class__.__name__}"
        )


def _validators_path(path: Path) -> Path:
    return path.with_name(path.name + ".validators.json")


def _write_validators_sync(sidecar: Path, validators: dict) -> None:
    _write_json_sync(sidecar, validators)


def _read_validators_sync(path: Path) -> dict:
    sidecar = _validators_path(path)
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def cache_read_validators(path: Path) -> dict:
    """读旁车校验器；缺失/损坏/非 dict 返回 {}（复核退化为全量下载）。"""
    if not _validators_path(path).exists():
        return {}
    try:
        return await asyncio.to_thread(_read_validators_sync, path)
    except Exception as exc:
        logger.debug(
            f"[ArknightsUID·缓存] 校验器读取失败 {path.name}: "
            f"{exc.__class__.__name__}"
        )
        return {}


def is_fresh(saved_ts: float, ttl: float) -> bool:
    """TTL 判定（saved_ts=0 视为不新鲜）。"""
    return saved_ts > 0 and time.time() - saved_ts <= ttl


def index_covers_sources(index_path: Path, *sources: Path) -> bool:
    """派生索引是否不早于其源表（按写入时间）。

    源表可能被其他入口旁路刷新（如 ark面板 走 ensure_char_table 重下
    新版表）：旧索引存在、源表也"新鲜"，但内容已落后——只查源表 TTL
    会继续复用旧索引，新干员/新物品最长 7 天查不到（审计 #39）。
    """
    try:
        index_mtime = index_path.stat().st_mtime
    except OSError:
        return False
    for source in sources:
        try:
            if index_mtime < source.stat().st_mtime:
                return False
        except OSError:
            continue
    return True
