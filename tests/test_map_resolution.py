"""地图身份、缓存更新与资源回退的端到端回归。"""

import asyncio
import base64
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ArknightsUID import arknightsuid_rogue as rogue
from ArknightsUID import arknightsuid_wiki as wiki
from ArknightsUID.utils import http, render
from ArknightsUID.utils.gamedata import rogue_enemy as enemies
from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url
from ArknightsUID.utils.prts import client as prts
from ArknightsUID.utils.resource import RESOURCE_PATH as paths
from ArknightsUID.utils.resource import bulk_download as bulk


def png(color):
    output = BytesIO()
    Image.new("RGB", (10, 10), color).save(output, "PNG")
    return output.getvalue()


def pixel(data_uri):
    with Image.open(BytesIO(base64.b64decode(data_uri.split(",", 1)[1]))) as image:
        return image.convert("RGB").getpixel((0, 0))


def test_fresh_map_response_replaces_old_hashed_picture(tmp_path, monkeypatch):
    cache = tmp_path / "wiki" / "map"
    cache.mkdir(parents=True)
    url = prts.map_file_url(prts.map_file_title("1-7", "暴君"))
    name = cache_names_for_url(url)[0]
    Image.new("RGB", (10, 10), "red").save(
        (cache / name).with_suffix(".webp"), lossless=True
    )
    fetch = AsyncMock(return_value=(200, png("blue")))
    monkeypatch.setattr(paths, "CACHE_PATH", tmp_path)
    monkeypatch.setattr(http, "download_bytes_status", fetch)
    result = asyncio.run(wiki._get_stage_map_b64("1-7", "暴君", "main_01-07"))
    fetch.assert_awaited_once()
    red, green, blue = pixel(result)
    assert blue > 240 and red < 10 and green < 10


def test_bulk_and_lazy_download_share_url_identity(tmp_path, monkeypatch):
    colors = {"https://a.example/map.png": "red", "https://b.example/map.png": "blue"}

    async def fetch(url, **kwargs):
        return 200, png(colors[url])

    monkeypatch.setattr(bulk, "download_bytes_status", fetch)

    async def scenario():
        for url in colors:
            assert (
                await bulk.download_target(bulk.ResourceTarget(url, tmp_path)) == "ok"
            )
        results = [
            pixel(await render.get_image_b64_with_cache(url, tmp_path))
            for url in colors
        ]
        assert results[0][0] > 240 and results[0][2] < 10
        assert results[1][2] > 240 and results[1][0] < 10

    asyncio.run(scenario())


@pytest.fixture
def stage_sources(monkeypatch):
    # 故意反序提供元数据，分支编号须按关卡 ID 稳定排序。
    metas = {
        "sui": {
            "level_rogue5_b-9-b": {"name": "止变", "code": "ISW-DF"},
            "level_rogue5_b-9-a": {"name": "止变", "code": "ISW-DF"},
        },
        "sarkaz": {"level_rogue4_2-2": {"name": "拆东补西", "code": "ISW-NO"}},
    }

    async def meta(theme):
        return metas.get(theme, {})

    monkeypatch.setattr(enemies, "_load_stage_meta", meta)
    load_data = AsyncMock(return_value={"enemies": [], "floors": [2]})
    monkeypatch.setattr(enemies, "load_stage_data", load_data)
    monkeypatch.setattr(enemies, "load_enemy_db", AsyncMock(return_value={"one": {}}))
    monkeypatch.setattr(enemies, "load_difficulty_mods", AsyncMock(return_value={}))
    monkeypatch.setattr(prts, "rogue_map_file_urls", AsyncMock(return_value=[]))
    fetch = AsyncMock(side_effect=lambda url, *a, **kw: url)
    monkeypatch.setattr(render, "get_image_b64_with_cache", fetch)
    return metas, load_data, fetch


def test_exact_name_with_multiple_variants_requires_choice(stage_sources):
    _, load_data, fetch = stage_sources
    result = asyncio.run(rogue._build_rogue_stage_context("止变"))
    assert isinstance(result, str)
    assert "分支1" in result and "分支2" in result
    load_data.assert_not_awaited()
    fetch.assert_not_awaited()


def test_exact_lookup_does_not_silently_select_first_variant(stage_sources):
    assert asyncio.run(enemies.find_stage("止变")) is None


def test_selected_variant_uses_matching_data_and_image(stage_sources, monkeypatch):
    _, load_data, fetch = stage_sources
    wiki_urls = AsyncMock(return_value=["https://example.test/ambiguous-wiki.png"])
    monkeypatch.setattr(prts, "rogue_map_file_urls", wiki_urls)
    result = asyncio.run(rogue._build_rogue_stage_context("止变 界园 分支2 N15 紧急"))
    assert isinstance(result, dict)
    assert result["level_id"] == "level_rogue5_b-9-b"
    assert result["maps"] == prts.rogue_map_preview_urls("level_rogue5_b-9-b")
    assert result["elite"] is True
    assert "分支2" in result["variant_note"]
    load_data.assert_awaited_once_with("level_rogue5_b-9-b")
    wiki_urls.assert_not_awaited()


@pytest.mark.parametrize("number", [0, 3])
def test_invalid_variant_does_not_choose_another_map(stage_sources, number):
    _, load_data, _ = stage_sources
    result = asyncio.run(rogue._build_rogue_stage_context(f"止变 分支{number}"))
    assert isinstance(result, str) and "分支" in result
    load_data.assert_not_awaited()


def test_theme_hint_disambiguates_identical_names(stage_sources):
    metas, _, _ = stage_sources
    metas["black"] = {"level_rogue6_t-8": {"name": "止变", "code": "ISW-NO"}}
    result = asyncio.run(rogue._build_rogue_stage_context("止变 黑流"))
    assert isinstance(result, dict)
    assert result["level_id"] == "level_rogue6_t-8"


@pytest.mark.parametrize("raises", [False, True])
def test_dead_wiki_image_tries_available_preview(stage_sources, monkeypatch, raises):
    monkeypatch.setattr(
        prts,
        "rogue_map_file_urls",
        AsyncMock(return_value=["https://example.test/dead.png"]),
    )

    async def fetch(url, *args, **kwargs):
        if "map_preview" in url:
            return "PREVIEW"
        if raises:
            raise OSError("download failed")
        return ""

    monkeypatch.setattr(render, "get_image_b64_with_cache", fetch)
    result = asyncio.run(rogue._build_rogue_stage_context("拆东补西"))
    assert result["maps"] == ["PREVIEW"]


def test_working_wiki_image_does_not_download_preview(stage_sources, monkeypatch):
    _, _, fetch = stage_sources
    monkeypatch.setattr(
        prts,
        "rogue_map_file_urls",
        AsyncMock(return_value=["https://example.test/map.png"]),
    )
    result = asyncio.run(rogue._build_rogue_stage_context("拆东补西"))
    assert result["maps"] == ["https://example.test/map.png"]
    fetch.assert_awaited_once()


def test_enemy_command_stage_fallback_preserves_variant(stage_sources):
    _, load_data, _ = stage_sources
    result = asyncio.run(rogue._build_rogue_enemy_context("止变 界园 分支2"))
    assert isinstance(result, dict)
    assert result["level_id"] == "level_rogue5_b-9-b"
    load_data.assert_awaited_once_with("level_rogue5_b-9-b")


def test_enemy_query_with_ambiguous_stage_requires_choice(stage_sources, monkeypatch):
    _, load_data, _ = stage_sources
    monkeypatch.setattr(
        enemies,
        "load_enemy_db",
        AsyncMock(
            return_value={
                "enemy_1": {"key": "enemy_1", "name_zh": "测试敌人"},
            }
        ),
    )
    result = asyncio.run(rogue._build_rogue_enemy_context("测试敌人 止变"))
    assert isinstance(result, str) and "分支2" in result
    load_data.assert_not_awaited()


def test_stage_card_followup_instruction_keeps_variant(stage_sources):
    result = asyncio.run(rogue._build_rogue_stage_context("止变 分支2"))
    html = render.ark_templates.get_template("ark_rogue_stage.html").render(
        **result, font_css_url="about:blank"
    )
    assert "分支2" in html
    assert "止变 分支2" in html


def test_missing_map_for_one_stage_does_not_hide_another_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CACHE_PATH", tmp_path)
    monkeypatch.setattr(wiki, "resolve_map_file_url", AsyncMock(return_value=""))

    async def fetch(url, **kwargs):
        if url == prts.stage_map_preview_url("act_new_01"):
            return 200, png("blue")
        return 404, None

    monkeypatch.setattr(http, "download_bytes_status", fetch)

    async def scenario():
        assert await wiki._get_stage_map_b64("EV-1", "同名关卡", "act_old_01") == ""
        assert await wiki._get_stage_map_b64("EV-1", "同名关卡", "act_new_01")

    asyncio.run(scenario())
