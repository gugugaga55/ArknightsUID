"""公招查询：标签组合 → 可公招干员列表。

数据来自 ArknightsGameData 的 character_table.json（鹰角游戏数据 dump）。
character_table 含所有角色（含 NPC/敌人），需过滤出公招池干员：
  - isNotObtainable/isSpChar 排除不可获得/特殊
  - itemObtainApproach 排除 NPC（空）与非常规渠道（活动/集成战略/信用/凭证等）
  - rarity 限定 1-5 星。6★（TIER_6）刻意不入名单：公招 6★ 只能经
    「高级资深干员」标签追加，而具体 6★ 池是服务端名单——静态表无可信
    来源（character_table 无限定标志、gacha_table 无池名单、PRTS 高资栏
    停更于 2021），宁缺毋滥不臆造名单，展示口径见 recommend_combos。

稀有度标签按阈值挂（游戏机制，PRTS 公开招募页实证）：
  - 4★+ → 「资深干员」；5★+ → 「高级资深干员」（即 5★ 两个标签都匹配）
  - 勾选稀有度标签并以 9:00:00 招募 → 标签不被划去，必得 5★/6★ 档

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
# 公招额外稀有度标签（阈值语义）：4★+ 匹配「资深干员」，5★+ 追加「高级资深干员」
_RARITY_TAG = {4: "资深干员", 5: "高级资深干员"}  # 阈值语义（见模块头注释）
SENIOR_TAG = "资深干员"
SENIOR_PLUS_TAG = "高级资深干员"
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
    # 完整公招标签 = 职业 + 位置 + (资深/高级资深，阈值挂载) + 能力标签
    all_tags = [t for t in (profession, position) if t]
    if rarity >= 4:
        all_tags.append(SENIOR_TAG)
    if rarity >= 5:
        all_tags.append(SENIOR_PLUS_TAG)
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


# ==================== 公招组合推荐（ark公招推荐） ====================

# 游戏机制（客观事实，PRTS 公开招募页实证）：
# - 单次公招最多同时勾选 3 个标签
# - 6★ 仅经「高级资深干员」标签追加出现概率（服务端池名单，静态表不可得，
#   本工具不列 6★ 具体名单——高资组合的价值用 tier 与文案表达）
# - 勾选资深/高级资深并以 9:00:00 招募：标签不被划去，必得 5★/6★ 档
MAX_SELECT_TAGS = 3


def build_tag_universe(data: list[dict[str, Any]]) -> set[str]:
    """全部合法公招标签（含职业/位置/稀有度标签）。"""
    universe: set[str] = set()
    for entry in data:
        universe.update(entry.get("all_tags", []))
    return universe


def _combo_tier(matches: list[dict[str, Any]], combo: tuple[str, ...]) -> tuple[int, str]:
    """组合价值分层：返回 (tier, 标签文案)。tier 越小越优。

    - 1：6★ 机会（含高级资深干员且池非空——公招唯一出 6★ 的途径，
          9 小时招募必得 5★/6★ 档；6★ 具体名单服务端维护，本表不列）
    - 2：5★ 保底（池内最低 5★）
    - 3：4★ 保底（池内最低 4★，含资深干员组合）
    - 4：常规（池内含 3★）
    """
    rarities = [int(e.get("rarity") or 0) for e in matches]
    min_rarity = min(rarities) if rarities else 0
    if SENIOR_PLUS_TAG in combo and matches:
        return 1, "6★ 机会"
    if min_rarity >= 5:
        return 2, "5★ 保底"
    if min_rarity >= 4:
        return 3, "4★ 保底"
    return 4, "常规"


def recommend_combos(
    tags: list[str],
    data: list[dict[str, Any]],
    max_select: int = MAX_SELECT_TAGS,
    top: int = 6,
) -> dict[str, Any]:
    """给定界面标签（通常 5 个）→ 1-3 标签组合的推荐排行。

    返回 {combos: [...], unknown: [...]}：
    - combos：[{tags, tier, tier_name, min_rarity, max_rarity, matches,
                match_names}]，按（tier 升序，max_rarity 降序，池更集中）排序
    - 空匹配组合剔除（公招不允许空集）；unknown：输入里不在标签全集的词
    """
    from itertools import combinations

    universe = build_tag_universe(data)
    known: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        tag = str(tag).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        (known if tag in universe else unknown).append(tag)

    combos: list[dict[str, Any]] = []
    for size in range(1, min(max_select, len(known)) + 1):
        for combo in combinations(known, size):
            matches = match_recruit(list(combo), data)
            if not matches:
                continue
            rarities = [int(e.get("rarity") or 0) for e in matches]
            tier, tier_name = _combo_tier(matches, combo)
            combos.append(
                {
                    "tags": list(combo),
                    "tier": tier,
                    "tier_name": tier_name,
                    "min_rarity": min(rarities),
                    "max_rarity": max(rarities),
                    "matches": matches,
                    "match_names": [e["name"] for e in matches],
                }
            )
    # 价值排序：分层 → 池内最高星 → 池更集中（同档下人少=出高星更稳）
    combos.sort(key=lambda c: (c["tier"], -c["max_rarity"], len(c["matches"])))
    return {"combos": combos[:top], "unknown": unknown}
