"""肉鸽藏品查询索引（roguelike_topic_table → 精简索引）。

数据源：ArknightsGameData roguelike_topic_table.json（tomimi.dev 同源），
每期 details[tid].relics 给藏品成员、items 给展示信息（name/usage/desc/
iconId/rarity，type=RELIC）。同名藏品跨期复用（钝爪-典训 每期都有），
索引按名聚合、展示按效果去重组。

主题编号注意（实测推位规律，5 对名字全部吻合）：主题表 rogue_N 从傀影起编，
玩家数据 rogue_N 从灰蕈迷境起编——表 rogue_5 = 岁的界园志异 = 玩家 rogue_6。
本模块只用主题表自身 id+name，不与玩家侧编号互转。
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

import httpx

from ._table_cache import (
    GAMEDATA_CACHE_DIR,
    ROGUE_TOPIC_TABLE_PATH,
    _cache_fresh,
    _read_sidecar,
    _write_sidecar,
    ensure_rogue_topic_table,
)

RELIC_INDEX_PATH = GAMEDATA_CACHE_DIR / "roguerelicindex.json"
RELIC_ICON_CACHE_DIR = GAMEDATA_CACHE_DIR / "relic_icons"

# 主题表 id 顺序（旧 → 新； rogue_1=傀影 … rogue_6=沉沦者的黑流树海）
TABLE_THEME_ORDER = [
    "rogue_1",
    "rogue_2",
    "rogue_3",
    "rogue_4",
    "rogue_5",
    "rogue_6",
]

RARITY_NAMES = {
    "NORMAL": "普通",
    "RARE": "稀有",
    "SUPER_RARE": "珍贵",
}

# 藏品图标：tomimi.dev 开源仓库静态资源（与表内 iconId 一一对应）。
# 经 jsdelivr CDN 镜像取（image/webp）。不走核心 pic_download_from_url：
# 其内部 httpx 客户端默认 5s 超时且不跟随重定向，实测此源必超时；
# 这里自带 20s + follow_redirects 的客户端，失败落 .miss 负缓存（1h）。
_TOMIMI_STATIC_BASE = (
    "https://cdn.jsdelivr.net/gh/tanch-p/tomimi-dev@master"
    "/static/images/relics"
)
_ICON_MISS_TTL = 3600.0


def _theme_sort_key(theme_id: str) -> int:
    try:
        return TABLE_THEME_ORDER.index(theme_id)
    except ValueError:
        return len(TABLE_THEME_ORDER)


def build_relic_index(topic_table: dict[str, Any]) -> dict[str, list[dict]]:
    """主题表 → {藏品名: [各期条目]}，条目按主题新→旧排序。

    条目：{theme_id, theme_name, usage, desc, rarity, icon_id}；
    同名跨期效果相同是常态，展示层再按 usage 去重。
    """
    topics = topic_table.get("topics") or {}
    details = topic_table.get("details") or {}
    index: dict[str, list[dict]] = {}
    for theme_id, detail in details.items():
        if not isinstance(detail, dict):
            continue
        theme_name = str((topics.get(theme_id) or {}).get("name") or theme_id)
        relics = detail.get("relics") or {}
        items = detail.get("items") or {}
        for relic_id, item in items.items():
            if relic_id not in relics or not isinstance(item, dict):
                continue
            if item.get("type") != "RELIC":
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            index.setdefault(name, []).append(
                {
                    "theme_id": theme_id,
                    "theme_name": theme_name,
                    "usage": str(item.get("usage") or ""),
                    "desc": str(item.get("description") or ""),
                    "rarity": str(item.get("rarity") or ""),
                    "icon_id": str(item.get("iconId") or ""),
                }
            )
    for entries in index.values():
        entries.sort(key=lambda e: -_theme_sort_key(e["theme_id"]))
    return index


async def load_relic_index() -> dict[str, list[dict]]:
    """藏品索引（精简缓存优先，TTL 同主题表 7 天）。失败返回 {}。"""
    if RELIC_INDEX_PATH.exists() and _cache_fresh(
        ROGUE_TOPIC_TABLE_PATH, 7 * 86400
    ):
        cached = _read_sidecar(RELIC_INDEX_PATH)
        if isinstance(cached, dict) and cached:
            return cached
    table = await ensure_rogue_topic_table()
    if table is None:
        cached = _read_sidecar(RELIC_INDEX_PATH)
        return cached if isinstance(cached, dict) else {}
    index = build_relic_index(table)
    if index:
        _write_sidecar(RELIC_INDEX_PATH, index)
    return index


def match_relics(
    query: str, index: dict[str, list[dict]], limit: int = 8
) -> list[str]:
    """按名匹配：精确 → 唯一前缀 → 唯一子串（书名号/引号开头的名常见）。

    多命中返回候选列表（≤limit），由调用方提示补全。
    """
    q = (query or "").strip()
    if not q:
        return []
    for name in index:
        if name == q:
            return [name]
    prefix_hits = [name for name in index if name.startswith(q)]
    if len(prefix_hits) == 1:
        return prefix_hits
    if len(prefix_hits) > 1:
        prefix_hits.sort()
        return prefix_hits[:limit]
    contains_hits = [name for name in index if q in name]
    if len(contains_hits) == 1:
        return contains_hits
    contains_hits.sort()
    return contains_hits[:limit]


def group_relic_effects(entries: list[dict]) -> list[dict]:
    """同名跨期条目按（效果, 稀有度）去重 → [{usage, rarity, themes: [主题名]}]。

    主题名按新 → 旧；效果完全一致的复用藏品只占一行。
    """
    groups: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    for entry in entries:
        key = (entry.get("usage") or "", entry.get("rarity") or "")
        if key in seen:
            if entry["theme_name"] not in seen[key]["themes"]:
                seen[key]["themes"].append(entry["theme_name"])
            continue
        group = {
            "usage": entry.get("usage") or "",
            "rarity": entry.get("rarity") or "",
            "rarity_name": RARITY_NAMES.get(entry.get("rarity") or "", ""),
            "icon_id": entry.get("icon_id") or "",
            "desc": entry.get("desc") or "",
            "themes": [entry.get("theme_name") or ""],
        }
        seen[key] = group
        groups.append(group)
    return groups


async def relic_icon_b64(icon_id: str) -> str:
    """藏品图标 base64 data URL（自下载 + 磁盘缓存 + 失败负缓存）。

    空 id / 下载失败 / 坏文件返回空串（模板降级为无图行）。
    """
    icon_id = (icon_id or "").strip()
    if not icon_id:
        return ""
    cache_dir = Path(RELIC_ICON_CACHE_DIR)
    filename = f"{icon_id}.webp"
    local_path = cache_dir / filename
    miss_path = cache_dir / f"{filename}.miss"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if miss_path.exists():
            if time.time() - miss_path.stat().st_mtime < _ICON_MISS_TTL:
                return ""
            miss_path.unlink()
        if not local_path.exists():
            url = f"{_TOMIMI_STATIC_BASE}/{filename}"
            async with httpx.AsyncClient(
                timeout=20.0, follow_redirects=True
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                local_path.write_bytes(resp.content)
        data = local_path.read_bytes()
        if not data:
            raise ValueError("empty icon file")
        return f"data:image/webp;base64,{base64.b64encode(data).decode('utf-8')}"
    except Exception:
        local_path.unlink(missing_ok=True)
        try:
            miss_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        return ""
