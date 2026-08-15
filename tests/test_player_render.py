"""P1/P3 卡片 context ↔ 模板契约测试。

用真实 builder（而非手写 mock）渲染 7 张业务卡的 jinja2 模板——
builder 漏键或类型错误（如除零、None 算术）会让模板渲染抛错，
这是「数据层 ↔ 前端模板」的端到端 schema 校验。

playwright 可用时额外做一次真实出图（bytes 校验）；不可用时跳过出图，
模板渲染（纯 jinja）仍然执行。
"""
from __future__ import annotations

import unittest

from test_player_parse import _envelope

from ArknightsUID.utils.gamedata import charinfo
from ArknightsUID.utils.hypergryph import gacha as gacha_svc
from ArknightsUID.utils.player import (
    build_box_context,
    build_campaign_context,
    build_char_card_context,
    build_char_stat_context,
    build_daily_context,
    build_status_context,
    parse_player_info,
)
from ArknightsUID.utils.render import ark_templates

try:
    from ArknightsUID.ArknightsUID import arknightsuid_help
except ModuleNotFoundError:
    from ArknightsUID import arknightsuid_help

VERSION = "test"


def _render(template_name: str, context: dict) -> str:
    template = ark_templates.get_template(template_name)
    return template.render(font_css_url="about:blank", **context)


class CardContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        envelope = _envelope()
        cls.data = parse_player_info(envelope)
        assert cls.data is not None

    def _assert_card(self, template_name: str, context: dict | None) -> None:
        self.assertIsNotNone(context, f"{template_name} context 不应为空")
        assert context is not None
        html = _render(template_name, context)
        self.assertIn("ArknightsUID", html)

    def test_status_card_contract(self) -> None:
        self._assert_card(
            "ark_status.html",
            build_status_context(self.data, VERSION, "123***789", now=1e9 + 3600),
        )

    def test_daily_card_contract(self) -> None:
        self._assert_card("ark_daily.html", build_daily_context(self.data, VERSION))

    def test_campaign_card_contract(self) -> None:
        self._assert_card(
            "ark_campaign.html", build_campaign_context(self.data, VERSION)
        )

    def test_char_stat_card_contract(self) -> None:
        self._assert_card(
            "ark_char_stat.html", build_char_stat_context(self.data, VERSION)
        )

    def test_box_card_contract(self) -> None:
        self._assert_card("ark_box_analysis.html", build_box_context(self.data, VERSION))

    def test_char_card_contract_without_network(self) -> None:
        # 图标/技能名走惰性下载，测试环境网络失败时降级为空串仍可出卡
        import asyncio

        entry = self.data.chars[0]
        context = asyncio.run(build_char_card_context(entry, VERSION))
        self._assert_card("ark_char_card.html", context)
        self.assertEqual(context["name"], "阿米娅(术师)")
        self.assertEqual(context["rarity"], 5)

    def test_gacha_card_contract(self) -> None:
        records = [
            {"category": "1", "char_id": "c1", "name": "维什戴尔", "rarity": 5,
             "is_new": True, "gacha_ts": 1700000100, "pos": 1, "pool_name": "限定寻访"},
            {"category": "1", "char_id": "c2", "name": "逻各斯", "rarity": 5,
             "is_new": False, "gacha_ts": 1700000200, "pos": 2, "pool_name": "常驻"},
            {"category": "1", "char_id": "c3", "name": "A", "rarity": 3,
             "is_new": False, "gacha_ts": 1700000300, "pos": 3, "pool_name": "常驻"},
        ]
        context = gacha_svc.build_gacha_context("博士", "123***789", records, VERSION)
        self._assert_card("ark_gacha_card.html", context)

    def test_help_card_contract(self) -> None:
        html = _render(
            "ark_help.html",
            {"version": VERSION, "groups": arknightsuid_help.HELP_GROUPS},
        )
        self.assertIn("ark练度卡", html)  # 新命令进入帮助卡
        self.assertIn("ark抽卡记录", html)

    def test_daily_card_zero_total_does_not_divide_by_zero(self) -> None:
        envelope = _envelope()
        envelope["data"]["routine"] = {
            "daily": {"current": 0, "total": 1},
            "weekly": {"current": 0, "total": 1},
        }
        data = parse_player_info(envelope)
        assert data is not None
        context = build_daily_context(data, VERSION)
        self._assert_card("ark_daily.html", context)


class LiveRenderTest(unittest.IsolatedAsyncioTestCase):
    """playwright 可用时的真实出图验证（不可用则跳过）。"""

    async def test_status_renders_to_bytes(self) -> None:
        from ArknightsUID.utils.render import render_html
        from ArknightsUID.utils.render.render_utils import PLAYWRIGHT_AVAILABLE

        if not PLAYWRIGHT_AVAILABLE:
            self.skipTest("playwright 未安装")
        data = parse_player_info(_envelope())
        assert data is not None
        context = build_status_context(data, VERSION, "123***789", now=1e9 + 3600)
        assert context is not None
        img = await render_html(ark_templates, "ark_status.html", context)
        self.assertIsInstance(img, bytes)
        self.assertGreater(len(img), 10000)  # jpeg 截图不可能小于 10KB


if __name__ == "__main__":
    unittest.main()
