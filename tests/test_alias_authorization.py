from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ArknightsUID import arknightsuid_alias as aliases
from ArknightsUID import arknightsuid_maintenance as maintenance
from ArknightsUID.utils.database.models import ArkCommandAlias
from gsuid_core.handler import _command_sort_key, _sv_authorized
from gsuid_core.sv import SL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,user_pm,allowed",
    [
        ({}, 6, False),
        ({}, 1, True),
        ({"enabled": False}, 1, False),
        ({"black_list": ["audit-user"]}, 1, False),
        ({"white_list": ["another-user"]}, 1, False),
        ({"area": "GROUP"}, 1, False),
    ],
)
async def test_alias_uses_target_service_authorization(monkeypatch, policy, user_pm, allowed):
    target_sv = maintenance.maintenance_sv
    monkeypatch.setattr(aliases, "_alias_map", {})
    monkeypatch.setattr(aliases.alias_sv, "TL", {
        kind: dict(triggers) for kind, triggers in aliases.alias_sv.TL.items()
    })
    for name, value in policy.items():
        monkeypatch.setattr(target_sv, name, value)
    target = next(
        trigger for trigger in target_sv.TL["command"].values()
        if trigger.keyword == "删除无效凭据"
    )
    handler = AsyncMock()
    monkeypatch.setattr(target, "func", handler)
    handler.__module__ = "ArknightsUID.arknightsuid_maintenance"
    with patch.object(ArkCommandAlias, "set_alias", new=AsyncMock()):
        assert await aliases.register_command_alias("审计清凭据", "删除无效凭据") is None

    event = SimpleNamespace(
        raw_text="ark审计清凭据", text="ark审计清凭据", command="",
        user_id="audit-user", group_id=None, user_type="direct",
        user_pm=user_pm, is_tome=False, file=None, file_name=None,
        meta_event_type=None,
    )
    candidates = {
        trigger: service.priority
        for service in SL.lst.values()
        if _sv_authorized(service, event, user_pm)
        for triggers in service.TL.values()
        for trigger in triggers.values()
        if trigger.type != "message" and trigger.check_command(event)
    }
    trigger = sorted(candidates.items(), key=_command_sort_key)[0][0]
    bot = SimpleNamespace(send=AsyncMock(), session_id="ark-alias-audit")
    await trigger.func(bot, await trigger.get_command(event))
    assert handler.await_count == int(allowed)
