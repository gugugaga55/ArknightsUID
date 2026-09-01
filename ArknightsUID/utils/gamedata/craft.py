"""材料合成树与刷图规划（合成 vs 直刷对比 + 目标规划）。

数据源：
- item_table.buildingProductList：成品 → WORKSHOP formulaId
- building_data.workshopFormulas：配方详情（产出数/龙门币/材料成本/副产物概率）
- 一图流 COS 掉落矩阵：单件期望理智（直刷成本基准，utils.yituliu）

口径：
- 合成路径的「理智成本」按叶子材料的直刷期望理智累加（加工站脑力
  消耗不折算——脑力与理智的兑换随版本调整，展示材料+龙门币成本）。
- 副产物按期望产出摊薄：extraOutcomeRate × group 期望件数计入产出，
  仅在展示层提示，不参与叶子成本折算。
"""
from __future__ import annotations

from typing import Any

from ._table_cache import ensure_building_table

# 防御：配方环（数据异常时兜底）
_MAX_DEPTH = 8


# 加工站四类配方：F_BUILDING 基建材料 / F_EVOLVE 精英化芯片 /
# F_ASC 升阶材料（T2/T3 合成主体）/ F_SKILL 技能书
_WORKSHOP_FORMULA_TYPES = {"F_BUILDING", "F_EVOLVE", "F_ASC", "F_SKILL"}


async def load_formulas() -> dict[str, dict[str, Any]]:
    """itemId → 加工站配方（一个 item 只保留一条配方；重复取最后一条）。"""
    building = await ensure_building_table()
    if not isinstance(building, dict):
        return {}
    formulas = building.get("workshopFormulas") or {}
    if not isinstance(formulas, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for formula in formulas.values():
        if not isinstance(formula, dict):
            continue
        if formula.get("formulaType") not in _WORKSHOP_FORMULA_TYPES:
            continue
        item_id = str(formula.get("itemId") or "")
        if item_id:
            result[item_id] = formula
    return result


def _cost_names(
    formula: dict[str, Any], item_index: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for cost in formula.get("costs") or []:
        cid = str(cost.get("id") or "")
        if not cid:
            continue
        rows.append(
            {
                "item_id": cid,
                "name": str((item_index.get(cid) or {}).get("name") or cid),
                "count": int(cost.get("count") or 0),
            }
        )
    return rows


def craft_tree(
    item_id: str,
    formulas: dict[str, dict[str, Any]],
    item_index: dict[str, dict[str, Any]],
    need: int = 1,
    depth: int = 0,
) -> dict[str, Any]:
    """合成树：need 件 item_id 的完整合成路径（含递归子材料）。

    子材料需求按制作次数缩放：crafts = ceil(need / output)，
    子节点 need = 配方成本 × crafts。不可合成（无 WORKSHOP 配方）时
    costs 为空、craftable=False（叶子节点）。
    """
    from math import ceil

    name = str((item_index.get(item_id) or {}).get("name") or item_id)
    node: dict[str, Any] = {
        "item_id": item_id,
        "name": name,
        "need": need,
        "craftable": False,
        "gold": 0,
        "output": 1,
        "costs": [],
        "children": [],
    }
    formula = formulas.get(item_id)
    if not formula or depth >= _MAX_DEPTH:
        return node
    output = max(1, int(formula.get("count") or 1))
    crafts = max(1, ceil(need / output))
    node["craftable"] = True
    node["output"] = output
    node["gold"] = int(formula.get("goldCost") or 0) * crafts
    node["costs"] = _cost_names(formula, item_index)
    for cost in node["costs"]:
        scaled = dict(cost)
        scaled["count"] = cost["count"] * crafts
        node["children"].append(
            craft_tree(
                scaled["item_id"],
                formulas,
                item_index,
                scaled["count"],
                depth + 1,
            )
        )
    return node


def leaf_materials(node: dict[str, Any]) -> dict[str, int]:
    """聚合合成树的叶子材料（不可再合成的底层材料）需求量。"""
    leaves: dict[str, int] = {}

    def walk(current: dict[str, Any]) -> None:
        if not current.get("craftable"):
            leaves[current["item_id"]] = (
                leaves.get(current["item_id"], 0) + int(current.get("need") or 0)
            )
            return
        for child in current.get("children") or []:
            walk(child)

    walk(node)
    return leaves


def format_craft_tree(node: dict[str, Any], item_index: dict[str, dict[str, Any]]) -> list[str]:
    """合成树文本行（缩进层级展开）。"""
    lines: list[str] = []

    def walk(current: dict[str, Any], indent: str) -> None:
        mark = "├─ " if indent else ""
        if current.get("craftable"):
            costs = " + ".join(
                f"{c['name']}×{c['count']}" for c in current.get("costs") or []
            )
            lines.append(
                f"{indent}{mark}{current['name']} ×{current.get('need')} "
                f"（合成：产出 {current.get('output')}/次"
                f"{'，龙门币共 ' + str(current['gold']) if current.get('gold') else ''}）"
                f"\n{indent}    ← 单次成本 {costs}"
            )
        else:
            lines.append(f"{indent}{mark}{current['name']} ×{current.get('need')}（刷取）")
        for child in current.get("children") or []:
            walk(child, indent + "  ")

    walk(node, "")
    return lines


def best_stage_cost(
    item_id: str,
    matrix_by_item: dict[str, list[dict[str, Any]]],
    stage_meta: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """单件期望理智最低的掉落关卡（样本>300）。无数据返回 None。"""
    from ..yituliu import item_drop_stats

    stats = item_drop_stats(item_id, matrix_by_item, stage_meta, top=8)
    if not stats:
        return None
    best = min(stats, key=lambda row: row["ap_expect"])
    return best


def craft_vs_farm(
    item_id: str,
    need: int,
    formulas: dict[str, dict[str, Any]],
    item_index: dict[str, dict[str, Any]],
    matrix_by_item: dict[str, list[dict[str, Any]]],
    stage_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """合成 vs 直刷对比（单材料）。

    返回 {name, need, farm: best|None, craft: tree|None, craft_leaves,
    craft_sanity, farm_sanity}；craft_sanity 为叶子材料全直刷的期望理智。
    """
    name = str((item_index.get(item_id) or {}).get("name") or item_id)
    farm = best_stage_cost(item_id, matrix_by_item, stage_meta)
    farm_sanity = round(farm["ap_expect"] * need, 1) if farm else None

    tree = craft_tree(item_id, formulas, item_index, need)
    craft_sanity: float | None = None
    craft_leaves: dict[str, int] = {}
    if tree["craftable"]:
        craft_leaves = leaf_materials(tree)
        total = 0.0
        ok = True
        for leaf_id, leaf_need in craft_leaves.items():
            leaf_best = best_stage_cost(leaf_id, matrix_by_item, stage_meta)
            if leaf_best is None:
                ok = False
                break
            total += leaf_best["ap_expect"] * leaf_need
        craft_sanity = round(total, 1) if ok else None

    return {
        "name": name,
        "item_id": item_id,
        "need": need,
        "farm": farm,
        "farm_sanity": farm_sanity,
        "craft": tree if tree["craftable"] else None,
        "craft_leaves": craft_leaves,
        "craft_sanity": craft_sanity,
    }
