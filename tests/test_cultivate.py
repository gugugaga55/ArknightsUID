"""养成计算测试（utils/cultivate）：成本解析、剩余需求、缺口汇总、完整链路 mock。"""
from __future__ import annotations

import unittest
from typing import Any

from ArknightsUID.utils.cultivate import (
    CultivateCost,
    _owned_items,
    build_remaining_need,
    parse_cultivate_cost,
    summarize_gap,
)
from ArknightsUID.utils.player.parse import CharEntry, SkillState, EquipState


def _cal_char_envelope() -> dict[str, Any]:
    return {
        "code": 0,
        "data": {
            "evolvePhaseCost": [
                {"items": [{"id": "PIONEER_2", "count": 4}, {"id": "4001", "count": 30000}]},
                {"items": [{"id": "PIONEER_4", "count": 4}, {"id": "4001", "count": 180000}]},
            ],
            "mainSkillLevelCost": [
                {"items": [{"id": "2001", "count": 2}]},  # Lv1
                {"items": [{"id": "2001", "count": 4}]},  # Lv2
                {"items": [{"id": "2002", "count": 3}]},  # Lv3
                {"items": [{"id": "2002", "count": 4}]},  # Lv4
                {"items": [{"id": "2003", "count": 2}]},  # Lv5
                {"items": [{"id": "2003", "count": 4}]},  # Lv6
                {"items": [{"id": "3003", "count": 2}]},  # Lv7
            ],
            "skillCost": [
                {
                    "name": "技能A",
                    "levelCost": [
                        {"items": [{"id": "book1", "count": 1}]} for _ in range(7)
                    ] + [
                        {"items": [{"id": "3003", "count": 2}]},  # 专1
                        {"items": [{"id": "3003", "count": 4}]},  # 专2
                        {"items": [{"id": "3004", "count": 3}]},  # 专3
                    ],
                },
            ],
            "equipCost": [
                {
                    "typeName": "X",
                    "levelCost": [
                        {"items": [{"id": "mod_x_mat", "count": 2}]},
                        {"items": [{"id": "mod_x_mat", "count": 4}]},
                        {"items": [{"id": "mod_x_mat", "count": 6}]},
                    ],
                },
            ],
        },
    }


def _char(
    evolve_phase: int = 1,
    main_skill_lvl: int = 5,
    specializes: list[int] | None = None,
    equip_levels: list[int] | None = None,
) -> CharEntry:
    return CharEntry(
        char_id="char_test",
        level=50,
        evolve_phase=evolve_phase,
        main_skill_lvl=main_skill_lvl,
        potential_rank=0,
        skills=[SkillState("sk1", sp) for sp in (specializes or [1])],
        equips=[EquipState("uniequip_001", lv) for lv in (equip_levels or [1])],
        name="测试干员",
        profession="WARRIOR",
        rarity=4,
    )


class ParseCultivateTest(unittest.TestCase):
    def test_parse_cost_sections(self) -> None:
        cost = parse_cultivate_cost(_cal_char_envelope())
        self.assertIsNotNone(cost)
        assert cost is not None
        self.assertEqual(len(cost.evolve), 2)
        self.assertEqual(cost.evolve[0][0], {"id": "PIONEER_2", "count": 4})
        self.assertEqual(len(cost.main_skill), 7)
        self.assertEqual(len(cost.skills), 1)
        self.assertEqual(len(cost.skills[0]["level_cost"]), 10)  # 7 级 + 3 专精
        self.assertEqual(len(cost.equips), 1)

    def test_parse_rejects_invalid(self) -> None:
        self.assertIsNone(parse_cultivate_cost(None))
        self.assertIsNone(parse_cultivate_cost({"code": 10002}))

    def test_owned_items_dict_and_list_forms(self) -> None:
        dict_form = {"data": {"player_item_data": {"3003": 120, "4001": "50000"}}}
        self.assertEqual(_owned_items(dict_form), {"3003": 120, "4001": 50000})
        list_form = {
            "data": {
                "player_item_data": [
                    {"id": "3003", "count": 120},
                    {"itemId": "3004", "count": 7},
                ]
            }
        }
        self.assertEqual(_owned_items(list_form), {"3003": 120, "3004": 7})
        self.assertEqual(_owned_items({"data": {}}), {})


class RemainingNeedTest(unittest.TestCase):
    def test_full_remaining_from_mid_progress(self) -> None:
        cost = parse_cultivate_cost(_cal_char_envelope())
        assert cost is not None
        # 精1/技能5/专1/模组1 → 剩：精2 段 + Lv6-7 + 专2-3 + 模组 2-3 级
        need, warnings = build_remaining_need(cost, _char())
        self.assertEqual(warnings, [])
        self.assertEqual(need["PIONEER_4"], 4)  # 精2 段
        self.assertNotIn("PIONEER_2", need)  # 精1 已过
        self.assertEqual(need["2003"], 4)  # Lv6 4（Lv5 已过，Lv7 无 2003）
        self.assertEqual(need["3003"], 2 + 4)  # Lv7 2 + 专2 4（专1 已过）
        self.assertEqual(need["3004"], 3)  # 专3
        self.assertEqual(need["mod_x_mat"], 4 + 6)  # 模组 2、3 级
        self.assertEqual(need["4001"], 180000)  # 只剩精2 段金币

    def test_fresh_char_takes_everything(self) -> None:
        cost = parse_cultivate_cost(_cal_char_envelope())
        assert cost is not None
        need, _ = build_remaining_need(cost, _char(0, 0, [0], [0]))
        self.assertEqual(need["PIONEER_2"], 4)
        self.assertEqual(need["2001"], 6)
        self.assertEqual(need["3003"], 2 + 2 + 4)
        self.assertEqual(need["mod_x_mat"], 12)

    def test_graduated_char_needs_nothing(self) -> None:
        cost = parse_cultivate_cost(_cal_char_envelope())
        assert cost is not None
        need, _ = build_remaining_need(cost, _char(2, 7, [3], [3]))
        self.assertEqual(need, {})

    def test_skill_position_mismatch_warns(self) -> None:
        cost = parse_cultivate_cost(_cal_char_envelope())
        assert cost is not None
        # 玩家侧 2 个技能 vs 接口 1 个技能成本 → 位置对位不全
        char = CharEntry(
            char_id="char_test",
            skills=[SkillState("sk1", 1), SkillState("sk2", 2)],
            equips=[],
            main_skill_lvl=5,
            evolve_phase=1,
        )
        _, warnings = build_remaining_need(cost, char)
        self.assertTrue(any("对位" in w for w in warnings))


class SummarizeGapTest(unittest.TestCase):
    def _index(self) -> dict[str, dict[str, Any]]:
        return {
            "3003": {"name": "固源岩", "rarity": "TIER_3", "sort": 30},
            "3004": {"name": "固源岩组", "rarity": "TIER_4", "sort": 40},
            "2003": {"name": "中级作战记录", "rarity": "TIER_2", "sort": 20},
        }

    def test_gap_separates_gold_exp_and_materials(self) -> None:
        gap = summarize_gap(
            need={"3003": 10, "3004": 5, "4001": 200000, "2003": 8},
            owned={"3003": 120, "3004": 1, "4001": 50000, "2003": 8},
            item_index=self._index(),
        )
        # 3003 仓库充足 → 不进缺口
        names = [row["name"] for row in gap["materials"]]
        self.assertEqual(names, ["固源岩组"])
        self.assertEqual(gap["materials"][0]["lack"], 4)
        self.assertEqual(gap["gold_lack"], 150000)
        # 经验书充足 → 不出现
        self.assertEqual(gap["exp_books"], [])

    def test_gap_unknown_items_use_id_as_name(self) -> None:
        gap = summarize_gap(need={"9999": 3}, owned={}, item_index={})
        self.assertEqual(gap["materials"][0]["name"], "9999")
        self.assertEqual(gap["materials"][0]["lack"], 3)

    def test_rarity_display_and_sort(self) -> None:
        gap = summarize_gap(
            need={"3003": 5, "3004": 5}, owned={}, item_index=self._index()
        )
        self.assertEqual(gap["materials"][0]["name"], "固源岩组")  # TIER_4 在前
        self.assertEqual(gap["materials"][0]["rarity"], "4★")


if __name__ == "__main__":
    unittest.main()
