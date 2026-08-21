"""ark面板：条件解析 + 游戏表数值计算（插值/潜能/好感/模组/天赋档）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.arknightsuid_wiki import _parse_panel_query
from ArknightsUID.utils.gamedata.oppanel import (
    compute_panel,
    favor_modifiers,
    find_module,
    interpolate,
    module_choices,
    module_stat_modifiers,
    potential_modifiers,
    talents_at,
)

# 银灰精2 关键帧实测值（character_table）
SVRASH_FRAMES = [
    {"level": 1, "data": {"maxHp": 2022, "atk": 577, "def": 329,
                          "magicResistance": 10.0, "cost": 20, "blockCnt": 2}},
    {"level": 90, "data": {"maxHp": 2560, "atk": 713, "def": 397,
                           "magicResistance": 10.0, "cost": 20, "blockCnt": 2}},
]


class ParsePanelQueryTest(unittest.TestCase):
    def test_full_conditions(self) -> None:
        config = _parse_panel_query("银灰 精2 Lv45 潜6 满好 X模 模3 技能2 专3")
        self.assertEqual(config["name"], "银灰")
        self.assertEqual(config["phase"], 2)
        self.assertEqual(config["level"], 45)
        self.assertEqual(config["potential"], 6)
        self.assertEqual(config["favor"], 200)
        self.assertEqual(config["module_type"], "X")
        self.assertEqual(config["module_level"], 3)
        self.assertEqual(config["skill_index"], 2)
        self.assertEqual(config["mastery"], 3)

    def test_skill_level_after_skill_context(self) -> None:
        # 「N级」跟在技能后 = 技能等级；独立大数 = 干员等级
        config = _parse_panel_query("艾雅法拉 技能1 4级")
        self.assertEqual(config["skill_index"], 1)
        self.assertEqual(config["skill_level"], 4)
        self.assertIsNone(config["level"])
        config = _parse_panel_query("艾雅法拉 45级")
        self.assertEqual(config["level"], 45)
        self.assertIsNone(config["skill_level"])

    def test_standalone_low_level_is_skill_level(self) -> None:
        # 独立 1-7 级 = 技能等级（低干员等级实战无意义）；Lv7 才是干员等级
        config = _parse_panel_query("银灰 7级")
        self.assertEqual(config["skill_level"], 7)
        self.assertIsNone(config["level"])
        config = _parse_panel_query("银灰 Lv7")
        self.assertEqual(config["level"], 7)
        self.assertIsNone(config["skill_level"])
        config = _parse_panel_query("银灰 技能2 7级")
        self.assertEqual(config["skill_index"], 2)
        self.assertEqual(config["skill_level"], 7)

    def test_special_tokens(self) -> None:
        config = _parse_panel_query("阿米娅 精满级 满潜 无模 二技能")
        self.assertEqual(config["phase"], None)  # 「精满级」非合法 token → 归入名字
        self.assertEqual(config["name"], "阿米娅 精满级")
        self.assertEqual(config["potential"], -1)  # 满潜
        self.assertTrue(config["no_module"])
        self.assertEqual(config["skill_index"], 2)
        config = _parse_panel_query("阿米娅 满 级".replace(" ", ""))
        # 名字含「级」字不误伤（无数字前缀不匹配）
        config2 = _parse_panel_query("阿米娅 满级")
        self.assertEqual(config2["level"], -1)


class InterpolateTest(unittest.TestCase):
    def test_endpoints(self) -> None:
        self.assertEqual(interpolate(SVRASH_FRAMES, 1)["maxHp"], 2022)
        self.assertEqual(interpolate(SVRASH_FRAMES, 90)["maxHp"], 2560)

    def test_midpoint_and_clamp(self) -> None:
        # Lv45：2022 + (2560-2022)*44/89 ≈ 2287.98
        self.assertAlmostEqual(
            interpolate(SVRASH_FRAMES, 45)["maxHp"], 2287.98, places=1
        )
        # 越界钳到端点
        self.assertEqual(interpolate(SVRASH_FRAMES, 120)["maxHp"], 2560)
        self.assertEqual(interpolate(SVRASH_FRAMES, 0)["maxHp"], 2022)


class PanelComputeTest(unittest.TestCase):
    CHAR = {
        "phases": [{"maxLevel": 90, "attributesKeyFrames": SVRASH_FRAMES}],
        "potentialRanks": [
            {"description": "部署费用-1",
             "buff": {"attributes": {"attributeModifiers": [
                 {"attributeType": "COST", "formulaItem": "ADDITION", "value": -1.0}]}}},
            {"description": "攻击力+26",
             "buff": {"attributes": {"attributeModifiers": [
                 {"attributeType": "ATK", "formulaItem": "ADDITION", "value": 26.0}]}}},
        ],
        "maxPotentialLevel": 6,
        "favorKeyFrames": [
            {"level": 0, "data": {"maxHp": 0, "atk": 0}},
            {"level": 50, "data": {"maxHp": 100.0, "atk": 50.0}},
        ],
        "talents": [
            {"candidates": [
                {"unlockCondition": {"phase": "PHASE_1", "level": 1},
                 "requiredPotentialRank": 0, "name": "领袖",
                 "description": "攻击力+10%（<@ba.vup>+2%</>）"},
                {"unlockCondition": {"phase": "PHASE_1", "level": 1},
                 "requiredPotentialRank": 5, "name": "领袖",
                 "description": "攻击力+12%（+2%）"},
            ]},
        ],
    }

    def test_potential_cumulative(self) -> None:
        # 潜能 1..2 叠加：费-1 攻+26
        self.assertEqual(
            potential_modifiers(self.CHAR, 2),
            {"cost": -1.0, "atk": 26.0},
        )

    def test_favor_scale_half(self) -> None:
        # 显示 200% → 帧 100 → 钳到帧 50 满值
        self.assertEqual(favor_modifiers(self.CHAR, 200), {"maxHp": 100.0, "atk": 50.0})
        # 显示 50% → 帧 25 → 半程
        self.assertEqual(favor_modifiers(self.CHAR, 50), {"maxHp": 50.0, "atk": 25.0})

    def test_full_compute_breakdown(self) -> None:
        panel = compute_panel(
            self.CHAR, 0, 90, potential=2, favor_percent=200,
            module_modifiers={"maxHp": 330.0, "atk": 70.0},
        )
        # HP 2560 + 100(好感) + 330(模组)；攻 713 + 26(潜) + 50(好) + 70(模) = 859
        self.assertEqual(panel["attrs"]["hp"], "2990")
        self.assertEqual(panel["attrs"]["atk"], "859")
        self.assertEqual(panel["parts"]["atk"]["potential"], 26.0)

    def test_talent_potential_stage_selection(self) -> None:
        talents = talents_at(self.CHAR, 2, 90, potential=5)
        self.assertEqual(len(talents), 1)
        # 潜5 档覆盖潜0 档；@ba 标签清理
        self.assertEqual(talents[0]["description"], "攻击力+12%（+2%）")
        # 潜4 未达 5 档 → 基础档
        base = talents_at(self.CHAR, 2, 90, potential=4)
        self.assertEqual(base[0]["description"], "攻击力+10%（+2%）")


class ModuleTest(unittest.TestCase):
    def test_module_stat_modifiers(self) -> None:
        battle = {"uniequip_002_svrash": {"phases": [
            {"equipLevel": 1, "attributeBlackboard": [
                {"key": "max_hp", "value": 190.0}, {"key": "atk", "value": 45.0}]},
            {"equipLevel": 3, "attributeBlackboard": [
                {"key": "max_hp", "value": 330.0}, {"key": "atk", "value": 70.0}]},
        ]}}
        self.assertEqual(
            module_stat_modifiers(battle, "uniequip_002_svrash", 3),
            {"maxHp": 330.0, "atk": 70.0},
        )
        self.assertEqual(
            module_stat_modifiers(battle, "uniequip_002_svrash", 9), {}
        )

    def test_module_choices_and_find(self) -> None:
        uniequip = {
            "equipDict": {
                "uniequip_001_svrash": {"charId": "c", "typeIcon": "original",
                                        "uniEquipName": "基础证章"},
                "uniequip_002_svrash": {"charId": "c", "typeIcon": "lor-x",
                                        "uniEquipName": "雪境羽兽护理套组"},
            },
            "charEquip": {"c": ["uniequip_001_svrash", "uniequip_002_svrash"]},
        }
        choices = module_choices("c", uniequip)
        # 基础证章（original）不参与
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["type"], "X")
        self.assertIs(find_module(choices, "Y"), choices[0])  # 无 Y 回退第一个
        self.assertIs(find_module(choices, "X"), choices[0])


if __name__ == "__main__":
    unittest.main()
