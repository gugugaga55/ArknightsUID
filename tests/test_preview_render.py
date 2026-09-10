"""全模板渲染回归：业务卡用预览假数据走真实渲染管线出图。

与 tools/render_previews.py 同源（MOCK_CONTEXTS）——新增业务模板必须
同步补预览假数据，否则覆盖守恒断言失败。Playwright 不可用时渲染用例
整体跳过（覆盖断言不依赖渲染引擎，始终执行）。

运行：在 gsuid_core venv 下 `python -m pytest tests/test_preview_render.py -v`
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

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


class RenderClockAndOfflineTest(unittest.TestCase):
    """预览工具的确定性旋钮：--now 钉时钟 / --offline 跳实时数据。

    默认时钟必须钉死（倒计时文本可回归比对）；--now/环境变量可覆盖；
    argv 里模板名与旗标可正确分离。
    """

    def setUp(self) -> None:
        self._env_now = os.environ.pop("ARK_RENDER_NOW", None)
        self._env_off = os.environ.pop("ARK_RENDER_OFFLINE", None)

    def tearDown(self) -> None:
        for key, val in (
            ("ARK_RENDER_NOW", self._env_now),
            ("ARK_RENDER_OFFLINE", self._env_off),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_default_now_is_pinned_constant(self) -> None:
        module = _load_preview_module()
        with mock.patch("sys.argv", ["render_previews.py"]):
            self.assertEqual(module._render_now_ts(), module._DEFAULT_RENDER_NOW)

    def test_now_override_by_env_epoch_and_iso(self) -> None:
        module = _load_preview_module()
        with mock.patch("sys.argv", ["render_previews.py"]):
            os.environ["ARK_RENDER_NOW"] = "1789996800"
            self.assertEqual(module._render_now_ts(), 1789996800.0)
            # ISO 覆盖 == 默认钉值（同一东八区时刻）；日期-only 解析到零点
            os.environ["ARK_RENDER_NOW"] = "2026-09-01 12:00"
            self.assertEqual(module._render_now_ts(), module._DEFAULT_RENDER_NOW)
            os.environ["ARK_RENDER_NOW"] = "2026-09-01"
            self.assertEqual(module._render_now_ts(), module._DEFAULT_RENDER_NOW - 43200.0)

    def test_now_arg_beats_env(self) -> None:
        module = _load_preview_module()
        os.environ["ARK_RENDER_NOW"] = "100"
        with mock.patch("sys.argv", ["render_previews.py", "--now", "200"]):
            self.assertEqual(module._render_now_ts(), 200.0)

    def test_offline_from_arg_and_env(self) -> None:
        module = _load_preview_module()
        with mock.patch("sys.argv", ["render_previews.py"]):
            self.assertFalse(module._offline())
            os.environ["ARK_RENDER_OFFLINE"] = "1"
            self.assertTrue(module._offline())
            del os.environ["ARK_RENDER_OFFLINE"]
            self.assertFalse(module._offline())
        with mock.patch("sys.argv", ["render_previews.py", "--offline"]):
            self.assertTrue(module._offline())

    def test_template_args_strips_flags(self) -> None:
        module = _load_preview_module()
        with mock.patch(
            "sys.argv",
            ["render_previews.py", "--offline", "ark_help", "--now", "2026-09-01", "ark_daily"],
        ):
            self.assertEqual(module._template_args(), ["ark_help", "ark_daily"])

    def test_gacha_plan_fixture_uses_pinned_now(self) -> None:
        """攒抽规划预览走 build_summary(now_ts=钉值)：days_left 跨次渲染不变。"""
        module = _load_preview_module()
        ctx = module.MOCK_CONTEXTS["ark_gacha_plan.html"]
        end_ts = 1e11
        expected = max(0.0, (end_ts - module._DEFAULT_RENDER_NOW) / 86400)
        # 上下文里是 1 位小数格式化串
        self.assertAlmostEqual(float(ctx["days_left"]), expected, delta=0.1)


if __name__ == "__main__":
    unittest.main()
