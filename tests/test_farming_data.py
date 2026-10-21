"""掉率统计与刷图规划共享真实数据处理入口，固定时间以验证活动窗口。"""

import pytest

from ArknightsUID.utils.gamedata.craft import best_stage_cost, craft_vs_farm
from ArknightsUID.utils.yituliu import item_drop_stats, latest_matrix_by_item


def stage(code, cost, **fields):
    return {"stageCode": code, "apCost": cost, "stageType": "MAIN", **fields}


def drop(code, quantity):
    return {"itemId": "material", "stageId": code, "times": 1000, "quantity": quantity}


def test_farm_plan_compares_cost_across_all_stages():
    matrix = [drop(f"expensive-{i}", 1000 + i) for i in range(8)]
    matrix.append(drop("cheap", 500))
    metadata = {f"expensive-{i}": stage(f"expensive-{i}", 30) for i in range(8)}
    metadata["cheap"] = stage("cheap", 1)
    plan = craft_vs_farm(
        "material", 10, {}, {}, latest_matrix_by_item(matrix), metadata
    )
    assert plan["farm"]["stage_code"] == "cheap"
    assert plan["farm_sanity"] == 20


@pytest.mark.parametrize(
    "start, end",
    [
        (0, 1_700_000_000_000),
        (1_900_000_000_000, 2_000_000_000_000),
        (0, 1_800_000_000_000),
    ],
)
def test_drop_stats_exclude_unavailable_activity_stages(monkeypatch, start, end):
    monkeypatch.setattr("ArknightsUID.utils.yituliu.time.time", lambda: 1_800_000_000)
    matrix = latest_matrix_by_item([drop("event", 2000), drop("main", 500)])
    metadata = {
        "event": stage("event", 10, stageType="ACT", start=start, end=end),
        "main": stage("main", 1),
    }
    assert [
        row["stage_code"] for row in item_drop_stats("material", matrix, metadata)
    ] == ["main"]
    assert best_stage_cost("material", matrix, metadata)["stage_code"] == "main"


def test_open_activity_remains_in_drop_stats(monkeypatch):
    monkeypatch.setattr("ArknightsUID.utils.yituliu.time.time", lambda: 1_800_000_000)
    matrix = latest_matrix_by_item([drop("event", 2000)])
    metadata = {
        "event": stage(
            "event", 10, stageType="ACT", start=1_700_000_000_000, end=1_900_000_000_000
        )
    }
    assert item_drop_stats("material", matrix, metadata)[0]["rate"] == 2
