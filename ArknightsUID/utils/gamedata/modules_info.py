"""干员模组详情查询（ark模组）。

数据源：uniequip_table（归属/解锁条件/解锁材料）+ battle_equip_table
（各级 attributeBlackboard 数值增量，复用 oppanel 的 module_stat_modifiers）。
天赋升级效果文本是未填充模板变量，不展示（数值结果见 ark面板）。
"""
from __future__ import annotations

from typing import Any

from .charinfo import load_char_index, load_item_index, match_entries
from .oppanel import load_module_tables, module_stat_modifiers

_ATTR_LABELS = {
    "maxHp": "生命",
    "atk": "攻击",
    "def": "防御",
    "magicResistance": "法抗",
    "cost": "部署费用",
    "attackSpeed": "攻速",
    "respawnTime": "再部署",
}


def _phase_int(value: Any) -> int:
    """精英阶段字段解析："PHASE_2" → 2；已是数字直接取。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "")
    digits = "".join(ch for ch in text.rsplit("_", 1)[-1] if ch.isdigit())
    return int(digits) if digits else 0


def _attr_text(modifiers: dict[str, float]) -> str:
    parts: list[str] = []
    for field, label in _ATTR_LABELS.items():
        value = modifiers.get(field)
        if not value:
            continue
        if field == "respawnTime":
            parts.append(f"再部署 {value:+g}s")
        else:
            parts.append(f"{label} {value:+g}")
    return "，".join(parts)


def resolve_char(
    query: str, char_index: dict[str, dict[str, Any]]
) -> tuple[str, str] | str:
    """query → (char_id, name)；歧义/无匹配返回提示文案。"""
    candidates = match_entries(query, char_index)
    if not candidates:
        return f"未找到干员：{query}（支持中文名/英文呼号/别名）。"
    if len(candidates) > 1:
        names = "、".join(str(c.get("name") or "") for c in candidates[:6])
        return f"匹配到多位干员：{names}，请写全名。"
    first = candidates[0]
    return str(first["char_id"]), str(first.get("name") or query)


async def build_module_context(query: str) -> dict | str:
    """ark模组 干员名 → {name, modules}；失败返回错误文案。"""
    char_index = await load_char_index()
    if not char_index:
        return "干员索引不可用（gamedata 拉取失败），请稍后再试。"
    resolved = resolve_char(query, char_index)
    if isinstance(resolved, str):
        return resolved
    char_id, char_name = resolved

    uniequip, battle = await load_module_tables()
    if not uniequip:
        return "模组数据不可用（uniequip_table 拉取失败），请稍后再试。"

    equip_dict = uniequip.get("equipDict") or {}
    char_equips = (uniequip.get("charEquip") or {}).get(char_id) or []
    item_index = await load_item_index()

    modules: list[dict[str, Any]] = []
    for equip_id in char_equips:
        info = equip_dict.get(equip_id) or {}
        if not info:
            continue
        # itemCost 按模组等级分档（"1"=解锁，"2"/"3"=升级）；只展示解锁档材料
        unlock_costs = (info.get("itemCost") or {}).get("1") or []
        costs = [
            {
                "name": str(
                    (item_index.get(str(c.get("id"))) or {}).get("name") or c.get("id")
                ),
                "count": int(c.get("count") or 0),
            }
            for c in unlock_costs
            if isinstance(c, dict)
            and str(c.get("type") or "") == "MATERIAL"
            and c.get("id")
        ]
        level_attrs = {}
        for level in (1, 2, 3):
            text = _attr_text(module_stat_modifiers(battle, equip_id, level))
            if text:
                level_attrs[f"L{level}"] = text
        # unlockFavors 是 {"1": v1, "2": v2, "3": v3}；0 值不展示
        favors = info.get("unlockFavors")
        unlock_favor = (
            int(favors.get("1") or 0)
            if isinstance(favors, dict)
            else _phase_int(favors)
        )
        modules.append(
            {
                "id": equip_id,
                "name": str(info.get("uniEquipName") or equip_id),
                "type": str(info.get("typeIcon") or ""),
                "type_name": str(info.get("typeName1") or ""),
                "desc": str(info.get("uniEquipDesc") or ""),
                "unlock_level": int(info.get("unlockLevel") or 0),
                "unlock_phase": _phase_int(info.get("unlockEvolvePhase")),
                "unlock_favor": unlock_favor,
                "costs": costs,
                "level_attrs": level_attrs,
            }
        )

    if not modules:
        return f"{char_name} 暂无模组数据。"

    return {"name": char_name, "modules": modules}


def format_module_context(context: dict) -> str:
    """模组详情 → 纯文本（渲染失败降级用）。"""
    lines = [f"「明日方舟」{context['name']} · 模组一览"]
    for m in context["modules"]:
        header = f"◆ {m['name']}（{m['type']}模组 · {m['type_name']}）"
        unlock = []
        if m["unlock_phase"]:
            unlock.append(f"精{m['unlock_phase']}")
        if m["unlock_level"]:
            unlock.append(f"Lv{m['unlock_level']}")
        if m["unlock_favor"]:
            unlock.append(f"好感{m['unlock_favor']}")
        lines.append(f"{header}｜解锁：{' '.join(unlock) or '—'}")
        if m["desc"]:
            desc = m["desc"].replace("\n", " ")
            lines.append(f"  {desc[:80]}{'…' if len(desc) > 80 else ''}")
        for level, text in (m["level_attrs"] or {}).items():
            lines.append(f"  {level}：{text}")
        if m["costs"]:
            costs = "、".join(f"{c['name']}×{c['count']}" for c in m["costs"])
            lines.append(f"  解锁材料：{costs}")
    lines.append("数据：ArknightsGameData（uniequip/battle_equip）")
    return "\n".join(lines)
