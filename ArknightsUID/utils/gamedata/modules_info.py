"""干员模组详情查询（ark模组）。

数据源双层：
1. PRTS {{模组}} 模板（主）：特性追加 / 天赋更新（Lv2/Lv3 + 满潜变体）/
   解锁·升级三档材料 / 解锁任务 —— 变量已填充的人话文本，页面缓存 24h
2. ArknightsGameData（兜底 + 补充）：uniequip_table（归属/解锁条件/
   itemCost 1/2/3 三档材料）+ battle_equip_table（各级属性白值增量）

PRTS 不可用时回退纯 GameData：材料仍给全三档（itemCost 2/3 是升级档），
但特性/天赋更新文本缺失（GameData 的天赋更新是未填充模板变量，无法
直接人读——数值模拟见 ark面板）。
"""

from __future__ import annotations

import re
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

# GameData itemCost 里的特殊材料 id → 中文名（item_table 不收录）
_TOKEN_ITEM_NAMES = {
    "mod_unlock_token": "模组数据块",
    "mod_update_token_1": "数据增补条",
    "mod_update_token_2": "数据增补仪",
}
_GOLD_ITEM_NAME = "龙门币"


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


def _game_data_costs(
    item_cost: dict, item_index: dict[str, dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """uniequip itemCost 三档 → {档位: [{name, count}]}（含龙门币）。"""
    tiers: dict[str, list[dict[str, Any]]] = {}
    for level, entries in (item_cost or {}).items():
        items: list[dict[str, Any]] = []
        for c in entries or []:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            item_id = str(c["id"])
            count = int(c.get("count") or 0)
            if str(c.get("type") or "") == "GOLD" or item_id == "4001":
                items.append({"name": _GOLD_ITEM_NAME, "count": count})
                continue
            if str(c.get("type") or "") != "MATERIAL":
                continue
            name = str(
                (item_index.get(item_id) or {}).get("name")
                or _TOKEN_ITEM_NAMES.get(item_id)
                or item_id
            )
            items.append({"name": name, "count": count})
        if items:
            tiers[str(level)] = items
    return tiers


def _parse_material_text(text: str) -> dict[str, Any]:
    """PRTS 材料串「模组数据块 ×4」→ {name, count}（拆不出数量则 count=""）。"""
    match = re.fullmatch(r"\s*(.+?)\s*[×x]\s*(\S+)\s*", text or "")
    if match:
        return {"name": match.group(1), "count": match.group(2)}
    return {"name": (text or "").strip(), "count": ""}


def _prts_costs(op_module) -> dict[str, list[dict[str, Any]]]:
    """PRTS upgrade_costs（"名 ×数" 串）→ {档位: [{name, count}]}。"""
    tiers: dict[str, list[dict[str, Any]]] = {}
    for level, texts in (getattr(op_module, "upgrade_costs", None) or {}).items():
        items = [_parse_material_text(t) for t in texts if t]
        if items:
            tiers[level] = items
    return tiers


async def _attach_item_icons(cost_rows: list[dict[str, Any]]) -> None:
    """给材料行挂 PRTS 带框图标（失败空串降级，不阻塞出卡）。"""
    try:
        from .itemicon import item_icon_b64

        for row in cost_rows:
            row["icon"] = await item_icon_b64(str(row.get("name") or ""))
    except Exception:
        for row in cost_rows:
            row.setdefault("icon", "")


def _merge_tier_costs(
    gamedata: dict[str, list[dict[str, Any]]],
    prts: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """三档材料合并：PRTS 优先（名称人读、含「8万」这类格式），缺档回退 GameData。"""
    merged: dict[str, list[dict[str, Any]]] = {}
    for level in ("1", "2", "3"):
        merged[level] = prts.get(level) or gamedata.get(level) or []
    return {k: v for k, v in merged.items() if v}


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

    # PRTS 增强（失败静默降级为纯 GameData 展示）
    prts_modules: dict[str, Any] = {}
    try:
        from ..prts.client import query_operator

        op = await query_operator(char_name)
        if op:
            prts_modules = {m.name: m for m in op.modules}
    except Exception:
        prts_modules = {}

    modules: list[dict[str, Any]] = []
    for equip_id in char_equips:
        info = equip_dict.get(equip_id) or {}
        if not info:
            continue
        name = str(info.get("uniEquipName") or equip_id)
        prts = prts_modules.get(name)
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
        # 三档材料：PRTS（人读格式）优先，GameData（itemCost 全三档）兜底
        tier_costs = _merge_tier_costs(
            _game_data_costs(info.get("itemCost") or {}, item_index),
            _prts_costs(prts) if prts else {},
        )
        # 材料图标统一挂（解锁档 + 升级档）
        all_cost_rows = [row for rows in tier_costs.values() for row in rows]
        await _attach_item_icons(all_cost_rows)

        talent_updates = [
            {"level": f"Lv{lvl}", "text": text}
            for lvl, text in sorted(
                (getattr(prts, "talent_updates", None) or {}).items()
            )
            if text
        ]
        potential_talents = [
            {"level": f"Lv{lvl}·满潜", "text": text}
            for lvl, text in sorted(
                (getattr(prts, "potential_talents", None) or {}).items()
            )
            if text
        ]
        # 类型归一：PRTS 类型值（LOR-X）优先；GameData typeIcon 统一大写，
        # original/基础证章 归一为「基础证章」
        raw_type = (
            getattr(prts, "type_", "") or str(info.get("typeIcon") or "")
        ).upper()
        type_display = "基础证章" if raw_type in ("基础证章", "ORIGINAL") else raw_type
        modules.append(
            {
                "id": equip_id,
                "name": name,
                "type": type_display,
                "type_name": str(info.get("typeName1") or ""),
                "desc": str(info.get("uniEquipDesc") or ""),
                "unlock_level": int(info.get("unlockLevel") or 0),
                "unlock_phase": _phase_int(info.get("unlockEvolvePhase")),
                "unlock_favor": unlock_favor,
                # 兼容旧模板键：解锁档材料
                "costs": tier_costs.get("1") or [],
                "tier_costs": tier_costs,
                "level_attrs": level_attrs,
                "trait": (getattr(prts, "trait", "") or "") if prts else "",
                "talent_updates": talent_updates,
                "potential_talents": potential_talents,
                "missions": list(getattr(prts, "missions", None) or []) if prts else [],
            }
        )

    if not modules:
        return f"{char_name} 暂无模组数据。"

    return {"name": char_name, "modules": modules}


def format_module_context(context: dict) -> str:
    """模组详情 → 纯文本（渲染失败降级用）。"""
    lines = [f"「明日方舟」{context['name']} · 模组一览"]
    tier_labels = {"1": "解锁材料", "2": "Lv2 升级材料", "3": "Lv3 升级材料"}
    for m in context["modules"]:
        type_label = m["type"] if m["type"] == "基础证章" else f"{m['type']}模组"
        header = f"◆ {m['name']}（{type_label} · {m['type_name']}）"
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
            lines.append(f"  {level}属性：{text}")
        if m.get("trait"):
            trait = m["trait"].replace("\n", " ")
            lines.append(f"  特性追加：{trait[:120]}{'…' if len(trait) > 120 else ''}")
        for row in m.get("talent_updates") or []:
            text = row["text"].replace("\n", " ")
            lines.append(
                f"  {row['level']}：{text[:140]}{'…' if len(text) > 140 else ''}"
            )
        for level, items in (m.get("tier_costs") or {}).items():
            if items:
                costs = "、".join(
                    f"{c['name']}×{c['count']}" if c.get("count") else str(c["name"])
                    for c in items
                )
                lines.append(f"  {tier_labels.get(level, level)}：{costs}")
        for i, mission in enumerate(m.get("missions") or [], start=1):
            mission = mission.replace("\n", " ")
            lines.append(f"  任务{i}：{mission[:80]}{'…' if len(mission) > 80 else ''}")
    lines.append("数据：PRTS Wiki · ArknightsGameData")
    return "\n".join(lines)
