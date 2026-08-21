"""危机合约赛季数据（crisis_table + crisis_v2_table，ArknightsGameData）。

旧版（#0-12 行动，crisis_table.seasonInfo）与新版（涤墨作战起，
crisis_v2_table.seasonInfoDataMap）合并为统一时间线；词条效果文本
走 PRTS SMW（prts.client.query_crisis_runes）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ._table_cache import ensure_crisis_tables


def _fmt_ts(ts: Any) -> str:
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d")


def _season_rows(payload: dict | None, version: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    seasons = payload.get("seasonInfo") or payload.get("seasonInfoDataMap") or {}
    rows: list[dict[str, Any]] = []
    if isinstance(seasons, dict):
        iterator = seasons.values()
    else:  # 旧表 seasonInfo 是 list
        iterator = seasons
    for season in iterator:
        if not isinstance(season, dict):
            continue
        try:
            start_ts = int(season.get("startTs") or 0)
        except (TypeError, ValueError):
            start_ts = 0
        rows.append(
            {
                "name": str(season.get("name") or season.get("seasonId") or ""),
                "start_ts": start_ts,
                "start": _fmt_ts(season.get("startTs")),
                "end": _fmt_ts(season.get("endTs")),
                "version": version,
            }
        )
    return rows


async def load_crisis_seasons() -> list[dict[str, Any]]:
    """全部危机合约赛季（旧+新合并，按开始时间降序）。失败返回 []。"""
    old, new = await ensure_crisis_tables()
    rows = _season_rows(old, "旧危机合约") + _season_rows(new, "危机合约V2")
    rows.sort(key=lambda r: r["start_ts"], reverse=True)
    return rows
