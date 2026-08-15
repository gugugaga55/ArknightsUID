"""肉鸽（集成战略）解析器。

parse_rogue 精确解析 records（基于真实 dump）；flatten_rogue 保留作未知结构兜底。
"""
from __future__ import annotations

from typing import Any

from .models import RogueBank, RogueInfo, RogueMedal, RogueRecord

__all__ = [
    "RogueInfo",
    "RogueRecord",
    "RogueBank",
    "RogueMedal",
    "parse_rogue",
    "flatten_rogue",
]


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_rogue(data: Any) -> RogueInfo | None:
    """精确解析 rogue.records（基于真实 dump 结构）。"""
    if not isinstance(data, dict) or not data:
        return None
    info = RogueInfo(raw=dict(data))
    for item in data.get("records") or []:
        if not isinstance(item, dict):
            continue
        bank = item.get("bank") or {}
        medal = item.get("medal") or {}
        info.records.append(
            RogueRecord(
                rogue_id=str(item.get("rogueId", "")),
                relic_cnt=_to_int(item.get("relicCnt")),
                bank=RogueBank(
                    current=_to_int(bank.get("current")),
                    record=_to_int(bank.get("record")),
                ),
                clear_time=_to_int(item.get("clearTime")),
                bp_level=_to_int(item.get("bpLevel")),
                medal=RogueMedal(
                    total=_to_int(medal.get("total")),
                    current=_to_int(medal.get("current")),
                ),
            )
        )
    return info


def flatten_rogue(
    data: Any, max_depth: int = 3, _depth: int = 0, _prefix: str = ""
) -> list[tuple[str, str]]:
    """防御性遍历未知结构，拍平成 [(标签, 标量值)]。

    records 已由 parse_rogue 精确解析；此函数仅作未知字段/未来扩展的兜底展示。
    """
    items: list[tuple[str, str]] = []
    if _depth >= max_depth:
        return items
    if isinstance(data, dict):
        for key, val in list(data.items())[:30]:
            label = f"{_prefix}.{key}" if _prefix else str(key)
            if isinstance(val, (str, int, float, bool)) and val != "":
                items.append((label, str(val)))
            elif isinstance(val, (dict, list)):
                items.extend(flatten_rogue(val, max_depth, _depth + 1, label))
    elif isinstance(data, list):
        for index, val in enumerate(list(data)[:20]):
            label = f"{_prefix}[{index}]"
            if isinstance(val, (str, int, float, bool)) and val != "":
                items.append((label, str(val)))
            elif isinstance(val, (dict, list)):
                items.extend(flatten_rogue(val, max_depth, _depth + 1, label))
    return items
