"""养成计算（P3）：当前练度 → 毕业所需的剩余材料与仓库缺口。

数据链：
- 需求：森空岛 `game/cultivate/character`（cal_character）——evolvePhaseCost /
  mainSkillLevelCost / skillCost / equipCost 四段成本（字段映射参考
  gxy12345 apps/calculate.js，客观接口事实）
- 持有：`game/cultivate/player`（cal_player）的 player_item_data
- 当前进度：player_info 的 chars（复用 utils/player 解析层）
- 物品名/稀有度：ArknightsGameData item_table（gamedata 共享缓存）

口径（v1，与 gxy 的差异均已注明）：
- 目标 = 精英化2 + 主技能7 + 全技能专精3 + 全模组满级（"毕业"）
- skillCost 与 player_info chars.skills 按**顺序对位**（同一 API 家族的顺序约定，
  位置对不上时退化为全量计入并打 warning）
- 模组 equipCost 按顺序对位 char.equips；未安装的模组全量计入
- 经验书（2001-2004）不按 200/400/1000/2000 经验值折算，按数量直列
- 刷图建议不并入单干员计算：掉率数据源（yituliu COS 掉落矩阵）已由
  wiki 模块接入，ark养成计划 经 _build_farm_rows 消费（arknightsuid_cultivate）
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..dict_utils import path, value

if TYPE_CHECKING:
    from ..enduid.adapter import ArkCredential
    from ..player.parse import CharEntry

# 龙门币 / 经验书（客观游戏物品 id）
GOLD_ITEM_ID = "4001"
EXP_BOOK_IDS = {"2001", "2002", "2003", "2004"}

_MAIN_SKILL_MAX = 7
_MASTERY_MAX = 3  # 专精 3
# skillCost.levelCost 下标：0-6 → 技能 1-7 级，7-9 → 专精 1-3（gxy 同款口径）
_MASTERY_COST_BASE = 7
_EVOLVE_MAX = 2


def _items_of(raw: Any) -> list[dict[str, Any]]:
    """[{id, count}] 规整（容错空值与字符串数字）。"""
    result: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return result
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id") or "")
        if not item_id:
            continue
        try:
            count = int(entry.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        result.append({"id": item_id, "count": count})
    return result


@dataclass(slots=True)
class CultivateCost:
    """cal_character 响应的四段成本结构。"""

    evolve: list[list[dict[str, Any]]] = field(default_factory=list)  # 每阶段 items
    main_skill: list[list[dict[str, Any]]] = field(
        default_factory=list
    )  # 每级 items（1-7）
    skills: list[dict[str, Any]] = field(default_factory=list)  # [{name, level_cost[]}]
    equips: list[dict[str, Any]] = field(
        default_factory=list
    )  # [{type_name, level_cost[]}]
    raw: dict[str, Any] = field(default_factory=dict)


def parse_cultivate_cost(response: Any) -> CultivateCost | None:
    """解析 cal_character 响应信封。结构无法识别返回 None。"""
    if not isinstance(response, dict):
        return None
    code = value(response, "code")
    if code not in (None, 0):
        return None
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    if not isinstance(data, dict):
        return None

    evolve = [
        _items_of(phase.get("items")) for phase in data.get("evolvePhaseCost") or []
    ]
    main_skill = [
        _items_of(level.get("items")) for level in data.get("mainSkillLevelCost") or []
    ]
    skills = []
    for skill in data.get("skillCost") or []:
        if not isinstance(skill, dict):
            continue
        levels = [
            _items_of(level.get("items")) for level in skill.get("levelCost") or []
        ]
        skills.append({"name": str(skill.get("name") or ""), "level_cost": levels})
    equips = []
    for equip in data.get("equipCost") or []:
        if not isinstance(equip, dict):
            continue
        levels = [
            _items_of(level.get("items")) for level in equip.get("levelCost") or []
        ]
        equips.append(
            {"type_name": str(equip.get("typeName") or ""), "level_cost": levels}
        )
    return CultivateCost(
        evolve=evolve, main_skill=main_skill, skills=skills, equips=equips, raw=data
    )


def _owned_items(response: Any) -> dict[str, int]:
    """解析 cal_player 的 player_item_data → {itemId: count}。

    官方未文档化该结构（gxy 直接对比），兼容 dict{id:count} 与
    list[{id,count}|{itemId,count}] 两种形态。
    """
    raw = path(response, "data", "player_item_data")
    owned: dict[str, int] = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            if isinstance(val, dict):
                val = val.get("count", val.get("have", val.get("cnt")))
            try:
                owned[str(key)] = int(val or 0)
            except (TypeError, ValueError):
                continue
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id") or entry.get("itemId") or "")
            if not item_id:
                continue
            try:
                owned[item_id] = int(entry.get("count") or 0)
            except (TypeError, ValueError):
                continue
    return owned


def build_remaining_need(
    cost: CultivateCost, char: CharEntry
) -> tuple[dict[str, int], list[str]]:
    """当前练度 → 毕业的剩余材料需求 {itemId: count} + 假设性 warning 列表。"""
    warnings: list[str] = []
    need: Counter[str] = Counter()

    def add(items: list[dict[str, Any]]) -> None:
        for entry in items:
            need[entry["id"]] += entry["count"]

    # 精英化：当前阶段之后的所有阶段
    for phase_items in cost.evolve[char.evolve_phase : _EVOLVE_MAX]:
        add(phase_items)

    # 主技能 1-7：main_skill 下标 = 等级-1，从当前级+1 起
    for level in range(char.main_skill_lvl + 1, _MAIN_SKILL_MAX + 1):
        if level - 1 < len(cost.main_skill):
            add(cost.main_skill[level - 1])

    # 专精：skillCost 顺序对位 chars.skills；level_cost[7+专精-1]
    if cost.skills and len(cost.skills) != len(char.skills):
        warnings.append("技能顺序对位不全，专精按已持有技能数计算")
    for index, skill_cost in enumerate(cost.skills):
        current = char.skills[index].specialize_level if index < len(char.skills) else 0
        for mastery in range(current + 1, _MASTERY_MAX + 1):
            level_index = _MASTERY_COST_BASE + mastery - 1
            if level_index < len(skill_cost["level_cost"]):
                add(skill_cost["level_cost"][level_index])

    # 模组：equipCost 顺序对位 char.equips（已安装的从当前级+1 起，未安装全量）
    if cost.equips:
        installed_levels = [equip.level for equip in char.equips]
        for index, equip_cost in enumerate(cost.equips):
            current = installed_levels[index] if index < len(installed_levels) else 0
            for level_items in equip_cost["level_cost"][current:]:
                add(level_items)

    return dict(need), warnings


def summarize_gap(
    need: dict[str, int],
    owned: dict[str, int],
    item_index: dict[str, dict[str, Any]],
    max_rows: int = 12,
    *,
    keep_zero: bool = False,
) -> dict[str, Any]:
    """需求 × 持有 → 缺口清单（含名称/稀有度，金币与经验书单列）。

    keep_zero=False（单干员展示）：零缺口行过滤、materials 截断 max_rows。
    keep_zero=True（多干员合并）：**保留全部需求行（含零缺口）且不截断**，
    另附 gold_need/gold_owned 原始值——合并必须在"每个干员的完整需求"上
    汇总后再统一扣库存；若在单干员阶段先过滤（旧行为），两个各需 8、
    仓库 10 的干员会各自显示无缺口，真实缺口 6 被漏算。龙门币同理必须
    用 gold_need 求和后统一 max(0, need-owned)，不能累加各自折算值。
    """

    def display_name(item_id: str) -> str:
        info = item_index.get(item_id)
        if info and info.get("name"):
            return str(info["name"])
        if item_id == GOLD_ITEM_ID:
            return "龙门币"
        if item_id in EXP_BOOK_IDS:
            return f"作战记录({item_id})"
        return item_id

    def display_rarity(item_id: str) -> str:
        info = item_index.get(item_id)
        rarity = str((info or {}).get("rarity", ""))
        if rarity.startswith("TIER_"):
            try:
                return f"{int(rarity[5:])}★"
            except ValueError:
                pass
        return ""

    materials: list[dict[str, Any]] = []
    gold_lack = 0
    exp_books: list[dict[str, Any]] = []
    for item_id, count in sorted(
        need.items(),
        key=lambda kv: (
            -int(
                str(
                    (item_index.get(kv[0]) or {})
                    .get("rarity", "0")
                    .replace("TIER_", "")
                )
                or 0
            ),
            (item_index.get(kv[0]) or {}).get("sort", 0),
            kv[0],
        ),
    ):
        have = owned.get(item_id, 0)
        lack = max(0, count - have)
        if lack <= 0 and not keep_zero:
            continue
        if item_id == GOLD_ITEM_ID:
            gold_lack = lack
            continue
        row = {
            "id": item_id,
            "name": display_name(item_id),
            "rarity": display_rarity(item_id),
            "need": count,
            "owned": have,
            "lack": lack,
        }
        if item_id in EXP_BOOK_IDS:
            exp_books.append(row)
        else:
            materials.append(row)
    return {
        "materials": materials if keep_zero else materials[:max_rows],
        "materials_total": len(materials),
        "exp_books": exp_books,
        "gold_lack": gold_lack,
        # 原始量纲：多干员合并用（求和后统一扣库存，见 merge_cultivation_results）
        "gold_need": int(need.get(GOLD_ITEM_ID, 0) or 0),
        "gold_owned": int(owned.get(GOLD_ITEM_ID, 0) or 0),
    }


async def calculate_cultivation(
    credential: ArkCredential,
    char_id: str,
    max_rows: int = 12,
    *,
    keep_zero: bool = False,
) -> dict[str, Any] | str:
    """完整计算链：进度（player_info 缓存）+ 需求（cal_character）+ 持有（cal_player）。

    返回卡片/文本两用的结果 dict；失败返回中文错误文案。
    keep_zero=True 供多干员合并（ark养成计划）：gap 保留零缺口行与原始
    gold 量纲，合并时统一扣库存（见 summarize_gap/merge_cultivation_results）。
    """
    from ..enduid.adapter import (
        fetch_cultivate_character,
        fetch_cultivate_player,
    )
    from ..gamedata.charinfo import load_item_index
    from ..player import fetch_cached_player_info, parse_player_info

    player_response = await fetch_cached_player_info(credential)
    if isinstance(player_response, str):
        return player_response
    player = parse_player_info(player_response)
    if player is None or not player.chars:
        return "未获取到干员练度数据，无法计算养成需求。"
    char = next((c for c in player.chars if c.char_id == char_id), None)
    if char is None:
        return "你还没有获得该干员（或数据尚未同步），无法计算养成需求。"

    cost_response = await fetch_cultivate_character(credential, char_id)
    if isinstance(cost_response, str):
        return cost_response
    cost = parse_cultivate_cost(cost_response)
    if cost is None:
        return "森空岛养成材料接口返回异常，无法计算。"

    # 持有量失败不中断计算（缺口仍可展示），但必须显式告警——
    # 否则按持有 0 计算会呈现"缺全部材料"的误导清单；
    # 成本接口失败则上方已直接 return，两者口径刻意不同
    owned_response = await fetch_cultivate_player(credential)
    owned: dict[str, int] = {}
    owned_warnings: list[str] = []
    if isinstance(owned_response, str):
        owned_warnings.append(
            f"持有材料获取失败（{owned_response}），以下缺口按持有 0 估算"
        )
    else:
        owned = _owned_items(owned_response)
        if value(owned_response, "code") not in (None, 0) or (
            path(owned_response, "data", "player_item_data") is None
        ):
            owned_warnings.append("持有材料接口返回异常，以下缺口按持有 0 估算")

    need, warnings = build_remaining_need(cost, char)
    warnings = owned_warnings + warnings
    item_index = await load_item_index()
    gap = summarize_gap(need, owned, item_index, max_rows=max_rows, keep_zero=keep_zero)

    return {
        "char": char,
        "need": need,
        "gap": gap,
        "warnings": warnings,
        "current": {
            "evolve_phase": char.evolve_phase,
            "main_skill_lvl": char.main_skill_lvl,
            "masteries": [s.specialize_level for s in char.skills],
            "equip_levels": [e.level for e in char.equips],
        },
    }


def merge_cultivation_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """多干员养成缺口合并（ark养成计划）：同账号仓库持有只扣一次。

    - 材料/经验书：按 itemId 汇总**完整需求**（keep_zero 的 gap 含零缺口行），
      lack = max(0, 合计 need - owned)——不能在单干员阶段先过滤零缺口行，
      否则"两个各需 8、仓库 10"会双双显示无缺口，漏掉真实缺口 6
    - 龙门币：优先用 gold_need 求和后统一 max(0, need - owned)；
      旧格式 gap（无 gold_need 字段）回退为 gold_lack 累加
    - owned 各结果一致（同一账号的 cal_player 快照）
    - 排序沿用 summarize_gap 口径：稀有度降序 → sort → id
    """
    materials: dict[str, dict[str, Any]] = {}
    exp_books: dict[str, dict[str, Any]] = {}
    gold_lack = 0
    gold_need_total: int | None = None  # None = 旧格式 gap，走回退
    gold_owned = 0
    warnings: list[str] = []
    chars: list[dict[str, Any]] = []

    def _absorb(bucket: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
        entry = bucket.get(row["id"])
        if entry is None:
            entry = dict(row)
            entry["need"] = 0
            bucket[row["id"]] = entry
        entry["need"] += int(row.get("need", 0))

    for result in results:
        gap = result["gap"]
        char = result["char"]
        if "gold_need" in gap:
            if gold_need_total is None:
                gold_need_total = 0
            gold_need_total += int(gap.get("gold_need") or 0)
            gold_owned = int(gap.get("gold_owned") or 0)
        else:
            gold_lack += int(gap.get("gold_lack") or 0)
        warnings.extend(result.get("warnings") or [])
        char_material_total = sum(
            1 for row in gap.get("materials") or [] if int(row.get("lack", 0)) > 0
        )
        for row in gap.get("materials") or []:
            _absorb(materials, row)
        for row in gap.get("exp_books") or []:
            _absorb(exp_books, row)
        chars.append(
            {
                "name": char.display_name,
                "char_id": getattr(char, "char_id", ""),  # 干员清单头像用
                "rarity": char.rarity_display,
                "current": result.get("current") or {},
                "gap_count": char_material_total,
            }
        )

    def _finalize(bucket: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        for entry in bucket.values():
            entry["lack"] = max(0, entry["need"] - int(entry.get("owned") or 0))
        return sorted(
            bucket.values(),
            key=lambda row: (
                -int(str(row.get("rarity") or "0").replace("★", "") or 0),
                str(row.get("name") or ""),
            ),
        )

    if gold_need_total is not None:
        gold_lack = max(0, gold_need_total - gold_owned)

    material_rows = _finalize(materials)
    exp_rows = [row for row in _finalize(exp_books) if row["lack"] > 0]
    return {
        "chars": chars,
        "materials": [row for row in material_rows if row["lack"] > 0],
        "materials_total": sum(1 for row in material_rows if row["lack"] > 0),
        "exp_books": exp_rows,
        "gold_lack": gold_lack,
        "warnings": warnings,
    }
