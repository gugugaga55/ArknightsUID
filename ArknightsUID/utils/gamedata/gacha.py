"""寻访卡池元数据（gacha_table.json）：池身份、开池时间窗与 UP 干员。

数据源：Kengxxiao/ArknightsGameData zh_CN gamedata/excel/gacha_table.json
（gachaPoolClient 数组）。职责刻意收窄：

- 读取卡池 gachaPoolId / gachaPoolName / openTime / endTime / gachaRuleType
- 从 detailInfo.upCharInfo（缺失时 availCharInfo）提取**能确定**的 UP 干员名
- 按「池名 + 抽取时间」为小黑盒/旧记录匹配 poolId

UP 数据缺失时返回空集合——不根据干员稀有度或池名猜测 UP（歪没歪
只认官方名单，猜测会误导垫刀解读）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._table_cache import ensure_gacha_table


@dataclass(slots=True)
class GachaPoolInfo:
    pool_id: str
    pool_name: str
    rule_type: str = ""
    open_time: int = 0
    end_time: int = 0
    up_chars: set[str] = field(default_factory=set)
    up_char_ids: set[str] = field(default_factory=set)


def _up_chars_from(
    detail: Any, id_to_name: dict[str, str] | None = None
) -> tuple[set[str], set[str]]:
    """detailInfo.upCharInfo → (UP 干员名集合, UP charId 集合)。

    兼容两种结构：
    1. 服务端/weedy 常见结构：{"perCharList": [{"rarityRank": 5, "charIdList": ["char_..."], ...}]}
    2. 具名列表结构：{"rarity6": [{item: {name, charId, ...}}, ...], ...}
    拿不到任何明确 UP 信息即返回空集合（宁缺毋滥，不猜 UP）。
    """
    names: set[str] = set()
    char_ids: set[str] = set()
    if not isinstance(detail, dict):
        return names, char_ids

    per_char_list = detail.get("perCharList")
    if isinstance(per_char_list, list):
        for group in per_char_list:
            if not isinstance(group, dict):
                continue
            for cid in group.get("charIdList") or []:
                cid_str = str(cid or "").strip()
                if cid_str:
                    char_ids.add(cid_str)
                    if id_to_name and cid_str in id_to_name:
                        names.add(id_to_name[cid_str])

    for key, entries in detail.items():
        if not key.startswith("rarity") or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item")
            name = ""
            cid_str = ""
            if isinstance(item, dict):
                name = str(item.get("name") or "")
                cid_str = str(item.get("charId") or item.get("id") or "")
            else:
                if entry.get("name"):
                    name = str(entry.get("name"))
                if entry.get("charId"):
                    cid_str = str(entry.get("charId"))
            if cid_str:
                char_ids.add(cid_str)
                if not name and id_to_name and cid_str in id_to_name:
                    name = id_to_name[cid_str]
            if name:
                names.add(name)
    return names, char_ids


def _up_from_dyn_meta(
    dyn_meta: Any, id_to_name: dict[str, str] | None = None
) -> tuple[set[str], set[str]]:
    """从解包表 gachaPoolClient[].dynMeta 提取明确的 UP 干员 ID 与名称。

    Kengxxiao/ArknightsGameData 的 gacha_table.json 不含 detailInfo，
    限定/联动/定向等卡池通过 dynMeta 给出 main6RarityCharId、
    sub6RarityCharId、rare5CharList、upCharIdList 等字段；常驻标准池的
    dynMeta 为 null（无 UP 名单，按原契约返回空集合，不猜测）。
    """
    names: set[str] = set()
    char_ids: set[str] = set()
    if not isinstance(dyn_meta, dict):
        return names, char_ids

    def _add_cid(raw_cid: Any) -> None:
        cid = str(raw_cid or "").strip()
        if not cid:
            return
        char_ids.add(cid)
        if id_to_name and cid in id_to_name:
            names.add(id_to_name[cid])

    for single_key in ("main6RarityCharId", "sub6RarityCharId", "charId"):
        if dyn_meta.get(single_key):
            _add_cid(dyn_meta.get(single_key))

    for list_key in ("rare5CharList", "upCharIdList"):
        raw_list = dyn_meta.get(list_key)
        if isinstance(raw_list, list):
            for item in raw_list:
                _add_cid(item)

    return names, char_ids


def _parse_pools(
    table: dict[str, Any], id_to_name: dict[str, str] | None = None
) -> list[GachaPoolInfo]:
    pools: list[GachaPoolInfo] = []
    raw_pools = table.get("gachaPoolClient")
    if not isinstance(raw_pools, list):
        return pools
    for raw in raw_pools:
        if not isinstance(raw, dict):
            continue
        pool_id = str(raw.get("gachaPoolId") or "")
        pool_name = str(raw.get("gachaPoolName") or "")
        if not pool_id or not pool_name:
            continue
        up_chars: set[str] = set()
        up_char_ids: set[str] = set()
        detail = raw.get("detailInfo")
        if isinstance(detail, dict):
            d_names, d_ids = _up_chars_from(detail.get("upCharInfo"), id_to_name)
            up_chars |= d_names
            up_char_ids |= d_ids
        m_names, m_ids = _up_from_dyn_meta(raw.get("dynMeta"), id_to_name)
        up_chars |= m_names
        up_char_ids |= m_ids
        pools.append(
            GachaPoolInfo(
                pool_id=pool_id,
                pool_name=pool_name,
                rule_type=str(raw.get("gachaRuleType") or ""),
                open_time=int(raw.get("openTime") or 0),
                end_time=int(raw.get("endTime") or 0),
                up_chars=up_chars,
                up_char_ids=up_char_ids,
            )
        )
    return pools


async def load_gacha_pools() -> list[GachaPoolInfo]:
    """全部卡池元数据（表不可用返回空列表，调用方自行降级）。

    不做模块级缓存：ensure_gacha_table 的 memo 已挡住重复解析，
    这里每次重新投影为 GachaPoolInfo（~600 条，开销可忽略），
    免去 ark刷新 后的内存缓存失效维护。
    """
    table = await ensure_gacha_table()
    if not isinstance(table, dict):
        return []
    id_to_name: dict[str, str] = {}
    try:
        from .charinfo import load_char_index

        char_index = await load_char_index() or {}
        id_to_name = {
            str(cid): str(info.get("name") or "")
            for cid, info in char_index.items()
            if isinstance(info, dict) and info.get("name")
        }
    except Exception:
        id_to_name = {}
    return _parse_pools(table, id_to_name or None)


def match_pool_in_window(
    pool_name: str, ts: int, pools: list[GachaPoolInfo]
) -> GachaPoolInfo | None:
    """严格按开池时间窗匹配同名池；越窗或缺失时间一律返回 None。

    供导入持久化（heybox 等）使用：新池未刷新入表时绝不能把旧轮换池的
    pool_id 写死进本地日志，否则后续刷新卡池表也无法恢复真实池身份。
    """
    if not pool_name or not pools or not ts:
        return None
    in_window = [
        p
        for p in pools
        if p.pool_name == pool_name
        and p.open_time
        and p.end_time
        and p.open_time <= ts <= p.end_time
    ]
    if len(in_window) == 1:
        return in_window[0]
    if in_window:
        return max(in_window, key=lambda p: p.open_time)
    return None


def match_pool(
    pool_name: str, ts: int, pools: list[GachaPoolInfo]
) -> GachaPoolInfo | None:
    """按「池名 + 抽取时间」匹配卡池：时间窗内的同名池唯一命中。

    匹配不到时间窗内的（记录时间越界/池表未收录）回退到同名池中
    开池时间距 ts 最近的一个；仍无同名池返回 None（不猜 poolId）。
    """
    if not pool_name or not pools:
        return None
    same_name = [p for p in pools if p.pool_name == pool_name]
    if not same_name:
        return None
    in_window = (
        [
            p
            for p in same_name
            if p.open_time and p.end_time and p.open_time <= ts <= p.end_time
        ]
        if ts
        else []
    )
    if len(in_window) == 1:
        return in_window[0]
    if in_window:
        # 同名池时间窗重叠（数据异常）：取开池最晚的，可复现且偏新池
        return max(in_window, key=lambda p: p.open_time)
    # 无时间可用或全部越界：取开池时间距 ts 最近的同名池
    if ts:
        return min(same_name, key=lambda p: abs(p.open_time - ts))
    return same_name[-1]


def rule_category(rule_type: str, pool_id: str, pool_name: str) -> str:
    """小黑盒记录的内部组键：能识别 gachaRuleType 用规则组，否则按池独立分组。

    绝不伪造官方 category ID，也不把全部历史塞进同一组（否则垫刀和
    五星分段会跨池串联）。
    """
    if rule_type:
        return f"heybox:rule:{rule_type}"
    return f"heybox:pool:{pool_id or pool_name}"
