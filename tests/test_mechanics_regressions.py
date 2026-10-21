"""依据 PRTS 寻访/公招规则及真实业务入口验证计算，避免复制实现常量。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ArknightsUID import arknightsuid_cultivate as cultivation
from ArknightsUID import arknightsuid_gacha as command
from ArknightsUID.utils import gacha_plan as plan
from ArknightsUID.utils.gamedata import gacha_sim, recruit


@pytest.mark.parametrize(
    "pity, expected",
    [(49, 0.02), (50, 0.04), (51, 0.06), (60, 0.24), (97, 0.98), (98, 1)],
)
def test_six_star_soft_pity_matches_published_schedule(pity, expected):
    assert plan.six_rate(pity) == pytest.approx(expected)


def test_short_probability_horizon_is_not_a_guarantee():
    table = plan.build_up_table(0.35, max_pulls=10)
    assert table[-1] == pytest.approx(1 - (1 - 0.02 * 0.35) ** 10)
    assert plan.build_up_table(0.35, max_pulls=0) == [0]


def test_short_horizon_keeps_existing_pity():
    assert plan.build_up_table(0.35, max_pulls=1, start_pity=50)[1] == pytest.approx(
        0.04 * 0.35
    )


def test_zero_draw_target_is_preserved():
    summary = plan.build_summary(plan.parse_plan_args("目标:0"), None, 0)
    assert summary.target_draws == 0 and summary.well_gap == 0
    assert "0 抽井线" not in plan.format_plan_text(summary, "", "")
    assert plan.build_plan_context(summary, "", "")["well_pct"] == 100


def test_unknown_pool_model_is_reported():
    assert plan.parse_plan_args("类型:错别字").errors


class FixedRarity:
    def __init__(self, rarity):
        self.rarity = rarity

    def choices(self, values, weights, k):
        return [self.rarity if self.rarity in values else min(values)]

    def choice(self, values):
        return values[0]


def pools():
    return {
        r: [{"char_id": f"char_{r}", "name": str(r), "profession": "WARRIOR"}]
        for r in (3, 4, 5, 6)
    }


def test_new_pool_first_ten_has_five_star_even_with_four_stars():
    result = gacha_sim.simulate_pulls(pools(), 10, FixedRarity(4))
    assert [r["rarity"] for r in result] == [4] * 9 + [5]
    assert result[-1]["guarantee"] is True


def test_missing_rarity_pool_does_not_mislabel_operator():
    result = gacha_sim.simulate_pulls({3: pools()[3]}, 1, FixedRarity(6))
    assert result[0]["rarity"] == 3
    assert result[0]["char_id"] == "char_3"


def test_auto_pity_is_scoped_to_selected_pool(monkeypatch):
    records = [
        {
            "category": "limited",
            "pool_name": "旧池",
            "gacha_ts": 10 + i,
            "pos": 0,
            "rarity": 3,
        }
        for i in range(20)
    ] + [
        {
            "category": "limited",
            "pool_name": "目标池",
            "gacha_ts": 50,
            "pos": 0,
            "rarity": 3,
        }
    ]
    monkeypatch.setattr(command, "load_local_logs", AsyncMock(return_value=records))
    credential = SimpleNamespace(uid="12345")
    assert asyncio.run(command._auto_pity(credential, "目标池")) == 1
    assert asyncio.run(command._auto_pity(credential, "未开新池")) == 0
    assert asyncio.run(command._auto_pity(credential, "")) == 0


def test_recruit_nine_hour_floor_excludes_short_duration_units():
    data = [
        {"name": "支援机械", "rarity": 1, "all_tags": ["治疗", "支援", "支援机械"]},
        {"name": "五星医疗", "rarity": 5, "all_tags": ["治疗", "支援", "资深干员"]},
    ]
    result = recruit.recommend_combos(["治疗", "支援"], data)
    assert result["combos"][0]["min_rarity"] == 5
    assert result["combos"][0]["duration"] == "9:00"


def test_recruit_robot_recommendation_uses_short_duration():
    data = [{"name": "Lancet-2", "rarity": 1, "all_tags": ["支援机械", "治疗"]}]
    result = recruit.recommend_combos(["支援机械", "治疗"], data)
    assert result["combos"][0]["duration"] == "3:50"
    assert result["combos"][0]["match_names"] == ["Lancet-2"]


@pytest.mark.parametrize("names, expected_count", [("史尔特尔 42", 1), ("银灰 42", 2)])
def test_aliases_and_shared_inventory_in_cultivation_plan(
    monkeypatch, names, expected_count
):
    credential = SimpleNamespace(uid="12345", nickname="博士")
    monkeypatch.setattr(
        cultivation, "resolve_or_reply", AsyncMock(return_value=credential)
    )
    monkeypatch.setattr(
        cultivation,
        "_resolve_query_to_char_id",
        AsyncMock(
            side_effect=lambda name, *args: (
                "char_172_svrash" if name == "银灰" else "char_350_surtr",
                "",
            )
        ),
    )
    from ArknightsUID.utils.enduid import adapter

    inventory = {"code": 0, "data": {"player_item_data": {}}}
    inventory_fetch = AsyncMock(return_value=inventory)
    monkeypatch.setattr(adapter, "fetch_cultivate_player", inventory_fetch)
    from ArknightsUID.utils import player
    from ArknightsUID.utils.gamedata import charinfo

    player_data = {
        "code": 0,
        "data": {
            "chars": [
                {"charId": "char_350_surtr", "evolvePhase": 0},
                {"charId": "char_172_svrash", "evolvePhase": 0},
            ]
        },
    }
    cost = {
        "code": 0,
        "data": {"evolvePhaseCost": [{"items": [{"id": "3003", "count": 8}]}]},
    }
    monkeypatch.setattr(
        player, "fetch_cached_player_info", AsyncMock(return_value=player_data)
    )
    monkeypatch.setattr(
        adapter, "fetch_cultivate_character", AsyncMock(return_value=cost)
    )
    monkeypatch.setattr(charinfo, "load_item_index", AsyncMock(return_value={}))
    calculate = AsyncMock(wraps=cultivation.calculate_cultivation)
    monkeypatch.setattr(cultivation, "calculate_cultivation", calculate)
    monkeypatch.setattr(cultivation, "_build_farm_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(cultivation, "item_icon_b64", AsyncMock(return_value=""))
    monkeypatch.setattr(cultivation, "char_avatar_b64", AsyncMock(return_value=""))
    monkeypatch.setattr(cultivation, "hide_uid_for", AsyncMock(return_value="1***5"))
    send = AsyncMock()
    monkeypatch.setattr(cultivation, "send_card_or_text", send)
    asyncio.run(
        cultivation.show_cultivation_plan(
            None, SimpleNamespace(text=names, user_id="u", bot_id="b")
        )
    )
    assert calculate.await_count == expected_count
    inventory_fetch.assert_awaited_once_with(credential)
    merged = send.await_args.args[3]["merged"]
    assert merged["materials"][0]["need"] == 8 * expected_count
    assert len(merged["chars"]) == expected_count


def test_cultivation_module_progress_matches_id_not_position(monkeypatch):
    from ArknightsUID.utils import cultivate
    from ArknightsUID.utils.enduid import adapter
    from ArknightsUID.utils.gamedata import _table_cache, charinfo
    from ArknightsUID.utils import player

    raw = {
        "code": 0,
        "data": {
            "chars": [
                {
                    "charId": "char_test",
                    "equip": [
                        {"id": "certificate", "level": 1},
                        {"id": "module_y", "level": 2},
                        {"id": "module_x", "level": 3},
                    ],
                }
            ]
        },
    }
    cost = {
        "code": 0,
        "data": {
            "equipCost": [
                {
                    "typeName": "X",
                    "levelCost": [
                        {"items": [{"id": "x", "count": n}]} for n in (2, 4, 6)
                    ],
                },
                {
                    "typeName": "Y",
                    "levelCost": [
                        {"items": [{"id": "y", "count": n}]} for n in (20, 40, 60)
                    ],
                },
            ]
        },
    }
    modules = {
        "equipDict": {
            "certificate": {"typeName1": "ORIGINAL", "typeName2": None},
            "module_x": {"typeName1": "LOR", "typeName2": "X"},
            "module_y": {"typeName1": "LOR", "typeName2": "Y"},
        }
    }
    monkeypatch.setattr(player, "fetch_cached_player_info", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        adapter, "fetch_cultivate_character", AsyncMock(return_value=cost)
    )
    monkeypatch.setattr(
        adapter,
        "fetch_cultivate_player",
        AsyncMock(return_value={"code": 0, "data": {"player_item_data": {}}}),
    )
    monkeypatch.setattr(
        _table_cache, "ensure_uniequip_table", AsyncMock(return_value=modules)
    )
    monkeypatch.setattr(charinfo, "load_item_index", AsyncMock(return_value={}))
    result = asyncio.run(
        cultivate.calculate_cultivation(SimpleNamespace(uid="12345"), "char_test")
    )
    assert isinstance(result, dict)
    assert result["need"] == {"y": 60}


def test_pool_keyword_must_not_silently_choose_ambiguous_pool(monkeypatch):
    pools = [
        SimpleNamespace(title=name, end="", end_ts=10)
        for name in ("联合行动一", "联合行动二")
    ]
    monkeypatch.setattr(command, "query_gacha_pools", AsyncMock(return_value=pools))
    pool, error = asyncio.run(command._pick_pool("联合"))
    assert pool is None and "联合行动一" in error and "联合行动二" in error


def test_both_senior_tags_use_highest_rarity():
    data = recruit.entries_from_smw(
        {
            "六星": {"printouts": {"稀有度": ["5"], "标签": ["输出"]}},
            "五星": {"printouts": {"稀有度": ["4"], "标签": ["输出"]}},
        }
    )
    matched = recruit.match_recruit(["高级资深干员", "资深干员", "输出"], data)
    assert [m["name"] for m in matched] == ["六星"]


def test_recruit_does_not_waste_recommendation_on_both_senior_tags():
    data = recruit.entries_from_smw(
        {
            "六星": {"printouts": {"稀有度": ["5"], "标签": ["输出"]}},
            "五星": {"printouts": {"稀有度": ["4"], "标签": ["输出"]}},
        }
    )
    result = recruit.recommend_combos(["高级资深干员", "资深干员", "输出"], data)
    assert all(
        not {"高级资深干员", "资深干员"} <= set(c["tags"]) for c in result["combos"]
    )
