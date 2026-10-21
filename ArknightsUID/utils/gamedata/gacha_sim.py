"""寻访模拟（ark十连 / ark单抽）：基础概率 + 新池首十抽五星保底的纯本地抽卡模拟。

概率口径（官方寻访规则，客观游戏事实）：
- 单抽稀有度：3★ 40% / 4★ 50% / 5★ 8% / 6★ 2%
- 每次模拟视为新卡池的首轮：前 10 抽至少一名 5★+。
  前 9 抽未得 5★+ 时，最后一抽的 3/4★ 结果提升为 5★；6★ 仍按基础概率。
- 寻访池最低 3★（1-2★ 干员不在池内）

干员池：干员索引（character_table 精简缓存）按稀有度分组随机；
这是「全干员池」模拟，不区分常驻/限定/中坚——模拟仅供娱乐，
卡面文案注明与真实卡池无关、不累计垫刀。
"""

from __future__ import annotations

import random
from typing import Any

# (稀有度, 权重)；权重即官方百分比
RARITY_WEIGHTS: tuple[tuple[int, int], ...] = (
    (3, 40),
    (4, 50),
    (5, 8),
    (6, 2),
)
FIRST_TEN_GUARANTEE = 5  # 新池前十抽至少一名五星及以上
POOL_RARITIES = (3, 4, 5, 6)


def roll_rarity(rng: random.Random) -> int:
    """按官方权重抽稀有度。"""
    rarities, weights = zip(*RARITY_WEIGHTS)
    return rng.choices(rarities, weights=weights, k=1)[0]


def build_draw_pools(
    char_index: dict[str, dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """干员索引 → {稀有度: [干员条目]}（仅 3-6 星入池）。"""
    pools: dict[int, list[dict[str, Any]]] = {r: [] for r in POOL_RARITIES}
    for char_id, info in char_index.items():
        try:
            rarity = int(info.get("rarity") or 0)
        except (TypeError, ValueError):
            continue
        if rarity in pools:
            pools[rarity].append(
                {
                    "char_id": str(char_id),
                    "name": str(info.get("name") or char_id),
                    "profession": str(info.get("profession") or ""),
                }
            )
    return pools


def simulate_pulls(
    pools: dict[int, list[dict[str, Any]]],
    count: int,
    rng: random.Random,
    guarantee: bool = True,
) -> list[dict[str, Any]]:
    """模拟 count 连抽（count<=10）；首十抽五星保底按前 9 抽判定。

    返回逐抽结果 [{rarity, char_id, name, profession}]，
    最后一抽带 guarantee=True 标记（若保底触发）。
    """
    count = max(1, min(int(count), 10))
    results: list[dict[str, Any]] = []
    for i in range(count):
        is_last = i == count - 1
        need_guarantee = (
            guarantee
            and is_last
            and count == 10
            and all(r["rarity"] < FIRST_TEN_GUARANTEE for r in results)
        )
        rarity = roll_rarity(rng)
        forced = need_guarantee and rarity < FIRST_TEN_GUARANTEE
        if forced:
            rarity = FIRST_TEN_GUARANTEE
        pool = pools.get(rarity) or []
        if not pool:
            # 稀有度池缺失（索引异常）时降级到任意非空池，保证模拟可出结果
            for fallback_rarity in POOL_RARITIES:
                if pools.get(fallback_rarity):
                    rarity = fallback_rarity
                    pool = pools[rarity]
                    forced = False
                    break
        if not pool:
            results.append(
                {"rarity": rarity, "char_id": "", "name": "?", "profession": ""}
            )
            continue
        char = rng.choice(pool)
        results.append(
            {
                "rarity": rarity,
                "char_id": char["char_id"],
                "name": char["name"],
                "profession": char["profession"],
                "guarantee": bool(forced),
            }
        )
    return results


def summarize_pulls(results: list[dict[str, Any]]) -> dict[str, int]:
    """按稀有度计数。"""
    summary = {r: 0 for r in POOL_RARITIES}
    for row in results:
        rarity = int(row.get("rarity") or 0)
        if rarity in summary:
            summary[rarity] += 1
    return summary


def format_pulls_text(results: list[dict[str, Any]]) -> str:
    lines = [
        f"{'★' * row['rarity']} {row['name']}"
        + ("（保底）" if row.get("guarantee") else "")
        for row in results
    ]
    return "\n".join(lines)
