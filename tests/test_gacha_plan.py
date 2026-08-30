"""抽卡规划（攒抽计算器）核心测试。

概率 DP 与一图流前端预计算表（bundle main-DPBXoxQ6.js 的 lEe/sEe，
提取自 ark.yituliu.cn/tools/gachaCalc）逐点对照：
- lEe：双 UP 池（指定限定 35%）"拿到限定"累计概率，301 项
- sEe：同池"限定+常驻陪跑六星"累计概率，301 项
前 50 抽（恒定概率区）应逐点一致（表 4 位小数舍入级）；
50 抽递增区一图流实现为黑盒（混合口径），整体容差放宽。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock
from pathlib import Path

from ArknightsUID.utils.gacha_plan import (
    ORUNDUM_PER_DRAW,
    UP_SHARE_DOUBLE,
    UP_SHARE_SINGLE,
    WELL_PULLS,
    build_summary,
    build_up_table,
    chance_within,
    parse_plan_args,
    resources_to_draws,
    six_rate,
    up_share_for,
)

FIXTURE = Path(__file__).parent / "fixtures_ytl_prob.json"


def load_tables() -> tuple[list[float], list[float]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["lEe"], data["sEe"]


class ProbabilityTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.limited_ref, cls.limited_off_ref = load_tables()
        cls.limited = build_up_table(UP_SHARE_DOUBLE)
        cls.limited_off = build_up_table(UP_SHARE_DOUBLE, want_off=1)

    def test_table_shape(self) -> None:
        self.assertEqual(len(self.limited), WELL_PULLS + 1)
        self.assertEqual(self.limited[0], 0.0)
        self.assertEqual(self.limited[WELL_PULLS], 1.0)  # 井

    def test_flat_region_matches_yituliu_exactly(self) -> None:
        """lEe（单限定）是解析表：前 50 抽恒定概率区逐点一致（4 位舍入容差）。"""
        for i in range(1, 51):
            self.assertAlmostEqual(
                self.limited[i], self.limited_ref[i], delta=5e-5,
                msg=f"单限定 n={i}",
            )

    def test_limited_off_tracks_simulated_table(self) -> None:
        """sEe（限定+陪跑）是蒙特卡洛模拟表：n=11 记 0.0051 而精确解 0.0044，
        噪声随概率增大收窄——只做结构性对照（恒定区相对偏差 <20%）。"""
        for i in range(1, 51):
            ref = self.limited_off_ref[i]
            mine = self.limited_off[i]
            if ref <= 0.0:
                self.assertEqual(mine, 0.0, msg=f"n={i}")
                continue
            self.assertLess(
                abs(mine - ref) / max(ref, 1e-4),
                0.20,
                msg=f"限定+陪跑 n={i}: mine={mine:.5f} ref={ref:.5f}",
            )

    def test_monotonic_and_endpoints(self) -> None:
        for table in (self.limited, self.limited_off):
            for i in range(WELL_PULLS):
                self.assertGreaterEqual(table[i + 1], table[i] - 1e-12)

    def test_increment_region_within_tolerance(self) -> None:
        """递增区：一图流为黑盒混合口径，整体偏差有界。"""
        worst_limited = max(
            abs(a - b) for a, b in zip(self.limited, self.limited_ref)
        )
        worst_off = max(
            abs(a - b)
            for a, b in zip(self.limited_off, self.limited_off_ref)
        )
        self.assertLess(worst_limited, 0.011)
        self.assertLess(worst_off, 0.045)

    def test_single_up_share(self) -> None:
        table = build_up_table(UP_SHARE_SINGLE)
        self.assertAlmostEqual(table[1], 0.014, places=6)  # 2%×70%

    def test_start_pity_raises_chance(self) -> None:
        base = chance_within(1, up_share=UP_SHARE_SINGLE)
        boosted = chance_within(
            1, up_share=UP_SHARE_SINGLE, start_pity=60
        )
        self.assertGreater(boosted, base)
        # 垫刀 60：第 61 抽 p6 = 2%+2.5%×11 = 29.5%，UP = 20.65%
        self.assertAlmostEqual(boosted, 0.295 * 0.7, places=6)


class SixRateTest(unittest.TestCase):
    def test_rate_schedule(self) -> None:
        self.assertEqual(six_rate(0), 0.02)
        self.assertEqual(six_rate(49), 0.02)      # 第 50 抽仍为基础
        self.assertEqual(six_rate(50), 0.045)     # 第 51 抽起递增
        self.assertEqual(six_rate(51), 0.07)
        self.assertAlmostEqual(six_rate(88), 0.995)   # 第 89 抽
        self.assertEqual(six_rate(89), 1.0)       # 第 90 抽起封顶
        self.assertEqual(six_rate(98), 1.0)


class ResourceConversionTest(unittest.TestCase):
    def test_yituliu_formula(self) -> None:
        # 一图流换算：玉/600 + 石×0.3 + 单 + 十×10
        self.assertEqual(resources_to_draws(600), 1.0)
        self.assertAlmostEqual(resources_to_draws(0, 10), 3.0)
        self.assertEqual(resources_to_draws(single_tickets=3), 3.0)
        self.assertEqual(resources_to_draws(ten_tickets=2), 20.0)
        total = resources_to_draws(
            orundum=12000, originium=30, single_tickets=2, ten_tickets=1
        )
        self.assertAlmostEqual(total, 20 + 9 + 2 + 10)

    def test_orundum_per_draw_constant(self) -> None:
        self.assertEqual(ORUNDUM_PER_DRAW, 600)


class ParsePlanArgsTest(unittest.TestCase):
    def test_full_keys(self) -> None:
        args = parse_plan_args(
            "玉:12000 源石:30 单抽:2 十连:1 垫刀:15 类型:双UP 池:深池 天数:10"
        )
        self.assertEqual(args.orundum, 12000)
        self.assertEqual(args.originium, 30)
        self.assertEqual(args.single_tickets, 2)
        self.assertEqual(args.ten_tickets, 1)
        self.assertEqual(args.pity, 15)
        self.assertEqual(args.up_mode, "双UP")
        self.assertEqual(args.pool_keyword, "深池")
        self.assertEqual(args.days, 10)
        self.assertEqual(args.errors, [])

    def test_bare_number_is_target(self) -> None:
        self.assertEqual(parse_plan_args("300").target_draws, 300)
        self.assertIsNone(parse_plan_args("300").pity)

    def test_unicode_digit_token_not_crashing(self) -> None:
        # `①`.isdigit() 为 True 但 int() 抛 ValueError：应按卡池关键词处理
        args = parse_plan_args("①")
        self.assertIsNone(args.target_draws)
        self.assertEqual(args.pool_keyword, "①")

    def test_bare_word_is_pool_keyword(self) -> None:
        self.assertEqual(parse_plan_args("深池").pool_keyword, "深池")

    def test_aliases_and_fullwidth_colon(self) -> None:
        args = parse_plan_args("合成玉：600 石头：5 刀：10")
        self.assertEqual(args.orundum, 600)
        self.assertEqual(args.originium, 5)
        self.assertEqual(args.pity, 10)

    def test_unknown_param_recorded(self) -> None:
        args = parse_plan_args("foo:1")
        self.assertEqual(len(args.errors), 1)

    def test_non_numeric_value_recorded(self) -> None:
        args = parse_plan_args("玉:abc")
        self.assertEqual(len(args.errors), 1)

    def test_negative_clamped(self) -> None:
        args = parse_plan_args("玉:-5")
        self.assertEqual(args.orundum, 0.0)


class BuildSummaryTest(unittest.TestCase):
    POOL = {
        "title": "·深池唤响·",
        "end": "2026-09-01 04:00",
        "end_ts": 1789996800.0,  # 固定值，配合 days 计算用相对时间验证
    }

    def test_summary_fields(self) -> None:
        import time

        pool = dict(self.POOL, end_ts=time.time() + 10 * 86400)
        args = parse_plan_args("玉:60000 垫刀:20")
        summary = build_summary(args, pool, pity=20)
        self.assertEqual(summary.current_draws, 100)
        self.assertAlmostEqual(summary.days_left, 10, delta=0.1)
        self.assertEqual(summary.pity, 20)
        self.assertEqual(summary.pity_source, "手动指定")
        # 井缺口 = 300 - 100（库存折算），不扣垫刀（井计数不跨池）
        self.assertEqual(summary.well_gap, 200)
        self.assertAlmostEqual(summary.daily_needed, 20, delta=0.1)
        self.assertGreater(summary.chance_now, 0.5)
        self.assertLess(summary.chance_now, summary.chance_50)
        self.assertEqual(summary.up_share, UP_SHARE_SINGLE)

    def test_double_up_mode(self) -> None:
        args = parse_plan_args("类型:双UP")
        self.assertEqual(up_share_for(args.up_mode), UP_SHARE_DOUBLE)
        summary = build_summary(args, None, pity=0)
        self.assertEqual(summary.up_share, UP_SHARE_DOUBLE)

    def test_no_pool_no_daily(self) -> None:
        summary = build_summary(parse_plan_args(""), None, pity=0)
        self.assertEqual(summary.daily_needed, 0.0)
        self.assertEqual(summary.pool_name, "")

    def test_auto_pity_source(self) -> None:
        summary = build_summary(parse_plan_args(""), None, pity=37)
        self.assertEqual(summary.pity, 37)
        self.assertEqual(summary.pity_source, "本地记录")


if __name__ == "__main__":
    unittest.main()


class DisplayLayerTest(unittest.TestCase):
    def test_plan_context_and_text(self) -> None:
        from ArknightsUID.utils.gacha_plan import (
            build_plan_context,
            format_plan_text,
        )

        args = parse_plan_args("玉:60000 石:30 单:2 十:1 垫刀:37")
        summary = build_summary(args, None, pity=37)
        ctx = build_plan_context(summary, "博士", "12****89", "v9.9.9")
        self.assertEqual(ctx["current_draws"], 121)
        self.assertEqual(ctx["pity"], 37)
        self.assertEqual(ctx["pity_source"], "手动指定")
        self.assertEqual(ctx["res_text"], "60000 玉 + 30 石 + 2 单 + 1 十")
        self.assertEqual(ctx["well_gap"], 179)
        self.assertEqual(ctx["well_pct"], 40)
        self.assertEqual(ctx["up_share_pct"], "70")
        self.assertEqual(ctx["version"], "v9.9.9")
        for key in ("chance_now", "chance_50", "chance_100", "chance_150"):
            float(ctx[key])  # 数字文案可解析

        text = format_plan_text(summary, "博士", "12****89")
        self.assertIn("「明日方舟」攒抽规划", text)
        self.assertIn("垫刀：37（手动指定）", text)
        self.assertIn("≈ 121 抽", text)
        self.assertIn("井缺口：300 - 121 = 179 抽", text)
        self.assertIn("单UP口径", text)

    def test_empty_resources_display(self) -> None:
        from ArknightsUID.utils.gacha_plan import build_plan_context

        summary = build_summary(parse_plan_args(""), None, pity=0)
        ctx = build_plan_context(summary, "", "")
        self.assertEqual(ctx["res_text"], "无")
        self.assertEqual(ctx["current_draws"], 0)
        self.assertEqual(ctx["name"], "博士")
        self.assertTrue(any("库存" in n for n in ctx["notes"]))


class PickPoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_pool_dataclass_to_dict(self) -> None:
        """_pick_pool 把 PRTS ActivityInfo 转 dict 供 build_summary 消费。"""
        from ArknightsUID.utils.prts.client import ActivityInfo
        from ArknightsUID.arknightsuid_gacha import _pick_pool

        pools = [
            ActivityInfo(
                title="·跨洲远航·", start="2026-08-01 04:00",
                end="2026-09-01 04:00", start_ts=1.0, end_ts=2.0,
            ),
            ActivityInfo(
                title="·联合行动·", start="", end="",
                start_ts=0.0, end_ts=3.0,
            ),
        ]
        with mock.patch(
            "ArknightsUID.arknightsuid_gacha.query_gacha_pools",
            return_value=pools,
        ):
            pool, err = await _pick_pool("")
            self.assertEqual(err, "")
            self.assertEqual(
                pool, {"title": "·跨洲远航·", "end": "2026-09-01 04:00", "end_ts": 2.0}
            )
            pool2, _ = await _pick_pool("联合")
            self.assertEqual(pool2["title"], "·联合行动·")
            miss, err2 = await _pick_pool("不存在的池")
            self.assertIsNone(miss)
            self.assertIn("未找到", err2)


class PlanCommandUidExtractionTest(unittest.IsolatedAsyncioTestCase):
    """ark攒抽：资源参数里的数字不能被误认成 UID/序号（绑定用户被当成未绑定）。"""

    class _Bot:
        def __init__(self) -> None:
            self.sent: list = []

        async def send(self, message, **kwargs):
            self.sent.append(str(message))

    class _Ev:
        text = ""
        group_id = None
        user_id = "u1"
        bot_id = "b1"

    async def test_resource_numbers_not_parsed_as_uid(self) -> None:
        from ArknightsUID.arknightsuid_gacha import plan_gacha_draws

        captured: dict = {}

        class Cred:
            uid = "123456789"
            nickname = "博士"

        async def fake_resolve(user_id, bot_id, uid="", index=0):
            captured["uid"] = uid
            captured["index"] = index
            return Cred()

        async def fake_pity(credential):
            return 20

        bot, ev = self._Bot(), self._Ev()
        ev.text = "玉:60000 石:30 垫刀:20"
        with (
            mock.patch(
                "ArknightsUID.arknightsuid_gacha.resolve_credential",
                new=fake_resolve,
            ),
            mock.patch(
                "ArknightsUID.arknightsuid_gacha._auto_pity", new=fake_pity
            ),
            mock.patch(
                "ArknightsUID.arknightsuid_gacha._pick_pool",
                new=mock.AsyncMock(return_value=(None, "")),
            ),
            mock.patch(
                "ArknightsUID.arknightsuid_gacha.send_card_or_text",
                new=mock.AsyncMock(return_value=None),
            ),
        ):
            await plan_gacha_draws(bot, ev)
        # 参数里的 60000 不再被 extract_uid 抓走：凭据按绑定解析
        self.assertEqual(captured["uid"], "")
        self.assertEqual(captured["index"], 0)

    async def test_index_token_still_works(self) -> None:
        from ArknightsUID.arknightsuid_gacha import plan_gacha_draws

        captured: dict = {}

        async def fake_resolve(user_id, bot_id, uid="", index=0):
            captured["uid"] = uid
            captured["index"] = index
            return "未绑定"  # 文本 → 匿名模式

        bot, ev = self._Bot(), self._Ev()
        ev.text = "玉:60000 #2"
        with (
            mock.patch(
                "ArknightsUID.arknightsuid_gacha.resolve_credential",
                new=fake_resolve,
            ),
            mock.patch(
                "ArknightsUID.arknightsuid_gacha._pick_pool",
                new=mock.AsyncMock(return_value=(None, "")),
            ),
            mock.patch(
                "ArknightsUID.arknightsuid_gacha.send_card_or_text",
                new=mock.AsyncMock(return_value=None),
            ),
        ):
            await plan_gacha_draws(bot, ev)
        self.assertEqual(captured["index"], 2)


class PlanArgHardeningTest(unittest.TestCase):
    """审计 #23/#28：UID token 不当抽数；nan/inf/超范围拒绝。"""

    def test_long_digits_not_target_draws(self) -> None:
        from ArknightsUID.utils.gacha_plan import parse_plan_args

        args = parse_plan_args("149809765")
        self.assertIsNone(args.target_draws)  # 5+ 位纯数字 = UID 候选
        self.assertEqual(args.pool_keyword, "")
        args = parse_plan_args("300")
        self.assertEqual(args.target_draws, 300)  # 1-4 位仍是抽数

    def test_non_finite_and_overflow_rejected(self) -> None:
        from ArknightsUID.utils.gacha_plan import parse_plan_args

        for bad in ("玉:inf", "玉:nan", "刀:nan", "目标:1e309", "玉:-5"):
            args = parse_plan_args(bad)
            self.assertTrue(args.errors, bad)
        # 合理值照常通过
        args = parse_plan_args("玉:60000 刀:30 目标:300")
        self.assertEqual(args.errors, [])
        self.assertEqual(args.orundum, 60000)

    def test_absurd_magnitude_rejected(self) -> None:
        from ArknightsUID.utils.gacha_plan import parse_plan_args

        args = parse_plan_args("刀:99999")
        self.assertTrue(args.errors)  # 超出 pity 合理上限
