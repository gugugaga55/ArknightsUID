"""寻访模拟测试：概率口径 + 十连保底 + 池构建 + 卡片上下文/文案。"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.gamedata.gacha_sim import (
    POOL_RARITIES,
    RARITY_WEIGHTS,
    build_draw_pools,
    format_pulls_text,
    roll_rarity,
    roll_rarity_at_least,
    simulate_pulls,
    summarize_pulls,
)


def _pools() -> dict:
    index = {
        "char_a": {"name": "三星A", "rarity": 3, "profession": "PIONEER"},
        "char_b": {"name": "四星B", "rarity": 4, "profession": "MEDIC"},
        "char_c": {"name": "五星C", "rarity": 5, "profession": "WARRIOR"},
        "char_d": {"name": "六星D", "rarity": 6, "profession": "CASTER"},
        "char_e": {"name": "二星E", "rarity": 2, "profession": "SNIPER"},
        "char_f": {"name": "无稀有度F", "rarity": 0, "profession": "SPECIAL"},
    }
    return build_draw_pools(index)


class RollRarityTest(unittest.TestCase):
    def test_weights_match_official_rates(self) -> None:
        self.assertEqual(dict(RARITY_WEIGHTS), {3: 40, 4: 50, 5: 8, 6: 2})

    def test_distribution_roughly_matches(self) -> None:
        rng = random.Random(42)
        n = 20000
        counts = {r: 0 for r in POOL_RARITIES}
        for _ in range(n):
            counts[roll_rarity(rng)] += 1
        # 3★ 40% ±2% / 6★ 2% ±1%（seeded，稳定）
        self.assertAlmostEqual(counts[3] / n, 0.40, delta=0.02)
        self.assertAlmostEqual(counts[6] / n, 0.02, delta=0.01)

    def test_at_least_floor_excludes_low(self) -> None:
        rng = random.Random(7)
        for _ in range(500):
            self.assertGreaterEqual(roll_rarity_at_least(rng, 4), 4)


class BuildPoolsTest(unittest.TestCase):
    def test_only_pool_rarities_included(self) -> None:
        pools = _pools()
        self.assertEqual(set(pools), set(POOL_RARITIES))
        self.assertNotIn(2, pools)  # 2★ 不入池
        self.assertEqual(pools[6][0]["name"], "六星D")


class SimulatePullsTest(unittest.TestCase):
    def test_count_clamped(self) -> None:
        pools = _pools()
        self.assertEqual(len(simulate_pulls(pools, 99, random.Random(1))), 10)
        self.assertEqual(len(simulate_pulls(pools, 0, random.Random(1))), 1)

    def test_guarantee_upgrades_last_when_all_three_star(self) -> None:
        """十连保底：前 9 抽全 3★ → 第 10 抽必 4★+ 且带 guarantee 标记。"""
        pools = _pools()
        # 用只出 3★ 的伪 RNG 触发保底路径
        class ThreeStarRNG(random.Random):
            def choices(self, population, weights=None, k=1):
                # roll_rarity 传全档 (3,4,5,6) → 恒 3★；
                # roll_rarity_at_least 传保底档 (4,5,6) → 取最高
                return [3] if 3 in population else [max(population)]

            def choice(self, seq):
                return seq[0]

        results = simulate_pulls(pools, 10, ThreeStarRNG(1))
        self.assertTrue(all(r["rarity"] == 3 for r in results[:9]))
        self.assertGreaterEqual(results[9]["rarity"], 4)
        self.assertTrue(results[9].get("guarantee"))

    def test_no_guarantee_when_four_star_present(self) -> None:
        pools = _pools()
        rng = random.Random(2024)
        for _ in range(50):
            results = simulate_pulls(pools, 10, rng)
            if any(r["rarity"] >= 4 for r in results[:9]):
                self.assertFalse(
                    any(r.get("guarantee") for r in results),
                    "前 9 抽已有 4★+ 时不应触发保底标记",
                )

    def test_single_pull_never_guarantee(self) -> None:
        pools = _pools()
        results = simulate_pulls(pools, 1, random.Random(3))
        self.assertFalse(results[0].get("guarantee"))

    def test_empty_pool_fallback(self) -> None:
        pools = {r: [] for r in POOL_RARITIES}
        results = simulate_pulls(pools, 3, random.Random(1))
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["name"], "?")


class SummarizeAndTextTest(unittest.TestCase):
    def test_summarize_counts(self) -> None:
        summary = summarize_pulls(
            [{"rarity": 6}, {"rarity": 6}, {"rarity": 3}, {"rarity": 4}]
        )
        self.assertEqual(summary, {3: 1, 4: 1, 5: 0, 6: 2})

    def test_format_text_marks_guarantee(self) -> None:
        text = format_pulls_text(
            [
                {"rarity": 4, "name": "梅", "guarantee": True},
                {"rarity": 3, "name": "芬"},
            ]
        )
        self.assertIn("梅（保底）", text)
        self.assertIn("芬", text)


class SimContextTest(unittest.TestCase):
    def test_context_maps_profession_names(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _simulate_context

        context = _simulate_context(
            [
                {
                    "rarity": 6,
                    "name": "银灰",
                    "char_id": "char_172_svrash",
                    "profession": "WARRIOR",
                }
            ],
            {3: 0, 4: 0, 5: 0, 6: 1},
            1,
        )
        self.assertEqual(context["results"][0]["profession"], "近卫")
        self.assertEqual(context["summary"][6], 1)


if __name__ == "__main__":
    unittest.main()
