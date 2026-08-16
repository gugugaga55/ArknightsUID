"""ArknightsGameData 表的共享下载/缓存层。

character_table.json（~15MB）的下载与 TTL 缓存逻辑原来只在 recruit.py，
别名系统（charinfo.py）也需要同源数据——抽到此处做单一事实源：

- 缓存文件：CACHE_PATH/gamedata/character_table.json，格式 {"_cache_time", "payload"}
- TTL：7 天（游戏数据随版本更新）
- 精简衍生缓存（recruit.json / charindex.json / skillnames.json）由各自模块生成
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from gsuid_core.logger import logger

try:
    from ArknightsUID.ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH
except ModuleNotFoundError:
    from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

GAMEDATA_BASE = (
    "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData"
    "/master/zh_CN/gamedata/excel"
)
CHAR_TABLE_URL = f"{GAMEDATA_BASE}/character_table.json"
SKILL_TABLE_URL = f"{GAMEDATA_BASE}/skill_table.json"
ITEM_TABLE_URL = f"{GAMEDATA_BASE}/item_table.json"
# 肉鸽全量表（~18MB：6 期主题的藏品/结局/分队/招募券等；tomimi.dev 同源数据）
ROGUE_TOPIC_TABLE_URL = f"{GAMEDATA_BASE}/roguelike_topic_table.json"

GAMEDATA_CACHE_DIR = CACHE_PATH / "gamedata"
GAMEDATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CHAR_TABLE_PATH = GAMEDATA_CACHE_DIR / "character_table.json"
SKILL_TABLE_PATH = GAMEDATA_CACHE_DIR / "skill_table.json"
ITEM_TABLE_PATH = GAMEDATA_CACHE_DIR / "item_table.json"
ROGUE_TOPIC_TABLE_PATH = GAMEDATA_CACHE_DIR / "roguelike_topic_table.json"

# 全量缓存 TTL（game data 随版本更新，7 天足够）
CHAR_TABLE_TTL = 7 * 86400


async def _download_json(url: str) -> Any | None:
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·gamedata] 拉取失败 {url.rsplit('/', 1)[-1]}: "
            f"{exc.__class__.__name__}"
        )
        return None


def _read_wrapped_cache(path: Path) -> dict[str, Any] | None:
    """读 {"_cache_time", "payload"} 包装格式；损坏返回 None。"""
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        payload = meta.get("payload")
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return None


def _cache_fresh(path: Path, ttl: float) -> bool:
    if not path.exists():
        return False
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        return time.time() - float(meta.get("_cache_time", 0)) <= ttl
    except Exception:
        return False


def _write_wrapped_cache(path: Path, payload: Any) -> None:
    try:
        path.write_text(
            json.dumps({"_cache_time": time.time(), "payload": payload},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug(
            f"[ArknightsUID·gamedata] 全量缓存写入失败 {path.name}: "
            f"{exc.__class__.__name__}"
        )


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
            f"{exc.__class__.__name__}"
        )


async def ensure_rogue_topic_table(force: bool = False) -> dict[str, Any] | None:
    """确保 roguelike_topic_table 可用（同 ensure_char_table 策略；~18MB）。"""
    if not force and _cache_fresh(ROGUE_TOPIC_TABLE_PATH, CHAR_TABLE_TTL):
        cached = _read_wrapped_cache(ROGUE_TOPIC_TABLE_PATH)
        if cached is not None:
            return cached
    fresh = await _download_json(ROGUE_TOPIC_TABLE_URL)
    if isinstance(fresh, dict):
        _write_wrapped_cache(ROGUE_TOPIC_TABLE_PATH, fresh)
        return fresh
    return _read_wrapped_cache(ROGUE_TOPIC_TABLE_PATH)


async def ensure_char_table(force: bool = False) -> dict[str, Any] | None:
    """确保 character_table 可用：新鲜缓存直读，否则下载并回写。

    下载失败时返回过期缓存兜底（可能为 None）。
    """
    if not force and _cache_fresh(CHAR_TABLE_PATH, CHAR_TABLE_TTL):
        cached = _read_wrapped_cache(CHAR_TABLE_PATH)
        if cached is not None:
            return cached
    fresh = await _download_json(CHAR_TABLE_URL)
    if isinstance(fresh, dict):
        _write_wrapped_cache(CHAR_TABLE_PATH, fresh)
        return fresh
    return _read_wrapped_cache(CHAR_TABLE_PATH)


async def ensure_skill_table(force: bool = False) -> dict[str, Any] | None:
    """确保 skill_table 可用（同 ensure_char_table 策略）。"""
    if not force and _cache_fresh(SKILL_TABLE_PATH, CHAR_TABLE_TTL):
        cached = _read_wrapped_cache(SKILL_TABLE_PATH)
        if cached is not None:
            return cached
    fresh = await _download_json(SKILL_TABLE_URL)
    if isinstance(fresh, dict):
        _write_wrapped_cache(SKILL_TABLE_PATH, fresh)
        return fresh
    return _read_wrapped_cache(SKILL_TABLE_PATH)


async def ensure_item_table(force: bool = False) -> dict[str, Any] | None:
    """确保 item_table 可用（同 ensure_char_table 策略）。"""
    if not force and _cache_fresh(ITEM_TABLE_PATH, CHAR_TABLE_TTL):
        cached = _read_wrapped_cache(ITEM_TABLE_PATH)
        if cached is not None:
            return cached
    fresh = await _download_json(ITEM_TABLE_URL)
    if isinstance(fresh, dict):
        _write_wrapped_cache(ITEM_TABLE_PATH, fresh)
        return fresh
    return _read_wrapped_cache(ITEM_TABLE_PATH)
