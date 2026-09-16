"""颜文字字体包（kaomoji_fonts）单元测试。

覆盖：字体字节校验、包发现/CSS 生成、系统符号字体发现、单飞下载与
冷却语义、render_html 注入、takumi 字节注册。涉及模块级缓存的用例
在 setUp/tearDown 显式保存/恢复，避免污染其他测试。

运行：在 gsuid_core venv 下 `python -m pytest tests/test_kaomoji_fonts.py -v`
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

_TTF_MAGIC = b"\x00\x01\x00\x00"


def _fake_font(size: int = 50_000) -> bytes:
    """魔数合法 + 尺寸过关的伪字体字节。"""
    return _TTF_MAGIC + b"\x00" * (size - 4)


class IsValidFontTest(unittest.TestCase):
    def test_valid_ttf(self) -> None:
        from ArknightsUID.utils.render.kaomoji_fonts import is_valid_font

        self.assertTrue(is_valid_font(_fake_font()))

    def test_reject_html_error_page(self) -> None:
        """镜像错误页（HTML 文档）必须被魔数拦下（实测曾下到 35KB HTML）。"""
        from ArknightsUID.utils.render.kaomoji_fonts import is_valid_font

        page = b"<!DOCTYPE html>" + b"x" * 40_000
        self.assertFalse(is_valid_font(page))

    def test_reject_small_and_oversize(self) -> None:
        from ArknightsUID.utils.render.kaomoji_fonts import is_valid_font

        self.assertFalse(is_valid_font(_TTF_MAGIC + b"\x00" * 100))
        self.assertFalse(is_valid_font(_fake_font(9_000_000)))


class PackDiscoveryTest(unittest.TestCase):
    """包发现 / CSS 生成（FONT_PACK_DIR 指到临时目录）。"""

    def setUp(self) -> None:
        from ArknightsUID.utils.render import kaomoji_fonts as kf

        self.kf = kf
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self._dir = kf.FONT_PACK_DIR
        kf.FONT_PACK_DIR = self.tmp / "kaomoji"  # type: ignore[misc]

    def tearDown(self) -> None:
        self.kf.FONT_PACK_DIR = self._dir  # type: ignore[misc]

    def test_missing_dir_is_not_complete(self) -> None:
        self.assertFalse(self.kf.pack_complete())
        self.assertEqual(self.kf.pack_font_paths(), [])
        self.assertEqual(self.kf.font_face_css("http://127.0.0.1:8080"), "")

    def test_partial_pack(self) -> None:
        self.kf.FONT_PACK_DIR.mkdir(parents=True)
        (self.kf.FONT_PACK_DIR / "NotoSansYi-Regular.ttf").write_bytes(_fake_font())
        # 损坏文件（HTML 错误页残留）：字节注册时必须被剔除
        bad = self.kf.FONT_PACK_DIR / "NotoSans-Regular.ttf"
        bad.write_bytes(b"<!DOCTYPE html>" + b"x" * 40_000)

        self.assertFalse(self.kf.pack_complete())
        paths = self.kf.pack_font_paths()
        self.assertEqual([p.name for p in paths], ["NotoSansYi-Regular.ttf"])

        # CSS 按存在性生成（坏文件最多让页面回退系统字体，无副作用）
        css = self.kf.font_face_css("http://127.0.0.1:8080")
        self.assertIn('"Noto Sans Yi"', css)
        self.assertIn('"Noto Sans"', css)
        self.assertIn("http://127.0.0.1:8080/ark/fonts/kaomoji/", css)

    def test_full_pack(self) -> None:
        self.kf.FONT_PACK_DIR.mkdir(parents=True)
        for filename, _, _ in self.kf._FONT_PACK:
            (self.kf.FONT_PACK_DIR / filename).write_bytes(_fake_font())
        self.assertTrue(self.kf.pack_complete())
        self.assertEqual(len(self.kf.pack_font_paths()), len(self.kf._FONT_PACK))
        # 每个字体一条 @font-face，family 与文件一一对应
        css = self.kf.font_face_css("http://127.0.0.1:8080")
        self.assertEqual(css.count("@font-face"), len(self.kf._FONT_PACK))
        # 括号必须平衡：曾因 f-string 转义漏改产出 `}}`，CSS 解析器错误
        # 恢复会链式吞掉后续 @font-face（实测首条之后全部失效）
        self.assertEqual(css.count("{"), css.count("}"))
        for rule in css.split("\n"):
            self.assertTrue(rule.endswith("}"), rule)
            self.assertFalse(rule.endswith("}}"), rule)


class SystemSymbolFontsTest(unittest.TestCase):
    def test_only_existing_paths_returned(self) -> None:
        from ArknightsUID.utils.render import kaomoji_fonts as kf

        with mock.patch.object(
            kf,
            "_SYSTEM_SYMBOL_FONTS",
            (Path("/definitely/not/exists/Symbola.ttf"), Path("/usr")),
        ):
            self.assertEqual(kf.system_symbol_font_paths(), [Path("/usr")])


class DownloadTaskTest(unittest.IsolatedAsyncioTestCase):
    """单飞下载 + 失败冷却语义（下载本体 mock，不碰网络）。"""

    def setUp(self) -> None:
        from ArknightsUID.utils.render import kaomoji_fonts as kf

        self.kf = kf
        self._callbacks = list(kf._on_pack_ready)
        kf._pack_task = None
        kf._fail_not_before = 0.0
        kf._on_pack_ready = []

    def tearDown(self) -> None:
        self.kf._pack_task = None
        self.kf._fail_not_before = 0.0
        self.kf._on_pack_ready = self._callbacks

    async def test_complete_pack_never_schedules(self) -> None:
        with mock.patch.object(self.kf, "pack_complete", return_value=True):
            self.kf.maybe_start_download()
        self.assertIsNone(self.kf._pack_task)

    async def test_single_flight(self) -> None:
        started = asyncio.Event()

        async def slow_ensure() -> tuple[bool, bool]:
            started.set()
            await asyncio.sleep(0.2)
            return False, False

        with (
            mock.patch.object(self.kf, "pack_complete", return_value=False),
            mock.patch.object(self.kf, "ensure_font_pack", slow_ensure),
        ):
            self.kf.maybe_start_download()
            self.assertIsNotNone(self.kf._pack_task)
            await started.wait()
            self.kf.maybe_start_download()  # 任务在跑：不得再建
            task_in_flight = self.kf._pack_task
            self.kf.maybe_start_download()
            self.assertIs(self.kf._pack_task, task_in_flight)
            await task_in_flight
        # 失败未补齐 → 进入冷却
        self.assertGreater(self.kf._fail_not_before, 0.0)

    async def test_cooldown_blocks_reschedule(self) -> None:
        self.kf._fail_not_before = 1e12  # 永久冷却
        with mock.patch.object(self.kf, "pack_complete", return_value=False):
            self.kf.maybe_start_download()
        self.assertIsNone(self.kf._pack_task)

    async def test_ready_callback_fires_on_change(self) -> None:
        cb = mock.Mock()
        self.kf.register_on_pack_ready(cb)
        with (
            mock.patch.object(self.kf, "pack_complete", return_value=False),
            mock.patch.object(self.kf, "ensure_font_pack", return_value=(True, True)),
        ):
            self.kf.maybe_start_download()
            await self.kf._pack_task
        cb.assert_called_once()


class RenderIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """render_html 注入 kaomoji_font_css + takumi 字节注册含包字体。"""

    def setUp(self) -> None:
        from ArknightsUID.utils.render import render_utils as ru

        self.ru = ru
        self._cache = ru._local_font_bytes

    def tearDown(self) -> None:
        self.ru._local_font_bytes = self._cache

    async def test_local_cjk_fonts_include_pack(self) -> None:
        """包/系统符号字体字节进入 takumi 注册列表；单点读失败降级跳过。"""
        import tempfile

        from ArknightsUID.utils.render import kaomoji_fonts as kf

        tmp_font = Path(tempfile.mkdtemp()) / "NotoSansYi-Regular.ttf"
        tmp_font.write_bytes(_fake_font())

        self.ru._local_font_bytes = None
        with (
            mock.patch.object(kf, "pack_font_paths", return_value=[tmp_font]),
            mock.patch.object(
                kf,
                "system_symbol_font_paths",
                return_value=[tmp_font],  # 同路径只应注册一次的去重由调用方语义保证
            ),
        ):
            fonts = self.ru._local_cjk_fonts()
        self.assertIn(tmp_font.read_bytes(), fonts)

        # 包文件读失败（文件在列出后消失/坏块）→ 跳过不抛，其余字体照常
        self.ru._local_font_bytes = None
        ghost = tmp_font.parent / "vanished.ttf"
        with (
            mock.patch.object(kf, "pack_font_paths", return_value=[ghost]),
            mock.patch.object(kf, "system_symbol_font_paths", return_value=[]),
        ):
            fonts_after = self.ru._local_cjk_fonts()
        self.assertNotIn(_fake_font(), fonts_after)
        self.assertIsInstance(fonts_after, list)

    async def test_render_html_injects_kaomoji_css(self) -> None:
        """本地渲染把内联 @font-face 传入模板上下文并触发后台补包。"""
        from ArknightsUID.utils.render import render_utils as ru

        captured: dict = {}

        class _FakeTemplate:
            def render(self, **ctx) -> str:
                captured.update(ctx)
                return (
                    '<html><body><div class="container" '
                    'style="width:100px;height:50px;">x</div></body></html>'
                )

        fake_templates = mock.Mock()
        fake_templates.get_template.return_value = _FakeTemplate()

        with (
            mock.patch.object(
                ru, "_render_local_takumi", return_value=b"\xff\xd8\xff fake"
            ),
            mock.patch.object(
                ru.kaomoji_fonts,
                "font_face_css",
                return_value="@font-face{marker}",
            ) as m_css,
            mock.patch.object(ru.kaomoji_fonts, "maybe_start_download") as m_sched,
        ):
            img = await ru.render_html(
                fake_templates, "whatever.html", {"version": "t"}
            )
        self.assertEqual(img, b"\xff\xd8\xff fake")
        m_css.assert_called_once()
        m_sched.assert_called_once()
        self.assertEqual(captured["kaomoji_font_css"], "@font-face{marker}")


if __name__ == "__main__":
    unittest.main()
