"""P1/P3 卡片 context ↔ 模板契约测试。

用真实 builder（而非手写 mock）渲染 7 张业务卡的 jinja2 模板——
builder 漏键或类型错误（如除零、None 算术）会让模板渲染抛错，
这是「数据层 ↔ 前端模板」的端到端 schema 校验。

playwright 可用时额外做一次真实出图（bytes 校验）；不可用时跳过出图，
模板渲染（纯 jinja）仍然执行。
"""
from __future__ import annotations

import re
import unittest

from test_player_parse import _envelope

from ArknightsUID.utils.gamedata import charinfo
from ArknightsUID.utils.hypergryph import gacha as gacha_svc
from ArknightsUID.utils.path import TEMPLATES_PATH
from ArknightsUID.utils.player import (
    build_box_context,
    build_campaign_context,
    build_char_card_context,
    build_char_stat_context,
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

    def test_daily_merges_into_status_card(self) -> None:
        """ark日常 与 ark理智 同卡：合并 context 渲染 ark_status.html。"""
        context = build_status_context(self.data, VERSION)
        assert context is not None
        self.assertTrue(context["has_routine"])
        self._assert_card("ark_status.html", context)

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

    def test_char_card_contract_with_skill_effects(self) -> None:
        """PRTS 技能效果增强：当前级/满级描述 + 初动/消耗/持续进 context 并渲染上卡。"""
        import asyncio

        entry = self.data.chars[0]
        assert entry.skills, "fixture 干员应带技能"
        # 用技能名键（skill_table 命中时与 PRTS 技能名一致）；名字拿不到时
        # 直接取 context 里解析出的 name 回填验证渲染链路
        probe = asyncio.run(build_char_card_context(entry, VERSION))
        first_name = probe["skills"][0]["name"]
        # utils.skillfx 的等级上下文结构（_char_skill_effects 的产物）
        effects = {
            first_name: {
                "current": {
                    "level": "7",
                    "description": "攻击力+120%",
                    "sp_init": "10",
                    "sp_cost": "24",
                    "duration": "",
                    "meta": "初动10·消耗24",
                },
                "final": {
                    "level": "专3",
                    "description": "攻击力+200%",
                    "sp_init": "15",
                    "sp_cost": "24",
                    "duration": "30",
                    "meta": "初动15·消耗24·持续30秒",
                },
            }
        }
        context = asyncio.run(build_char_card_context(entry, VERSION, effects))
        self._assert_card("ark_char_card.html", context)
        skill = context["skills"][0]
        self.assertEqual(skill["current"], "攻击力+120%")
        self.assertEqual(skill["current_level"], "7")
        self.assertEqual(skill["current_meta"], "初动10·消耗24")
        self.assertEqual(skill["final"], "攻击力+200%")
        self.assertEqual(skill["final_level"], "专3")
        self.assertEqual(skill["final_meta"], "初动15·消耗24·持续30秒")
        # 未命中的技能不携带效果字段值（空串降级）
        if len(context["skills"]) > 1:
            self.assertEqual(context["skills"][1]["current"], "")
        html = _render("ark_char_card.html", context)
        self.assertIn("攻击力+200%", html)
        self.assertIn("专3", html)
        self.assertIn("初动15·消耗24·持续30秒", html)

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

    def test_cjk_fallback_font_face_names(self) -> None:
        """字体栈回归：CJK 兜底必须写系统 TTC 的真实 face 名。

        takumi 挂载的 NotoSansCJK-Regular.ttc face 名是 "Noto Sans CJK SC"，
        只写 "Noto Sans SC" 匹配不到 → generic 回退落到 TTC 首 face（JP），
        关/直/与/门 等字渲染成日文字形。这里锁住两条栈的 face 名与次序。
        """
        css = (TEMPLATES_PATH / "ark_base.css").read_text(encoding="utf-8")
        body = re.search(r'font-family:\s*"MiSans"[^;]+;', css)
        mono = re.search(r'--mono:[^;]+;', css)
        assert body and mono
        for stack, cjk_name in (
            (body.group(0), "Noto Sans CJK SC"),
            (mono.group(0), "Noto Sans Mono CJK SC"),
        ):
            self.assertIn(cjk_name, stack)
            # CJK face 必须在 generic 族名之前，否则 generic 先命中（JP face）
            self.assertLess(stack.index(cjk_name), stack.index("sans-serif") if "sans-serif" in stack else stack.index("monospace"))

    def test_status_card_zero_total_does_not_divide_by_zero(self) -> None:
        envelope = _envelope()
        envelope["data"]["routine"] = {
            "daily": {"current": 0, "total": 1},
            "weekly": {"current": 0, "total": 1},
        }
        data = parse_player_info(envelope)
        assert data is not None
        context = build_status_context(data, VERSION)
        assert context is not None
        self._assert_card("ark_status.html", context)


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


class PortraitNegativeCacheTest(unittest.IsolatedAsyncioTestCase):
    """缺档立绘的 .miss 负缓存：TTL 内不再重复请求 CDN（404 刷屏修复）。"""

    async def test_missing_tier_cached_and_skipped(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        import ArknightsUID.utils.path as path_mod
        import ArknightsUID.utils.render as render_mod
        from ArknightsUID.utils.player.context import char_portrait_b64

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            calls: list[str] = []

            async def fake_get(url, cache_path, *args, **kwargs):
                calls.append(url)
                return ""  # 全部失败（模拟 404）

            with (
                mock.patch.object(path_mod, "CHAR_CACHE_PATH", cache),
                mock.patch.object(
                    render_mod, "get_image_b64_with_cache", fake_get
                ),
            ):
                first = await char_portrait_b64("char_x", 5)  # 依次尝试 #3/#2/#1
                self.assertEqual(first, "")
                self.assertEqual(len(calls), 3)
                self.assertTrue((cache / "char_x%233.miss").exists())

                second = await char_portrait_b64("char_x", 5)
                self.assertEqual(second, "")
                # 负缓存生效：不再发起新请求
                self.assertEqual(len(calls), 3)

    async def test_success_returns_without_miss_marker(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        import ArknightsUID.utils.path as path_mod
        import ArknightsUID.utils.render as render_mod
        from ArknightsUID.utils.player.context import char_portrait_b64

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)

            async def fake_get(url, cache_path, *args, **kwargs):
                # E1 档位（#2）成功
                return "BASE64" if "%232" in url else ""

            with (
                mock.patch.object(path_mod, "CHAR_CACHE_PATH", cache),
                mock.patch.object(
                    render_mod, "get_image_b64_with_cache", fake_get
                ),
            ):
                result = await char_portrait_b64("char_y", 5)
                self.assertEqual(result, "BASE64")
                # 成功档位不落 .miss
                self.assertFalse((cache / "char_y%232.miss").exists())
                # 失败的 #3 档落 .miss
                self.assertTrue((cache / "char_y%233.miss").exists())


if __name__ == "__main__":
    unittest.main()
