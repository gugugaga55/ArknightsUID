"""验证刷新命令及条件请求不会继续使用旧数据/旧校验器。"""

import asyncio
import json
import threading
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID.utils import disk_cache
from ArknightsUID.utils.gamedata import _table_cache as tables
from ArknightsUID.utils.gamedata import rogue_enemy


@pytest.fixture(autouse=True)
def isolate_table_write_lock(monkeypatch):
    # 多个测试各自启动事件循环，互斥锁不可带着旧循环的waiters复用。
    monkeypatch.setattr(tables, "_table_write_lock", asyncio.Lock())


def test_replacing_payload_without_validators_clears_old_sidecar(tmp_path):
    async def scenario():
        path = tmp_path / "table.json"
        await disk_cache.cache_write(path, {"v": 1}, validators={"etag": '"old"'})
        await disk_cache.cache_write(path, {"v": 2})
        assert await disk_cache.cache_read_validators(path) == {}
        assert (await disk_cache.cache_read(path))[0] == {"v": 2}

    asyncio.run(scenario())


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["new", "changed", "unchanged"])
async def test_refresh_does_not_wait_for_old_network_or_restore_its_table(
    tmp_path, monkeypatch, response_kind
):
    from ArknightsUID import arknightsuid_maintenance as maintenance
    from ArknightsUID.utils import path as paths

    path = tmp_path / "gamedata" / "character_table.json"
    if response_kind != "new":
        await disk_cache.cache_write(path, {"v": 1}, validators={"etag": "old"})
    started, release = asyncio.Event(), asyncio.Event()

    async def old_network(*args, **kwargs):
        started.set()
        await release.wait()
        return (
            (304, None, {})
            if response_kind == "unchanged"
            else (200, b'{"v": 1}', {"etag": "old"})
        )

    async def old_full(url):
        await old_network()
        return {"v": 1}, {"etag": "old"}

    monkeypatch.setattr(paths, "CACHE_BASE", tmp_path)
    monkeypatch.setattr(tables, "_table_memo", OrderedDict())
    monkeypatch.setattr(tables, "_download_json", old_full)
    monkeypatch.setattr(tables, "download_bytes_conditional", old_network)
    monkeypatch.setattr(maintenance, "_send_text", AsyncMock())
    query = asyncio.create_task(tables._ensure_table("https://example.test/table", path, force=True))
    await started.wait()
    try:
        # 刷新仅清盘，不应排在尚未完成的网络请求后面。
        await asyncio.wait_for(maintenance.refresh_gamedata_cache(None, SimpleNamespace()), 0.5)
    finally:
        release.set()
    await query
    assert not path.exists(), "旧在途响应在刷新后重新写回主表"
    assert not path.with_name(path.name + ".validators.json").exists()
    assert path not in tables._table_memo
    monkeypatch.setattr(tables, "_download_json", AsyncMock(return_value=({"v": 2}, {})))
    assert await tables._ensure_table("https://example.test/table", path) == {"v": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["new", "changed", "unchanged"])
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_refresh_waits_for_committed_table_write_even_if_query_cancelled(
    tmp_path, monkeypatch, response_kind, cancel_count
):
    from ArknightsUID import arknightsuid_maintenance as maintenance
    from ArknightsUID.utils import path as paths

    path = tmp_path / "gamedata" / "character_table.json"
    if response_kind != "new":
        await disk_cache.cache_write(path, {"v": 1}, validators={"etag": "old"})
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    write_sync = disk_cache._write_sync

    def slow_write(target, payload):
        if target == path:
            started.set()
            assert release.wait(3)
        write_sync(target, payload)
        if target == path:
            finished.set()

    response = (304, None, {}) if response_kind == "unchanged" else (200, b'{"v": 1}', {"etag": "old"})
    monkeypatch.setattr(paths, "CACHE_BASE", tmp_path)
    monkeypatch.setattr(tables, "_table_memo", OrderedDict())
    monkeypatch.setattr(disk_cache, "_write_sync", slow_write)
    monkeypatch.setattr(tables, "_download_json", AsyncMock(return_value=({"v": 1}, {"etag": "old"})))
    monkeypatch.setattr(tables, "download_bytes_conditional", AsyncMock(return_value=response))
    monkeypatch.setattr(maintenance, "_send_text", AsyncMock())
    query = asyncio.create_task(tables._ensure_table("https://example.test/table", path, force=True))
    assert await asyncio.to_thread(started.wait, 1)
    query.cancel()
    refreshing = asyncio.create_task(maintenance.refresh_gamedata_cache(None, SimpleNamespace()))
    try:
        await asyncio.sleep(0.02)
        if cancel_count == 2:
            query.cancel()
            await asyncio.sleep(0.02)
        assert not refreshing.done(), "刷新必须回收已提交的线程写盘"
    finally:
        release.set()
        await asyncio.gather(query, refreshing, return_exceptions=True)
    assert finished.is_set()
    assert not path.exists()
    assert not path.with_name(path.name + ".validators.json").exists()
    assert path not in tables._table_memo


@pytest.mark.asyncio
async def test_disk_read_started_before_refresh_cannot_refill_table_memo(tmp_path, monkeypatch):
    from ArknightsUID import arknightsuid_maintenance as maintenance
    from ArknightsUID.utils import path as paths

    path = tmp_path / "gamedata" / "character_table.json"
    await disk_cache.cache_write(path, {"v": 1})
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_read(target):
        snapshot = await disk_cache.cache_read(target)
        started.set()
        await release.wait()
        return snapshot

    monkeypatch.setattr(paths, "CACHE_BASE", tmp_path)
    monkeypatch.setattr(tables, "_table_memo", OrderedDict())
    monkeypatch.setattr(tables, "cache_read", delayed_read)
    monkeypatch.setattr(maintenance, "_send_text", AsyncMock())
    reading = asyncio.create_task(tables._read_fresh_from_disk(path, tables.CHAR_TABLE_TTL))
    await started.wait()
    await maintenance.refresh_gamedata_cache(None, SimpleNamespace())
    release.set()
    await reading
    assert path not in tables._table_memo


@pytest.mark.asyncio
async def test_old_char_index_build_cannot_replace_new_index_after_refresh(tmp_path, monkeypatch):
    from ArknightsUID import arknightsuid_maintenance as maintenance
    from ArknightsUID.utils import path as paths
    from ArknightsUID.utils.gamedata import charinfo

    table_path = tmp_path / "gamedata" / "character_table.json"
    index_path = tmp_path / "gamedata" / "charindex.json"
    old = {"char_old": {"name": "旧干员", "rarity": "TIER_6", "profession": "WARRIOR"}}
    new = {"char_new": {"name": "新干员", "rarity": "TIER_6", "profession": "WARRIOR"}}
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def source():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return old
        await disk_cache.cache_write(table_path, new)
        return new

    monkeypatch.setattr(paths, "CACHE_BASE", tmp_path)
    monkeypatch.setattr(charinfo, "CHAR_TABLE_PATH", table_path)
    monkeypatch.setattr(charinfo, "CHARINDEX_PATH", index_path)
    monkeypatch.setattr(charinfo, "ensure_char_table", source)
    monkeypatch.setattr(maintenance, "_send_text", AsyncMock())
    old_query = asyncio.create_task(charinfo.load_char_index())
    await started.wait()
    await maintenance.refresh_gamedata_cache(None, SimpleNamespace())
    assert "char_new" in await charinfo.load_char_index()
    release.set()
    assert "char_old" in await old_query
    assert "char_new" in await charinfo.load_char_index()
    assert "char_new" in json.loads(index_path.read_text())


@pytest.mark.asyncio
async def test_refresh_command_invalidates_ending_memo(tmp_path, monkeypatch):
    from ArknightsUID import arknightsuid_maintenance as maintenance
    from ArknightsUID.utils import path as paths
    from ArknightsUID.utils.gamedata import rogue_ending

    def topic(name):
        return {"details": {"rogue_6": {"endings": {"ro6_ending_99": {"name": name}}}}}

    monkeypatch.setattr(paths, "CACHE_BASE", tmp_path)
    monkeypatch.setattr(rogue_ending, "_cache", {"themes": {}, "ts": 0})
    ensure = AsyncMock(side_effect=[topic("旧结局"), topic("新结局")])
    monkeypatch.setattr(rogue_ending, "ensure_rogue_topic_table", ensure)
    monkeypatch.setattr(maintenance, "_send_text", AsyncMock())
    await rogue_ending.load_all_endings()
    await maintenance.refresh_gamedata_cache(None, SimpleNamespace())
    result = await rogue_ending.resolve_query("新结局")
    assert isinstance(result, tuple)
    assert result[1] == ["新结局"]
    assert ensure.await_count == 2


@pytest.mark.parametrize("source", ["table", "enemy"])
def test_200_response_does_not_inherit_old_validators(tmp_path, monkeypatch, source):
    async def scenario():
        path = tmp_path / "table.json"
        await disk_cache.cache_write(
            path,
            {"v": 1},
            validators={"etag": '"old"', "last_modified": "yesterday"},
        )
        download = AsyncMock(return_value=(200, b'{"v": 2}', {}))
        if source == "table":
            monkeypatch.setattr(tables, "download_bytes_conditional", download)
            data = await tables._ensure_table(
                "https://example.test/table", path, force=True
            )
        else:
            monkeypatch.setattr(rogue_enemy, "_DATA_TTL", 0)
            monkeypatch.setattr(rogue_enemy, "download_bytes_conditional", download)
            data = await rogue_enemy._fetch_url_json_cached(
                "https://example.test/table", path
            )
        assert data == {"v": 2}
        validators = await disk_cache.cache_read_validators(path)
        assert "etag" not in validators
        assert "last_modified" not in validators

    asyncio.run(scenario())


def test_refresh_command_invalidates_warm_table_cache(tmp_path, monkeypatch):
    from ArknightsUID import arknightsuid_maintenance as maintenance
    from ArknightsUID.utils import path as paths

    async def scenario():
        path = tmp_path / "gamedata" / "character_table.json"
        download = AsyncMock(side_effect=[({"v": 1}, {}), ({"v": 2}, {})])
        monkeypatch.setattr(paths, "CACHE_BASE", tmp_path)
        # 用真实 LRU 容器，触达查询 -> 命令 -> 再查询的整条链。
        monkeypatch.setattr(tables, "_table_memo", OrderedDict())
        monkeypatch.setattr(tables, "_download_json", download)
        monkeypatch.setattr(maintenance, "_send_text", AsyncMock())
        assert await tables._ensure_table("https://example.test/table", path) == {
            "v": 1
        }
        await maintenance.refresh_gamedata_cache(None, SimpleNamespace())
        assert await tables._ensure_table("https://example.test/table", path) == {
            "v": 2
        }
        assert download.await_count == 2

    asyncio.run(scenario())
