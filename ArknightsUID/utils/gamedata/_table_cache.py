"""ArknightsGameData 表的共享下载/缓存层。

character_table.json（~15MB）的下载与 TTL 缓存逻辑原来只在 recruit.py，
别名系统（charinfo.py）也需要同源数据——抽到此处做单一事实源：

- 缓存文件：CACHE_PATH/gamedata/character_table.json，格式 {"_cache_time", "payload"}
- 复核间隔：每日条件 diff（内容近永久，304 零下载仅续期；详见 CHAR_TABLE_TTL 注释）
- 精简衍生缓存（recruit.json / charindex.json / itemindex.json）由各自模块生成；
  charinfo 系索引按"源表 mtime 覆盖"判鲜（源表刷新即重建，无独立 TTL）

下载走共享 HTTP 层（utils.http），缓存读写走统一磁盘缓存层
（utils.disk_cache：异步 I/O + 原子写，15-18MB 的全量表不再阻塞事件循环）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from gsuid_core.logger import logger

from ..http import download_bytes_conditional
from ..disk_cache import (
    cache_read,
    cache_read_validators,
    cache_saved_ts,
    cache_write,
    is_fresh,
)
from ..resource.RESOURCE_PATH import CACHE_PATH

GAMEDATA_BASE = (
    "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData"
    "/master/zh_CN/gamedata/excel"
)
CHAR_TABLE_URL = f"{GAMEDATA_BASE}/character_table.json"
SKILL_TABLE_URL = f"{GAMEDATA_BASE}/skill_table.json"
ITEM_TABLE_URL = f"{GAMEDATA_BASE}/item_table.json"
# 模组（uniequip 元数据 + battle_equip 各级数值；干员面板模拟用）
UNIEQUIP_TABLE_URL = f"{GAMEDATA_BASE}/uniequip_table.json"
BATTLE_EQUIP_TABLE_URL = f"{GAMEDATA_BASE}/battle_equip_table.json"
# 肉鸽全量表（~18MB：6 期主题的藏品/结局/分队/招募券等；tomimi.dev 同源数据）
ROGUE_TOPIC_TABLE_URL = f"{GAMEDATA_BASE}/roguelike_topic_table.json"
# 加工站配方（~5MB：workshopFormulas 合成树）与危机合约赛季表（小表）
BUILDING_TABLE_URL = f"{GAMEDATA_BASE}/building_data.json"
CRISIS_TABLE_URL = f"{GAMEDATA_BASE}/crisis_table.json"
CRISIS_V2_TABLE_URL = f"{GAMEDATA_BASE}/crisis_v2_table.json"
# 皮肤目录（~4MB：charSkins 含全部时装 displaySkin 元数据）
SKIN_TABLE_URL = f"{GAMEDATA_BASE}/skin_table.json"
# 关卡目录（~4MB：stages 的 code/name 供地图文件名枚举）
STAGE_TABLE_URL = f"{GAMEDATA_BASE}/stage_table.json"

GAMEDATA_CACHE_DIR = CACHE_PATH / "gamedata"
GAMEDATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CHAR_TABLE_PATH = GAMEDATA_CACHE_DIR / "character_table.json"
SKILL_TABLE_PATH = GAMEDATA_CACHE_DIR / "skill_table.json"
ITEM_TABLE_PATH = GAMEDATA_CACHE_DIR / "item_table.json"
UNIEQUIP_TABLE_PATH = GAMEDATA_CACHE_DIR / "uniequip_table.json"
BATTLE_EQUIP_TABLE_PATH = GAMEDATA_CACHE_DIR / "battle_equip_table.json"
ROGUE_TOPIC_TABLE_PATH = GAMEDATA_CACHE_DIR / "roguelike_topic_table.json"
BUILDING_TABLE_PATH = GAMEDATA_CACHE_DIR / "building_data.json"
CRISIS_TABLE_PATH = GAMEDATA_CACHE_DIR / "crisis_table.json"
CRISIS_V2_TABLE_PATH = GAMEDATA_CACHE_DIR / "crisis_v2_table.json"
SKIN_TABLE_PATH = GAMEDATA_CACHE_DIR / "skin_table.json"
STAGE_TABLE_PATH = GAMEDATA_CACHE_DIR / "stage_table.json"

# 复核间隔（非数据寿命）：全量表内容近永久，只在版本更新时变化——
# 间隔到点走条件 GET（ETag/If-None-Match）diff，上游没变（304）时
# 零下载零解析仅续期；所以间隔可以比旧 TTL（7 天）激进得多，
# 新干员/新期数据最长 1 天即可见。
CHAR_TABLE_TTL = 86400

# per-path 下载锁：TTL 过期瞬间的并发调用只触发一次 15-18MB 下载
# （single-flight：锁内复查缓存新鲜度，等锁的协程直接命中新缓存）
_table_locks: dict[Path, asyncio.Lock] = {}


def _lock_for(path: Path) -> asyncio.Lock:
    lock = _table_locks.get(path)
    if lock is None:
        lock = _table_locks[path] = asyncio.Lock()
    return lock


# 进程内已解析全量表的 LRU memo：TTL 内重复命令不再为同一张 15-18MB
# 表反复 json.loads（此前每条用到表的命令都要整表解析一次）。
# 上限 8 张（单条命令最多触达 2-3 张表）；淘汰只是回到"下次再解析一次"，
# 不改变任何对外行为。内存代价：常驻表的原生 dict 形态。
_TABLE_MEMO_MAX = 8
_table_memo: OrderedDict[Path, tuple[float, dict[str, Any]]] = OrderedDict()


def clear_table_memory_cache() -> None:
    """刷新游戏数据时同步失效内存表，下一次查询重新读取/下载。"""
    _table_memo.clear()


def _memo_get(path: Path, ttl: float) -> dict[str, Any] | None:
    hit = _table_memo.get(path)
    if hit is None:
        return None
    saved_ts, payload = hit
    if is_fresh(saved_ts, ttl):
        _table_memo.move_to_end(path)
        return payload
    _table_memo.pop(path, None)
    return None


def _memo_put(path: Path, saved_ts: float, payload: dict[str, Any]) -> None:
    _table_memo[path] = (saved_ts, payload)
    _table_memo.move_to_end(path)
    while len(_table_memo) > _TABLE_MEMO_MAX:
        _table_memo.popitem(last=False)


# 下载镜像：raw 直链偶发 SSL 中断，jsdelivr CDN 兜底（同一仓库同一分支）
_GAMEDATA_MIRROR_TEMPLATES = (
    None,  # 占位：先用原始 raw URL
    "https://cdn.jsdelivr.net/gh/Kengxxiao/ArknightsGameData@master"
    "/zh_CN/gamedata/excel/{name}",
    "https://fastly.jsdelivr.net/gh/Kengxxiao/ArknightsGameData@master"
    "/zh_CN/gamedata/excel/{name}",
)


async def _parse_json_bytes(raw: bytes) -> Any | None:
    """全量表 15-18MB，json.loads 实测 ~130ms/15MB——下放线程池，
    否则复核到点后第一个用到该表的命令会卡住整个事件循环。"""
    try:
        return await asyncio.to_thread(json.loads, raw)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·gamedata] 响应非 JSON: {exc.__class__.__name__}: {exc}"
        )
        return None


async def _download_json(url: str) -> tuple[Any | None, dict]:
    """下载 JSON（共享 HTTP 层；全量表放宽到 120s；raw 失败走 jsdelivr 镜像）。

    返回 (parsed, validators)：validators 含命中 URL 的 ETag/Last-Modified
    与 source_url，落盘后供下次复核做条件 diff（多镜像 ETag 不互通，
    条件请求必须回当初命中的源）。
    """
    name = url.rsplit("/", 1)[-1]
    for template in _GAMEDATA_MIRROR_TEMPLATES:
        try_url = url if template is None else template.format(name=name)
        status, raw, validators = await download_bytes_conditional(
            try_url, timeout=120.0
        )
        if raw is None:
            continue
        parsed = await _parse_json_bytes(raw)
        if parsed is None:
            continue
        merged = {**validators, "source_url": try_url}
        return parsed, merged
    return None, {}


async def _read_wrapped_cache(path: Path) -> dict[str, Any] | None:
    """读 {"_cache_time", "payload"} 包装格式；损坏返回 None。"""
    payload, _saved_ts = await cache_read(path)
    return payload if isinstance(payload, dict) else None


async def _cache_fresh(path: Path, ttl: float) -> bool:
    """包装缓存的 TTL 判定（只读文件头时间戳，不再解析全量 payload）。"""
    return is_fresh(await cache_saved_ts(path), ttl)


async def _read_fresh_from_disk(path: Path, ttl: float) -> dict[str, Any] | None:
    """磁盘新鲜缓存直读：判鲜只读文件头，命中才整表解析并回填 memo。"""
    saved_ts = await cache_saved_ts(path)
    if not is_fresh(saved_ts, ttl):
        return None
    payload, actual_ts = await cache_read(path)
    if isinstance(payload, dict):
        _memo_put(path, actual_ts, payload)
        return payload
    return None


async def _ensure_table(
    url: str, path: Path, ttl: float = CHAR_TABLE_TTL, force: bool = False
) -> dict[str, Any] | None:
    """ensure_* 表的共用策略：memo/新鲜缓存直读 → 下载回写 → 过期缓存兜底。

    下载段持 per-path 锁并复查新鲜度，避免 TTL 过期瞬间的并发调用
    重复拉取同一个 15-18MB 全量表。
    """
    if not force:
        memo = _memo_get(path, ttl)
        if memo is not None:
            return memo
        fresh = await _read_fresh_from_disk(path, ttl)
        if fresh is not None:
            return fresh
    async with _lock_for(path):
        if not force:
            # 等锁期间可能已有并发调用完成下载回写，复查一次
            memo = _memo_get(path, ttl)
            if memo is not None:
                return memo
            fresh = await _read_fresh_from_disk(path, ttl)
            if fresh is not None:
                return fresh
        # ---- 条件 diff 复核：有校验器且上游未变（304）→ 零下载零解析续期 ----
        validators = await cache_read_validators(path)
        if validators.get("etag") or validators.get("last_modified"):
            cond_url = str(validators.get("source_url") or url)
            status, raw, new_v = await download_bytes_conditional(
                cond_url,
                timeout=120.0,
                etag=str(validators.get("etag")) if validators.get("etag") else None,
                last_modified=(
                    str(validators.get("last_modified"))
                    if validators.get("last_modified")
                    else None
                ),
            )
            if status == 304:
                old_payload, _ = await cache_read(path)
                if isinstance(old_payload, dict):
                    merged = {**validators, **new_v, "source_url": cond_url}
                    await cache_write(path, old_payload, validators=merged)
                    _memo_put(path, time.time(), old_payload)
                    return old_payload
                # 304 却无旧 payload（旁车与主缓存脱节的异常态）：落全量
            elif raw is not None:
                parsed = await _parse_json_bytes(raw)
                if isinstance(parsed, dict):
                    merged = {**new_v, "source_url": cond_url}
                    await cache_write(path, parsed, validators=merged)
                    _memo_put(path, time.time(), parsed)
                    return parsed
        # ---- 无校验器（历史缓存）/ 条件请求网络失败 → 全量下载（镜像链）----
        fresh, fresh_validators = await _download_json(url)
        if isinstance(fresh, dict):
            await cache_write(path, fresh, validators=fresh_validators or None)
            _memo_put(path, time.time(), fresh)
            return fresh
        return await _read_wrapped_cache(path)


async def ensure_rogue_topic_table(force: bool = False) -> dict[str, Any] | None:
    """确保 roguelike_topic_table 可用（同 ensure_char_table 策略；~18MB）。"""
    return await _ensure_table(
        ROGUE_TOPIC_TABLE_URL, ROGUE_TOPIC_TABLE_PATH, force=force
    )


async def ensure_char_table(force: bool = False) -> dict[str, Any] | None:
    """确保 character_table 可用：新鲜缓存直读，否则下载并回写。

    下载失败时返回过期缓存兜底（可能为 None）。
    """
    return await _ensure_table(CHAR_TABLE_URL, CHAR_TABLE_PATH, force=force)


async def ensure_skill_table(force: bool = False) -> dict[str, Any] | None:
    """确保 skill_table 可用（同 ensure_char_table 策略）。"""
    return await _ensure_table(SKILL_TABLE_URL, SKILL_TABLE_PATH, force=force)


async def ensure_item_table(force: bool = False) -> dict[str, Any] | None:
    """确保 item_table 可用（同 ensure_char_table 策略）。"""
    return await _ensure_table(ITEM_TABLE_URL, ITEM_TABLE_PATH, force=force)


async def ensure_uniequip_table(force: bool = False) -> dict[str, Any] | None:
    """确保 uniequip_table 可用（模组元数据/归属，~3MB）。"""
    return await _ensure_table(UNIEQUIP_TABLE_URL, UNIEQUIP_TABLE_PATH, force=force)


async def ensure_battle_equip_table(force: bool = False) -> dict[str, Any] | None:
    """确保 battle_equip_table 可用（模组各级数值，~5MB）。"""
    return await _ensure_table(
        BATTLE_EQUIP_TABLE_URL, BATTLE_EQUIP_TABLE_PATH, force=force
    )


async def ensure_building_table(force: bool = False) -> dict[str, Any] | None:
    """确保 building_data 可用（加工站配方 workshopFormulas，~5MB）。"""
    return await _ensure_table(
        BUILDING_TABLE_URL, BUILDING_TABLE_PATH, ttl=CHAR_TABLE_TTL, force=force
    )


async def ensure_skin_table(force: bool = False) -> dict[str, Any] | None:
    """确保 skin_table 可用（charSkins 全皮肤目录，~4MB）。"""
    return await _ensure_table(
        SKIN_TABLE_URL, SKIN_TABLE_PATH, ttl=CHAR_TABLE_TTL, force=force
    )


async def ensure_stage_table(force: bool = False) -> dict[str, Any] | None:
    """确保 stage_table 可用（stages 的 code/name，供地图文件名枚举）。"""
    return await _ensure_table(
        STAGE_TABLE_URL, STAGE_TABLE_PATH, ttl=CHAR_TABLE_TTL, force=force
    )


async def ensure_crisis_tables(
    force: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """确保危机合约两代表可用（crisis + crisis_v2，均为小表）。

    旧危机合约（#1-12 行动）在 crisis_table，新版（涤墨作战起）在
    crisis_v2_table；任一失败返回该位 None，由调用方降级展示。
    """
    old = await _ensure_table(
        CRISIS_TABLE_URL, CRISIS_TABLE_PATH, ttl=CHAR_TABLE_TTL, force=force
    )
    new = await _ensure_table(
        CRISIS_V2_TABLE_URL, CRISIS_V2_TABLE_PATH, ttl=CHAR_TABLE_TTL, force=force
    )
    return old, new
