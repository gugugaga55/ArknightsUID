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

from ..http import download_bytes_status
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

# 主题表 id 顺序（旧 → 新）收敛至 rogue_registry（唯一事实源）
from .rogue_registry import TABLE_THEME_ORDER

RARITY_NAMES = {
    "NORMAL": "普通",
    "RARE": "稀有",
    "SUPER_RARE": "珍贵",
}

# 藏品图标：tomimi.dev 开源仓库静态资源（与表内 iconId 一一对应）。
# 经 jsdelivr CDN 镜像取（image/webp）。不走核心 pic_download_from_url：
# 其内部 httpx 客户端默认 5s 超时且不跟随重定向，实测此源必超时；
# 这里自带 20s + follow_redirects 的客户端，失败落 .miss 负缓存（1h）。
# 注意钉在活分支 @main：上游已删除 master 分支（2026-09 实测），@main 之外
# 的引用是 jsdelivr 缓存的死快照，最新期（黑流树海）图标上传后永远取不到。
_TOMIMI_STATIC_BASE = (
    "https://cdn.jsdelivr.net/gh/tanch-p/tomimi-dev@main/static/images/relics"
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
    """藏品索引（精简缓存优先，TTL 30 天——肉鸽数据只随版本更新）。

    该检查先于 18MB 主题表 ensure：30 天内直接读精简 sidecar，
    不触发全量表下载。失败返回 {}。
    """
    if RELIC_INDEX_PATH.exists() and await _cache_fresh(
        ROGUE_TOPIC_TABLE_PATH, 30 * 86400
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


def match_relics(query: str, index: dict[str, list[dict]], limit: int = 8) -> list[str]:
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
    iconId 来自外部表：过 safe_cache_stem 清洗后再拼 URL/路径，防数据源
    污染时的路径逃逸（常规 ascii id 文件名不变，缓存兼容）。
    """
    from .path_safety import safe_cache_stem

    icon_id = (icon_id or "").strip()
    if not icon_id:
        return ""
    stem = safe_cache_stem(icon_id)
    cache_dir = Path(RELIC_ICON_CACHE_DIR)
    filename = f"{stem}.webp"
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
            # quiet_404：最新期（黑流树海等）图标上游渐进上传，404 是预期
            # 缺口（debug 级），网络故障仍 warning；均落 .miss 负缓存自愈
            _status, raw = await download_bytes_status(
                url, timeout=20.0, quiet_404=True
            )
            if raw is None:
                raise ValueError(f"download failed: {filename}")
            local_path.write_bytes(raw)
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
