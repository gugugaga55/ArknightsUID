from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID import arknightsuid_alias as aliases
from ArknightsUID.utils.database import models
from gsuid_core import server


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start_before", "start", "reload"])
async def test_core_start_replays_aliases_before_first_message(monkeypatch, phase):
    monkeypatch.setattr(aliases, "_installed", False)
    monkeypatch.setattr(aliases, "_alias_map", {})
    monkeypatch.setattr(aliases.alias_sv, "TL", {
        kind: dict(triggers) for kind, triggers in aliases.alias_sv.TL.items()
    })
    aliases._uninstall_alias_trigger("招募")
    aliases._uninstall_alias_trigger("首条别名")
    rows = AsyncMock(return_value=[SimpleNamespace(alias="首条别名", target="公招推荐")])
    monkeypatch.setattr(models.ArkCommandAlias, "list_aliases", rows)
    hook_name = "core_start_before_def" if phase == "start_before" else "core_start_def"
    hooks = {
        hook for hook in getattr(server, hook_name)
        if hook.func.__module__ == aliases.__name__
    }
    monkeypatch.setattr(server, hook_name, hooks)

    if phase == "reload":
        from gsuid_core.utils.plugins_update import reload_plugin

        reload_plugin._run_plugin_start_hooks("ArknightsUID")
        await reload_plugin._plugin_start_tasks["ArknightsUID"]
    elif phase == "start_before":
        await server.core_start_before_execute()
    else:
        await server.core_start_execute()

    assert aliases._installed
    rows.assert_awaited_once()
    event = SimpleNamespace(raw_text="ark首条别名 术师", is_tome=False)
    assert aliases.alias_sv.TL["command"]["ark首条别名"].check_command(event)
    assert min(hook.priority for hook in hooks) > 0
