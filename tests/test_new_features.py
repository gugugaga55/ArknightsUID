"""新功能回归：合成/计划、模组、危机合约、语音、别名、年度、推送增强。

原则：纯函数直测；数据层 mock 表加载；断言面向用户可见契约
（文本内容/结构字段），不锁内部实现。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))


# ---------------------------------------------------------------------------
# craft：合成树 / 叶子聚合 / 合成 vs 直刷
# ---------------------------------------------------------------------------


def _fake_building() -> dict:
    return {
        "workshopFormulas": {
            "1": {
                "formulaId": "1",
                "formulaType": "F_BUILDING",
                "itemId": "B",     # B = 2×A + 龙门币
                "count": 1,
                "goldCost": 800,
                "costs": [{"id": "A", "count": 2, "type": "MATERIAL"}],
            },
            "2": {
                "formulaId": "2",
                "formulaType": "F_BUILDING",
                "itemId": "C",     # C = 1×B + 1×A（二级合成）
                "count": 1,
                "goldCost": 1000,
                "costs": [
                    {"id": "B", "count": 1, "type": "MATERIAL"},
                    {"id": "A", "count": 1, "type": "MATERIAL"},
                ],
            },
            "3": {
                "formulaId": "3",
                "formulaType": "F_COOKING",  # 非加工站配方：应被过滤
                "itemId": "X",
                "count": 1,
                "costs": [],
            },
        }
    }


def _item_index() -> dict:
    return {
        "A": {"name": "基础材料"},
        "B": {"name": "中级材料"},
        "C": {"name": "高级材料"},
    }


class CraftTreeTest(unittest.IsolatedAsyncioTestCase):
    async def test_load_formulas_filters_workshop_only(self) -> None:
        from ArknightsUID.utils.gamedata import craft as craft_mod

        with mock.patch.object(
            craft_mod, "ensure_building_table", new=mock.AsyncMock(return_value=_fake_building())
        ):
            formulas = await craft_mod.load_formulas()
        self.assertEqual(set(formulas), {"B", "C"})

    async def test_craft_tree_recursive_and_leaves(self) -> None:
        from ArknightsUID.utils.gamedata import craft as craft_mod

        formulas = {
            f["itemId"]: f
            for f in _fake_building()["workshopFormulas"].values()
            if f["formulaType"] == "F_BUILDING"
        }
        tree = craft_mod.craft_tree("C", formulas, _item_index(), need=3)
        self.assertTrue(tree["craftable"])
        self.assertEqual(tree["gold"], 3000)  # 3 次 × 1000/次
        # 叶子：C×3 = 3×(B + A) = 3×(2A + A) = 9A
        leaves = craft_mod.leaf_materials(tree)
        self.assertEqual(leaves, {"A": 9})
        lines = craft_mod.format_craft_tree(tree, _item_index())
        self.assertTrue(any("高级材料" in ln for ln in lines))
        self.assertTrue(any("合成" in ln for ln in lines))

    def test_craft_vs_farm_compares_sanity(self) -> None:
        from ArknightsUID.utils.gamedata import craft as craft_mod

        formulas = {
            f["itemId"]: f
            for f in _fake_building()["workshopFormulas"].values()
            if f["formulaType"] == "F_BUILDING"
        }
        matrix_by_item = {
            "A": [{"stageId": "s1", "itemId": "A", "times": 1000, "quantity": 1000.0}],
            "C": [{"stageId": "s2", "itemId": "C", "times": 1000, "quantity": 100.0}],
        }

        def fake_stage_meta(stage_id):
            return {"s1": {"code": "1-7", "zone": "主线"}, "s2": {"code": "5-10", "zone": "主线"}}[stage_id]

        stats_patch = {
            "s1": {"stage_code": "1-7", "zone": "主线", "rate": 1.0, "ap_expect": 18.0, "times": 1000},
            "s2": {"stage_code": "5-10", "zone": "主线", "rate": 0.1, "ap_expect": 180.0, "times": 1000},
        }
        with mock.patch.object(
            craft_mod, "best_stage_cost",
            side_effect=lambda iid, m, s: stats_patch.get(
                "s1" if iid == "A" else "s2"
            ),
        ):
            result = craft_mod.craft_vs_farm(
                "C", 1, formulas, _item_index(), matrix_by_item, {}
            )
        # 直刷 C：180 理智；合成 C：3×A = 3×18 = 54 理智 → 合成更省
        self.assertEqual(result["farm_sanity"], 180.0)
        self.assertEqual(result["craft_sanity"], 54.0)
        self.assertIsNotNone(result["craft"])


# ---------------------------------------------------------------------------
# 模组详情：build_module_context
# ---------------------------------------------------------------------------


def _fake_uniequip() -> dict:
    return {
        "equipDict": {
            "uniequip_001_svrash": {
                "uniEquipId": "uniequip_001_svrash",
                "uniEquipName": "领袖的远见",
                "typeIcon": "X",
                "typeName1": "领袖",
                "uniEquipDesc": "银灰的模组。",
                "unlockLevel": 60,
                "unlockEvolvePhase": 2,
                "unlockFavors": 50,
                "itemCost": {  # 真实结构：按模组等级分档
                    "1": [
                        {"id": "A", "count": 4, "type": "MATERIAL"},
                        {"id": "2001", "count": 2, "type": "GOLD"},  # 非材料：应被过滤
                    ],
                    "2": [{"id": "A", "count": 8, "type": "MATERIAL"}],
                },
                "unlockEvolvePhase": "PHASE_2",
                "unlockFavors": {"1": 0, "2": 2732, "3": 10070},
            }
        },
        "charEquip": {"char_172_svrash": ["uniequip_001_svrash"]},
    }


class ModuleInfoTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_module_context(self) -> None:
        from ArknightsUID.utils.gamedata import modules_info as mi

        char_index = {
            "char_172_svrash": {"name": "银灰", "appellation": "SilverAsh"}
        }
        with (
            mock.patch.object(mi, "load_char_index", new=mock.AsyncMock(return_value=char_index)),
            mock.patch.object(
                mi, "load_module_tables",
                new=mock.AsyncMock(return_value=(_fake_uniequip(), {})),
            ),
            mock.patch.object(
                mi, "load_item_index", new=mock.AsyncMock(return_value=_item_index())
            ),
            mock.patch.object(
                mi, "module_stat_modifiers",
                side_effect=lambda battle, eid, lv: {"atk": 8.0} if lv == 3 else {},
            ),
        ):
            context = await mi.build_module_context("银灰")
        self.assertNotIsInstance(context, str)
        self.assertEqual(context["name"], "银灰")
        module = context["modules"][0]
        self.assertEqual(module["name"], "领袖的远见")
        self.assertEqual(module["level_attrs"], {"L3": "攻击 +8"})
        self.assertEqual(module["costs"], [{"name": "基础材料", "count": 4}])
        text = mi.format_module_context(context)
        self.assertIn("领袖的远见", text)
        self.assertIn("解锁材料：基础材料×4", text)

    async def test_build_module_context_ambiguous(self) -> None:
        from ArknightsUID.utils.gamedata import modules_info as mi

        index = {
            "c1": {"name": "阿米娅", "appellation": "Amiya"},
            "c2": {"name": "阿米娅(近卫)", "appellation": "AmiyaGuard"},
        }
        with mock.patch.object(mi, "load_char_index", new=mock.AsyncMock(return_value=index)):
            result = await mi.build_module_context("阿米")  # 前缀歧义（精确名不命中）
        self.assertIsInstance(result, str)
        self.assertIn("匹配到多位干员", result)


# ---------------------------------------------------------------------------
# 危机合约：赛季合并 + 词条检索解析
# ---------------------------------------------------------------------------


class CrisisTest(unittest.IsolatedAsyncioTestCase):
    async def test_load_crisis_seasons_merges_both_tables(self) -> None:
        from ArknightsUID.utils.gamedata import crisis as crisis_mod

        old_table = {
            "seasonInfo": [  # 旧表是 list
                {"seasonId": "rune_season_1_1", "name": "旧赛季", "startTs": 1_000, "endTs": 2_000}
            ]
        }
        new_table = {
            "seasonInfoDataMap": {  # 新表是 dict
                "crisis_v2_season_5_1": {
                    "seasonId": "crisis_v2_season_5_1",
                    "name": "涤墨作战",
                    "startTs": 1_800_000_000,
                    "endTs": 1_800_100_000,
                }
            }
        }
        with mock.patch.object(
            crisis_mod, "ensure_crisis_tables",
            new=mock.AsyncMock(return_value=(old_table, new_table)),
        ):
            seasons = await crisis_mod.load_crisis_seasons()
        self.assertEqual(len(seasons), 2)
        self.assertEqual(seasons[0]["name"], "涤墨作战")  # 按开始时间降序
        self.assertEqual(seasons[1]["version"], "旧危机合约")


# ---------------------------------------------------------------------------
# 语音：wikitext 解析
# ---------------------------------------------------------------------------

_VOICE_WIKITEXT = """{{VoiceTable|表格标题=语音记录
|语音key=char_test
|标题1=任命助理
|台词1={{VoiceData/word|中文|你好博士。}}{{VoiceData/word|日文|こんにちは}}
|语音1=CN_001.wav
|标题2=闲置
|台词2={{VoiceData/word|中文|别担心，我会守着这里。}}
|语音2=CN_002.wav
}}"""


class VoiceTest(unittest.IsolatedAsyncioTestCase):
    def test_parse_voice_lines_chinese_only(self) -> None:
        from ArknightsUID.utils.prts import client as prts

        lines = prts.parse_voice_lines(_VOICE_WIKITEXT)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["title"], "任命助理")
        self.assertEqual(lines[0]["text"], "你好博士。")
        self.assertEqual(lines[1]["title"], "闲置")

    def test_parse_voice_lines_empty(self) -> None:
        from ArknightsUID.utils.prts import client as prts

        self.assertEqual(prts.parse_voice_lines(""), [])


# ---------------------------------------------------------------------------
# 别名：命令文本解析 + 查询侧替换
# ---------------------------------------------------------------------------


class AliasTest(unittest.IsolatedAsyncioTestCase):
    def test_parse_alias_text(self) -> None:
        from ArknightsUID.arknightsuid_bind import _parse_alias_text

        self.assertEqual(_parse_alias_text("银羽毛=银灰"), ("银羽毛", "银灰"))
        self.assertEqual(_parse_alias_text("银羽毛 → 银灰"), ("银羽毛", "银灰"))
        self.assertEqual(_parse_alias_text("银羽毛 银灰"), ("银羽毛", "银灰"))
        self.assertIsNone(_parse_alias_text(""))
        self.assertIsNone(_parse_alias_text("只有一个词"))

    async def test_apply_user_alias_hit_and_miss(self) -> None:
        import asyncio

        from ArknightsUID.utils import user_alias

        async def fake_resolve(**kwargs):
            return "银灰" if kwargs.get("alias") == "银羽毛" else None

        with mock.patch(
            "ArknightsUID.utils.database.models.ArkAlias.resolve_alias",
            new=fake_resolve,
        ):
            self.assertEqual(
                await user_alias.apply_user_alias("bot", "u1", "银羽毛"), "银灰"
            )
            self.assertEqual(
                await user_alias.apply_user_alias("bot", "u1", "别的"), "别的"
            )

    async def test_apply_user_alias_db_error_fallback(self) -> None:
        from ArknightsUID.utils import user_alias

        async def boom(**kwargs):
            raise RuntimeError("db down")

        with mock.patch(
            "ArknightsUID.utils.database.models.ArkAlias.resolve_alias", new=boom
        ):
            self.assertEqual(
                await user_alias.apply_user_alias("bot", "u1", "银羽毛"), "银羽毛"
            )


# ---------------------------------------------------------------------------
# 年度报告
# ---------------------------------------------------------------------------


def _records_2026() -> list[dict]:
    from datetime import datetime

    def ts(month, day):
        return int(datetime(2026, month, day, 12).timestamp())

    return [
        {"name": "阿米娅", "rarity": 5, "category": "NORMAL", "gacha_ts": ts(1, 1), "pos": 3, "is_new": False},
        {"name": "银灰", "rarity": 5, "category": "NORMAL", "gacha_ts": ts(1, 2), "pos": 2, "is_new": True},
        {"name": "刻俄柏", "rarity": 5, "category": "LIMITED", "gacha_ts": ts(5, 1), "pos": 1, "is_new": True},
        # 2025 的记录：不应计入
        {"name": "W", "rarity": 5, "category": "NORMAL", "gacha_ts": int(datetime(2025, 3, 1).timestamp()), "pos": 9, "is_new": False},
    ]


class AnnualReportTest(unittest.TestCase):
    def test_annual_report_filters_year_and_counts(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _annual_report

        report = _annual_report(_records_2026(), "2026")
        self.assertIsNotNone(report)
        self.assertIn("总抽数：3", report)
        self.assertIn("六星 3", report)
        self.assertIn("六星新干员：2 位", report)
        self.assertIn("2026", report)

    def test_annual_report_no_data(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _annual_report

        self.assertIsNone(_annual_report(_records_2026(), "2020"))


# ---------------------------------------------------------------------------
# 推送增强：理智预警 + 剿灭周提醒
# ---------------------------------------------------------------------------


class SanityWarnTest(unittest.TestCase):
    def test_warn_band_and_rearm(self) -> None:
        from ArknightsUID.arknightsuid_notify import evaluate_sanity_warn

        # 进入 85% 区间且未预警 → 预警
        self.assertEqual(evaluate_sanity_warn(120, 135, False), (True, True))
        # 已预警过 → 不再
        self.assertEqual(evaluate_sanity_warn(120, 135, True), (False, True))
        # 回满 → 交给回满推送，预警复位
        self.assertEqual(evaluate_sanity_warn(135, 135, True), (False, False))
        # 用理智掉出区间 → 重新武装
        self.assertEqual(evaluate_sanity_warn(80, 135, True), (False, False))
        self.assertEqual(evaluate_sanity_warn(100, 135, False), (False, False))
        # 无效上限
        self.assertEqual(evaluate_sanity_warn(10, 0, False), (False, False))

    def test_original_evaluate_sanity_untouched(self) -> None:
        from ArknightsUID.arknightsuid_notify import evaluate_sanity

        self.assertEqual(evaluate_sanity(135, 135, False), (True, True))

    def test_campaign_evaluate_and_week_key(self) -> None:
        from ArknightsUID.arknightsuid_notify import _iso_week_key, evaluate_campaign

        self.assertEqual(evaluate_campaign(400, 1200), (True, 800))
        self.assertEqual(evaluate_campaign(1200, 1200), (False, 0))
        self.assertEqual(evaluate_campaign(0, 0), (False, 0))
        key = _iso_week_key(1_700_000_000)
        self.assertRegex(key, r"^\d{4}-W\d{2}$")


# ---------------------------------------------------------------------------
# 抽签 + 帮助搜索 + 计划解析：纯函数与文档同步
# ---------------------------------------------------------------------------


class FortuneAndHelpTest(unittest.TestCase):
    def test_daily_seed_deterministic(self) -> None:
        from ArknightsUID.arknightsuid_help import _daily_seed, _draw_fortune

        self.assertEqual(
            _daily_seed("bot:u1", "2026-08-21"), _daily_seed("bot:u1", "2026-08-21")
        )
        self.assertNotEqual(
            _daily_seed("bot:u1", "2026-08-21"), _daily_seed("bot:u2", "2026-08-21")
        )
        fortune = _draw_fortune(_daily_seed("bot:u1", "2026-08-21"))
        self.assertIn(fortune["level"], ["大吉", "中吉", "小吉", "平", "小凶", "凶"])
        self.assertEqual(len(fortune["good"]), 3)
        self.assertEqual(len(fortune["bad"]), 2)

    def test_plan_targets_parsing(self) -> None:
        from ArknightsUID.arknightsuid_push import _match_item_id, _parse_plan_targets

        self.assertEqual(
            _parse_plan_targets("聚酸酯块:10 龙门币:20000"),
            [("聚酸酯块", 10), ("龙门币", 20000)],
        )
        self.assertEqual(_parse_plan_targets("固源岩"), [("固源岩", 1)])
        self.assertEqual(_parse_plan_targets("x:abc"), [("x", 1)])

        index = {"3001": {"name": "聚酸酯"}, "3002": {"name": "聚酸酯组"}}
        self.assertEqual(_match_item_id("聚酸酯", index), "3001")
        self.assertIsNone(_match_item_id("聚酸酯…歧义前缀", {"a": {"name": "聚"}}))

    def test_new_commands_in_help_and_readme(self) -> None:
        """文档同步守恒：新命令必须同时出现在帮助卡数据与 README。"""
        from ArknightsUID.arknightsuid_help import HELP_GROUPS

        help_cmds = " ".join(
            c["cmd"] for g in HELP_GROUPS for c in g["cmds"]
        )
        readme = Path(__file__).parents[1].joinpath("README.md").read_text(
            encoding="utf-8"
        )
        for name in (
            "ark模组", "ark语音", "ark危机合约", "ark合成", "ark计划",
            "ark年度", "ark别名", "ark抽签", "ark帮助搜索", "ark订阅剿灭",
        ):
            self.assertIn(name, help_cmds, f"{name} 未收录进 HELP_GROUPS")
            self.assertIn(name, readme, f"{name} 未收录进 README")


if __name__ == "__main__":
    unittest.main()
