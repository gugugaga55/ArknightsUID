"""Takumi 渲染引擎（pytakumi）接入测试。

覆盖：配置默认值、直接渲染、render_html 引擎分发、takumi 失败回退
playwright。pytakumi 未安装时渲染用例跳过（生产同路径会回退 playwright）。

运行：在 gsuid_core venv 下 `python -m pytest tests/test_takumi_render.py -v`
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

_JPEG_MAGIC = b"\xff\xd8"


def _takumi_available() -> bool:
    from ArknightsUID.utils.render import render_utils

    return render_utils.TAKUMI_AVAILABLE


class TakumiConfigTest(unittest.TestCase):
    def test_default_engine_is_takumi(self) -> None:
        from ArknightsUID.utils.config import ArkConfig

        self.assertEqual(ArkConfig.get_config("RenderEngine").data, "takumi")


@unittest.skipIf(not _takumi_available(), "pytakumi 不可用，跳过 takumi 用例")
class TakumiDirectRenderTest(unittest.TestCase):
    def test_render_via_takumi_returns_jpeg(self) -> None:
        from ArknightsUID.utils.render.render_utils import _render_via_takumi

        html = (
            '<html><head><meta charset="utf-8"><style>'
            "body{margin:0;font-family:sans-serif}"
            '.c{width:300px;padding:12px;background:#222;color:#eee}'
            "</style></head><body><div class=\"c\">takumi 中文渲染自检</div></body></html>"
        )
        data = _render_via_takumi(html)
        self.assertTrue(data.startswith(_JPEG_MAGIC))
        self.assertGreater(len(data), 2000)

    def test_local_cjk_fonts_cached(self) -> None:
        from ArknightsUID.utils.render.render_utils import _local_cjk_fonts

        fonts = _local_cjk_fonts()
        # 有系统 CJK 字体则必须命中缓存（二次调用同一对象）
        if fonts:
            self.assertIs(fonts, _local_cjk_fonts())


@unittest.skipIf(not _takumi_available(), "pytakumi 不可用，跳过 takumi 用例")
class TakumiPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_render_html_dispatches_to_takumi(self) -> None:
        """render_html 默认引擎 takumi：出图且为 JPEG。"""
        from ArknightsUID.utils.render import ark_templates, render_html

        context = {
            "font_css_url": "about:blank",
            "ping_id": "takumi-test",
            "status": "OK",
            "engine": "takumi",
            "template_path": "/tmp",
            "timestamp": str(int(time.time())),
            "version": "test",
        }
        img = await render_html(ark_templates, "ark_ping.html", context)
        self.assertIsNotNone(img)
        assert img is not None
        self.assertTrue(img.startswith(_JPEG_MAGIC))

    async def test_render_html_falls_back_to_playwright(self) -> None:
        """takumi 抛异常时自动回退 playwright 出图。"""
        from ArknightsUID.utils.render import ark_templates, render_utils

        if not render_utils.PLAYWRIGHT_AVAILABLE:
            self.skipTest("Playwright 不可用，跳过回退用例")

        context = {
            "font_css_url": "about:blank",
            "ping_id": "fallback-test",
            "status": "OK",
            "engine": "playwright(fallback)",
            "template_path": "/tmp",
            "timestamp": str(int(time.time())),
            "version": "test",
        }
        with mock.patch.object(
            render_utils,
            "_render_via_takumi",
            side_effect=RuntimeError("模拟 takumi 故障"),
        ):
            img = await render_utils.render_html(ark_templates, "ark_ping.html", context)
        self.assertIsNotNone(img)
        assert img is not None
        self.assertTrue(img.startswith(_JPEG_MAGIC))


if __name__ == "__main__":
    unittest.main()
