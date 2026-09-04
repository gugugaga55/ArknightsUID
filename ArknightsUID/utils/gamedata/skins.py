"""皮肤收集目录（ark皮肤）：skin_table × player_info.skins 的持有对比。

数据结构一手证据：
- ArknightsGameData skin_table.json 的 charSkins：{skinId → {charId, displaySkin}}
  - 时装类皮肤 ID 带 "@"（char_002_amiya@winter#1）；基础形态（charId#1/#2）
    人手一份，不进收集统计
  - displaySkin.skinName = 皮肤名（"报童"）、skinGroupName = 系列名
    （"忒斯特收藏/I"）；513 个 @ 皮肤全部有 displaySkin（2026-08 实测）
- Skland player_info 的 data.skins：[{id, ts}]（已收集皮肤 ID 列表，
  dump 实测 22 条，与 status.skinCnt 口径不同——后者样本为 0，不可依赖）

目录口径：@ 皮肤中 charId 命中干员索引（可获得干员）的全体；
个别不可购买的内部/特殊皮肤可能与玩家实际可收集集略有出入，卡片文案
已注明目录口径。
"""

from __future__ import annotations

from typing import Any

from ._table_cache import (
    CHAR_TABLE_PATH,
    CHAR_TABLE_TTL,
    GAMEDATA_CACHE_DIR,
    SKIN_TABLE_PATH,
    _cache_fresh,
    _read_sidecar,
    _write_sidecar,
    ensure_skin_table,
)

SKININDEX_PATH = GAMEDATA_CACHE_DIR / "skinindex.json"


def build_skin_index(
    skin_table: dict[str, Any], char_index: dict[str, dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """从 skin_table 提取时装目录精简索引：skinId → {name, group, char_id}。

    只收带 "@" 的时装皮肤，且 charId 必须在干员索引（可获得干员）中。
    """
    index: dict[str, dict[str, str]] = {}
    char_skins = skin_table.get("charSkins")
    if not isinstance(char_skins, dict):
        return index
    for skin_id, skin in char_skins.items():
        if not isinstance(skin, dict) or "@" not in str(skin_id):
            continue
        char_id = str(skin.get("charId") or "")
        if not char_id or char_id not in char_index:
            continue
        display = skin.get("displaySkin")
        if not isinstance(display, dict):
            continue
        index[str(skin_id)] = {
            "name": str(display.get("skinName") or "") or str(skin_id),
            "group": str(display.get("skinGroupName") or ""),
            "char_id": char_id,
        }
    return index


async def load_skin_index() -> dict[str, dict[str, str]]:
    """加载时装目录（精简缓存优先，TTL 同 skin_table 7 天）。失败返回 {}。"""
    from .charinfo import _index_covers_sources

    if (
        SKININDEX_PATH.exists()
        and await _cache_fresh(CHAR_TABLE_PATH, CHAR_TABLE_TTL)
        and await _cache_fresh(SKIN_TABLE_PATH, CHAR_TABLE_TTL)
        and (
            # 索引由 char_table + skin_table 共同派生（审计 #24）：任一源表
            # 更新都应触发重建——此前漏查 skin_table，新皮肤最长滞后 7 天；
            # mtime 比对再兜住"源表被旁路刷新"的路径（审计 #39）
            _index_covers_sources(SKININDEX_PATH, CHAR_TABLE_PATH, SKIN_TABLE_PATH)
        )
    ):
        cached = _read_sidecar(SKININDEX_PATH)
        if isinstance(cached, dict) and cached:
            return cached

    skin_table = await ensure_skin_table()
    if skin_table is None:
        return {}
    from .charinfo import load_char_index

    char_index = await load_char_index()
    if not char_index:
        return {}
    index = build_skin_index(skin_table, char_index)
    if index:
        _write_sidecar(SKININDEX_PATH, index)
    return index


def summarize_collection(
    catalog: dict[str, dict[str, str]],
    owned_ids: list[str],
    char_names: dict[str, str],
    max_rows: int = 12,
) -> dict[str, Any]:
    """目录 × 持有 → 收集进度与未收集清单（纯函数，供卡片与测试消费）。

    char_names：charId → 干员名（干员索引），缺失回退皮肤条目里的 char_id。
    """
    owned = {str(sid) for sid in owned_ids if sid}
    owned_hit = [sid for sid in catalog if sid in owned]
    missing: list[dict[str, str]] = []
    for skin_id, info in catalog.items():
        if skin_id in owned:
            continue
        missing.append(
            {
                "skin_id": skin_id,
                "name": info["name"],
                "group": info["group"],
                "char": char_names.get(info["char_id"], info["char_id"]),
            }
        )
    missing.sort(key=lambda row: (row["group"], row["char"], row["name"]))
    total = len(catalog)
    owned_count = len(owned_hit)
    return {
        "total": total,
        "owned": owned_count,
        "missing_count": len(missing),
        # 玩家持有但不在目录（如目录过滤掉的内部皮肤）——仅计数，不上卡
        "extra_owned": len(owned) - owned_count,
        "pct": round(owned_count * 100 / total, 1) if total else 0.0,
        "missing": missing[:max_rows],
    }
