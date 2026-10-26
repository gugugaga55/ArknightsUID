"""关卡覆写/倍率/模式计数契约。

真实字段切片来自 tomimi src/lib/data/stages/ro_stage_data：
level_rogue1_1-1（overwrittenData.atk=100）、level_rogue4_b-7（ALL×1.2）、
level_rogue1_1-3（普通0/紧急2替换炮手）。stageLoad.prepareStage先覆写，
statHelpers.parseStats先round地图runes，再应用difficulty/floor，不平铺initial倍率。
"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from ArknightsUID import arknightsuid_rogue as commands
from ArknightsUID.utils.gamedata import rogue_enemy as enemies
from ArknightsUID.utils.prts import client as prts
from ArknightsUID.utils import render


@pytest.fixture
def source(monkeypatch):
    entry = {
        "key": "enemy_test",
        "name_zh": "测试敌人",
        "type": ["BOSS"],
        "stats": [{"hp": 100, "atk": 180, "def": 80, "res": 50}],
        "forms": [],
    }
    item = {
        "id": "enemy_test",
        "level": 0,
        "min_count": 1,
        "max_count": 1,
        "elite_min_count": 1,
        "elite_max_count": 1,
    }
    stage = {"enemies": [item], "floors": [1]}
    grades = [{"difficulty": 3, "effects": []}]
    monkeypatch.setattr(
        enemies,
        "_load_stage_meta",
        AsyncMock(
            side_effect=lambda theme: (
                {"level_rogue4_1-1": {"name": "测试关卡", "code": "ISW-NO"}}
                if theme == "sarkaz"
                else {}
            )
        ),
    )
    monkeypatch.setattr(enemies, "load_stage_data", AsyncMock(return_value=stage))
    monkeypatch.setattr(
        enemies, "load_enemy_db", AsyncMock(return_value={"enemy_test": entry})
    )
    monkeypatch.setattr(
        enemies, "load_difficulty_mods", AsyncMock(return_value={"sarkaz": grades})
    )
    monkeypatch.setattr(enemies, "load_skill_dict", AsyncMock(return_value={}))
    monkeypatch.setattr(
        enemies, "load_stage_appearances", AsyncMock(return_value=({}, "ready"))
    )
    monkeypatch.setattr(enemies, "enemy_icon_b64", AsyncMock(return_value=""))
    monkeypatch.setattr(prts, "rogue_map_file_urls", AsyncMock(return_value=[]))
    monkeypatch.setattr(render, "get_image_b64_with_cache", AsyncMock(return_value=""))
    return entry, item, stage, grades


@pytest.mark.parametrize("overrides", [{"atk": 100}, {"atk": 0, "res": 0}])
def test_declared_stage_overrides_and_inheritance_match_both_commands(
    source, overrides
):
    entry, item, _, _ = source
    original = deepcopy(entry)
    item["overwrittenData"] = overrides

    async def scenario():
        stage = await commands._build_rogue_stage_context("测试关卡")
        enemy = await commands._build_rogue_enemy_context("测试敌人 测试关卡")
        expected = {**original["stats"][0], **overrides}
        assert stage["rows"][0]["stats"] == expected
        assert enemy["focus"]["stats"] == expected

    asyncio.run(scenario())
    assert entry == original, "stage overrides must not mutate shared enemy database"


@pytest.mark.parametrize("elite,expected", [(False, 198), (True, 234)])
def test_map_runes_finish_before_difficulty_initial_multiplier(source, elite, expected):
    entry, _, stage, grades = source
    stage["all_mods"] = [
        {
            "targets": ["ALL"],
            "mods": [{"key": "hp", "value": 0.2, "mode": "mul", "order": "initial"}],
        }
    ]
    stage["n_mods"] = [
        {"targets": ["ALL"], "mods": [{"key": "hp", "value": 1.1, "mode": "mul"}]}
    ]
    stage["elite_mods"] = [
        {"targets": ["ALL"], "mods": [{"key": "hp", "value": 1.3, "mode": "mul"}]}
    ]
    grades[0]["effects"] = [
        {
            "targets": ["BOSS"],
            "mods": [{"key": "hp", "value": 0.5, "mode": "mul", "order": "initial"}],
        }
    ]
    suffix = " N3" + (" 紧急" if elite else "")

    async def scenario():
        stage_context = await commands._build_rogue_stage_context("测试关卡" + suffix)
        enemy_context = await commands._build_rogue_enemy_context(
            "测试敌人 测试关卡" + suffix
        )
        assert stage_context["rows"][0]["stats"]["hp"] == expected
        assert enemy_context["focus"]["stats"]["hp"] == expected
        notes = enemy_context["focus"]["buffs"]
        assert any(note.startswith("关卡：") for note in notes)
        assert any(note.startswith("紧急：" if elite else "普通：") for note in notes)

    asyncio.run(scenario())
    assert entry["stats"][0]["hp"] == 100


def test_overwritten_normal_type_does_not_keep_boss_difficulty_buff(source):
    entry, item, stage, grades = source
    item["overwrittenData"] = {"levelType": "NORMAL"}
    grades[0]["effects"] = [
        {"targets": ["BOSS"], "mods": [{"key": "hp", "value": 2, "mode": "mul"}]}
    ]
    row = enemies.stage_panel_rows(stage, {entry["key"]: entry}, grades, 3)[0]
    assert row["stats"]["hp"] == 100
    assert entry["type"] == ["BOSS"]


def test_mode_counts_filter_replacements_but_preserve_unknown_summons(source):
    entry, _, _, _ = source
    db = {}
    items = []
    for key, normal, elite in [
        ("replaced", (2, 2), (0, 0)),
        ("replacement", (0, 0), (2, 2)),
        ("random", (1, 3), (2, 4)),
        ("summoned", (0, 0), (0, 0)),
    ]:
        db[key] = {**entry, "key": key}
        items.append(
            {
                "id": key,
                "min_count": normal[0],
                "max_count": normal[1],
                "elite_min_count": elite[0],
                "elite_max_count": elite[1],
            }
        )
    stage = {"enemies": items}
    normal = {row["key"]: row for row in enemies.stage_panel_rows(stage, db, [], None)}
    elite = {
        row["key"]: row for row in enemies.stage_panel_rows(stage, db, [], None, True)
    }
    assert set(normal) == {"replaced", "random", "summoned"}
    assert set(elite) == {"replacement", "random", "summoned"}
    assert normal["replaced"]["count"] == "2"
    assert elite["replacement"]["count"] == "2"
    assert elite["random"]["count"] == "2-4"
    assert normal["summoned"]["count"] == "波次"


def test_missing_elite_count_fields_keep_normal_count(source):
    entry, item, stage, _ = source
    item.pop("elite_min_count")
    item.pop("elite_max_count")
    item.update(min_count=1, max_count=3)
    row = enemies.stage_panel_rows(stage, {entry["key"]: entry}, [], None, True)[0]
    assert row["count"] == "1-3"


def test_map_half_rounds_up_before_difficulty_and_floor(source):
    entry, _, stage, grades = source
    entry["stats"][0]["hp"] = 101
    stage["all_mods"] = [
        {"targets": ["ALL"], "mods": [{"key": "hp", "value": 0.5, "mode": "mul"}]}
    ]
    grades[0].update(
        floorBuff=0.1,
        effects=[
            {"targets": ["BOSS"], "mods": [{"key": "hp", "value": 2, "mode": "mul"}]}
        ],
    )
    # Map 101×0.5=50.5 → JS Math.round=51; then 51×2×1.1²=123.42 →123.
    rows = enemies.stage_panel_rows(stage, {entry["key"]: entry}, grades, 3, floor=2)
    assert rows[0]["stats"]["hp"] == 123


def test_mode_filtered_enemies_are_not_reported_as_unrecorded(source):
    entry, item, stage, _ = source
    item.update(min_count=0, max_count=0, elite_min_count=2, elite_max_count=2)

    async def scenario():
        normal = await commands._build_rogue_stage_context("测试关卡")
        assert normal["rows"] == []
        assert normal["skipped"] == 0
        focused = await commands._build_rogue_enemy_context("测试敌人 测试关卡")
        assert isinstance(focused, str) and "普通作战中出场" in focused
        stage["enemies"].append({"id": "not_in_db", "max_count": 1})
        normal = await commands._build_rogue_stage_context("测试关卡")
        assert normal["skipped"] == 1

    asyncio.run(scenario())


def test_missing_template_key_preserves_explicit_stage_difficulty_targets(source):
    entry, _, stage, grades = source
    entry.pop("key")
    grades[0]["effects"] = [
        {"targets": ["enemy_test"], "mods": [{"key": "hp", "value": 2, "mode": "mul"}]}
    ]
    row = enemies.stage_panel_rows(stage, {"enemy_test": entry}, grades, 3)[0]
    assert row["stats"]["hp"] == 200
    assert "key" not in entry


def test_difficulty_table_and_focused_panel_round_half_consistently(source):
    entry, _, _, grades = source
    entry["stats"][0]["hp"] = 101
    grades[0]["effects"] = [
        {"targets": ["BOSS"], "mods": [{"key": "hp", "value": 0.5, "mode": "mul"}]}
    ]
    table = enemies.build_diff_panels(entry, grades)
    selected = next(row for row in table if row["grade"] == 3)
    focus = enemies.build_panel_at(entry, grades, 3)
    assert selected["stats"]["hp"] == focus["stats"]["hp"] == 51
