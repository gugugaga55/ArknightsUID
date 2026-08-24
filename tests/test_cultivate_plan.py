"""多干员合并养成测试：merge_cultivation_results + 名单切分 + 刷图建议文案。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.cultivate import merge_cultivation_results


def _char(name: str, rarity: int = 6):
    class _C:
        display_name = name
        rarity_display = rarity

    return _C()


def _result(name: str, materials: list[dict], exp_books=None, gold=0) -> dict:
    return {
        "char": _char(name),
        "gap": {
            "materials": materials,
            "exp_books": exp_books or [],
            "gold_lack": gold,
        },
        "current": {"evolve_phase": 2, "main_skill_lvl": 7, "masteries": [1, 0, 0]},
        "warnings": [f"{name} 的技能顺序对位不全"] if name == "42" else [],
    }


class MergeCultivationTest(unittest.TestCase):
    def test_same_material_needs_sum_lack_recomputed(self) -> None:
        """同材料合并：need 相加、owned 只扣一次。"""
        merged = merge_cultivation_results(
            [
                _result(
                    "银灰",
                    [{"id": "2004", "name": "D32钢", "rarity": "5★",
                      "need": 4, "owned": 2, "lack": 2}],
                ),
                _result(
                    "史尔特尔",
                    [{"id": "2004", "name": "D32钢", "rarity": "5★",
                      "need": 6, "owned": 2, "lack": 4}],
                ),
            ]
        )
        row = merged["materials"][0]
        self.assertEqual(row["need"], 10)
        self.assertEqual(row["owned"], 2)
        self.assertEqual(row["lack"], 8)  # 10 - 2（不是两段 lack 相加的 6）

    def test_gold_and_exp_sum(self) -> None:
        merged = merge_cultivation_results(
            [
                _result("A", [], gold=100000,
                        exp_books=[{"id": "2001", "name": "基础作战记录",
                                    "rarity": "", "need": 10, "owned": 4, "lack": 6}]),
                _result("B", [], gold=386000,
                        exp_books=[{"id": "2001", "name": "基础作战记录",
                                    "rarity": "", "need": 20, "owned": 4, "lack": 16}]),
            ]
        )
        self.assertEqual(merged["gold_lack"], 486000)
        self.assertEqual(merged["exp_books"][0]["need"], 30)
        self.assertEqual(merged["exp_books"][0]["lack"], 26)

    def test_no_lack_rows_filtered(self) -> None:
        merged = merge_cultivation_results(
            [
                _result(
                    "A",
                    [
                        {"id": "2004", "name": "D32钢", "rarity": "5★",
                         "need": 1, "owned": 9, "lack": 0},
                        {"id": "3003", "name": "聚酸酯组", "rarity": "3★",
                         "need": 12, "owned": 5, "lack": 7},
                    ],
                ),
            ]
        )
        self.assertEqual(merged["materials_total"], 1)
        self.assertEqual(merged["materials"][0]["name"], "聚酸酯组")

    def test_chars_list_and_warnings_kept(self) -> None:
        merged = merge_cultivation_results(
            [_result("银灰", []), _result("42", [])]
        )
        self.assertEqual(
            [c["name"] for c in merged["chars"]], ["银灰", "42"]
        )
        self.assertEqual(len(merged["warnings"]), 1)
        self.assertIn("42", merged["warnings"][0])

    def test_rarity_desc_sort(self) -> None:
        merged = merge_cultivation_results(
            [
                _result(
                    "A",
                    [
                        {"id": "3003", "name": "聚酸酯组", "rarity": "3★",
                         "need": 5, "owned": 0, "lack": 5},
                        {"id": "2004", "name": "D32钢", "rarity": "5★",
                         "need": 5, "owned": 0, "lack": 5},
                    ],
                ),
            ]
        )
        self.assertEqual(merged["materials"][0]["name"], "D32钢")


class PlanHelpersTest(unittest.TestCase):
    def test_split_names(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _split_plan_names

        self.assertEqual(
            _split_plan_names("银灰 史尔特尔、42，w"),
            ["银灰", "史尔特尔", "42", "w"],
        )
        self.assertEqual(_split_plan_names(""), [])
        self.assertEqual(_split_plan_names("银灰 银灰"), ["银灰"])

    def test_farm_advice_direct_vs_craft(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _farm_advice_text

        advice = _farm_advice_text(
            {
                "farm": {"stage_code": "1-7", "rate": 1.1, "ap_expect": 18.2},
                "farm_sanity": 165.5,
                "craft_sanity": 140.0,
                "need": 10,
            }
        )
        self.assertIn("直刷 1-7", advice)
        self.assertIn("合成更省", advice)
        self.assertIn("把", advice)

    def test_farm_advice_craft_only(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _farm_advice_text

        advice = _farm_advice_text(
            {"farm": None, "farm_sanity": None, "craft_sanity": 980.0, "need": 5}
        )
        self.assertIn("不可直刷", advice)
        self.assertIn("980", advice)

    def test_farm_advice_no_data(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _farm_advice_text

        self.assertEqual(
            _farm_advice_text(
                {"farm": None, "farm_sanity": None, "craft_sanity": None, "need": 1}
            ),
            "无直刷/合成数据",
        )


if __name__ == "__main__":
    unittest.main()
