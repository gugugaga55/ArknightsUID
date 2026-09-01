"""多干员合并养成测试：merge_cultivation_results + 名字切分 + 刷图建议文案。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.cultivate import (
    GOLD_ITEM_ID,
    merge_cultivation_results,
    summarize_gap,
)


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


class SharedInventoryMergeTest(unittest.TestCase):
    """审计 #8：多干员共享库存漏算——单干员各自无缺口 ≠ 合并后无缺口。"""

    @staticmethod
    def _keep_zero_result(name: str, need: dict, owned: dict) -> dict:
        """走真实 summarize_gap(keep_zero=True)，与 ark养成计划 同链路。"""
        gap = summarize_gap(need, owned, item_index={}, keep_zero=True)
        return {
            "char": _char(name),
            "gap": gap,
            "current": {},
            "warnings": [],
        }

    def test_shared_material_two_operators_need_8_own_10(self) -> None:
        """两个各需 8、仓库 10：单看都无缺口，合并真实缺口 6。"""
        merged = merge_cultivation_results(
            [
                self._keep_zero_result(
                    "A", {"3003": 8, GOLD_ITEM_ID: 0}, {"3003": 10}
                ),
                self._keep_zero_result(
                    "B", {"3003": 8, GOLD_ITEM_ID: 0}, {"3003": 10}
                ),
            ]
        )
        # 旧逻辑：两行 lack=0 在 summarize 阶段被丢 → 合并桶空 → 误报无缺口
        self.assertEqual(merged["materials_total"], 1)
        row = merged["materials"][0]
        self.assertEqual(row["need"], 16)
        self.assertEqual(row["owned"], 10)
        self.assertEqual(row["lack"], 6)

    def test_shared_gold_summed_before_deduction(self) -> None:
        """龙门币：gold_need 求和后统一扣库存（不能累加各自折算值）。"""
        merged = merge_cultivation_results(
            [
                self._keep_zero_result(
                    "A", {GOLD_ITEM_ID: 10000}, {GOLD_ITEM_ID: 12000}
                ),
                self._keep_zero_result(
                    "B", {GOLD_ITEM_ID: 5000}, {GOLD_ITEM_ID: 12000}
                ),
            ]
        )
        # 旧逻辑：各自 gold_lack=0 相加 → 误报 0；真实 15000-12000=3000
        self.assertEqual(merged["gold_lack"], 3000)

    def test_partial_gap_operator_still_counts(self) -> None:
        """A 有缺口 B 无缺口：B 的需求并入后缺口扩大（跨行抵消不被掩盖）。"""
        merged = merge_cultivation_results(
            [
                self._keep_zero_result(
                    "A", {"3003": 12, GOLD_ITEM_ID: 0}, {"3003": 2}
                ),
                self._keep_zero_result(
                    "B", {"3003": 8, GOLD_ITEM_ID: 0}, {"3003": 2}
                ),
            ]
        )
        row = merged["materials"][0]
        self.assertEqual(row["need"], 20)
        self.assertEqual(row["lack"], 18)  # 旧逻辑只显示 A 的 10

    def test_keep_zero_mode_keeps_rows_and_raw_gold(self) -> None:
        """keep_zero 模式契约：零缺口行保留、不截断、带原始 gold 量纲。"""
        need = {GOLD_ITEM_ID: 5, "2004": 8}
        need.update({f"99{i}": 1 for i in range(20)})  # 超 max_rows 也不截断
        gap = summarize_gap(
            need, {GOLD_ITEM_ID: 10, "2004": 10}, item_index={}, keep_zero=True
        )
        self.assertEqual(len(gap["materials"]), 20)  # 20 个 99x 全保留（无截断）
        self.assertEqual(len(gap["exp_books"]), 1)   # 2004（经验书）零缺口行也在
        self.assertEqual(gap["gold_lack"], 0)  # 折算口径保持
        self.assertEqual(gap["gold_need"], 5)
        self.assertEqual(gap["gold_owned"], 10)
        # 默认模式（单干员展示）：零缺口行依旧过滤（2004/gold 不出现）
        plain = summarize_gap(
            need, {GOLD_ITEM_ID: 10, "2004": 10}, item_index={}
        )
        self.assertEqual(plain["exp_books"], [])
        self.assertEqual(plain["gold_lack"], 0)
        self.assertEqual(len(plain["materials"]), 12)  # 99x 有缺口，仅截断到 12


class PlanHelpersTest(unittest.TestCase):
    def test_split_names(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _split_plan_names

        self.assertEqual(
            _split_plan_names("银灰 史尔特尔、42，w"),
            ["银灰", "史尔特尔", "42", "w"],
        )
        self.assertEqual(_split_plan_names(""), [])
        self.assertEqual(_split_plan_names("银灰 银灰"), ["银灰"])

    def test_split_index_numeric_alias_not_hijacked(self) -> None:
        """审计 #39：数字干员别名（42=史尔特尔）不得被当账号序号劫持。

        帮助文案明示支持「ark养成 42」「ark养成计划 银灰 史尔特尔 42」，
        旧逻辑按 index=42 解析必然报"未找到第 42 个明日方舟账号"。
        """
        from ArknightsUID.arknightsuid_cultivate import _split_index

        # 两位裸数字：干员名，不是序号
        self.assertEqual(_split_index("42"), ("42", 0))
        self.assertEqual(_split_index("银灰 42"), ("银灰 42", 0))
        self.assertEqual(
            _split_index("银灰 史尔特尔 42"), ("银灰 史尔特尔 42", 0)
        )
        # 一位数字 / 显式「第 N / N号」标记：账号序号
        self.assertEqual(_split_index("银灰 2"), ("银灰", 2))
        self.assertEqual(_split_index("银灰 3号"), ("银灰", 3))
        self.assertEqual(_split_index("银灰 第42号"), ("银灰", 42))
        # 无尾部数字 / UID（5 位以上）/ 空输入
        self.assertEqual(_split_index("银灰"), ("银灰", 0))
        self.assertEqual(_split_index("123456789"), ("123456789", 0))
        self.assertEqual(_split_index(""), ("", 0))

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
