"""全模板渲染回归：业务卡用预览假数据走真实渲染管线出图。

与 tools/render_previews.py 同源（MOCK_CONTEXTS）——新增业务模板必须
同步补预览假数据，否则覆盖守恒断言失败。Playwright 不可用时渲染用例
整体跳过（覆盖断言不依赖渲染引擎，始终执行）。

运行：在 gsuid_core venv 下 `python -m pytest tests/test_preview_render.py -v`
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

# 非业务卡：head 是 include 片段；ping 是渲染管线自检（test_p0_render 覆盖）；
# web_login 是手机号网页登录的浏览器页面（自包含样式，不经 PicRender 出图，
# 由 web_login._render_page 直接 HTMLResponse）
_NON_BUSINESS = {"ark_head.html", "ark_ping.html", "ark_web_login.html"}


def _load_preview_module():
    tool_path = Path(__file__).parents[1] / "tools" / "render_previews.py"
    spec = importlib.util.spec_from_file_location("ark_render_previews", tool_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreviewCoverageTest(unittest.TestCase):
    """覆盖守恒：每张业务模板都有预览假数据（防新增卡片漏建假数据）。"""

    def test_every_business_template_has_preview_data(self) -> None:
        module = _load_preview_module()
        templates_dir = Path(__file__).parents[1] / "ArknightsUID" / "templates"
        business = {
            p.name for p in templates_dir.glob("ark_*.html")
        } - _NON_BUSINESS
        self.assertEqual(
            business,
            set(module.MOCK_CONTEXTS),
            "templates/ 业务模板与 render_previews 假数据清单不一致",
        )


class PreviewRenderRegressionTest(unittest.IsolatedAsyncioTestCase):
    """真实渲染管线全量出图（Playwright 页池 + .container 截图）。"""

    async def test_all_business_templates_render(self) -> None:
        from ArknightsUID.utils.render.render_utils import PLAYWRIGHT_AVAILABLE

        if not PLAYWRIGHT_AVAILABLE:
            self.skipTest("Playwright 不可用，跳过渲染回归")

        from ArknightsUID.utils.render import ark_templates, render_html

        module = _load_preview_module()
        for name, context in module.MOCK_CONTEXTS.items():
            with self.subTest(template=name):
                img = await render_html(ark_templates, name, dict(context))
                self.assertIsNotNone(img, f"{name} 渲染返回空")
                self.assertGreater(
                    len(img), 10_000, f"{name} 出图异常偏小，疑似空卡/坏卡"
                )


if __name__ == "__main__":
    unittest.main()
