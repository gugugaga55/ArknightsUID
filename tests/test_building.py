"""基建（building）解析与卡片契约测试。

字段结构依据：ProbiusOfficial/Skland_API dump.json 的 data.building
（一手 dump）。心情换算：满值哨兵 8640000 = 24 点 × 360000/点。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils.player import (
    MORALE_LOW_THRESHOLD,
    TRADING_STRATEGY_NAMES,
    BuildingData,
    build_building_context,
    format_training_remaining,
    morale_points,
    parse_building,
    parse_player_info,
    training_remaining_secs,
)
from ArknightsUID.utils.render import ark_templates  # noqa: F401  模板存在性
from ArknightsUID.version import ArknightsUID_version

VERSION = ArknightsUID_version


def _building_payload() -> dict[str, Any]:
    """真实 dump 的最小同构切片（字段名/嵌套与一手证据一致）。"""
    return {
        "labor": {"maxValue": 200, "value": 130, "lastUpdateTime": 1, "remainSecs": 1},
        "control": {
            "slotId": "slot_34",
            "slotState": 2,
            "level": 5,
            "chars": [
                # 控制中枢 5 人：2 人耗尽、1 人半血以下、其余健康
                {"charId": "char_002_amiya", "ap": 0, "index": 0, "workTime": 1},
                {"charId": "char_1024_hbisc2", "ap": 0, "index": 1, "workTime": 1},
                {"charId": "char_308_swire", "ap": 360000 * 10, "index": 2, "workTime": 1},
                {"charId": "char_130_doberm", "ap": 8640000, "index": 3, "workTime": 1},
                {"charId": "char_149_scave", "ap": 8640000, "index": 4, "workTime": 1},
            ],
        },
        "manufactures": [
            {
                "slotId": "slot_14",
                "level": 3,
                "chars": [
                    {"charId": "char_1013_chen2", "ap": 1828195, "index": 0},
                    {"charId": "char_118_yuki", "ap": 0, "index": 1},
                ],
                "completeWorkTime": -1,
                "formulaId": "4",
                "capacity": 54,
                "weight": 50,
                "complete": 10,
                "remain": 89,
                "speed": 1.63,
            },
            {
                "slotId": "slot_5",
                "level": 2,
                "chars": [{"charId": "char_141_nights", "ap": 15, "index": 0}],
                "formulaId": "3",
                "speed": 1.03,
            },
        ],
        "tradings": [
            {
                "slotId": "slot_24",
                "level": 3,
                "chars": [{"charId": "char_4055_bgsnow", "ap": 2372730, "index": 0}],
                "strategy": "O_GOLD",
                "stock": [
                    {"instId": 1, "type": "O_GOLD"},
                    {"instId": 2, "type": "O_GOLD"},
                ],
                "stockLimit": 12,
            },
            {
                "slotId": "slot_25",
                "level": 3,
                "chars": [],
                "strategy": "O_DIAMOND",
                "stock": [],
                "stockLimit": 20,
            },
        ],
        "powers": [
            {"slotId": "slot_26", "level": 3, "chars": [{"charId": "char_385_finlpp", "ap": 0}]},
            {"slotId": "slot_16", "level": 3, "chars": []},
        ],
        "dormitories": [
            {"slotId": "slot_28", "level": 5, "chars": [], "comfort": 5000},
            {"slotId": "slot_9", "level": 4, "chars": [], "comfort": 3145},
        ],
        "meeting": {
            "slotId": "slot_36",
            "level": 3,
            "chars": [{"charId": "char_275_breeze", "ap": 8640000, "index": 0}],
            "clue": {
                "own": 10,
                "received": 0,
                "dailyReward": True,
                "needReceive": 0,
                "board": ["PENGUIN", "GLASGOW", "URSUS", "UNKNOWN_FACTION"],
                "sharing": False,
            },
            "lastUpdateTime": 1,
        },
        "hire": {"slotId": "slot_23", "level": 3, "chars": [], "state": 0, "refreshCount": 3},
        "training": {
            "slotId": "slot_13",
            "level": 3,
            "trainee": {"charId": "char_123_fang"},
            "trainer": None,
            "remainPoint": -1,
            "speed": 1,
            # 训练中快照：lastUpdateTime 时刻还剩 7200s（语义见
            # training_remaining_secs 头注释，待真实凭据训练中样本校准）
            "lastUpdateTime": 1700000000,
            "remainSecs": 7200,
            "slotState": 2,
        },
        "tiredChars": [],
        "furniture": {"total": 225},
        "elevators": [],
        "corridors": [],
    }


def _player_info() -> dict:
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "status": {
                "name": "刀客塔不吃香菜",
                "uid": "123456789",
                "level": 120,
                "ap": {"current": 30, "max": 135, "completeRecoveryTime": 0},
            },
            "chars": [],
            "charInfoMap": {
                "char_002_amiya": {"name": "阿米娅", "profession": "CASTER", "rarity": 4},
                "char_1024_hbisc2": {"name": "濯尘芙蓉", "profession": "MEDIC", "rarity": 5},
                "char_308_swire": {"name": "诗怀雅", "profession": "PIONEER", "rarity": 5},
                "char_130_doberm": {"name": "杜宾", "profession": "PIONEER", "rarity": 4},
                "char_149_scave": {"name": "清道夫", "profession": "SPECIAL", "rarity": 4},
                "char_1013_chen2": {"name": "假日威龙陈", "profession": "SNIPER", "rarity": 5},
                "char_118_yuki": {"name": "雪雉", "profession": "SPECIAL", "rarity": 5},
                "char_141_nights": {"name": "夜烟", "profession": "CASTER", "rarity": 4},
                "char_4055_bgsnow": {"name": "承曦格雷伊", "profession": "CASTER", "rarity": 5},
                "char_385_finlpp": {"name": "芳汀", "profession": "WARRIOR", "rarity": 4},
                "char_275_breeze": {"name": "微风", "profession": "MEDIC", "rarity": 5},
                "char_123_fang": {"name": "芬", "profession": "PIONEER", "rarity": 3},
            },
            "manufactureFormulaInfoMap": {
                "3": {"id": "3", "itemId": "2003", "count": 1, "weight": 5, "costPoint": 10800},
                "4": {"id": "4", "itemId": "3003", "count": 1, "weight": 2, "costPoint": 4320},
            },
            "building": _building_payload(),
        },
    }


class MoraleTest(unittest.TestCase):
    def test_full_sentinel_is_24_points(self) -> None:
        self.assertEqual(morale_points(8640000), 24.0)

    def test_point_conversion(self) -> None:
        self.assertAlmostEqual(morale_points(360000 * 10), 10.0)
        self.assertAlmostEqual(morale_points(1828195), 5.1)

    def test_invalid_values(self) -> None:
        self.assertEqual(morale_points(None), 0.0)
        self.assertEqual(morale_points(-5), 0.0)
        self.assertEqual(morale_points("x"), 0.0)

    def test_threshold_constant(self) -> None:
        self.assertEqual(MORALE_LOW_THRESHOLD, 12.0)


class ParseBuildingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        data = parse_player_info(_player_info())
        assert data is not None
        cls.building = parse_building(data)
        assert cls.building is not None

    def test_readouts(self) -> None:
        b = self.building
        self.assertEqual(b.control_level, 5)
        self.assertEqual(b.labor_value, 130)
        self.assertEqual(b.labor_max, 200)

    def test_control_members_with_profession(self) -> None:
        b = self.building
        self.assertEqual(
            [m.name for m in b.control_members],
            ["阿米娅", "濯尘芙蓉", "诗怀雅", "杜宾", "清道夫"],
        )
        self.assertEqual(b.control_members[0].profession, "CASTER")
        self.assertEqual(b.control_members[0].morale, 0.0)

    def test_meeting_and_hire_rooms(self) -> None:
        """会客室/办公室的房间级字段（等级 + 进驻成员）解析。"""
        b = self.building
        self.assertEqual(b.meeting_level, 3)
        self.assertEqual([m.name for m in b.meeting_members], ["微风"])
        self.assertEqual(b.hire_level, 3)
        self.assertEqual(b.hire_members, [])

    def test_training_room_level(self) -> None:
        self.assertEqual(self.building.training_level, 3)

    def test_manufactures_with_formula(self) -> None:
        b = self.building
        self.assertEqual(len(b.manufactures), 2)
        first = b.manufactures[0]
        self.assertEqual(first.formula_id, "4")
        self.assertEqual(first.item_id, "3003")
        self.assertEqual(first.item_count, 1)
        self.assertAlmostEqual(first.speed, 1.63)
        self.assertEqual([m.name for m in first.members], ["假日威龙陈", "雪雉"])

    def test_trading_strategy_and_stock(self) -> None:
        b = self.building
        self.assertEqual(len(b.tradings), 2)
        gold = b.tradings[0]
        self.assertEqual(gold.strategy, "O_GOLD")
        self.assertEqual(gold.stock_count, 2)
        self.assertEqual(gold.stock_limit, 12)
        # 真实凭据 实测出现的源石订单策略（2026-08-15）
        self.assertEqual(b.tradings[1].strategy, "O_DIAMOND")

    def test_strategy_names_cover_all_live_codes(self) -> None:
        """真实接口出现过的策略代码必须全部有中文名（防再出现原始代码上卡）。"""
        for code in ("O_GOLD", "O_ORUNDUM", "O_DIAMOND"):
            self.assertIn(code, TRADING_STRATEGY_NAMES)

    def test_powers_and_dormitories(self) -> None:
        b = self.building
        self.assertEqual([p.level for p in b.powers], [3, 3])
        self.assertEqual([d.comfort for d in b.dormitories], [5000, 3145])

    def test_clue_board_translated_with_fallback(self) -> None:
        b = self.building
        self.assertEqual(b.clue_own, 10)
        self.assertTrue(b.clue_daily_reward)
        # URSUS 为真实接口代码（2026-08-15 实测）；未知代码回退原文
        self.assertEqual(
            b.clue_board, ["企鹅物流", "格拉斯哥", "乌萨斯学生自治团", "UNKNOWN_FACTION"]
        )

    def test_training_trainee_resolved(self) -> None:
        self.assertEqual(self.building.training_trainee, "芬")
        self.assertEqual(self.building.training_trainee_id, "char_123_fang")

    def test_training_remaining_folded_from_snapshot(self) -> None:
        """remainSecs 按 lastUpdateTime 快照折算：随时间流逝递减，下限 0。"""
        b = self.building
        self.assertEqual(b.training_remain_secs, 7200)
        self.assertEqual(training_remaining_secs(b, now=1700000000), 7200)
        self.assertEqual(training_remaining_secs(b, now=1700000000 + 3600), 3600)
        self.assertEqual(training_remaining_secs(b, now=1700000000 + 99999), 0)

    def test_training_remaining_idle_and_without_snapshot(self) -> None:
        # 空闲/未知（dump 实测空闲态 remainSecs=-1）→ -1，文案为空
        idle = BuildingData(training_remain_secs=-1, training_last_update=1)
        self.assertEqual(training_remaining_secs(idle, now=2), -1)
        self.assertEqual(format_training_remaining(-1), "")
        # lastUpdateTime 缺失：remainSecs 直接当作当前剩余
        no_snap = BuildingData(training_remain_secs=600, training_last_update=0)
        self.assertEqual(training_remaining_secs(no_snap, now=1700000000), 600)

    def test_format_training_remaining(self) -> None:
        self.assertEqual(format_training_remaining(0), "已完成")
        self.assertEqual(format_training_remaining(600), "约10分钟")
        self.assertEqual(format_training_remaining(5400), "约1小时30分")
        self.assertEqual(format_training_remaining(86400 + 3600), "约1天1小时")

    def test_training_status_text_full_phrase(self) -> None:
        """完整短语（含「剩」前缀/已完成），调用方不再自行拼接避免「剩已完成」。"""
        from ArknightsUID.utils.player import training_status_text

        self.assertEqual(training_status_text(-1), "")
        self.assertEqual(training_status_text(0), "已完成")
        self.assertEqual(training_status_text(5400), "剩约1小时30分")

    def test_tired_chars_from_working_rooms_only(self) -> None:
        """低心情聚合：控制中枢/制造/贸易/发电计入，宿舍与会客室不计。"""
        tired = self.building.tired_chars
        names = [t.name for t in tired]
        # 阿米娅(0)/濯尘芙蓉(0)/诗怀雅(10)/雪雉(0)/夜烟(0.0)/芳汀(0) 低于 12
        for expected in ("阿米娅", "濯尘芙蓉", "诗怀雅", "雪雉", "夜烟", "芳汀"):
            self.assertIn(expected, names)
        # 承曦格雷伊 6.6 也低于阈值
        self.assertIn("承曦格雷伊", names)
        # 杜宾/清道夫（满）与微风（会客室满）不应出现
        self.assertNotIn("杜宾", names)
        self.assertNotIn("清道夫", names)
        self.assertNotIn("微风", names)
        # 升序排列
        morales = [t.morale for t in tired]
        self.assertEqual(morales, sorted(morales))

    def test_missing_building_returns_none(self) -> None:
        payload = _player_info()
        del payload["data"]["building"]
        data = parse_player_info(payload)
        assert data is not None
        self.assertIsNone(parse_building(data))

    def test_empty_building_returns_none(self) -> None:
        payload = _player_info()
        payload["data"]["building"] = {}
        data = parse_player_info(payload)
        assert data is not None
        self.assertIsNone(parse_building(data))


class BuildingContextTest(unittest.IsolatedAsyncioTestCase):
    async def _build_context(self, item_index: dict[str, dict] | None):
        """受控 item_table：None = 索引不可用（回落 物品#<id>）。

        头像/职业图标挂载打桩为离线假值（不触网），验证挂载路径本身。
        """
        from unittest import mock

        data = parse_player_info(_player_info())
        assert data is not None
        building = parse_building(data)
        assert building is not None

        from ArknightsUID.utils.gamedata import charinfo as charinfo_mod

        async def fake_index():
            return item_index

        orig = charinfo_mod.load_item_index
        charinfo_mod.load_item_index = fake_index
        try:
            with (
                mock.patch(
                    "ArknightsUID.utils.player.context.char_avatar_b64",
                    new=mock.AsyncMock(return_value="A"),
                ),
                mock.patch(
                    "ArknightsUID.utils.gamedata.itemicon.profession_icon_b64",
                    new=mock.AsyncMock(return_value="P"),
                ),
            ):
                return await build_building_context(
                    data, building, VERSION, "123***789"
                )
        finally:
            charinfo_mod.load_item_index = orig

    async def test_context_shape(self) -> None:
        context = await self._build_context(None)
        assert context is not None
        for key in (
            "name", "uid", "control", "labor_value", "labor_max", "labor_pct",
            "manufactures", "tradings", "powers", "dormitories",
            "meeting", "hire", "training",
            "clue_own", "clue_board", "clue_daily_reward",
            "recruit_total", "recruit_done", "recruit_next",
            "tired_count", "version",
        ):
            self.assertIn(key, context)
        self.assertEqual(context["labor_pct"], 65)
        # item_table 不可用时配方名回退 "物品#<id>"
        self.assertEqual(context["manufactures"][0]["formula"], "物品#3003×1")

    async def test_room_slots_and_assets(self) -> None:
        """房间网格上下文：容量补位 + 头像/职业图标挂载 + 心情口径。"""
        context = await self._build_context(None)
        assert context is not None
        # 控制中枢：5 人全进驻（容量=等级），无补位
        control = context["control"]
        self.assertEqual(control["level"], 5)
        self.assertEqual(len(control["slots"]), 5)
        amiya = control["slots"][0]
        self.assertEqual(amiya["name"], "阿米娅")
        self.assertEqual(amiya["avatar"], "A")
        self.assertEqual(amiya["prof_icon"], "P")
        self.assertTrue(amiya["tired"])  # 心情 0 < 12
        self.assertEqual(amiya["morale_pct"], 0)
        # 贸易站库存百分比
        self.assertEqual(context["tradings"][0]["stock_pct"], 17)
        # 发电站：固定 1 进驻位，空房间补 1 个空槽
        powers = context["powers"]
        self.assertEqual([p["label"] for p in powers], ["发电站1", "发电站2"])
        self.assertEqual(len(powers[0]["slots"]), 1)
        self.assertEqual(powers[0]["slots"][0]["name"], "芳汀")
        self.assertTrue(powers[1]["slots"][0]["empty"])
        # 宿舍：空房间补 5 个 AUTO 位
        dorm = context["dormitories"][0]
        self.assertEqual(dorm["level"], 5)
        self.assertEqual(len(dorm["slots"]), 5)
        self.assertTrue(all(s.get("empty") for s in dorm["slots"]))
        self.assertEqual(dorm["slots"][0]["face"], "AUTO")
        # 会客室：1 人 + 1 空位（容量 2）；办公室：0 人 + 1 空位
        meeting = context["meeting"]
        assert meeting is not None
        self.assertEqual(meeting["level"], 3)
        self.assertEqual(len(meeting["slots"]), 2)
        self.assertEqual(meeting["slots"][0]["name"], "微风")
        self.assertTrue(meeting["slots"][1]["empty"])
        hire = context["hire"]
        assert hire is not None
        self.assertEqual(hire["level"], 3)
        self.assertEqual(len(hire["slots"]), 1)
        self.assertTrue(hire["slots"][0]["empty"])
        # 训练室：受训位芬（无心情口径）+ 协助位空槽；快照久远 → 已完成
        training = context["training"]
        self.assertEqual(training["level"], 3)
        self.assertEqual(training["state"], "已完成")
        self.assertEqual(len(training["slots"]), 2)
        self.assertEqual(training["slots"][0]["name"], "芬")
        self.assertIsNone(training["slots"][0]["morale"])
        self.assertTrue(training["slots"][1]["empty"])
        self.assertEqual(training["slots"][1]["label"], "协助位")
        # 低心情计数：工作房间聚合（fixture 共 8 人 < 12）
        self.assertEqual(context["tired_count"], 8)

    async def test_context_with_item_names(self) -> None:
        context = await self._build_context(
            {"3003": {"name": "赤金"}, "2003": {"name": "高级作战记录"}}
        )
        assert context is not None
        formulas = [m["formula"] for m in context["manufactures"]]
        self.assertIn("赤金×1", formulas)

    async def test_render_contract(self) -> None:
        """ark_building.html 与 builder context 的 schema 契约（jinja 全量渲染）。"""
        context = await self._build_context(None)
        assert context is not None
        template = ark_templates.get_template("ark_building.html")
        html = template.render(font_css_url="about:blank", **context)
        for marker in (
            "基建总览", "无人机", "待换班", "公开招募",
            "控制中枢", "贸易站", "制造站", "发电站1",
            "会客室", "办公室", "宿舍1", "训练室",
            "物品#3003×1", "龙门币",
            "企鹅物流",  # 会客室线索板
            "自动补位", "协助位",
            "专精训练已完成",  # fixture 快照久远 → 折算为已完成
        ):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()


class TrainingContextTest(unittest.IsolatedAsyncioTestCase):
    """ark_training.html 上下文契约：受训中/已完成/空闲三态。"""

    def _payload_with_training(self, training: dict | None) -> dict:
        payload = _player_info()
        if training is not None:
            payload["data"]["building"]["training"] = training
        else:
            payload["data"]["building"].pop("training", None)
        return payload

    async def _context(self, payload: dict) -> dict | None:
        from unittest import mock

        from ArknightsUID.utils.player.context import build_training_context

        data = parse_player_info(payload)
        assert data is not None
        building = parse_building(data)
        assert building is not None
        with (
            mock.patch(
                "ArknightsUID.utils.player.context.char_portrait_b64",
                new=mock.AsyncMock(return_value="P"),
            ),
            mock.patch(
                "ArknightsUID.utils.player.context.char_avatar_b64",
                new=mock.AsyncMock(return_value="A"),
            ),
        ):
            return await build_training_context(data, building, VERSION, "123***789")

    async def test_training_state_fields(self) -> None:
        import time as time_mod

        now = int(time_mod.time())
        ctx = await self._context(self._payload_with_training({
            "level": 3, "speed": 1.3, "remainSecs": 8100,
            "lastUpdateTime": now, "trainee": {"charId": "char_002_amiya"},
        }))
        assert ctx is not None
        self.assertEqual(ctx["trainee"], "阿米娅")
        self.assertEqual(ctx["state"], "受训中")
        self.assertEqual(ctx["state_en"], "TRAINING")
        self.assertEqual(ctx["remain_text"], "约2小时15分")
        self.assertTrue(ctx["finish_at"])
        self.assertIsNotNone(ctx["pct"])
        assert ctx["pct"] is not None
        self.assertTrue(0 <= ctx["pct"] <= 100)
        self.assertEqual(ctx["speed"], 1.3)
        self.assertEqual(ctx["portrait"], "P")
        self.assertEqual(ctx["avatar"], "A")

    async def test_done_state(self) -> None:
        import time as time_mod

        now = int(time_mod.time())
        ctx = await self._context(self._payload_with_training({
            "level": 3, "speed": 1.0, "remainSecs": 0,
            "lastUpdateTime": now, "trainee": {"charId": "char_002_amiya"},
        }))
        assert ctx is not None
        self.assertEqual(ctx["state"], "已完成")
        self.assertEqual(ctx["remain_text"], "已完成")
        self.assertEqual(ctx["finish_at"], "")

    async def test_idle_state(self) -> None:
        ctx = await self._context(self._payload_with_training(None))
        assert ctx is not None
        self.assertEqual(ctx["state"], "空闲")
        self.assertEqual(ctx["trainee"], "")
        self.assertIsNone(ctx["pct"])
        self.assertEqual(ctx["portrait"], "")

    async def test_target_skill_and_trainer(self) -> None:
        """targetSkill + chars 当前专精 → 目标档；基准时长按档取（专精二 16h）。"""
        import time as time_mod
        from unittest import mock

        now = int(time_mod.time())
        payload = self._payload_with_training({
            "level": 3, "speed": 1.0, "remainSecs": 8100,
            "lastUpdateTime": now,
            "trainee": {"charId": "char_002_amiya", "targetSkill": 1},
            "trainer": {"charId": "char_1024_hbisc2"},
        })
        payload["data"]["chars"] = [{
            "charId": "char_002_amiya",
            "skills": [
                {"id": "skcom_amiya_1", "specializeLevel": 3},
                {"id": "skchr_amiya_2", "specializeLevel": 1},
                {"id": "skchr_amiya_3", "specializeLevel": 0},
            ],
        }]
        with mock.patch(
            "ArknightsUID.utils.gamedata.charinfo.load_skill_names",
            new=mock.AsyncMock(return_value={"skchr_amiya_2": "精神爆发"}),
        ):
            ctx = await self._context(payload)
        assert ctx is not None
        self.assertEqual(ctx["skill_label"], "2技能")
        self.assertEqual(ctx["skill_name"], "精神爆发")
        self.assertEqual((ctx["mastery_from"], ctx["mastery_to"]), (1, 2))
        self.assertTrue(ctx["base_exact"])
        # 16h 基准：total=57600，pct=(1-8100/57600)*100≈86
        self.assertEqual(ctx["pct"], 86)
        self.assertEqual(ctx["trainer"], "濯尘芙蓉")
        self.assertEqual(ctx["trainer_avatar"], "A")

    async def test_target_skill_without_chars_falls_back(self) -> None:
        """chars 缺失推不出目标档：base_exact=False 按 24h 兜底，不臆造。"""
        import time as time_mod

        now = int(time_mod.time())
        ctx = await self._context(self._payload_with_training({
            "level": 3, "speed": 1.0, "remainSecs": 8100,
            "lastUpdateTime": now,
            "trainee": {"charId": "char_002_amiya", "targetSkill": 1},
        }))
        assert ctx is not None
        self.assertEqual(ctx["skill_label"], "2技能")
        self.assertEqual(ctx["skill_name"], "")
        self.assertEqual(ctx["mastery_to"], 0)
        self.assertFalse(ctx["base_exact"])

    async def test_template_renders(self) -> None:
        """模板契约：三态上下文都能被 jinja 渲染（漏键/类型错误会抛错）。"""
        import time as time_mod

        template = ark_templates.get_template("ark_training.html")
        for training in (
            {"level": 3, "speed": 1.3, "remainSecs": 8100,
             "lastUpdateTime": int(time_mod.time()),
             "trainee": {"charId": "char_002_amiya"}},
            None,
        ):
            ctx = await self._context(self._payload_with_training(training))
            html = template.render(font_css_url="about:blank", **ctx)
            self.assertIn("专精", html)
