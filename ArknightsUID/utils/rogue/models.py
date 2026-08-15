"""肉鸽（集成战略）数据模型。

基于真实 player_info.data.rogue dump（tools/rogue_dump.json）填充精确字段。
rogue 顶层结构：{"records": [主题1, 主题2, ...]}，每个主题对应一期集成战略
（rogue_1..rogue_6 = IS1..IS6），含通关/收藏/蜡烛/声望/奖章。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RogueMedal:
    total: int = 0      # 奖章总数
    current: int = 0    # 已获得


@dataclass(slots=True)
class RogueBank:
    current: int = 0    # 当前蜡烛/投资
    record: int = 0     # 历史最高


@dataclass(slots=True)
class RogueRecord:
    rogue_id: str = ""  # 主题 ID（rogue_1..rogue_6）
    relic_cnt: int = 0  # 收藏品解锁数
    bank: RogueBank = field(default_factory=RogueBank)
    clear_time: int = 0   # 通关次数
    bp_level: int = 0     # 声望等级
    medal: RogueMedal = field(default_factory=RogueMedal)


@dataclass(slots=True)
class RogueInfo:
    records: list[RogueRecord] = field(default_factory=list)
    # 原始 dict 兜底（保留未知字段，供将来扩展）
    raw: dict[str, Any] = field(default_factory=dict)
