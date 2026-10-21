"""按第一方字段结构验证个人记录与真实模组名称，避免测试沿用旧假设。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID import arknightsuid_account as account
from ArknightsUID.utils.gamedata import _table_cache as tables
from ArknightsUID.utils.player.context import build_char_card_context
from ArknightsUID.utils.player.parse import CharEntry, EquipState, parse_player_info


def campaign_response():
    return {
        "code": 0,
        "data": {
            "status": {"name": "博士", "uid": "12345"},
            "campaign": {
                "reward": {"current": 1200, "total": 1800},
                "records": [
                    {"campaignId": "camp_done", "maxKills": 400},
                    {"campaignId": "camp_partial", "maxKills": 355},
                    {"campaignId": "camp_zero", "maxKills": 0},
                    {"campaignId": "camp_missing"},
                ],
            },
            "campaignInfoMap": {
                "camp_done": {"name": "切尔诺伯格"},
                "camp_partial": {"name": "龙门外环"},
            },
        },
    }


def test_campaign_array_preserves_records_and_real_zero():
    data = parse_player_info(campaign_response())
    assert data.campaign_records == {
        "camp_done": 400,
        "camp_partial": 355,
        "camp_zero": 0,
    }


def test_campaign_legacy_dictionary_remains_supported():
    response = campaign_response()
    response["data"]["campaign"]["records"] = {"camp_done": 400, "camp_zero": 0}
    assert parse_player_info(response).campaign_records == {
        "camp_done": 400,
        "camp_zero": 0,
    }


def test_campaign_command_renders_record_names_in_card_and_text(monkeypatch):
    monkeypatch.setattr(
        account,
        "resolve_credential",
        AsyncMock(return_value=SimpleNamespace(uid="12345")),
    )
    monkeypatch.setattr(
        account, "fetch_cached_player_info", AsyncMock(return_value=campaign_response())
    )
    monkeypatch.setattr(account, "hide_uid_for", AsyncMock(return_value="12***45"))
    send = AsyncMock()
    monkeypatch.setattr(account, "send_card_or_text", send)
    asyncio.run(
        account.show_ark_campaign(
            None, SimpleNamespace(user_id="user", bot_id="bot", text="")
        )
    )
    context, text = send.await_args.args[3:5]
    assert context["stages"] == [
        {"name": "切尔诺伯格", "progress": 400, "total": 400},
        {"name": "龙门外环", "progress": 355, "total": 400},
        {"name": "camp_zero", "progress": 0, "total": 400},
    ]
    assert "切尔诺伯格：击杀 400" in text and "龙门外环：击杀 355" in text
    assert "camp_missing" not in text


@pytest.mark.usefixtures("offline_card_assets")
@pytest.mark.parametrize("known", [True, False])
def test_char_card_uses_module_metadata_instead_of_id_parity(monkeypatch, known):
    metadata = (
        {
            "equipDict": {
                "uniequip_003_amiya": {
                    "uniEquipName": "真实模组名称",
                    "typeIcon": "corecaster-x",
                }
            }
        }
        if known
        else {}
    )
    fetch = AsyncMock(return_value=metadata)
    monkeypatch.setattr(tables, "ensure_uniequip_table", fetch)
    entry = CharEntry(
        char_id="char_002_amiya",
        equips=[EquipState(equip_id="uniequip_003_amiya", level=3)],
    )
    context = asyncio.run(build_char_card_context(entry, "test"))
    assert context["equips"][0]["name"] == (
        "真实模组名称" if known else "uniequip_003_amiya"
    )
    assert context["equips"][0]["stage"] == 3
    fetch.assert_awaited_once()


@pytest.mark.usefixtures("offline_card_assets")
def test_char_card_without_modules_does_not_fetch_module_table(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(tables, "ensure_uniequip_table", fetch)
    context = asyncio.run(
        build_char_card_context(CharEntry(char_id="char_002_amiya"), "test")
    )
    assert context["equips"] == []
    fetch.assert_not_awaited()
