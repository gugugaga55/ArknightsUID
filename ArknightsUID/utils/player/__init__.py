"""player_info 富数据层（P1）：解析、context 组装、缓存获取。"""

from __future__ import annotations

from .building import (
    MORALE_LOW_THRESHOLD,
    TRADING_STRATEGY_NAMES,
    BuildingData,
    TiredChar,
    format_training_remaining,
    morale_points,
    parse_building,
    training_remaining_secs,
    training_status_text,
)
from .context import (
    build_box_context,
    build_building_context,
    build_campaign_context,
    build_char_card_context,
    build_char_stat_context,
    build_skin_context,
    build_status_context,
)
from .parse import (
    CharEntry,
    PlayerData,
    RecruitSlot,
    compute_ap_current,
    effective_ap,
    parse_player_info,
    recruit_done_count,
    recruit_next_remaining,
)
from .service import (
    DEFAULT_TTL,
    drop_cached_player_info,
    fetch_cached_player_info,
    read_cached_player_info,
)

__all__ = [
    "DEFAULT_TTL",
    "BuildingData",
    "CharEntry",
    "MORALE_LOW_THRESHOLD",
    "PlayerData",
    "RecruitSlot",
    "TRADING_STRATEGY_NAMES",
    "TiredChar",
    "build_box_context",
    "build_building_context",
    "build_campaign_context",
    "build_char_card_context",
    "build_char_stat_context",
    "build_skin_context",
    "build_status_context",
    "compute_ap_current",
    "drop_cached_player_info",
    "effective_ap",
    "fetch_cached_player_info",
    "format_training_remaining",
    "morale_points",
    "parse_building",
    "parse_player_info",
    "read_cached_player_info",
    "recruit_done_count",
    "recruit_next_remaining",
    "training_remaining_secs",
    "training_status_text",
]
