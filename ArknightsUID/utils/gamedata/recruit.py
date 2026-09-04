"""公招查询：标签组合 → 可公招干员列表（数据 PRTS 公开招募 SMW）。

游戏机制口径（游戏内 / PRTS 公开招募页实证）：
  - 「资深干员」标签池 = 5★；「高级资深干员」标签池 = 6★
  - 勾选稀有度标签并以 9:00:00 招募 → 标签不被划去，必得对应星级档

数据源：PRTS SMW「分类:干员 × 获得方式::公开招募」（PRTS 公招计算器同源，
含 6★ 池 35 人；per-operator 稀有度/职业/位置/能力标签）。
character_table 的 itemObtainApproach=招募寻访 区分不了公招池与寻访限定，
且稀有度标签曾被错挂成 4★+/5★+ 阈值——已弃用该推导路径。

rarity 口径：PRTS SMW 稀有度存 0-5，展示 +1（0=1★ … 5=6★）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ._table_cache import GAMEDATA_CACHE_DIR, _read_sidecar, _write_sidecar

RECRUIT_DATA_PATH = GAMEDATA_CACHE_DIR / "recruit.json"

# 稀有度标签（游戏内语义）：5★ → 资深干员；6★ → 高级资深干员
SENIOR_TAG = "资深干员"
SENIOR_PLUS_TAG = "高级资深干员"

# 精简缓存 TTL（公招池随版本缓慢变化，7 天刷新足够）
_RECRUIT_TTL_SECONDS = 7 * 24 * 3600

# 缓存格式版本：v1 = character_table 推导（稀有度标签错档），v2 = PRTS SMW。
# 旧格式缓存一律视为失效，强制重建
_CACHE_VERSION = 2

_POOL_QUERY = "[[分类:干员]][[获得方式::公开招募]]|?稀有度|?职业|?位置|?标签|limit=500"


def _first(values: Any) -> str:
    if isinstance(values, list) and values:
        return str(values[0] or "")
    return ""


def entries_from_smw(results: dict[str, Any]) -> list[dict[str, Any]]:
    """PRTS SMW 公开招募查询结果 → 公招干员精简列表（纯函数，可测）。

    all_tags = 职业 + 位置 + 稀有度标签（资深=5★ / 高级资深=6★）+ 能力标签；
    能力标签的 SMW printout 是空格分隔的长串（"支援机械 控场"），逐段拆开。
    """
    entries: list[dict[str, Any]] = []
    for name, info in results.items():
        if not isinstance(info, dict):
            continue
        pr = info.get("printouts") or {}
        try:
            rarity = int(_first(pr.get("稀有度"))) + 1
        except ValueError:
            continue
        if not 1 <= rarity <= 6:
            continue
        profession = _first(pr.get("职业"))
        position = _first(pr.get("位置"))
        tags: list[str] = []
        for chunk in pr.get("标签") or []:
            tags.extend(str(chunk).split())
        tags = list(dict.fromkeys(t for t in tags if t))
        all_tags = [t for t in (profession, position) if t]
        if rarity == 5:
            all_tags.append(SENIOR_TAG)
        elif rarity == 6:
            all_tags.append(SENIOR_PLUS_TAG)
        all_tags.extend(tags)
        entries.append(
            {
                "id": "",
                "name": str(name),
                "rarity": rarity,
                "profession": profession,
                "position": position,
                "tags": tags,
                "all_tags": all_tags,
            }
        )
    return entries


def _sidecar_fresh(path: Path, ttl: float) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) < ttl
    except OSError:
        return False


async def _fetch_pool() -> list[dict[str, Any]]:
    """拉取 PRTS 公开招募池；失败返回空列表（调用方走缓存兜底）。"""
    from ..prts.client import _smw_ask

    data = await _smw_ask(_POOL_QUERY)
    # v3 响应的 results 可能是 list（异常/改版）——prts client 同模式
    # 已加守卫（审计 #26g），此处漏改会让 .items() 直接崩掉
    # ark公招/公招推荐 整条命令（审计 #39）
    payload = data if isinstance(data, dict) else {}
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    results = query.get("results")
    results = results if isinstance(results, dict) else {}
    return entries_from_smw(results)


async def load_recruit_data() -> list[dict[str, Any]]:
    """加载公招干员数据。新鲜精简缓存优先；过期则拉 PRTS 重建，
    拉取失败用过期缓存兜底；都没有则返回空列表。
    """
    cached = _read_cache()
    if cached and _sidecar_fresh(RECRUIT_DATA_PATH, _RECRUIT_TTL_SECONDS):
        return cached
    entries = await _fetch_pool()
    if entries:
        # 补 char_id（头像用）：干员名 → charId（索引加载失败静默留空）
        try:
            from .charinfo import load_char_index

            index = await load_char_index()
            name2id = {str(v.get("name") or ""): cid for cid, v in index.items()}
            for entry in entries:
                entry["id"] = name2id.get(entry["name"], "")
        except Exception:
            pass
        _write_sidecar(RECRUIT_DATA_PATH, {"v": _CACHE_VERSION, "entries": entries})
        return entries
    return cached or []


def _read_cache() -> list[dict[str, Any]]:
    """读精简缓存（仅接受当前版本格式；v1 旧档视为无缓存）。"""
    payload = _read_sidecar(RECRUIT_DATA_PATH)
    if isinstance(payload, dict) and payload.get("v") == _CACHE_VERSION:
        entries = payload.get("entries")
        if isinstance(entries, list):
            return entries
    return []


def _prepare_entries(
    data: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], frozenset[str], int]]:
    """预计算 (词条, 标签frozenset, 稀有度)：组合枚举时不再逐词条重建 set。"""
    prepared: list[tuple[dict[str, Any], frozenset[str], int]] = []
    for entry in data:
        try:
            rarity = int(entry.get("rarity") or 0)
        except (TypeError, ValueError):
            rarity = 0
        prepared.append((entry, frozenset(entry.get("all_tags") or ()), rarity))
    return prepared


def _match_prepared(
    qset: frozenset[str] | set[str],
    prepared: list[tuple[dict[str, Any], frozenset[str], int]],
    senior_plus_selected: bool,
) -> list[dict[str, Any]]:
    """match_recruit 的预计算内核（同一语义，免去 per-combo 的 set 重建）。"""
    matched = [
        entry
        for entry, entry_tags, rarity in prepared
        if qset <= entry_tags and (rarity < 6 or senior_plus_selected)
    ]
    matched.sort(key=lambda e: (-e["rarity"], e["name"]))
    return matched


def match_recruit(
    query_tags: list[str], data: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """标签 → all_tags ⊇ query_tags 的可公招干员（按稀有度降序、名字升序）。

    游戏机制：6★ 只在勾选「高级资深干员」后入池——未选高资的组合必须
    排除 6★（否则单查「输出」这类组合会把 6★ 错误列进池里）；5★ 在
    常规池内，不要求资深标签（资深只是保底手段）。
    """
    qset = {t.strip() for t in query_tags if t.strip()}
    if not qset:
        return []
    return _match_prepared(qset, _prepare_entries(data), SENIOR_PLUS_TAG in qset)


# ==================== 公招组合推荐（ark公招推荐） ====================

# 游戏机制（客观事实，PRTS 公开招募页实证）：
# - 单次公招最多同时勾选 3 个标签
# - 勾选资深/高级资深并以 9:00:00 招募：标签不被划去，必得 5★/6★ 档
MAX_SELECT_TAGS = 3

# 文本输入的已知标签数上限：游戏界面单次只刷 5 个标签，文本口放宽到 8。
# 不设上限时 C(20,3)=1140 个组合 × ~300 词条的逐词条集合重建会同步占用
# 事件循环数百毫秒；截断只影响 >8 标签的极端输入（取前 8 个）。
MAX_INPUT_TAGS = 8


def build_tag_universe(data: list[dict[str, Any]]) -> set[str]:
    """全部合法公招标签（含职业/位置/稀有度标签）。"""
    universe: set[str] = set()
    for entry in data:
        universe.update(entry.get("all_tags", []))
    return universe


def _combo_tier(
    matches: list[dict[str, Any]], combo: tuple[str, ...]
) -> tuple[int, str]:
    """组合价值分层：返回 (tier, 标签文案)。tier 越小越优。

    - 1：6★ 保底（池内最低 6★——6★ 只在组合含高级资深时入池
          （match_recruit 的排除规则），故池含 6★ 当且仅当组合含高资）
    - 2：5★ 保底（池内最低 5★）
    - 3：4★ 保底（池内最低 4★）
    - 4：常规（池内含 3★ 及以下）
    """
    rarities = [int(e.get("rarity") or 0) for e in matches]
    min_rarity = min(rarities) if rarities else 0
    if min_rarity >= 6:
        return 1, "6★ 保底"
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
    - 已知标签超过 MAX_INPUT_TAGS 时截断取前 N（防文本口的组合枚举爆炸）
    """
    from itertools import combinations

    prepared = _prepare_entries(data)
    universe: set[str] = set()
    for _entry, entry_tags, _rarity in prepared:
        universe.update(entry_tags)
    known: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        tag = str(tag).strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        (known if tag in universe else unknown).append(tag)

    if len(known) > MAX_INPUT_TAGS:
        known = known[:MAX_INPUT_TAGS]

    combos: list[dict[str, Any]] = []
    for size in range(1, min(max_select, len(known)) + 1):
        for combo in combinations(known, size):
            matches = _match_prepared(
                frozenset(combo), prepared, SENIOR_PLUS_TAG in combo
            )
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
