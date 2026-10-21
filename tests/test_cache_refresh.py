"""验证刷新命令及条件请求不会继续使用旧数据/旧校验器。"""

import asyncio
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID.utils import disk_cache
from ArknightsUID.utils.gamedata import _table_cache as tables
from ArknightsUID.utils.gamedata import rogue_enemy


def test_replacing_payload_without_validators_clears_old_sidecar(tmp_path):
    async def scenario():
        path = tmp_path / "table.json"
        await disk_cache.cache_write(path, {"v": 1}, validators={"etag": '"old"'})
        await disk_cache.cache_write(path, {"v": 2})
        assert await disk_cache.cache_read_validators(path) == {}
        assert (await disk_cache.cache_read(path))[0] == {"v": 2}

    asyncio.run(scenario())


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
