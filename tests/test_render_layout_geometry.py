"""在真实 Chromium 中检查模板几何；不把裁切、留白或动态长卡当作失真。"""

import base64
import copy
import importlib.util
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from ArknightsUID.utils.render import ark_templates


def _sample_image(width: int, height: int) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class RenderLayoutGeometryTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).parents[1] / "tools" / "render_previews.py"
        spec = importlib.util.spec_from_file_location("geometry_previews", path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"ARK_RENDER_OFFLINE": "1"}):
            spec.loader.exec_module(module)
        cls.contexts = module.MOCK_CONTEXTS

    async def asyncSetUp(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.skipTest("Playwright 不可用")
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context(
            viewport={"width": 1200, "height": 1000},
            device_scale_factor=2,
            offline=True,
        )
        self.page = await self.context.new_page()

    async def _load(self, name: str, context: dict | None = None) -> None:
        sample = copy.deepcopy(self.contexts[name]) if context is None else context
        html = ark_templates.get_template(name).render(
            font_css_url="data:text/css,", **sample
        )
        await self.page.set_content(html, wait_until="load")
        await self.page.evaluate("document.fonts.ready")

    async def test_status_frame_scale_is_uniform(self) -> None:
        await self._load("ark_status.html")
        dimensions = await self.page.locator(".frame").evaluate(
            """frame => {
                const rect = frame.getBoundingClientRect();
                const container = frame.closest('.container').getBoundingClientRect();
                const matrix = new DOMMatrix(getComputedStyle(frame).transform);
                return {width: rect.width, height: rect.height,
                    containerWidth: container.width, containerHeight: container.height,
                    scaleX: matrix.a, scaleY: matrix.d, skewX: matrix.b, skewY: matrix.c};
            }"""
        )
        self.assertAlmostEqual(dimensions["scaleX"], 0.56)
        self.assertAlmostEqual(dimensions["scaleY"], 0.56)
        self.assertEqual(dimensions["skewX"], 0)
        self.assertEqual(dimensions["skewY"], 0)
        self.assertAlmostEqual(dimensions["width"], 560, delta=0.01)
        self.assertAlmostEqual(dimensions["height"], 358.4, delta=0.01)
        self.assertAlmostEqual(dimensions["width"], dimensions["containerWidth"])
        self.assertAlmostEqual(
            dimensions["height"], dimensions["containerHeight"], delta=0.01
        )

    async def test_stage_maps_keep_landscape_and_portrait_ratio(self) -> None:
        sample = copy.deepcopy(self.contexts["ark_rogue_stage.html"])
        sample["maps"] = [_sample_image(640, 360), _sample_image(180, 360)]
        await self._load("ark_rogue_stage.html", sample)
        images = await self.page.locator("img:not(.sticker)").evaluate_all(
            """images => images.map(image => {
                const css = getComputedStyle(image);
                const width = image.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight);
                const height = image.clientHeight - parseFloat(css.paddingTop) - parseFloat(css.paddingBottom);
                return {width, height, naturalWidth: image.naturalWidth,
                    naturalHeight: image.naturalHeight, fit: css.objectFit};
            })"""
        )
        self.assertEqual(len(images), 2)
        for image in images:
            with self.subTest(natural_width=image["naturalWidth"]):
                self.assertGreater(image["naturalWidth"], 0)
                self.assertEqual(image["fit"], "fill")
                self.assertAlmostEqual(
                    image["width"] / image["height"],
                    image["naturalWidth"] / image["naturalHeight"],
                    delta=0.01,
                )

    async def test_friends_crop_and_avatar_catalog_contain(self) -> None:
        for kind, expected_fit, expected_size in (
            ("friends", "cover", 48),
            ("avatars", "contain", 88),
        ):
            with self.subTest(kind=kind):
                sample = copy.deepcopy(self.contexts["ark_social.html"])
                sample["kind"] = kind
                sample["rows"][0]["avatar"] = _sample_image(180, 360)
                await self._load("ark_social.html", sample)
                image = await self.page.locator("img:not(.sticker)").evaluate(
                    """image => {const css = getComputedStyle(image);
                        return {width: parseFloat(css.width), height: parseFloat(css.height),
                            naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight,
                            fit: css.objectFit};} """
                )
                self.assertEqual(image["naturalWidth"], 180)
                self.assertEqual(image["naturalHeight"], 360)
                self.assertEqual(image["width"], expected_size)
                self.assertEqual(image["height"], expected_size)
                self.assertEqual(image["fit"], expected_fit)

    async def test_building_long_card_keeps_footer_inside(self) -> None:
        await self._load("ark_building.html")
        geometry = await self.page.locator(".container").evaluate(
            """container => {
                const rect = container.getBoundingClientRect();
                const footer = container.querySelector('.ticker').getBoundingClientRect();
                return {width: rect.width, height: rect.height,
                    footerBottom: footer.bottom - rect.top,
                    outsideRooms: [...container.querySelectorAll('.room, .op')].filter(element => {
                        const bounds = element.getBoundingClientRect();
                        return bounds.left < rect.left || bounds.right > rect.right || bounds.bottom > rect.bottom;
                    }).length};
            }"""
        )
        self.assertEqual(geometry["width"], 560)
        self.assertGreater(geometry["height"], 1000)
        self.assertLessEqual(geometry["footerBottom"], geometry["height"])
        self.assertEqual(geometry["outsideRooms"], 0)
