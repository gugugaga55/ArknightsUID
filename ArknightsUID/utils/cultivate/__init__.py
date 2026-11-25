"""养成计算（P3）：当前练度 → 毕业所需的剩余材料与仓库缺口。

数据链：
- 需求：森空岛 `game/cultivate/character`（cal_character）——evolvePhaseCost /
  mainSkillLevelCost / skillCost / equipCost 四段成本（字段映射参考
  gxy12345 apps/calculate.js，客观接口事实）
- 持有：`game/cultivate/player`（cal_player）的 player_item_data
- 当前进度：player_info 的 chars（复用 utils/player 解析层）
- 物品名/稀有度：ArknightsGameData item_table（gamedata 共享缓存）

口径：
- 默认按精2、技能7、全部专3及可用模组满级计算，也可为每位干员指定目标。
- skillCost 与 player_info chars.skills 按**顺序对位**（同一 API 家族的顺序约定，
  位置对不上时退化为全量计入并打 warning）
- 模组按 uniequip_table 的 ID/类型对应当前进度，基础证章不参与扣减
- 经验书（2001-2004）不按 200/400/1000/2000 经验值折算，按数量直列
- 单人和多人卡片共用缺口及刷图建议；未持有干员从精0、技能1级预估。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..dict_utils import path, value
from ..gamedata.panel_args import parse_panel_query

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
_FULL_GOAL_LABEL = "毕业（精2 · 技能7 · 全技能专3 · 全可用模组满级）"


@dataclass(frozen=True, slots=True)
class CultivationGoal:
    phase: int
    skill_level: int
    skill_index: int | None = None
    mastery: int = 0
    module_type: str = ""
    module_level: int = 0

    @property
    def label(self) -> str:
        parts = [f"精{self.phase}", f"技能{self.skill_level}级"]
        if self.mastery:
            parts.append(f"{self.skill_index}技能专{self.mastery}")
        if self.module_level:
            parts.append(f"{self.module_type}模{self.module_level}级")
        return " · ".join(parts)


def parse_cultivation_goal(text: str) -> tuple[str, CultivationGoal | None]:
    config = parse_panel_query(text)
    name = config.pop("name")
    if not any(value is not None and value is not False for value in config.values()):
        return name, None
    if any(config[key] is not None for key in ("level", "potential", "favor")):
        raise ValueError("养成暂只计算精英化、技能和模组材料；干员等级、潜能和信赖请用 ark面板。")
    phase, skill = config["phase"], config["skill_level"]
    skill_index, mastery = config["skill_index"], config["mastery"]
    module_type, module_level = config["module_type"], config["module_level"]
    if (skill_index is None) != (mastery is None) or (mastery or 0) > _MASTERY_MAX:
        raise ValueError("专精请同时指定技能和档位，例如「3技能 专3」。")
    if module_level and not module_type:
        raise ValueError("请指定模组类型，例如「X模 模1」。")
    if config["no_module"] and module_type:
        raise ValueError("「无模」不能和指定模组一起使用。")
    if phase is None:
        phase = 2 if mastery or module_type or skill is None else int(skill > 4)
    if skill is None:
        skill = 4 if phase == 0 else _MAIN_SKILL_MAX
    if phase == 0 and skill > 4:
        raise ValueError("精0最高技能4级。")
    if (mastery or module_type) and phase != 2:
        raise ValueError("专精和模组需精2。")
    if mastery and skill != _MAIN_SKILL_MAX:
        raise ValueError("专精前需要技能7级。")
    return name, CultivationGoal(
        phase, skill, skill_index, mastery or 0, module_type or "",
        (module_level or 1) if module_type else 0,
    )


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


def parse_inventory(response: Any) -> dict[str, int] | None:
    """区分空仓库与读取失败，避免用 0 伪装未知持有量。"""
    if value(response, "code") not in (None, 0):
        return None
    if not isinstance(path(response, "data", "player_item_data"), (dict, list)):
        return None
    return _owned_items(response)


def build_remaining_need(
    cost: CultivateCost,
    char: CharEntry,
    *,
    equip_types: dict[str, str] | None = None,
    goal: CultivationGoal | None = None,
    estimated_start: bool = False,
) -> tuple[dict[str, int], list[str]]:
    """当前练度 → 毕业的剩余材料需求 {itemId: count} + 假设性 warning 列表。"""
    warnings: list[str] = []
    need: Counter[str] = Counter()

    def add(items: list[dict[str, Any]]) -> None:
        for entry in items:
            need[entry["id"]] += entry["count"]

    # 精英化：当前阶段之后的所有阶段
    phase_limit = goal.phase if goal else _EVOLVE_MAX
    skill_limit = goal.skill_level if goal else _MAIN_SKILL_MAX
    if goal and phase_limit > max(char.evolve_phase, len(cost.evolve)):
        raise ValueError(f"该干员没有可计算的精{phase_limit}材料档位。")
    if goal and skill_limit > max(char.main_skill_lvl, len(cost.main_skill)):
        raise ValueError(f"该干员没有可计算的技能{skill_limit}级材料档位。")
    for phase_items in cost.evolve[char.evolve_phase : phase_limit]:
        add(phase_items)

    # 主技能 1-7：main_skill 下标 = 等级-1，从当前级+1 起
    for level in range(char.main_skill_lvl + 1, skill_limit + 1):
        if level - 1 < len(cost.main_skill):
            add(cost.main_skill[level - 1])

    # 专精：skillCost 顺序对位 chars.skills；level_cost[7+专精-1]
    mastery_limit = goal.mastery if goal else _MASTERY_MAX
    skill_target = goal.skill_index if goal else None
    if mastery_limit and skill_target and (
        skill_target > len(cost.skills)
        or len(cost.skills[skill_target - 1]["level_cost"]) < _MASTERY_COST_BASE + mastery_limit
    ):
        raise ValueError(f"该干员没有可计算的{skill_target}技能专{mastery_limit}档位。")
    if mastery_limit and cost.skills and len(cost.skills) != len(char.skills) and not estimated_start:
        warnings.append("技能顺序对位不全，超出持有技能的部分按专精0估算")
    for index, skill_cost in enumerate(cost.skills):
        if skill_target and skill_target != index + 1:
            continue
        current = char.skills[index].specialize_level if index < len(char.skills) else 0
        for mastery in range(current + 1, mastery_limit + 1):
            level_index = _MASTERY_COST_BASE + mastery - 1
            if level_index < len(skill_cost["level_cost"]):
                add(skill_cost["level_cost"][level_index])

    # 玩家 equip 列表可能含基础证章且顺序与成本表不同，不能按下标扣材料。
    module_level = goal.module_level if goal else 3
    module_type = goal.module_type if goal else ""
    target_equips = [
        equip for equip in cost.equips
        if not module_type or equip["type_name"].upper() == module_type
    ]
    if module_type and not target_equips:
        raise ValueError(f"该干员没有可计算的 {module_type} 模。")
    if module_level and target_equips:
        types = equip_types or {}
        if any(e.level > 0 and e.equip_id not in types for e in char.equips):
            warnings.append("部分模组类型未能对应，相关进度未扣除，模组材料按未解锁估算")
        installed = {
            types[e.equip_id].upper(): e.level for e in char.equips if types.get(e.equip_id)
        }
        for equip_cost in target_equips:
            current = installed.get(equip_cost["type_name"].upper(), 0)
            if goal and module_level > max(current, len(equip_cost["level_cost"])):
                raise ValueError(f"该干员没有可计算的 {module_type} 模{module_level}档位。")
            for level_items in equip_cost["level_cost"][current:module_level]:
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
    inventory_response: Any | None = None,
    force_refresh: bool = True,
    goal: CultivationGoal | None = None,
) -> dict[str, Any] | str:
    """完整计算链：进度（player_info）+ 需求（cal_character）+ 持有（cal_player）。

    返回卡片/文本两用的结果 dict；失败返回中文错误文案。
    keep_zero=True 供多干员合并（ark养成计划）：gap 保留零缺口行与原始
    gold 量纲，合并时统一扣库存（见 summarize_gap/merge_cultivation_results）。
    inventory_response 供批量规划共用同一次库存响应，避免重复请求与快照不一致。
    """
    from ..enduid.adapter import (
        fetch_cultivate_character,
        fetch_cultivate_player,
    )
    from ..gamedata.charinfo import load_char_index, load_item_index
    from ..player import fetch_cached_player_info, parse_player_info
    from ..player.parse import CharEntry

    player_response = (
        await fetch_cached_player_info(credential, force=True)
        if force_refresh
        else await fetch_cached_player_info(credential)
    )
    if isinstance(player_response, str):
        return player_response
    player = parse_player_info(player_response)
    if player is None or (not player.chars and not isinstance(player.raw.get("chars"), list)):
        return "未获取到干员练度数据，无法计算养成需求。"
    char = next((c for c in player.chars if c.char_id == char_id), None)
    estimated_start = char is None

    cost_response = await fetch_cultivate_character(credential, char_id)
    if isinstance(cost_response, str):
        return cost_response
    cost = parse_cultivate_cost(cost_response)
    if cost is None:
        return "森空岛养成材料接口返回异常，无法计算。"
    if char is None:
        info = (await load_char_index()).get(char_id) or {}
        if not info and not (cost.evolve or cost.main_skill or cost.skills or cost.equips):
            return "未找到该干员的养成数据，请检查干员名。"
        char = CharEntry(
            char_id=char_id, level=1, main_skill_lvl=1,
            name=str(info.get("name") or cost.raw.get("name") or char_id),
            profession=str(info.get("profession") or ""), rarity=int(info.get("rarity") or 0),
        )

    # 持有量失败不中断计算（缺口仍可展示），但必须显式告警——
    # 否则按持有 0 计算会呈现"缺全部材料"的误导清单；
    # 成本接口失败则上方已直接 return，两者口径刻意不同
    owned_response = inventory_response
    if owned_response is None:
        owned_response = await fetch_cultivate_player(credential)
    inventory = parse_inventory(owned_response)
    owned_warnings: list[str] = []
    if isinstance(owned_response, str):
        owned_warnings.append(
            f"持有材料获取失败（{owned_response}），以下缺口按持有 0 估算"
        )
    elif inventory is None:
        owned_warnings.append("持有材料接口返回异常，以下缺口按持有 0 估算")

    equip_types: dict[str, str] = {}
    if cost.equips and char.equips:
        from ..gamedata._table_cache import ensure_uniequip_table

        equip_table = await ensure_uniequip_table()
        for equip_id, info in (equip_table or {}).get("equipDict", {}).items():
            equip_types[equip_id] = str(info.get("typeName2") or "")
    try:
        need, warnings = build_remaining_need(
            cost, char, equip_types=equip_types, goal=goal, estimated_start=estimated_start,
        )
    except ValueError as exc:
        return str(exc)
    if estimated_start:
        warnings.append(f"{char.display_name} 未在账号干员列表中，按精0、技能1级估算。")
    warnings = owned_warnings + warnings
    item_index = await load_item_index()
    gap = summarize_gap(need, inventory or {}, item_index, max_rows=max_rows, keep_zero=keep_zero)

    return {
        "char": char,
        "need": need,
        "gap": gap,
        "inventory": inventory,
        "goal_label": goal.label if goal else _FULL_GOAL_LABEL,
        "full_goal": goal is None,
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
                "goal_label": result.get("goal_label") or _FULL_GOAL_LABEL,
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
