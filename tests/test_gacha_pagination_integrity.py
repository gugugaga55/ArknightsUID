"""分页异常应保留已拉记录并提示缺口；终页无需提供下一页游标。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID.utils.hypergryph import client as hg
from ArknightsUID.utils.hypergryph import gacha


def record(ts=1_700_000_000_123, pos=0):
    return {
        "charId": "char_1",
        "charName": "干员",
        "rarity": 3,
        "gachaTs": ts,
        "pos": pos,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pages, expected_calls, expected_count, incomplete",
    [
        ([{"list": [record(None)], "hasMore": True}], 1, 1, True),
        ([{"list": [], "hasMore": True}], 1, 0, True),
        ([{"list": [record()], "hasMore": True}] * 2, 2, 1, True),
        ([{"list": [record(None)], "hasMore": False}], 1, 1, False),
        ([{"list": [], "hasMore": False}], 1, 0, False),
        (
            [
                {"list": [record(pos=1)], "hasMore": True},
                {"list": [record(pos=0)], "hasMore": False},
            ],
            2,
            2,
            False,
        ),
    ],
)
async def test_sync_handles_pagination_termination(
    monkeypatch, tmp_path, pages, expected_calls, expected_count, incomplete
):
    monkeypatch.setattr(gacha, "PLAYER_PATH", tmp_path)
    monkeypatch.setattr(
        hg,
        "login_gacha_chain",
        AsyncMock(return_value=hg.ArkGachaAuth("T", "U8", "CK")),
    )
    monkeypatch.setattr(
        hg, "get_gacha_categories", AsyncMock(return_value=[{"id": "standard"}])
    )
    fetch = AsyncMock(side_effect=pages)
    monkeypatch.setattr(hg, "get_gacha_history_page", fetch)
    records, warning = await gacha.sync_gacha_logs(SimpleNamespace(uid="12345"))
    assert fetch.await_count == expected_calls
    assert len(records) == expected_count
    assert ("历史未完整拉取" in warning) is incomplete
    assert not await gacha._load_sync_floors("12345")
    if records:
        assert await gacha.load_local_logs("12345") == records
    if expected_calls > 1:
        assert fetch.await_args_list[1].kwargs == {
            "gacha_ts": pages[0]["list"][-1]["gachaTs"],
            "pos": pages[0]["list"][-1]["pos"],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("has_more", [False, True])
async def test_failed_log_write_does_not_commit_backfill_cursor(
    monkeypatch, tmp_path, has_more
):
    monkeypatch.setattr(gacha, "PLAYER_PATH", tmp_path)
    monkeypatch.setattr(gacha, "_MAX_PAGES", 1)
    uid = "12345"
    local = [gacha._normalize_record(record(1_700_000_100_123), "standard")]
    floor = {"standard": {"ts": 1_700_000_080_123, "pos": 0}}
    await gacha.save_local_logs(uid, local)
    await gacha._save_sync_floors(uid, floor)
    monkeypatch.setattr(
        hg, "login_gacha_chain", AsyncMock(return_value=hg.ArkGachaAuth("T", "U8", "CK"))
    )
    monkeypatch.setattr(
        hg, "get_gacha_categories", AsyncMock(return_value=[{"id": "standard"}])
    )
    monkeypatch.setattr(
        hg,
        "get_gacha_history_page",
        AsyncMock(side_effect=[
            {"list": [record(1_700_000_090_123)], "hasMore": True},
            {"list": [record(1_700_000_070_123)], "hasMore": has_more},
        ]),
    )
    write = gacha.write_player_json

    async def fail_logs_only(path, payload):
        if path == gacha._logs_path(uid):
            raise OSError("disk full")
        await write(path, payload)

    monkeypatch.setattr(gacha, "write_player_json", fail_logs_only)

    fetched, warning = await gacha.sync_gacha_logs(SimpleNamespace(uid=uid))

    assert len(fetched) == 3
    assert await gacha.load_local_logs(uid) == local
    assert await gacha._load_sync_floors(uid) == floor
    assert "本地记录保存失败" in warning

    monkeypatch.setattr(gacha, "write_player_json", write)
    retry = AsyncMock(side_effect=[
        {"list": [record(1_700_000_090_123)], "hasMore": True},
        {"list": [record(1_700_000_070_123)], "hasMore": has_more},
    ])
    monkeypatch.setattr(hg, "get_gacha_history_page", retry)
    recovered, retry_warning = await gacha.sync_gacha_logs(SimpleNamespace(uid=uid))

    assert recovered == fetched
    assert await gacha.load_local_logs(uid) == recovered
    assert retry.await_args_list[1].kwargs == {
        "gacha_ts": floor["standard"]["ts"], "pos": 0
    }
    assert await gacha._load_sync_floors(uid) == (
        {"standard": {"ts": 1_700_000_070_123, "pos": 0}} if has_more else {}
    )
    assert "本地记录保存失败" not in retry_warning
