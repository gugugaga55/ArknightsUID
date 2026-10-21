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


class TakumiRuntimeInstallTest(unittest.TestCase):
    """审计 #35：ark安装依赖 takumi 装完后本进程即时生效（无需重载）。"""

    def test_import_retry_picks_up_late_install(self) -> None:
        from types import SimpleNamespace

        from ArknightsUID.utils.render import render_utils as ru

        fake = SimpleNamespace(html_to_pic=lambda *a, **k: b"jpg")
        with mock.patch.object(ru, "pytakumi", None), mock.patch.object(
            ru, "TAKUMI_AVAILABLE", False
        ), mock.patch.object(
            ru, "_import_takumi", new=mock.Mock(return_value=fake)
        ) as m_import:
            # 首次调用：全局为 None → 重试导入 → 拿到"刚装好"的模块
            self.assertIs(ru._takumi_module(), fake)
            # 已拿到后不再重复 import
            self.assertIs(ru._takumi_module(), fake)
        self.assertEqual(m_import.call_count, 1)
        self.assertTrue(ru.TAKUMI_AVAILABLE)

    def test_render_via_takumi_uses_live_module(self) -> None:
        from types import SimpleNamespace

        from ArknightsUID.utils.render import render_utils as ru

        calls: dict = {}

        def fake_html_to_pic(html, **kwargs):
            calls.update(kwargs, html=html)
            return b"fake-jpeg"

        fake = SimpleNamespace(html_to_pic=fake_html_to_pic)
        with mock.patch.object(ru, "pytakumi", None), mock.patch.object(
            ru, "_import_takumi", new=mock.Mock(return_value=fake)
        ):
            out = ru._render_via_takumi("<p>x</p>")
        self.assertEqual(out, b"fake-jpeg")
        self.assertEqual(calls["html"], "<p>x</p>")


@unittest.skipIf(not _takumi_available(), "pytakumi 不可用，跳过 takumi 用例")
class TakumiDirectRenderTest(unittest.TestCase):
    def test_render_via_takumi_returns_jpeg(self) -> None:
        import io

        from PIL import Image
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
        image = Image.open(io.BytesIO(data))
        image.load()
        from ArknightsUID.utils.render.render_utils import _render_scale

        self.assertEqual(image.width, int(560 * _render_scale()))
        self.assertGreater(image.height, 0)

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
        from ArknightsUID.utils.render import ark_templates, render_html, render_utils

        context = {
            "font_css_url": "about:blank",
            "ping_id": "takumi-test",
            "status": "OK",
            "engine": "takumi",
            "template_path": "/tmp",
            "timestamp": str(int(time.time())),
            "version": "test",
        }
        with mock.patch.object(
            render_utils,
            "_acquire_page",
            new=mock.AsyncMock(side_effect=AssertionError("Takumi 不应静默回退")),
        ) as acquire:
            img = await render_html(ark_templates, "ark_ping.html", context)
        acquire.assert_not_awaited()
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
        acquire_page = render_utils._acquire_page

        async def acquire_offline_page():
            page, generation = await acquire_page()
            if page is not None:
                await page.context.set_offline(True)
            return page, generation

        with (
            mock.patch.object(
                render_utils,
                "_render_via_takumi",
                side_effect=RuntimeError("模拟 takumi 故障"),
            ),
            mock.patch.object(render_utils, "_acquire_page", acquire_offline_page),
            mock.patch.object(render_utils.kaomoji_fonts, "maybe_start_download"),
        ):
            try:
                img = await render_utils.render_html(
                    ark_templates, "ark_ping.html", context
                )
            finally:
                if render_utils._browser is not None:
                    await render_utils._browser.close()
                if render_utils._playwright is not None:
                    await render_utils._playwright.stop()
        self.assertIsNotNone(img)
        assert img is not None
        self.assertTrue(img.startswith(_JPEG_MAGIC))
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(img))
        image.load()
        self.assertEqual(image.width, int(560 * render_utils._render_scale()))


if __name__ == "__main__":
    unittest.main()
