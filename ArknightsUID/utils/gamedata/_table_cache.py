"""ArknightsGameData 表的共享下载/缓存层。

character_table.json（~15MB）的下载与 TTL 缓存逻辑原来只在 recruit.py，
别名系统（charinfo.py）也需要同源数据——抽到此处做单一事实源：

- 缓存文件：CACHE_PATH/gamedata/character_table.json，格式 {"_cache_time", "payload"}
- TTL：7 天（游戏数据随版本更新）
- 精简衍生缓存（recruit.json / charindex.json / skillnames.json）由各自模块生成

下载走共享 HTTP 层（utils.http），缓存读写走统一磁盘缓存层
（utils.disk_cache：异步 I/O + 原子写，15-18MB 的全量表不再阻塞事件循环）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from gsuid_core.logger import logger

try:
    from ArknightsUID.ArknightsUID.utils.http import download_bytes
    from ArknightsUID.ArknightsUID.utils.disk_cache import (
        cache_read,
        cache_write,
        is_fresh,
    )
    from ArknightsUID.ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH
except ModuleNotFoundError:
    from ArknightsUID.utils.http import download_bytes
    from ArknightsUID.utils.disk_cache import (
        cache_read,
        cache_write,
        is_fresh,
    )
    from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

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

# 全量缓存 TTL（game data 随版本更新，7 天足够）
CHAR_TABLE_TTL = 7 * 86400

# per-path 下载锁：TTL 过期瞬间的并发调用只触发一次 15-18MB 下载
# （single-flight：锁内复查缓存新鲜度，等锁的协程直接命中新缓存）
_table_locks: dict[Path, asyncio.Lock] = {}


def _lock_for(path: Path) -> asyncio.Lock:
    lock = _table_locks.get(path)
    if lock is None:
        lock = _table_locks[path] = asyncio.Lock()
    return lock


# 下载镜像：raw 直链偶发 SSL 中断，jsdelivr CDN 兜底（同一仓库同一分支）
_GAMEDATA_MIRROR_TEMPLATES = (
    None,  # 占位：先用原始 raw URL
    "https://cdn.jsdelivr.net/gh/Kengxxiao/ArknightsGameData@master"
    "/zh_CN/gamedata/excel/{name}",
    "https://fastly.jsdelivr.net/gh/Kengxxiao/ArknightsGameData@master"
    "/zh_CN/gamedata/excel/{name}",
)


async def _download_json(url: str) -> Any | None:
    """下载 JSON（共享 HTTP 层；全量表放宽到 120s；raw 失败走 jsdelivr 镜像）。"""
    name = url.rsplit("/", 1)[-1]
    for template in _GAMEDATA_MIRROR_TEMPLATES:
        try_url = url if template is None else template.format(name=name)
        raw = await download_bytes(try_url, timeout=120.0)
        if raw is None:
            continue
        try:
            return json.loads(raw)
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·gamedata] 响应非 JSON {try_url.rsplit('/', 1)[-1]}: "
                f"{exc.__class__.__name__}: {exc}"
            )
    return None


async def _read_wrapped_cache(path: Path) -> dict[str, Any] | None:
    """读 {"_cache_time", "payload"} 包装格式；损坏返回 None。"""
    payload, _saved_ts = await cache_read(path)
    return payload if isinstance(payload, dict) else None


async def _cache_fresh(path: Path, ttl: float) -> bool:
    """包装缓存的 TTL 判定（async 版，供 charinfo/recruit/rogue_relic 用）。"""
    _payload, saved_ts = await cache_read(path)
    return is_fresh(saved_ts, ttl)


def _read_sidecar(path: Path) -> Any | None:
    """读精简衍生缓存（纯 JSON，无包装）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_sidecar(path: Path, payload: Any) -> None:
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.debug(
            f"[ArknightsUID·gamedata] 精简缓存写入失败 {path.name}: "
            f"{exc.__class__.__name__}: {exc}"
        )


async def _ensure_table(
    url: str, path: Path, ttl: float = CHAR_TABLE_TTL, force: bool = False
) -> dict[str, Any] | None:
    """ensure_* 表的共用策略：新鲜缓存直读 → 下载回写 → 过期缓存兜底。

    下载段持 per-path 锁并复查新鲜度，避免 TTL 过期瞬间的并发调用
    重复拉取同一个 15-18MB 全量表。
    """
    if not force:
        payload, saved_ts = await cache_read(path)
        if isinstance(payload, dict) and is_fresh(saved_ts, ttl):
            return payload
    async with _lock_for(path):
        if not force:
            # 等锁期间可能已有并发调用完成下载回写，复查一次
            payload, saved_ts = await cache_read(path)
            if isinstance(payload, dict) and is_fresh(saved_ts, ttl):
                return payload
        fresh = await _download_json(url)
        if isinstance(fresh, dict):
            await cache_write(path, fresh)
            return fresh
        return await _read_wrapped_cache(path)


async def ensure_rogue_topic_table(force: bool = False) -> dict[str, Any] | None:
    """确保 roguelike_topic_table 可用（同 ensure_char_table 策略；~18MB）。"""
    return await _ensure_table(ROGUE_TOPIC_TABLE_URL, ROGUE_TOPIC_TABLE_PATH, force=force)


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
