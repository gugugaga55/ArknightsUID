"""活动个人数据命令：第一方字段的合成样例，不使用真实账号。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID import arknightsuid_account as account


def test_activity_progress_aggregates_zones_and_filters_event(monkeypatch):
    response = {
        "code": 0,
        "data": {
            "activity": [
                {
                    "actId": "act_test",
                    "zones": [
                        {"clearedStage": 5, "totalStage": 8},
                        {"clearedStage": 1, "totalStage": 4},
                    ],
                },
                {"actId": "act_old", "zones": [{"clearedStage": 10, "totalStage": 10}]},
            ],
            "activityInfoMap": {
                "act_test": {
                    "name": "测试新活动",
                    "type": "SIDESTORY",
                    "isReplicate": False,
                },
                "act_old": {
                    "name": "旧活动复刻",
                    "type": "SIDESTORY",
                    "isReplicate": True,
                },
            },
        },
    }
    monkeypatch.setattr(
        account,
        "resolve_or_reply",
        AsyncMock(return_value=SimpleNamespace(uid="12345")),
    )
    monkeypatch.setattr(
        account, "fetch_cached_player_info", AsyncMock(return_value=response)
    )
    reply = AsyncMock()
    monkeypatch.setattr(account, "_send_text", reply)
    asyncio.run(account.send_activity_progress(None, SimpleNamespace(text="新活动")))
    text = reply.await_args.args[2]
    assert "测试新活动" in text and "6/12" in text and "50%" in text
    assert "旧活动" not in text


def test_activity_records_show_confirmed_modes_without_claiming_unplayed_result(
    monkeypatch,
):
    response = {
        "code": 0,
        "data": {
            "tower": {"records": [{"towerId": "tower_test", "best": 6}]},
            "towerInfoMap": {"tower_test": {"name": "测试保全", "subName": "标准"}},
            "bossRush": [
                {
                    "id": "boss_test",
                    "record": {
                        "stageId": "boss_stage",
                        "difficulty": "EX",
                        "played": True,
                    },
                },
                {
                    "id": "boss_none",
                    "record": {
                        "stageId": "boss_stage",
                        "difficulty": "SP",
                        "played": False,
                    },
                },
            ],
            "stageInfoMap": {"boss_stage": {"code": "TN-3"}},
            "sandbox": [{"id": "sandbox_test", "maxDay": 120, "baseLv": 5}],
        },
    }
    monkeypatch.setattr(
        account,
        "resolve_or_reply",
        AsyncMock(return_value=SimpleNamespace(uid="12345")),
    )
    monkeypatch.setattr(
        account, "fetch_cached_player_info", AsyncMock(return_value=response)
    )
    reply = AsyncMock()
    monkeypatch.setattr(account, "_send_text", reply)
    asyncio.run(account.send_activity_records(None, SimpleNamespace(text="")))
    text = reply.await_args.args[2]
    assert "测试保全" in text and "最高进度：6" in text
    assert "恢弘试炼 TN-3" in text
    assert "暂无记录" in text and "最终试炼" not in text
    assert "120 天" in text and "营地等级：5" in text


@pytest.mark.parametrize("command", ["send_activity_progress", "send_activity_records"])
def test_missing_or_malformed_personal_data_degrades_to_reply(monkeypatch, command):
    monkeypatch.setattr(
        account,
        "resolve_or_reply",
        AsyncMock(return_value=SimpleNamespace(uid="12345")),
    )
    monkeypatch.setattr(
        account,
        "fetch_cached_player_info",
        AsyncMock(
            return_value={
                "code": 0,
                "data": {
                    "activity": [{"actId": [], "zones": "bad"}],
                    "tower": {"records": [{"towerId": []}]},
                    "bossRush": [
                        {"record": {"stageId": [], "difficulty": [], "played": True}}
                    ],
                },
            }
        ),
    )
    reply = AsyncMock()
    monkeypatch.setattr(account, "_send_text", reply)
    asyncio.run(getattr(account, command)(None, SimpleNamespace(text="")))
    assert reply.await_count == 1


def test_unavailable_account_does_not_fetch_personal_data(monkeypatch):
    monkeypatch.setattr(account, "resolve_or_reply", AsyncMock(return_value=None))
    fetch = AsyncMock()
    monkeypatch.setattr(account, "fetch_cached_player_info", fetch)
    asyncio.run(account.send_activity_progress(None, SimpleNamespace(text="")))
    fetch.assert_not_awaited()


def test_partial_zone_data_does_not_claim_full_completion(monkeypatch):
    monkeypatch.setattr(
        account,
        "resolve_or_reply",
        AsyncMock(return_value=SimpleNamespace(uid="12345")),
    )
    monkeypatch.setattr(
        account,
        "fetch_cached_player_info",
        AsyncMock(
            return_value={
                "code": 0,
                "data": {
                    "activity": [
                        {
                            "actId": "act_test",
                            "zones": [
                                {"clearedStage": 10, "totalStage": 10},
                                {"totalStage": 5},
                            ],
                        }
                    ],
                    "activityInfoMap": {
                        "act_test": {"name": "测试活动", "type": "SIDESTORY"}
                    },
                },
            }
        ),
    )
    reply = AsyncMock()
    monkeypatch.setattr(account, "_send_text", reply)
    asyncio.run(account.send_activity_progress(None, SimpleNamespace(text="")))
    text = reply.await_args.args[2]
    assert "进度暂不可用" in text
    assert "100%" not in text and "已通关" not in text
