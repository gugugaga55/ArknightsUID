"""公招部分响应不能取代完整缓存或产生虚假标签保底。"""

from unittest.mock import AsyncMock

import pytest

from ArknightsUID.utils.disk_cache import read_json, write_json
from ArknightsUID.utils.gamedata import recruit
from ArknightsUID.utils.prts import client as prts


def pool_member(rarity):
    return {
        "printouts": {
            "稀有度": [rarity],
            "职业": ["近卫"],
            "位置": ["近战位"],
            "标签": ["输出"],
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pagination", "dropped_member"])
@pytest.mark.parametrize("has_cache", [False, True])
async def test_partial_pool_does_not_replace_complete_cache(
    monkeypatch, tmp_path, failure, has_cache
):
    cache_path = tmp_path / "recruit.json"
    monkeypatch.setattr(recruit, "RECRUIT_DATA_PATH", cache_path)
    monkeypatch.setattr(recruit, "_sidecar_fresh", lambda *_: False)
    cached = recruit.entries_from_smw(
        {"五星": pool_member(4), "四星": pool_member(3)}
    )
    if has_cache:
        await write_json(cache_path, {"v": recruit._CACHE_VERSION, "entries": cached})
    response = {"query": {"results": {"五星": pool_member(4)}}}
    if failure == "pagination":
        response["query-continue-offset"] = 500
    else:
        response["query"]["results"]["四星"] = pool_member("invalid")
    monkeypatch.setattr(prts, "_smw_ask", AsyncMock(return_value=response))
    index = AsyncMock(return_value={})
    monkeypatch.setattr("ArknightsUID.utils.gamedata.charinfo.load_char_index", index)

    loaded = await recruit.load_recruit_data()

    assert loaded == (cached if has_cache else [])
    index.assert_not_awaited()
    if has_cache:
        assert await read_json(cache_path) == {
            "v": recruit._CACHE_VERSION,
            "entries": cached,
        }
        assert [entry["name"] for entry in recruit.match_recruit(["输出"], loaded)] == [
            "五星", "四星"
        ]
    else:
        assert not cache_path.exists()


@pytest.mark.asyncio
async def test_complete_pool_is_cached(monkeypatch, tmp_path):
    cache_path = tmp_path / "recruit.json"
    monkeypatch.setattr(recruit, "RECRUIT_DATA_PATH", cache_path)
    response = {
        "query": {"results": {"五星": pool_member(4), "四星": pool_member(3)}}
    }
    monkeypatch.setattr(prts, "_smw_ask", AsyncMock(return_value=response))
    monkeypatch.setattr(
        "ArknightsUID.utils.gamedata.charinfo.load_char_index",
        AsyncMock(return_value={"char_1": {"name": "五星"}}),
    )

    loaded = await recruit.load_recruit_data()

    assert len(loaded) == 2
    assert loaded[0]["id"] == "char_1"
    assert (await read_json(cache_path))["entries"] == loaded
