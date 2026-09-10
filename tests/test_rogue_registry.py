"""肉鸽期注册表不变量测试：三套编号体系换算的唯一事实源。

新一期加行后这些不变量必须仍然成立——玩家/表编号差一、别名零漂移、
官方显示名口径（sami 期曾误写"萨米的腐朽安眠"，2026-09 勘正）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.gamedata.rogue_registry import (
    ROGUE_THEME_NAMES,
    TABLE_THEME_ORDER,
    THEMES,
    THEME_ALIASES,
    THEME_DIRS,
    THEME_TABLE_IDS,
    TOPIC_SHORT_NAMES,
    diff_file_for,
)

# 收敛前各消费方持有的期别名表（逐字快照，锁零漂移）
LEGACY_ALIASES = {
    "傀影": "phantom",
    "猩红": "phantom",
    "傀影与猩红孤钻": "phantom",
    "傀影肉鸽": "phantom",
    "猩红肉鸽": "phantom",
    "水月": "mizuki",
    "深蓝": "mizuki",
    "水月与深蓝之树": "mizuki",
    "水月肉鸽": "mizuki",
    "萨米": "sami",
    "银凇": "sami",
    "萨米的腐朽安眠": "sami",
    "萨米肉鸽": "sami",
    "萨卡兹": "sarkaz",
    "无终奇语": "sarkaz",
    "萨卡兹的无终奇语": "sarkaz",
    "萨卡兹肉鸽": "sarkaz",
    "岁": "sui",
    "界园": "sui",
    "岁的界园志异": "sui",
    "界园肉鸽": "sui",
    "黑流": "black",
    "树海": "black",
    "沉沦者": "black",
    "沉沦者的黑流树海": "black",
    "黑流树海": "black",
    "树海肉鸽": "black",
    "黑流肉鸽": "black",
}


class RegistryInvariantTest(unittest.TestCase):
    def test_theme_aliases_zero_drift(self) -> None:
        self.assertEqual(THEME_ALIASES, LEGACY_ALIASES)

    def test_table_ids_contiguous_from_phantom(self) -> None:
        for i, t in enumerate(THEMES, start=1):
            self.assertEqual(t.table_id, f"rogue_{i}")
        self.assertEqual(TABLE_THEME_ORDER, [f"rogue_{i}" for i in range(1, 7)])
        self.assertEqual(len(THEMES), len(THEME_DIRS))
        self.assertEqual(THEME_TABLE_IDS["black"], "rogue_6")

    def test_player_numbering_offset_by_mushroom(self) -> None:
        """玩家编号 = 表编号 + 1（灰蕈迷境垫在 rogue_1）。"""
        self.assertEqual(ROGUE_THEME_NAMES["rogue_1"], "刻俄柏的灰蕈迷境")
        for i, t in enumerate(THEMES):
            self.assertEqual(ROGUE_THEME_NAMES[f"rogue_{i + 2}"], t.name)

    def test_sami_display_name_canonical(self) -> None:
        """官方主题表口径：sami = 探索者的银凇止境（勘正历史误写）。"""
        self.assertEqual(THEME_DIRS["sami"], "探索者的银凇止境")

    def test_short_names_cover_all_table_ids(self) -> None:
        self.assertEqual(
            sorted(TOPIC_SHORT_NAMES), sorted(THEME_TABLE_IDS.values())
        )
        self.assertEqual(TOPIC_SHORT_NAMES["rogue_6"], "树海")

    def test_diff_file_for_black_special_case(self) -> None:
        self.assertEqual(diff_file_for("black"), "difficulty_mods.json")
        self.assertEqual(
            diff_file_for("phantom"), "difficulty_mods_phantom.json"
        )


if __name__ == "__main__":
    unittest.main()
