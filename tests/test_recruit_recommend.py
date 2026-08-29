"""公招组合推荐测试：稀有度标签语义（资深=5★ / 高资=6★）+ 组合枚举/分层/排序。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.gamedata.recruit import (
    build_tag_universe,
    entries_from_smw,
    match_recruit,
    recommend_combos,
)


def _data() -> list:
    return entries_from_smw(
        {
            "六星术师": {"printouts": {
                "稀有度": ["5"], "职业": ["术师"], "位置": ["远程位"],
                "标签": ["输出"]}},
            "五星术师": {"printouts": {
                "稀有度": ["4"], "职业": ["术师"], "位置": ["远程位"],
                "标签": ["输出"]}},
            "四星近卫": {"printouts": {
                "稀有度": ["3"], "职业": ["近卫"], "位置": ["近战位"],
                "标签": []}},
            "三星先锋": {"printouts": {
                "稀有度": ["2"], "职业": ["先锋"], "位置": ["近战位"],
                "标签": ["费用回复"]}},
        }
    )


class RarityTagSemanticsTest(unittest.TestCase):
    def test_six_star_senior_plus_only(self) -> None:
        entry = {e["name"]: e for e in _data()}["六星术师"]
        self.assertIn("高级资深干员", entry["all_tags"])
        self.assertNotIn("资深干员", entry["all_tags"])

    def test_five_star_senior_only(self) -> None:
        entry = {e["name"]: e for e in _data()}["五星术师"]
        self.assertIn("资深干员", entry["all_tags"])
        self.assertNotIn("高级资深干员", entry["all_tags"])

    def test_senior_plus_pool_is_six_star_only(self) -> None:
        matched = match_recruit(["高级资深干员"], _data())
        self.assertEqual([m["name"] for m in matched], ["六星术师"])

    def test_senior_pool_is_five_star_only(self) -> None:
        matched = match_recruit(["资深干员"], _data())
        self.assertEqual([m["name"] for m in matched], ["五星术师"])

    def test_six_star_excluded_without_senior_plus(self) -> None:
        # 游戏机制：未选高资时 6★ 不入池（单查「输出」不应列出 6★）
        matched = match_recruit(["输出"], _data())
        self.assertEqual([m["name"] for m in matched], ["五星术师"])
        # 5★ 在常规池，不要求资深标签
        self.assertEqual(matched[0]["rarity"], 5)


class RecommendCombosTest(unittest.TestCase):
    def test_senior_plus_combo_ranks_first(self) -> None:
        result = recommend_combos(
            ["高级资深干员", "输出", "近战位"], _data()
        )
        self.assertTrue(result["combos"])
        best = result["combos"][0]
        self.assertEqual(best["tier"], 1)
        self.assertEqual(best["tier_name"], "6★ 保底")
        self.assertIn("高级资深干员", best["tags"])
        # 池内只应有 6★
        self.assertTrue(all(m["rarity"] == 6 for m in best["matches"]))

    def test_senior_combo_five_star_floor(self) -> None:
        result = recommend_combos(["资深干员", "输出"], _data())
        combo = next(
            c for c in result["combos"] if c["tags"] == ["资深干员", "输出"]
        )
        self.assertEqual(combo["tier"], 2)
        self.assertEqual(combo["tier_name"], "5★ 保底")
        self.assertEqual(combo["match_names"], ["五星术师"])
        # 资深 单标签 → 5★ 保底（池 = 5★）
        single = next(
            c for c in result["combos"] if c["tags"] == ["资深干员"]
        )
        self.assertEqual(single["tier"], 2)

    def test_empty_combo_dropped(self) -> None:
        result = recommend_combos(["高级资深干员", "费用回复"], _data())
        names = ["+".join(c["tags"]) for c in result["combos"]]
        # 费用回复仅 3★ 持有，与高资交集为空 → 组合剔除
        self.assertNotIn("高级资深干员+费用回复", names)
        self.assertIn("高级资深干员", names)  # 单标签仍在

    def test_unknown_tags_reported(self) -> None:
        result = recommend_combos(["不存在的标签", "输出"], _data())
        self.assertEqual(result["unknown"], ["不存在的标签"])
        self.assertTrue(result["combos"])

    def test_duplicate_tags_deduped(self) -> None:
        result = recommend_combos(["输出", "输出", "远程位"], _data())
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
        result = recommend_combos(["高级资深干员", "输出", "术师"], _data())
        combos = [c for c in result["combos"] if c["tier"] == 1]
        sizes = [len(c["matches"]) for c in combos]
        self.assertEqual(sizes, sorted(sizes))

    def test_universe(self) -> None:
        universe = build_tag_universe(_data())
        self.assertIn("高级资深干员", universe)
        self.assertIn("资深干员", universe)
        self.assertIn("费用回复", universe)
        self.assertIn("术师", universe)


if __name__ == "__main__":
    unittest.main()
