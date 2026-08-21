"""公招查询：标签组合 → 可公招干员列表。

数据来自 ArknightsGameData 的 character_table.json（鹰角游戏数据 dump）。
character_table 含所有角色（含 NPC/敌人），需过滤出公招池干员：
  - isNotObtainable/isSpChar 排除不可获得/特殊
  - itemObtainApproach 排除 NPC（空）与非常规渠道（活动/集成战略/信用/凭证等）
  - rarity 限定 1-5 星（6 星 TIER_6 不在公招池）

rarity 映射：character_table 的 TIER_x = x 星（银灰 TIER_6=6星，芬 TIER_3=3星），
与 gxy12345 character_info.json 的 0-5 编号不同，切勿混用。
"""
from __future__ import annotations

from typing import Any

from ._table_cache import (
    CHAR_TABLE_PATH,
    CHAR_TABLE_TTL,
    GAMEDATA_CACHE_DIR,
    _cache_fresh,
    _read_sidecar,
    _write_sidecar,
    ensure_char_table,
)

RECRUIT_DATA_PATH = GAMEDATA_CACHE_DIR / "recruit.json"

# character_table rarity: TIER_x = x 星
_TIER_MAP = {f"TIER_{i}": i for i in range(1, 7)}
_PROFESSION_MAP = {
    "PIONEER": "先锋", "WARRIOR": "近卫", "SNIPER": "狙击", "TANK": "重装",
    "MEDIC": "医疗", "SUPPORT": "辅助", "CASTER": "术师", "SPECIAL": "特种",
}
_POSITION_MAP = {"MELEE": "近战位", "RANGED": "远程位"}
# 公招额外稀有度标签：4 星→资深干员，5 星→高级资深干员
_RARITY_TAG = {4: "资深干员", 5: "高级资深干员"}
# 非常规获取渠道（活动/集成战略/信用/凭证/剧情等），不在公招池
_NON_REGULAR_APPROACH = {
    "活动获得", "集成战略获得", "信用交易所", "凭证交易所",
    "限时礼包", "周年奖励", "主题曲剧情",
}
# 公招池稀有度（6 星 TIER_6 不在公招）
_RECRUIT_RARITIES = {"TIER_1", "TIER_2", "TIER_3", "TIER_4", "TIER_5"}


def _is_recruitable(char: dict[str, Any]) -> bool:
    if char.get("isNotObtainable") or char.get("isSpChar"):
        return False
    approach = char.get("itemObtainApproach") or ""
    if not approach or approach in _NON_REGULAR_APPROACH:
        return False
    return char.get("rarity") in _RECRUIT_RARITIES


def _extract_entry(char_id: str, char: dict[str, Any]) -> dict[str, Any] | None:
    if not _is_recruitable(char):
        return None
    rarity = _TIER_MAP.get(char.get("rarity", ""), 0)
    if rarity == 0:
        return None
    profession = _PROFESSION_MAP.get(char.get("profession", ""), "")
    position = _POSITION_MAP.get(char.get("position", ""), "")
    tags = list(char.get("tagList") or [])
    # 完整公招标签 = 职业 + 位置 + (资深/高级资深) + 能力标签
    all_tags = [t for t in (profession, position) if t]
    if rarity in _RARITY_TAG:
        all_tags.append(_RARITY_TAG[rarity])
    all_tags.extend(tags)
    return {
        "id": char_id,
        "name": char.get("name", ""),
        "rarity": rarity,
        "profession": profession,
        "position": position,
        "tags": tags,
        "all_tags": all_tags,
    }


def build_recruit_data(char_table: dict[str, Any]) -> list[dict[str, Any]]:
    """从 character_table 提取公招干员精简列表（~45KB）。"""
    entries: list[dict[str, Any]] = []
    for char_id, char in char_table.items():
        entry = _extract_entry(char_id, char)
        if entry:
            entries.append(entry)
    return entries


async def load_recruit_data() -> list[dict[str, Any]]:
    """加载公招干员数据。精简缓存优先；全量过期或缺失则重新拉取。

    拉取失败时用过期精简缓存兜底；都没有则返回空列表。
    """
    if RECRUIT_DATA_PATH.exists() and await _cache_fresh(CHAR_TABLE_PATH, CHAR_TABLE_TTL):
        cached = _read_sidecar(RECRUIT_DATA_PATH)
        if isinstance(cached, list):
            return cached
    char_table = await ensure_char_table()
    if char_table is None:
        cached = _read_sidecar(RECRUIT_DATA_PATH)
        return cached if isinstance(cached, list) else []
    recruit = build_recruit_data(char_table)
    _write_sidecar(RECRUIT_DATA_PATH, recruit)
    return recruit


def match_recruit(
    query_tags: list[str], data: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """标签 → all_tags ⊇ query_tags 的可公招干员（按稀有度降序、名字升序）。"""
    qset = {t.strip() for t in query_tags if t.strip()}
    if not qset:
        return []
    matched = [e for e in data if qset.issubset(set(e.get("all_tags", [])))]
    matched.sort(key=lambda e: (-e["rarity"], e["name"]))
    return matched
