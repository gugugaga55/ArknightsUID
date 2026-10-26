"""藏品与敌人图标拒绝坏响应、修复旧坏缓存，并保留失败退避语义。"""

import base64
import os
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ArknightsUID.utils.gamedata import rogue_relic as relic, rogue_enemy as enemy


@pytest.fixture(params=["relic", "enemy"])
def icon(request, tmp_path, monkeypatch):
    module = relic if request.param == "relic" else enemy
    if module is relic:
        cache = tmp_path
        monkeypatch.setattr(module, "RELIC_ICON_CACHE_DIR", cache)
        fetch = module.relic_icon_b64
        key = "rogue_6_relic_final_1"
    else:
        cache = tmp_path / "icons"
        cache.mkdir()
        monkeypatch.setattr(module, "IS_ENEMY_CACHE_DIR", tmp_path)
        fetch = module.enemy_icon_b64
        key = "enemy_test"
    return SimpleNamespace(module=module, cache=cache, fetch=fetch, key=key)


def webp_bytes():
    output = BytesIO()
    Image.new("RGB", (12, 12), "red").save(output, "WEBP")
    return output.getvalue()


def assert_decodable(uri):
    with Image.open(BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as image:
        image.load()
        assert image.size == (12, 12)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["html", "truncated"])
async def test_bad_download_does_not_poison_later_success(icon, monkeypatch, bad_kind):
    good = webp_bytes()
    bad = b"<html>temporary error</html>" if bad_kind == "html" else good[:-8]
    download = AsyncMock(side_effect=[(200, bad), (200, good)])
    monkeypatch.setattr(icon.module, "download_bytes_status", download)

    assert await icon.fetch(icon.key) == ""
    assert not (icon.cache / f"{icon.key}.webp").exists()
    marker = icon.cache / f"{icon.key}.webp.miss"
    assert marker.exists()
    expired = time.time() - icon.module._ICON_MISS_TTL - 1
    os.utime(marker, (expired, expired))
    assert_decodable(await icon.fetch(icon.key))
    assert download.await_count == 2
    assert not marker.exists()
    assert not list(icon.cache.glob("*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["html", "truncated"])
async def test_existing_corrupt_icon_is_redownloaded(icon, monkeypatch, bad_kind):
    good = webp_bytes()
    path = icon.cache / f"{icon.key}.webp"
    path.write_bytes(b"<html>cached error</html>" if bad_kind == "html" else good[:-8])
    download = AsyncMock(return_value=(200, good))
    monkeypatch.setattr(icon.module, "download_bytes_status", download)

    assert_decodable(await icon.fetch(icon.key))
    download.assert_awaited_once()
    with Image.open(path) as image:
        image.load()


@pytest.mark.asyncio
async def test_valid_icon_cache_is_reused_without_network(icon, monkeypatch):
    (icon.cache / f"{icon.key}.webp").write_bytes(webp_bytes())
    download = AsyncMock()
    monkeypatch.setattr(icon.module, "download_bytes_status", download)

    assert_decodable(await icon.fetch(icon.key))
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_network_failure_writes_only_backoff_marker(icon, monkeypatch):
    download = AsyncMock(return_value=(0, None))
    monkeypatch.setattr(icon.module, "download_bytes_status", download)

    assert await icon.fetch(icon.key) == ""
    assert await icon.fetch(icon.key) == ""
    download.assert_awaited_once()
    assert sorted(path.name for path in icon.cache.iterdir()) == [
        f"{icon.key}.webp.miss"
    ]
