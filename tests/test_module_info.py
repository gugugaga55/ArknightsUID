"""ark模组 详情增强：PRTS {{模组}} 富解析 + GameData 三档材料合并。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.gamedata.modules_info import (
    _game_data_costs,
    _merge_tier_costs,
    _parse_material_text,
    format_module_context,
)
from ArknightsUID.utils.prts.client import parse_operator_wikitext

_WIKITEXT = """
{{CharinfoV2
|干员名=测试干员
|稀有度=5
|职业=近卫
}}
{{模组
|名称=测试证章
|基础证章=yes
|基础信息=证章说明
}}
{{模组
|名称=测试套组
|类型图标=lor-x
|类型=LOR-X
|解锁等级=60
|解锁信赖=100
|特性=攻击附带{{color|#0098DC|10%}}攻击力的法术伤害<br>※附带说明
|天赋2=天赋【领袖】更新<br/>攻击力{{变动数值lite|up|蓝|+20%}}{{变动数值lite||橙|且提升至110%}}
|天赋3=天赋【领袖】更新<br/>攻击力{{变动数值lite|up|蓝|+25%}}
|潜能天赋3=满潜变体{{color|#0098DC|（+2%）}}
|任务1=战斗中累计使用技能10次
|任务2=3星通关[[关卡一览#别传|别传]][[BI-7]]
|材料消耗={{材料消耗|模组数据块|4}} {{材料消耗|聚合剂|2}} {{材料消耗|龙门币|8万}}
|材料消耗2={{材料消耗|模组数据块|4}} {{材料消耗|数据增补条|60}}
|材料消耗3={{材料消耗|数据增补仪|20}} {{材料消耗|烧结核凝晶|4}} {{材料消耗|龙门币|12万}}
|基础信息=模组故事文本
}}
"""


class ModuleParseTest(unittest.TestCase):
    def test_operator_module_rich_fields(self) -> None:
        op = parse_operator_wikitext(_make_page(_WIKITEXT))
        self.assertIsNotNone(op)
        assert op is not None
        self.assertEqual(len(op.modules), 2)
        basic, upgrade = op.modules

        self.assertEqual(basic.name, "测试证章")
        self.assertEqual(basic.type_, "基础证章")
        self.assertEqual(upgrade.name, "测试套组")
        self.assertEqual(upgrade.type_, "LOR-X")

        # 特性：color 模板归约 + br 换行保留
        self.assertIn("攻击附带10%攻击力的法术伤害", upgrade.trait)
        self.assertIn("\n", upgrade.trait)
        # 天赋更新：变动数值lite 归约出数值、无 ↑↓ 装饰、链接取显示文本
        self.assertEqual(len(upgrade.talent_updates), 2)
        self.assertIn("攻击力+20%且提升至110%", upgrade.talent_updates["2"])
        self.assertIn("攻击力+25%", upgrade.talent_updates["3"])
        self.assertNotIn("↑", upgrade.talent_updates["2"])
        self.assertIn("（+2%）", upgrade.potential_talents["3"])
        # 三档材料：材料名不丢（raw 解析）
        self.assertEqual(
            upgrade.upgrade_costs.get("1"),
            ["模组数据块 ×4", "聚合剂 ×2", "龙门币 ×8万"],
        )
        self.assertEqual(
            upgrade.upgrade_costs.get("2"),
            ["模组数据块 ×4", "数据增补条 ×60"],
        )
        self.assertEqual(
            upgrade.upgrade_costs.get("3"),
            ["数据增补仪 ×20", "烧结核凝晶 ×4", "龙门币 ×12万"],
        )
        # 任务：[[BI-7]] 链接保留显示文本
        self.assertEqual(len(upgrade.missions), 2)
        self.assertIn("BI-7", upgrade.missions[1])
        self.assertEqual(upgrade.unlock_level, "60")
        self.assertEqual(upgrade.unlock_favor, "100")


class ModuleCostsTest(unittest.TestCase):
    def test_parse_material_text(self) -> None:
        self.assertEqual(
            _parse_material_text("模组数据块 ×4"),
            {"name": "模组数据块", "count": "4"},
        )
        self.assertEqual(
            _parse_material_text("龙门币 ×8万"),
            {"name": "龙门币", "count": "8万"},
        )
        self.assertEqual(
            _parse_material_text("无名材料"),
            {"name": "无名材料", "count": ""},
        )

    def test_game_data_costs_maps_tokens_and_gold(self) -> None:
        item_cost = {
            "1": [
                {"id": "mod_unlock_token", "count": 4, "type": "MATERIAL"},
                {"id": "30115", "count": 2, "type": "MATERIAL"},
                {"id": "4001", "count": 80000, "type": "GOLD"},
                {"id": "skip", "count": 1, "type": "OTHER"},
            ],
            "3": [{"id": "mod_update_token_2", "count": 20, "type": "MATERIAL"}],
        }
        item_index = {"30115": {"name": "聚合剂"}}
        tiers = _game_data_costs(item_cost, item_index)
        self.assertEqual(
            tiers["1"],
            [
                {"name": "模组数据块", "count": 4},
                {"name": "聚合剂", "count": 2},
                {"name": "龙门币", "count": 80000},
            ],
        )
        self.assertEqual(
            tiers["3"], [{"name": "数据增补仪", "count": 20}]
        )

    def test_merge_tier_costs_prts_first(self) -> None:
        gamedata = {"1": [{"name": "G1", "count": 1}], "2": [{"name": "G2", "count": 2}]}
        prts = {"1": [{"name": "P1", "count": "1"}], "3": [{"name": "P3", "count": "3"}]}
        merged = _merge_tier_costs(gamedata, prts)
        # PRTS 命中档优先；PRTS 缺档回退 GameData；两边都缺不出现
        self.assertEqual([r["name"] for r in merged["1"]], ["P1"])
        self.assertEqual([r["name"] for r in merged["2"]], ["G2"])
        self.assertEqual([r["name"] for r in merged["3"]], ["P3"])


class ModuleFormatTest(unittest.TestCase):
    def test_format_includes_effects_and_upgrade_costs(self) -> None:
        context = {
            "name": "测试",
            "modules": [
                {
                    "name": "套组",
                    "type": "LOR-X",
                    "type_name": "LOR",
                    "desc": "故事",
                    "unlock_phase": 2,
                    "unlock_level": 60,
                    "unlock_favor": 100,
                    "level_attrs": {"L1": "生命 +190，攻击 +45"},
                    "trait": "攻击附带10%攻击力的法术伤害",
                    "talent_updates": [
                        {"level": "Lv2", "text": "天赋【领袖】更新：攻击力+20%"}
                    ],
                    "tier_costs": {
                        "1": [{"name": "模组数据块", "count": "4"}],
                        "2": [{"name": "数据增补条", "count": "60"}],
                        "3": [{"name": "数据增补仪", "count": "20"}],
                    },
                    "missions": ["战斗中累计使用技能10次"],
                }
            ],
        }
        text = format_module_context(context)
        self.assertIn("特性追加：攻击附带10%攻击力的法术伤害", text)
        self.assertIn("Lv2：天赋【领袖】更新：攻击力+20%", text)
        self.assertIn("解锁材料：模组数据块×4", text)
        self.assertIn("Lv2 升级材料：数据增补条×60", text)
        self.assertIn("Lv3 升级材料：数据增补仪×20", text)
        self.assertIn("任务1：战斗中累计使用技能10次", text)
        self.assertIn("精2 Lv60 好感100", text)


def _make_page(wikitext: str):
    from ArknightsUID.utils.prts.client import PageWikitext

    return PageWikitext(title="测试干员", wikitext=wikitext)


if __name__ == "__main__":
    unittest.main()
