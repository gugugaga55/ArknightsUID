"""真实引擎像素倍率、截图范围与缓存图片裁切回归。"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import DictLoader, Environment
from PIL import Image

from ArknightsUID.utils.render import render_utils as ru


GEOMETRY_HTML = """<!doctype html><html><head><style>
html,body { margin:0; padding:0; }
.container { width:560px; height:358.4px; overflow:hidden; }
.frame { width:1000px; height:640px; transform:scale(.56);
transform-origin:top left; background:#ff0000; position:relative; }
.edge { position:absolute; right:0; bottom:0; width:100px; height:100px;
background:#0000ff; }
</style></head><body><div class="container"><div class="frame">
<div class="edge"></div></div></div></body></html>"""


@pytest.fixture
def render_settings(monkeypatch):
    settings = {
        "RenderScale": 1,
        "RenderEngine": "playwright",
        "RemoteRenderEnable": False,
    }
    original = ru.ArkConfig.get_config
    monkeypatch.setattr(
        ru.ArkConfig,
        "get_config",
        lambda name: (
            SimpleNamespace(data=settings[name]) if name in settings else original(name)
        ),
    )
    monkeypatch.setattr(ru.kaomoji_fonts, "maybe_start_download", lambda: None)
    return settings


def decoded(data):
    assert data is not None
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def assert_geometry(image, scale):
    assert image.width == 560 * scale
    assert abs(image.height - 358.4 * scale) <= scale
    assert image.getpixel((10 * scale, 10 * scale))[0] > 230
    assert (
        image.getpixel((image.width - 10 * scale, image.height - 10 * scale))[2] > 230
    )


@pytest.mark.asyncio
async def test_playwright_scale_changes_apply_to_pooled_pages(render_settings):
    pytest.importorskip("playwright")
    templates = Environment(loader=DictLoader({"geometry.html": GEOMETRY_HTML}))
    try:
        for scale in (1, 2, 3, 1):
            render_settings["RenderScale"] = scale
            image = decoded(await ru.render_html(templates, "geometry.html", {}))
            assert_geometry(image, scale)
    finally:
        if ru._browser is not None:
            await ru._browser.close()
        if ru._playwright is not None:
            await ru._playwright.stop()


@pytest.mark.asyncio
async def test_scale_change_does_not_close_active_page(render_settings):
    pytest.importorskip("playwright")
    first_page = second_page = None
    try:
        first_page, first_generation = await ru._acquire_page()
        render_settings["RenderScale"] = 2
        second_page, second_generation = await ru._acquire_page()
        assert await first_page.evaluate("devicePixelRatio") == 1
        assert await second_page.evaluate("devicePixelRatio") == 2
        first_context = first_page.context
        await ru._release_page(first_page, first_generation)
        first_page = None
        assert not first_context.pages
        await ru._release_page(second_page, second_generation)
        second_page = None
        assert ru._active_renders == 0
    finally:
        if first_page is not None:
            await ru._release_page(first_page, first_generation)
        if second_page is not None:
            await ru._release_page(second_page, second_generation)
        if ru._browser is not None:
            await ru._browser.close()
        if ru._playwright is not None:
            await ru._playwright.stop()


def test_takumi_transformed_frame_geometry_at_each_scale(render_settings):
    pytest.importorskip("pytakumi")
    for scale in (1, 2, 3):
        render_settings["RenderScale"] = scale
        assert_geometry(decoded(ru._render_via_takumi(GEOMETRY_HTML)), scale)


@pytest.mark.asyncio
async def test_image_bake_preserves_aspect_alpha_and_crop_anchor(tmp_path, monkeypatch):
    from ArknightsUID.utils import image as image_utils
    from ArknightsUID.utils import path as paths

    cache = tmp_path / "cache"
    bake = tmp_path / "bake"
    cache.mkdir()
    bake.mkdir()
    monkeypatch.setattr(paths, "BAKE_PATH", bake)
    monkeypatch.setattr(image_utils, "pic_download_from_url", AsyncMock())
    source = Image.new("RGBA", (120, 240), (0, 0, 0, 0))
    source.paste((255, 0, 0, 255), (20, 0, 100, 80))
    source.paste((0, 0, 255, 255), (20, 80, 100, 160))
    source.paste((0, 255, 0, 255), (20, 160, 100, 240))
    source.save(cache / "portrait.png")
    url = "https://example.invalid/portrait.png"

    async def baked(**kwargs):
        result = await ru.get_image_b64_with_cache(url, cache, **kwargs)
        return decoded(base64.b64decode(result.split(",", 1)[1]))

    raw = await baked()
    assert raw.size == source.size
    compressed = await baked(quality=90)
    assert compressed.size == source.size
    assert compressed.getpixel((0, 120))[3] == 0
    centered = await baked(quality=90, cover_size=(60, 40))
    top = await baked(quality=90, cover_size=(60, 40), anchor="top")
    assert centered.size == top.size == (60, 40)
    assert centered.getpixel((30, 20))[2] > 230
    assert top.getpixel((30, 20))[0] > 230
    assert top.getpixel((0, 20))[3] == 0
    assert await baked(quality=90, cover_size=(60, 40), anchor="top") is not None


@pytest.mark.asyncio
async def test_cover_resize_fills_target_despite_float_rounding(tmp_path, monkeypatch):
    from ArknightsUID.utils import image as image_utils
    from ArknightsUID.utils import path as paths

    cache = tmp_path / "cache"
    bake = tmp_path / "bake"
    cache.mkdir()
    bake.mkdir()
    monkeypatch.setattr(paths, "BAKE_PATH", bake)
    monkeypatch.setattr(image_utils, "pic_download_from_url", AsyncMock())
    Image.new("RGBA", (7, 11), (255, 0, 0, 255)).save(cache / "opaque.png")
    result = await ru.get_image_b64_with_cache(
        "https://example.invalid/opaque.png", cache, quality=90, cover_size=(61, 10)
    )
    image = decoded(base64.b64decode(result.split(",", 1)[1]))
    assert image.size == (61, 10)
    assert image.convert("RGBA").getchannel("A").getextrema() == (255, 255)
    assert image.getpixel((0, 5))[0] > 230
