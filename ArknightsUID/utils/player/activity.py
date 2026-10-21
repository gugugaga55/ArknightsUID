"""森空岛个人活动数据：字段契约见 docs/ACTIVITY-PROGRESS-API-RESEARCH-2026-10-06.md。"""

from typing import Any


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict]:
    return (
        [row for row in value if isinstance(row, dict)]
        if isinstance(value, list)
        else []
    )


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 and value < 10**12 else None


def _name(value: Any, fallback: str) -> str:
    if (
        not isinstance(value, (str, int, float))
        or isinstance(value, bool)
        or value == ""
    ):
        return fallback
    return str(value).replace("\n", " ")[:80]


def _lookup(metadata: dict, key: Any) -> dict:
    return _mapping(metadata.get(key)) if isinstance(key, str) else {}


def format_activity_progress(data: dict, keyword: str = "") -> str:
    metadata = _mapping(data.get("activityInfoMap"))
    lines = []
    for activity in _rows(data.get("activity")):
        info = _lookup(metadata, activity.get("actId"))
        if info.get("type") not in ("SIDESTORY", "BRANCHLINE") or info.get(
            "isReplicate"
        ):
            continue
        name = _name(info.get("name"), "未命名活动")
        if keyword and keyword not in name:
            continue
        cleared = total = 0
        zones = activity.get("zones")
        valid = isinstance(zones, list) and bool(zones)
        for zone in zones if isinstance(zones, list) else []:
            if not isinstance(zone, dict):
                valid = False
                break
            value, maximum = (
                _count(zone.get("clearedStage")),
                _count(zone.get("totalStage")),
            )
            if value is None or maximum is None or maximum <= 0 or value > maximum:
                valid = False
                break
            cleared += value
            total += maximum
        if valid:
            state = "已通关" if cleared == total else "通关进度"
            lines.append(
                f"{name}：{state} {cleared}/{total}（{cleared * 100 // total}%）"
            )
        else:
            lines.append(f"{name}：进度暂不可用")
    if not lines:
        return (
            "森空岛暂无匹配的个人活动进度。"
            if keyword
            else "森空岛暂无可展示的个人活动进度。"
        )
    return (
        "「明日方舟」个人活动进度\n"
        + "\n".join(lines[:20])
        + "\n关卡通关进度；领奖、商店与蚀刻章明细尚未提供。"
    )


def format_activity_records(data: dict, keyword: str = "") -> str:
    """玩法最高进度摘要，与官方资料页字段口径一致，不推测单局战斗细节。"""
    rows = []
    tower_metadata = _mapping(data.get("towerInfoMap"))
    for record in _rows(_mapping(data.get("tower")).get("records")):
        info = _lookup(tower_metadata, record.get("towerId"))
        name = _name(info.get("name"), "保全派驻")
        best = record.get("best")
        if isinstance(best, (int, str)) and not isinstance(best, bool):
            rows.append(f"保全派驻 · {name}：最高进度：{_name(best, '暂无记录')}")
    difficulties = {
        "NORMAL": "初始试炼",
        "TEAM": "定向试炼",
        "EX": "恢弘试炼",
        "SP": "最终试炼",
    }
    stage_metadata = _mapping(data.get("stageInfoMap"))
    for challenge in _rows(data.get("bossRush")):
        record = _mapping(challenge.get("record"))
        label = "暂无记录"
        if record.get("played") is True:
            label = difficulties.get(_name(record.get("difficulty"), ""), "已参与")
            stage = _lookup(stage_metadata, record.get("stageId"))
            if stage.get("code"):
                label += f" {_name(stage['code'], '')}"
        rows.append(f"引航者试炼 · {_name(challenge.get('id'), '未知期次')}：{label}")
    for sandbox in _rows(data.get("sandbox")):
        stats = []
        for key, label, suffix in (
            ("maxDay", "最高生存", " 天"),
            ("maxDayChallenge", "挑战生存", " 天"),
            ("baseLv", "营地等级", ""),
            ("unlockNode", "解锁节点", ""),
            ("enemyKill", "击败敌人", ""),
        ):
            count = _count(sandbox.get(key))
            if count is not None:
                stats.append(f"{label}：{count}{suffix}")
        if stats:
            rows.append(
                f"生息演算 · {_name(sandbox.get('id'), '未知期次')}："
                + " / ".join(stats)
            )
    if keyword:
        rows = [row for row in rows if keyword in row]
    if not rows:
        return "森空岛暂无匹配的个人活动战绩。肉鸽详细战绩请用 ark肉鸽战绩。"
    return (
        "「明日方舟」个人活动战绩\n"
        + "\n".join(rows[:25])
        + "\n肉鸽详细战绩：ark肉鸽战绩 [期名]"
    )
