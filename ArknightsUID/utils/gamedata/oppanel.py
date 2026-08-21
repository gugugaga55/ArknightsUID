"""干员面板模拟：任意 精英/等级/潜能/好感/模组 下的数值计算。

数据源（ArknightsGameData，_table_cache 30 天缓存）：
- character_table（phases[].attributesKeyFrames 关键帧线性插值出任意等级面板；
  potentialRanks 潜能逐级累加；talents[].candidates 按 阶段/等级/潜能档 取生效档；
  favorKeyFrames 好感 HP/攻击加成）
- uniequip_table（charEquip 归属 + typeIcon X/Y 模组类型）
- battle_equip_table（模组各级 attributeBlackboard 数值）

技能逐级文本不走本模块——skill_table 的描述是未填充的模板变量
（{def:0%} + 黑板键），PRTS 干员页已给完整文本，由 wiki 层复用。
"""
from __future__ import annotations

import re
from typing import Any

try:
    from ArknightsUID.ArknightsUID.utils.gamedata._table_cache import (
        ensure_battle_equip_table,
        ensure_char_table,
        ensure_uniequip_table,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.gamedata._table_cache import (
        ensure_battle_equip_table,
        ensure_char_table,
        ensure_uniequip_table,
    )

# 面板属性（表内字段 → 展示键）；间隔单位秒保留一位
PANEL_ATTRS: tuple[tuple[str, str], ...] = (
    ("maxHp", "hp"),
    ("atk", "atk"),
    ("def", "def"),
    ("magicResistance", "res"),
    ("cost", "cost"),
    ("blockCnt", "block"),
    ("respawnTime", "respawn"),
    ("baseAttackTime", "interval"),
)

# 潜能/模组的 attributeType / blackboard 键 → 表内字段
_POTENTIAL_ATTR_MAP = {
    "ATK": "atk", "MAXHP": "maxHp", "DEF": "def", "COST": "cost",
    "MAGIC_RESISTANCE": "magicResistance", "RESPAWN_TIME": "respawnTime",
    "ATTACK_SPEED": "attackSpeed",
}
_MODULE_ATTR_MAP = {
    "max_hp": "maxHp", "atk": "atk", "def": "def", "cost": "cost",
    "attack_speed": "attackSpeed", "magic_resistance": "magicResistance",
    "respawn_time": "respawnTime",
}


def _fmt(value: float) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text if text else "0"


def interpolate(frames: list[dict], level: int) -> dict[str, float]:
    """attributesKeyFrames 线性插值出任意等级的属性 dict。

    帧内数值字段全部参与插值（生命/攻击等随等级线性，成长曲线已由
    关键帧端点值吸收——银灰精2 Lv45 实测与游戏面板一致）。
    """
    if not frames:
        return {}
    frames = sorted(frames, key=lambda f: int(f.get("level") or 0))
    lo, hi = frames[0], frames[-1]
    lo_lv, hi_lv = int(lo.get("level") or 0), int(hi.get("level") or 0)
    if level <= lo_lv:
        anchor = lo
        ratio = 0.0
    elif level >= hi_lv:
        # 边界外取端点帧本身（ratio 基点必须同为 anchor，否则线性项污染）
        lo = anchor = hi
        ratio = 0.0
    else:
        prev = lo
        anchor = hi
        for frame in frames:
            lv = int(frame.get("level") or 0)
            if lv <= level:
                prev = frame
            else:
                anchor = frame
                break
        prev_lv, anchor_lv = int(prev.get("level") or 0), int(
            anchor.get("level") or 0
        )
        ratio = (
            (level - prev_lv) / (anchor_lv - prev_lv)
            if anchor_lv > prev_lv
            else 0.0
        )
        lo = prev
    result: dict[str, float] = {}
    lo_data = lo.get("data") or {}
    hi_data = anchor.get("data") or {}
    for key, value in hi_data.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        lo_value = lo_data.get(key)
        if not isinstance(lo_value, (int, float)) or isinstance(lo_value, bool):
            result[key] = float(value)
            continue
        result[key] = lo_value + (value - lo_value) * ratio
    return result


async def load_char_entry(char_id: str) -> dict | None:
    """character_table → 该干员完整条目。"""
    table = await ensure_char_table()
    if not isinstance(table, dict):
        return None
    entry = table.get(char_id)
    return entry if isinstance(entry, dict) else None


def max_level_at(char_entry: dict, phase: int) -> int:
    phases = char_entry.get("phases") or []
    if phase >= len(phases):
        phase = len(phases) - 1
    return int((phases[phase] or {}).get("maxLevel") or 1)


def potential_modifiers(char_entry: dict, rank: int) -> dict[str, float]:
    """潜能 1..rank 的累加修正（游戏语义：各档效果叠加）。"""
    result: dict[str, float] = {}
    for potential in (char_entry.get("potentialRanks") or [])[: max(rank, 0)]:
        modifiers = (
            ((potential.get("buff") or {}).get("attributes") or {}).get(
                "attributeModifiers"
            )
            or []
        )
        for modifier in modifiers:
            field = _POTENTIAL_ATTR_MAP.get(
                str(modifier.get("attributeType") or "").upper()
            )
            if not field:
                continue
            value = float(modifier.get("value") or 0)
            if modifier.get("formulaItem") == "PERCENTAGE":
                # 百分比潜能按基础值折算延迟到 compute_panel（罕见，先线性近似）
                result[field] = result.get(field, 0.0) + value
            else:
                result[field] = result.get(field, 0.0) + value
    return result


def favor_modifiers(char_entry: dict, favor_percent: int) -> dict[str, float]:
    """好感度 HP/攻击加成（favorKeyFrames 线性）。

    帧刻度为显示值一半（显示 0-200%，帧 0-100，满值在帧 50 = 显示 100%）。
    """
    frames = char_entry.get("favorKeyFrames") or []
    data = interpolate(frames, favor_percent // 2) if frames else {}
    return {
        key: value
        for key, value in data.items()
        if key in ("maxHp", "atk") and value
    }


def module_stat_modifiers(
    battle_equip: dict, equip_id: str, equip_level: int
) -> dict[str, float]:
    """模组某级 attributeBlackboard → 表内字段修正。"""
    entry = (battle_equip or {}).get(equip_id)
    phases = (entry or {}).get("phases") or []
    for phase in phases:
        if int(phase.get("equipLevel") or 0) == equip_level:
            result: dict[str, float] = {}
            for item in phase.get("attributeBlackboard") or []:
                field = _MODULE_ATTR_MAP.get(str(item.get("key") or ""))
                if field:
                    result[field] = result.get(field, 0.0) + float(
                        item.get("value") or 0
                    )
            return result
    return {}


def _clean_talent_text(text: str) -> str:
    # <$ba.invisible>隐匿</> → 隐匿；<@ba.vup>+200%</> → +200%
    return re.sub(r"<[^<>]*>", "", str(text or "")).strip()


def _phase_level_ok(condition: dict, phase: int, level: int) -> bool:
    if not isinstance(condition, dict):
        return True
    cond_phase = str(condition.get("phase") or "")
    try:
        want = int(cond_phase.rsplit("_", 1)[-1])
    except ValueError:
        want = 0
    if phase < want:
        return False
    cond_level = int(condition.get("level") or 0)
    if phase == want and level < cond_level:
        return False
    return True


def talents_at(
    char_entry: dict, phase: int, level: int, potential: int
) -> list[dict]:
    """当前（阶段,等级,潜能）下每个天赋位的生效档（高潜能档覆盖低档）。"""
    result: list[dict] = []
    for talent in char_entry.get("talents") or []:
        best: dict | None = None
        best_rank = -1
        for candidate in talent.get("candidates") or []:
            if not _phase_level_ok(
                candidate.get("unlockCondition") or {}, phase, level
            ):
                continue
            rank = int(candidate.get("requiredPotentialRank") or 0)
            if rank > potential:
                continue
            if rank >= best_rank:
                best, best_rank = candidate, rank
        if best and best.get("name"):
            result.append(
                {
                    "name": str(best.get("name") or ""),
                    "description": _clean_talent_text(
                        best.get("description") or ""
                    ),
                }
            )
    return result


def potential_texts(char_entry: dict, rank: int) -> list[str]:
    """潜能 1..rank 的描述文本（用于当前潜能已生效列表）。"""
    texts = []
    for potential in (char_entry.get("potentialRanks") or [])[: max(rank, 0)]:
        desc = str(potential.get("description") or "").strip()
        if desc:
            texts.append(desc)
    return texts


def compute_panel(
    char_entry: dict,
    phase: int,
    level: int,
    potential: int = 0,
    favor_percent: int = 0,
    module_modifiers: dict[str, float] | None = None,
) -> dict:
    """完整面板：基础插值 + 潜能 + 好感 + 模组，带分项来源。

    返回 {attrs: {hp/atk/...: 展示值}, parts: {field: {base/potential/favor/module}}}
    """
    phases = char_entry.get("phases") or []
    phase = max(0, min(phase, len(phases) - 1))
    frames = phases[phase].get("attributesKeyFrames") or []
    base = interpolate(frames, level)
    pot = potential_modifiers(char_entry, potential)
    favor = favor_modifiers(char_entry, favor_percent)
    module = module_modifiers or {}

    attrs: dict[str, str] = {}
    parts: dict[str, dict[str, float]] = {}
    for field, key in PANEL_ATTRS:
        total = (
            base.get(field, 0.0)
            + pot.get(field, 0.0)
            + favor.get(field, 0.0)
            + module.get(field, 0.0)
        )
        attrs[key] = _fmt(total)
        parts[key] = {
            "base": base.get(field, 0.0),
            "potential": pot.get(field, 0.0),
            "favor": favor.get(field, 0.0),
            "module": module.get(field, 0.0),
        }
    return {"attrs": attrs, "parts": parts}


async def load_module_tables() -> tuple[dict, dict]:
    """(uniequip, battle_equip)；失败返回 ({}, {})。"""
    uniequip, battle = await _gather_tables()
    return uniequip, battle


async def _gather_tables() -> tuple[dict, dict]:
    import asyncio

    uniequip, battle = await asyncio.gather(
        ensure_uniequip_table(), ensure_battle_equip_table()
    )
    return (
        uniequip if isinstance(uniequip, dict) else {},
        battle if isinstance(battle, dict) else {},
    )


def module_choices(
    char_id: str, uniequip: dict
) -> list[dict]:
    """干员可用模组（除基础证章）：[{id, name, type: 'X'|'Y'}]。"""
    equip_dict = uniequip.get("equipDict") or {}
    char_equips = (uniequip.get("charEquip") or {}).get(char_id) or []
    result = []
    for equip_id in char_equips:
        entry = equip_dict.get(equip_id) or {}
        type_icon = str(entry.get("typeIcon") or "")
        if type_icon == "original":
            continue  # 基础证章无数值面板，不参与模拟
        module_type = "Y" if type_icon.endswith("y") else "X"
        result.append(
            {
                "id": equip_id,
                "name": str(entry.get("uniEquipName") or equip_id),
                "type": module_type,
            }
        )
    return result


def find_module(
    choices: list[dict], module_type: str | None
) -> dict | None:
    """按 X/Y 选模组（无该类型回退第一个）。"""
    if not choices:
        return None
    if module_type:
        for choice in choices:
            if choice["type"] == module_type.upper():
                return choice
    return choices[0]
