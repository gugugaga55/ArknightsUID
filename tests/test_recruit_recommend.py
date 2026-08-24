"""公招组合推荐测试：阈值标签语义 + 组合枚举/分层/排序 + 未识别标签。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.gamedata.recruit import (
    build_recruit_data,
    build_tag_universe,
    match_recruit,
    recommend_combos,
)


def _table() -> dict:
    return {
        # 5★ 术师（阈值语义：资深 + 高级资深 双标签）
        "c5": {"name": "五星术师", "rarity": "TIER_5", "profession": "CASTER",
               "position": "RANGED", "tagList": ["输出"],
               "itemObtainApproach": "招募寻访"},
        # 4★ 近卫
        "c4": {"name": "四星近卫", "rarity": "TIER_4", "profession": "WARRIOR",
               "position": "MELEE", "tagList": [],
               "itemObtainApproach": "招募寻访"},
        # 3★ 先锋
        "c3": {"name": "三星先锋", "rarity": "TIER_3", "profession": "PIONEER",
               "position": "MELEE", "tagList": ["费用回复"],
               "itemObtainApproach": "招募寻访"},
        # NPC（排除）
        "cn": {"name": "NPC", "rarity": "TIER_6", "profession": "TRAP",
               "position": "NONE", "tagList": [], "itemObtainApproach": ""},
    }


def _data() -> list:
    return build_recruit_data(_table())


class ThresholdSemanticsTest(unittest.TestCase):
    def test_five_star_matches_both_senior_tags(self) -> None:
        entry = {e["name"]: e for e in _data()}["五星术师"]
        self.assertIn("资深干员", entry["all_tags"])
        self.assertIn("高级资深干员", entry["all_tags"])

    def test_four_star_senior_only(self) -> None:
        entry = {e["name"]: e for e in _data()}["四星近卫"]
        self.assertIn("资深干员", entry["all_tags"])
        self.assertNotIn("高级资深干员", entry["all_tags"])

    def test_npc_excluded(self) -> None:
        self.assertEqual(len(_data()), 3)

    def test_match_senior_includes_five_star(self) -> None:
        matched = match_recruit(["资深干员"], _data())
        self.assertEqual(
            sorted(m["name"] for m in matched), ["五星术师", "四星近卫"]
        )


class RecommendCombosTest(unittest.TestCase):
    def test_senior_plus_combo_ranks_first(self) -> None:
        result = recommend_combos(
            ["高级资深干员", "输出", "近战位"], _data()
        )
        self.assertTrue(result["combos"])
        best = result["combos"][0]
        self.assertEqual(best["tier"], 1)
        self.assertEqual(best["tier_name"], "6★ 机会")
        self.assertIn("高级资深干员", best["tags"])
        # 池内只应有 5★（6★ 名单不入表，见模块口径）
        self.assertTrue(all(m["rarity"] == 5 for m in best["matches"]))

    def test_guaranteed_floor_tiers(self) -> None:
        result = recommend_combos(["资深干员", "费用回复"], _data())
        tiers = {c["tier"] for c in result["combos"]}
        # 资深干员+费用回复 → 只剩 3★? 费用回复在 3★ 上，交集为空 → 无此组合
        names = ["+".join(c["tags"]) for c in result["combos"]]
        self.assertNotIn("资深干员+费用回复", names)
        self.assertIn(3, tiers)  # 资深干员 单标签 → 4★ 保底

    def test_empty_combo_dropped(self) -> None:
        result = recommend_combos(["高级资深干员", "费用回复"], _data())
        names = ["+".join(c["tags"]) for c in result["combos"]]
        self.assertNotIn("高级资深干员+费用回复", names)

    def test_unknown_tags_reported(self) -> None:
        result = recommend_combos(["不存在的标签", "输出"], _data())
        self.assertEqual(result["unknown"], ["不存在的标签"])
        # 已知部分仍可推荐
        self.assertTrue(result["combos"])

    def test_duplicate_tags_deduped(self) -> None:
        result = recommend_combos(["输出", "输出", "近战位"], _data())
        for combo in result["combos"]:
            self.assertEqual(len(combo["tags"]), len(set(combo["tags"])))

    def test_top_limit(self) -> None:
        result = recommend_combos(
            ["高级资深干员", "输出", "近战位", "远程位", "资深干员"], _data(),
            top=3,
        )
        self.assertLessEqual(len(result["combos"]), 3)

    def test_sorting_more_focused_first(self) -> None:
        """同档下池更集中的组合排前面。"""
        result = recommend_combos(["高级资深干员", "输出", "近战位"], _data())
        combos = [c for c in result["combos"] if c["tier"] == 1]
        sizes = [len(c["matches"]) for c in combos]
        self.assertEqual(sizes, sorted(sizes))

    def test_universe(self) -> None:
        universe = build_tag_universe(_data())
        self.assertIn("高级资深干员", universe)
        self.assertIn("费用回复", universe)
        self.assertIn("术师", universe)


if __name__ == "__main__":
    unittest.main()
