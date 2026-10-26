"""地图/PRTS 页面缓存的失败恢复及结局刷新回归。"""

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ArknightsUID import arknightsuid_wiki as wiki
from ArknightsUID.utils import http
from ArknightsUID.utils.gamedata import rogue_ending
from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url
from ArknightsUID.utils.prts import client as prts
from ArknightsUID.utils.resource import RESOURCE_PATH as paths


@pytest.mark.parametrize(
    "status,content", [(503, None), (200, b"not image"), (200, b"")]
)
def test_map_network_failure_keeps_readable_cached_picture(
    tmp_path, monkeypatch, status, content
):
    cache = tmp_path / "wiki" / "map"
    cache.mkdir(parents=True)
    url = prts.map_file_url(prts.map_file_title("1-7", "暴君"))
    name = cache_names_for_url(url)[0]
    Image.new("RGB", (10, 10), "red").save((cache / name).with_suffix(".webp"))
    fetch = AsyncMock(return_value=(status, content))
    monkeypatch.setattr(paths, "CACHE_PATH", tmp_path)
    monkeypatch.setattr(http, "download_bytes_status", fetch)
    monkeypatch.setattr(wiki, "resolve_map_file_url", AsyncMock(return_value=""))

    result = asyncio.run(wiki._get_stage_map_b64("1-7", "暴君", "main_01-07"))

    assert result.startswith("data:image/webp;base64,")
    fetch.assert_awaited_once()


def test_map_corrupt_cache_recovers_on_next_download(tmp_path, monkeypatch):
    from io import BytesIO

    cache = tmp_path / "wiki" / "map"
    cache.mkdir(parents=True)
    url = prts.map_file_url(prts.map_file_title("1-7", "暴君"))
    name = cache_names_for_url(url)[0]
    (cache / name).with_suffix(".webp").write_bytes(b"corrupt image")
    output = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(output, "PNG")

    async def fetch(url, **kwargs):
        return (200, output.getvalue()) if available else (503, None)

    monkeypatch.setattr(paths, "CACHE_PATH", tmp_path)
    monkeypatch.setattr(http, "download_bytes_status", fetch)
    monkeypatch.setattr(wiki, "resolve_map_file_url", AsyncMock(return_value=""))

    async def scenario():
        nonlocal available
        assert await wiki._get_stage_map_b64("1-7", "暴君", "main_01-07") == ""
        available = True
        assert await wiki._get_stage_map_b64("1-7", "暴君", "main_01-07")

    available = False
    asyncio.run(scenario())


def test_prts_cleanup_preserves_valid_page_for_later_outage(tmp_path, monkeypatch):
    params = {"action": "parse", "page": "1-7", "prop": "wikitext"}
    monkeypatch.setattr(prts, "PRTS_CACHE_PATH", tmp_path)
    monkeypatch.setattr(prts, "_last_cleanup_at", 0)
    monkeypatch.setattr(prts, "_MIN_REQUEST_INTERVAL", 0)
    client = SimpleNamespace(get=AsyncMock(side_effect=RuntimeError("offline")))
    monkeypatch.setattr(prts, "get_client", AsyncMock(return_value=client))
    path = prts._cache_file(params)
    saved = time.time() - 8 * 86400
    path.write_text(
        json.dumps(
            {
                "_cache_time": saved,
                "payload": {
                    "parse": {"title": "1-7", "wikitext": {"*": "cached stage"}}
                },
            }
        )
    )
    os.utime(path, (saved, saved))

    async def scenario():
        assert await prts.parse_page("1-7")
        assert await prts.parse_page("1-7"), "valid cache must survive cleanup"

    asyncio.run(scenario())
    client.get.assert_not_awaited()


def ending_table(name):
    return {"details": {"rogue_6": {"endings": {"ro6_ending_99": {"name": name}}}}}


def test_ending_cache_clear_reloads_new_endings(monkeypatch):
    monkeypatch.setattr(rogue_ending, "_cache", {"themes": {}, "ts": 0})
    monkeypatch.setattr(rogue_ending, "_cache_generation", 0)
    ensure = AsyncMock(side_effect=[ending_table("旧结局"), ending_table("新结局")])
    monkeypatch.setattr(rogue_ending, "ensure_rogue_topic_table", ensure)

    async def scenario():
        await rogue_ending.load_all_endings()
        rogue_ending.clear_ending_cache()
        result = await rogue_ending.resolve_query("新结局")
        assert isinstance(result, tuple)
        assert result[1] == ["新结局"]

    asyncio.run(scenario())
    assert ensure.await_count == 2


def test_ending_refresh_during_load_prevents_old_memo_refill(monkeypatch):
    monkeypatch.setattr(rogue_ending, "_cache", {"themes": {}, "ts": 0})
    monkeypatch.setattr(rogue_ending, "_cache_generation", 0)

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def ensure():
            if not entered.is_set():
                entered.set()
                await release.wait()
                return ending_table("旧结局")
            return ending_table("新结局")

        monkeypatch.setattr(rogue_ending, "ensure_rogue_topic_table", ensure)
        loading = asyncio.create_task(rogue_ending.load_all_endings())
        await entered.wait()
        try:
            rogue_ending.clear_ending_cache()
            fresh = await rogue_ending.load_all_endings()
        finally:
            release.set()
            await loading
        assert any(row["name"] == "新结局" for row in fresh["black"]["rows"])
        result = await rogue_ending.resolve_query("新结局")
        assert isinstance(result, tuple), (
            "old in-flight load must not overwrite fresh memo"
        )

    asyncio.run(scenario())
