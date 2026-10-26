"""肉鸽图鉴先返回核心资料，并如实标注可选出没信息的加载状态。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID import arknightsuid_rogue as commands
from ArknightsUID.utils.gamedata import rogue_enemy as enemies


@pytest.mark.parametrize("state", ["loading", "failed", "ready"])
@pytest.mark.parametrize("explicit_theme", [False, True])
def test_enemy_context_does_not_wait_for_full_index(monkeypatch, state, explicit_theme):
    entry = {
        "key": "enemy_test",
        "name_zh": "测试敌人",
        "type": ["NORMAL", "sarkaz"],
        "stats": [{"hp": 100, "atk": 10, "def": 0, "res": 0}],
        "forms": [],
    }
    monkeypatch.setattr(
        enemies, "load_enemy_db", AsyncMock(return_value={"enemy_test": entry})
    )
    # 未指定期时，难度表显式 targets 仍优先于敌人类型启发式。
    difficulty = AsyncMock(
        return_value={
            "black": [
                {
                    "difficulty": 0,
                    "effects": [{"targets": ["enemy_test"], "mods": []}],
                }
            ]
        }
    )
    monkeypatch.setattr(enemies, "load_difficulty_mods", difficulty)
    monkeypatch.setattr(enemies, "enemy_icon_b64", AsyncMock(return_value=""))
    monkeypatch.setattr(enemies, "load_skill_dict", AsyncMock(return_value={}))
    appearances = AsyncMock(return_value=({}, state))
    monkeypatch.setattr(enemies, "load_stage_appearances", appearances, raising=False)

    async def slow_index():
        await asyncio.Event().wait()

    monkeypatch.setattr(enemies, "build_stage_index", slow_index)
    context = asyncio.run(
        asyncio.wait_for(
            commands._build_rogue_enemy_context(
                "测试敌人 树海 N0" if explicit_theme else "测试敌人 N0"
            ),
            timeout=0.05,
        )
    )
    assert context["name"] == "测试敌人"
    assert context["forms"][0]["hp"] == 100
    assert context["stage_status"] == state
    if explicit_theme:
        difficulty.assert_awaited_once_with(theme_dir="black")
    else:
        difficulty.assert_awaited_once_with()
    appearances.assert_awaited_once_with("black")
    note = context["stage_note"]
    if state == "loading":
        assert "加载中" in note
        assert "未查到" not in note
    elif state == "failed":
        assert "暂时不可用" in note
        assert "未查到" not in note
    else:
        assert "未查到" in note
    assert note in commands._format_rogue_enemy(context)
    from ArknightsUID.utils.render import ark_templates

    html = ark_templates.get_template("ark_rogue_enemy.html").render(**context)
    assert note in html


def test_refresh_clears_rogue_data_but_preserves_images(tmp_path, monkeypatch):
    from ArknightsUID import arknightsuid_maintenance as maintenance
    from ArknightsUID.utils import path as paths

    remove = [
        "gamedata/character_table.json",
        "gamedata/is_enemy/enemy_database.json",
        "gamedata/is_enemy/enemy_database.json.validators.json",
        "gamedata/is_enemy/enemy_database.json.miss",
        "gamedata/is_enemy/enemy_skills.json",
        "gamedata/is_enemy/difficulty_black.json",
        "gamedata/is_enemy/difficulty_black.json.miss",
        "gamedata/is_enemy/stages/meta_black.json",
        "gamedata/is_enemy/stages/meta_black.json.validators.json",
        "gamedata/is_enemy/stages/level_rogue6_6-2.json.miss",
        "gamedata/is_enemy/stages/ro6/level_rogue6_6-2.json",
        "gamedata/is_enemy/stages/ro6/level_rogue6_6-2.json.miss",
    ]
    keep = [
        "gamedata/is_enemy/icons/enemy_test.webp",
        "gamedata/is_enemy/icons/enemy_test.webp.miss",
        "gamedata/is_enemy/other.json",
        "wiki/map_rogue/map.webp",
        "prts/page.json",
    ]
    for relative in remove + keep:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("cached")
    monkeypatch.setattr(paths, "CACHE_BASE", tmp_path)
    clear = AsyncMock()
    monkeypatch.setattr(enemies, "clear_stage_index_cache", clear, raising=False)
    monkeypatch.setattr(maintenance, "_send_text", AsyncMock())
    asyncio.run(maintenance.refresh_gamedata_cache(None, SimpleNamespace()))
    clear.assert_awaited_once()
    assert all(not (tmp_path / relative).exists() for relative in remove)
    assert all((tmp_path / relative).exists() for relative in keep)
