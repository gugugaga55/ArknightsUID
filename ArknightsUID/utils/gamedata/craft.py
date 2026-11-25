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

from collections import Counter
from collections.abc import Mapping
from typing import Any

from ._table_cache import ensure_building_table

# 防御：配方环（数据异常时兜底）
_MAX_DEPTH = 8


# 加工站四类配方：F_BUILDING 基建材料 / F_EVOLVE 精英化芯片 /
# F_ASC 升阶材料（T2/T3 合成主体）/ F_SKILL 技能书
_WORKSHOP_FORMULA_TYPES = {"F_BUILDING", "F_EVOLVE", "F_ASC", "F_SKILL"}


async def load_formulas() -> dict[str, dict[str, Any]]:
    """itemId → 加工站配方（一个 item 只保留一条升阶合成配方）。

    排除 F_EVOLVE 中的跨职业同阶芯片 3:2 转换配方（count=2, costs=[{count:3}]），
    否则任意芯片/芯片组会在跨职业转换环里来回递归耗损，导致理智估算爆炸。
    """
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
        ftype = formula.get("formulaType")
        if ftype not in _WORKSHOP_FORMULA_TYPES:
            continue
        output = int(formula.get("count") or 1)
        costs = formula.get("costs") or []
        if ftype == "F_EVOLVE" and (
            output == 2
            or (
                isinstance(costs, list)
                and len(costs) == 1
                and isinstance(costs[0], dict)
                and int(costs[0].get("count") or 0) == 3
            )
        ):
            # 芯片/芯片组跨职业 3 换 2 转换配方，不属于纵向升阶合成
            continue
        item_id = str(formula.get("itemId") or "")
        if item_id:
            result[item_id] = formula
    return result


def _cost_names(
    formula: dict[str, Any], item_index: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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
    ancestors: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """合成树：need 件 item_id 的完整合成路径（含递归子材料与防环检测）。

    子材料需求按制作次数缩放：crafts = ceil(need / output)，
    子节点 need = 配方成本 × crafts。不可合成（无 WORKSHOP 配方或成环）时
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
    if not formula or depth >= _MAX_DEPTH or item_id in ancestors:
        return node
    raw_costs = _cost_names(formula, item_index)
    if any(c["item_id"] in ancestors or c["item_id"] == item_id for c in raw_costs):
        return node
    output = max(1, int(formula.get("count") or 1))
    crafts = max(1, ceil(need / output))
    node["craftable"] = True
    node["output"] = output
    node["gold"] = int(formula.get("goldCost") or 0) * crafts
    node["costs"] = raw_costs
    next_ancestors = ancestors | {item_id}
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
                next_ancestors,
            )
        )
    return node


def leaf_materials(node: dict[str, Any]) -> dict[str, int]:
    """聚合合成树的叶子材料（不可再合成的底层材料）需求量。"""
    leaves: dict[str, int] = {}

    def walk(current: dict[str, Any]) -> None:
        if not current.get("craftable"):
            leaves[current["item_id"]] = leaves.get(current["item_id"], 0) + int(
                current.get("need") or 0
            )
            return
        for child in current.get("children") or []:
            walk(child)

    walk(node)
    return leaves


def format_craft_tree(
    node: dict[str, Any], item_index: dict[str, dict[str, Any]]
) -> list[str]:
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
            lines.append(
                f"{indent}{mark}{current['name']} ×{current.get('need')}（刷取）"
            )
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

    stats = item_drop_stats(item_id, matrix_by_item, stage_meta, top=None)
    if not stats:
        return None
    best = min(stats, key=lambda row: row["ap_expect"])
    return best


def _craft_inventory_gap(
    item_id: str, need: int, formulas: dict[str, dict[str, Any]],
    inventory: Mapping[str, int],
) -> dict[str, int]:
    """强制合成成品，递归优先消耗已有子材料，并保留批次余量。"""
    stock = Counter(inventory)
    missing: Counter[str] = Counter()
    stock.pop(item_id, None)

    def consume(target: str, count: int, ancestors: frozenset[str]) -> None:
        used = min(count, max(0, stock[target]))
        stock[target] -= used
        count -= used
        if not count:
            return
        formula = formulas.get(target) or {}
        costs = formula.get("costs") or []
        if (
            not costs or target in ancestors or len(ancestors) >= _MAX_DEPTH
            or any(str(cost.get("id")) in ancestors | {target} for cost in costs)
        ):
            missing[target] += count
            return
        output = max(1, int(formula.get("count") or 1))
        batches = (count + output - 1) // output
        for cost in costs:
            cost_id = str(cost.get("id") or "")
            cost_count = int(cost.get("count") or 0)
            if cost_id and cost_count > 0:
                consume(cost_id, cost_count * batches, ancestors | {target})
        stock[target] += batches * output - count

    consume(item_id, need, frozenset())
    return dict(missing)


def craft_vs_farm(
    item_id: str,
    need: int,
    formulas: dict[str, dict[str, Any]],
    item_index: dict[str, dict[str, Any]],
    matrix_by_item: dict[str, list[dict[str, Any]]],
    stage_meta: dict[str, dict[str, Any]],
    *,
    inventory: Mapping[str, int] | None = None,
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
    stock_craftable: int | None = None
    craft_missing_rows = []
    if tree["craftable"]:
        if inventory is None:
            craft_leaves = leaf_materials(tree)
        else:
            craft_leaves = _craft_inventory_gap(item_id, need, formulas, inventory)
            if not craft_leaves:
                stock_craftable = need
            else:
                # 找出无需额外刷取的最大合成量，批次和重复子材料按同一口径扣减。
                lo, hi = 0, need
                while lo + 1 < hi:
                    mid = (lo + hi) // 2
                    if _craft_inventory_gap(item_id, mid, formulas, inventory):
                        hi = mid
                    else:
                        lo = mid
                stock_craftable = lo
        total = 0.0
        ok = True
        for leaf_id, leaf_need in craft_leaves.items():
            leaf_best = best_stage_cost(leaf_id, matrix_by_item, stage_meta)
            craft_missing_rows.append({
                "name": str((item_index.get(leaf_id) or {}).get("name") or leaf_id),
                "count": leaf_need,
                "stage": leaf_best["stage_code"] if leaf_best else "",
            })
            if leaf_best is None:
                ok = False
            else:
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
        "stock_craftable": stock_craftable,
        "craft_missing_rows": craft_missing_rows,
    }
