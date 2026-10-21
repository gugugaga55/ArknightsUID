"""干员别名解析系统（P2）。

数据源：ArknightsGameData character_table.json（与公招模块共享缓存），
构建 charId → {name, appellation, rarity, profession} 精简索引。

解析优先级（宁缺毋滥，避免错误映射）：
1. 中文名精确匹配（"银灰" → 银灰）
2. 英文代号 appellation 忽略大小写精确匹配（"silverash"/"EXAS" → 银灰）
3. 唯一前缀匹配（"维什" → 维什戴尔；歧义时返回全部候选由调用方提示）
4. 内建高置信度社区别名（仅收录无歧义项，见 CURATED_ALIASES）

character_table 的 rarity 是 TIER_x（=x 星），与 player_info charInfoMap 的
0-5 编号不同，本模块统一输出 1-6 人读值。
"""

from __future__ import annotations

from ..disk_cache import read_json, write_json
from ..disk_cache import index_covers_sources as _index_covers_sources

from typing import Any, NamedTuple

from gsuid_core.logger import logger

from ._table_cache import (
    CHAR_TABLE_PATH,
    CHAR_TABLE_TTL,
    GAMEDATA_CACHE_DIR,
    ITEM_TABLE_PATH,
    _cache_fresh,
    ensure_char_table,
)

CHARINDEX_PATH = GAMEDATA_CACHE_DIR / "charindex.json"
ITEMINDEX_PATH = GAMEDATA_CACHE_DIR / "itemindex.json"


# 内建社区别名（别名 → 中文名）。只收录广泛公认且无歧义的。
CURATED_ALIASES: dict[str, str] = {
    "42": "史尔特尔",
    "陈sir": "陈",
    "近卫阿米娅": "阿米娅",  # 三形态由 profession 区分，名称统一指向阿米娅
    "医疗阿米娅": "阿米娅",
}

# 阿米娅三形态直查（社区习惯叫法 → charId，客观角色事实；练度卡/养成计算
# 需要具体形态的 charId，名称级别名见上方 CURATED_ALIASES）。
AMIYA_FORM_IDS: dict[str, str] = {
    "近卫阿米娅": "char_1001_amiya2",
    "阿米娅近卫": "char_1001_amiya2",
    "医疗阿米娅": "char_1037_amiya3",
    "阿米娅医疗": "char_1037_amiya3",
}

_TIER_MAP = {f"TIER_{i}": i for i in range(1, 8)}


def _is_obtainable(char: dict[str, Any]) -> bool:
    """可获得的干员（含异格与 6 星，比公招口径宽；排除装置/召唤物）。

    isSpChar 是「异格」标记而非不可得标记——维什戴尔、浊心斯卡蒂等异格
    干员均为招募寻访获得，必须进索引；真正要排除的是 TRAP/TOKEN 职业的
    装置与召唤物（其 isNotObtainable 为空，仅靠职业识别）。
    """
    if char.get("isNotObtainable"):
        return False
    if char.get("profession") in ("TRAP", "TOKEN"):
        return False
    return bool(char.get("name")) and char.get("rarity") in _TIER_MAP


def build_char_index(char_table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从 character_table 提取干员精简索引（~100KB）。"""
    index: dict[str, dict[str, Any]] = {}
    for char_id, char in char_table.items():
        if not isinstance(char, dict) or not _is_obtainable(char):
            continue
        index[char_id] = {
            "name": char.get("name", ""),
            "appellation": char.get("appellation", ""),
            "rarity": _TIER_MAP.get(char.get("rarity", ""), 0),
            "profession": char.get("profession", ""),
        }
    return index


async def load_char_index() -> dict[str, dict[str, Any]]:
    """加载干员索引（精简缓存优先，TTL 同 character_table 7 天）。失败返回 {}。"""
    if (
        CHARINDEX_PATH.exists()
        and await _cache_fresh(CHAR_TABLE_PATH, CHAR_TABLE_TTL)
        and _index_covers_sources(CHARINDEX_PATH, CHAR_TABLE_PATH)
    ):
        cached = await read_json(CHARINDEX_PATH)
        if isinstance(cached, dict) and cached:
            return cached
    char_table = await ensure_char_table()
    if char_table is None:
        cached = await read_json(CHARINDEX_PATH)
        return cached if isinstance(cached, dict) else {}
    index = build_char_index(char_table)
    if index:
        await write_json(CHARINDEX_PATH, index)
    return index


async def load_skill_names() -> dict[str, str]:
    """skillId → 技能名（单干员卡用）。失败返回 {}。"""
    from ._table_cache import ensure_skill_table

    skill_table = await ensure_skill_table()
    if not isinstance(skill_table, dict):
        return {}
    names: dict[str, str] = {}
    for skill_id, skill in skill_table.items():
        if not isinstance(skill, dict):
            continue
        # skill_table 的名称在 levels[0].name（顶层无 name 字段）
        name = skill.get("name")
        if not name:
            levels = skill.get("levels") or []
            if levels and isinstance(levels[0], dict):
                name = levels[0].get("name")
        if name:
            names[skill_id] = str(name)
    return names


def build_item_index(item_table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从 item_table 提取 itemId → {name, rarity, sort} 精简索引（养成计算用）。"""
    items = item_table.get("items")
    index: dict[str, dict[str, Any]] = {}
    if isinstance(items, dict):
        iterator = ((str(k), v) for k, v in items.items())
    elif isinstance(items, list):
        iterator = ((str(v.get("id", "")), v) for v in items if isinstance(v, dict))
    else:
        return index
    for item_id, item in iterator:
        if not item_id or not isinstance(item, dict) or not item.get("name"):
            continue
        try:
            sort = int(item.get("sortId", 0) or 0)
        except (TypeError, ValueError):
            sort = 0
        index[item_id] = {
            "name": str(item.get("name", "")),
            "rarity": item.get("rarity", ""),
            "sort": sort,
        }
    return index


async def load_item_index() -> dict[str, dict[str, Any]]:
    """加载物品索引（精简缓存优先，TTL 同 character_table）。失败返回 {}。"""
    if (
        ITEMINDEX_PATH.exists()
        and await _cache_fresh(ITEM_TABLE_PATH, CHAR_TABLE_TTL)
        and _index_covers_sources(ITEMINDEX_PATH, ITEM_TABLE_PATH)
    ):
        cached = await read_json(ITEMINDEX_PATH)
        if isinstance(cached, dict) and cached:
            return cached
    from ._table_cache import ensure_item_table

    item_table = await ensure_item_table()
    if not isinstance(item_table, dict):
        cached = await read_json(ITEMINDEX_PATH)
        return cached if isinstance(cached, dict) else {}
    index = build_item_index(item_table)
    if index:
        await write_json(ITEMINDEX_PATH, index)
    return index


def match_entries(query: str, index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """按优先级匹配，返回候选列表（含 char_id 键，0 个即无匹配）。

    精确名/别名/appellation 命中只返回唯一项；前缀命中可能多项（≤8）。
    """
    q = (query or "").strip()
    if not q:
        return []
    lowered = q.lower()

    def entry(char_id: str) -> dict[str, Any]:
        item = dict(index[char_id])
        item["char_id"] = char_id
        return item

    # 1. 精确中文名
    for char_id, item in index.items():
        if item.get("name") == q:
            return [entry(char_id)]

    # 2. appellation 忽略大小写精确
    for char_id, item in index.items():
        if str(item.get("appellation", "")).lower() == lowered:
            return [entry(char_id)]

    # 3. 内建别名（指向中文名后再精确匹配一次）
    curated = CURATED_ALIASES.get(q) or CURATED_ALIASES.get(lowered)
    if curated:
        for char_id, item in index.items():
            if item.get("name") == curated:
                return [entry(char_id)]

    # 4. 唯一前缀（中文名或 appellation 前缀）
    prefix_hits = [
        char_id
        for char_id, item in index.items()
        if str(item.get("name", "")).startswith(q)
        or str(item.get("appellation", "")).lower().startswith(lowered)
    ]
    if len(prefix_hits) == 1:
        return [entry(prefix_hits[0])]
    if len(prefix_hits) > 1:
        prefix_hits.sort(
            key=lambda cid: (-index[cid].get("rarity", 0), index[cid].get("name", ""))
        )
        return [entry(cid) for cid in prefix_hits[:8]]
    return []


def resolve_char(
    query: str, index: dict[str, dict[str, Any]]
) -> tuple[str, str] | None:
    """便捷封装：唯一命中返回 (char_id, name)，无/多命中返回 None。"""
    matched = match_entries(query, index)
    if len(matched) == 1:
        first = matched[0]
        return str(first["char_id"]), str(first.get("name", ""))
    return None


class QueryResolution(NamedTuple):
    """resolve_query 的结构化结果；错误文案由调用方按场景组织。"""

    char_id: str
    name: str
    candidates: list[dict[str, Any]]
    index_ready: bool

    @property
    def unique(self) -> bool:
        """是否唯一命中（char_id 非空，此时 candidates 必空）。"""
        return bool(self.char_id)

    def ambiguous_message(self, query: str) -> str:
        """多名干员候选的统一提示（原先在 cultivate/account 逐字重复）。"""
        names = "、".join(str(m.get("name", "")) for m in self.candidates[:8])
        return f"「{query}」匹配到多名干员：{names}，请写全名。"


async def resolve_query(
    query: str,
    *,
    bot_id: str = "",
    user_id: str = "",
    amiya_forms: bool = True,
) -> QueryResolution:
    """查询词 → 干员解析的统一组合管线（收编 cultivate/wiki/account 三处实现）。

    管线：用户自定义别名（传 bot_id+user_id 时启用）→ 阿米娅形态直查
    （amiya_forms=False 跳过，如 wiki 词条搜索需名称级结果）→ gamedata
    索引匹配。结果语义：

    - 唯一命中：char_id/name 非空（阿米娅形态捷径时 name 为空串）
    - 多名候选：candidates 非空（≤8，用 ambiguous_message 生成提示）
    - 无命中：candidates 空；index_ready=False 表示索引未就绪（首次
      使用需下载全量表），True 表示索引可用但确无此干员
    """
    query = (query or "").strip()
    if bot_id and user_id:
        try:
            from ..user_alias import apply_user_alias

            query = await apply_user_alias(bot_id, user_id, query)
        except Exception as exc:
            logger.debug(
                f"[ArknightsUID·干员解析] 用户别名应用失败: {exc.__class__.__name__}"
            )

    if amiya_forms:
        amiya_id = AMIYA_FORM_IDS.get(query)
        if amiya_id:
            return QueryResolution(amiya_id, "", [], True)

    index = await load_char_index()
    if not index:
        return QueryResolution("", "", [], False)
    matched = match_entries(query, index)
    if len(matched) == 1:
        first = matched[0]
        return QueryResolution(
            str(first["char_id"]), str(first.get("name", "")), [], True
        )
    if matched:
        return QueryResolution("", "", matched, True)
    return QueryResolution("", "", [], True)
