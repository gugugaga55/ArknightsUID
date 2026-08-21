"""一图流（yituliu）数据源（P4：刷图推荐 + 材料掉率）。

允许数据源。实况（2026-08-15 无头浏览器抓包勘正）：
- `GET backend.yituliu.cn/stage/info` ✅：全部关卡元数据
  （stageCode/zoneName/stageType/apCost/spm/start/end）
- **`GET cos.yituliu.cn/arknights/stage-drop/matrix.json` ✅**：掉落矩阵
  （10909 条 {stageId, itemId, times, quantity, start, end}）——网站把
  掉落数据从 backend API 迁到了 COS 静态文件，这才是材料掉率的真实数据源
  （此前"backend 材料级接口 404 = 数据不可用"的结论有误，v16 勘正）
- backend `/stage/result` 等旧 API ❌ 确已下线（不影响上述两源）

spm 为一图流口径的关卡效率值（样本 >300 的关卡才收录），越大越优先。
掉率 = quantity / times（每把期望件数）；单件期望理智 = apCost / 掉率。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from gsuid_core.logger import logger

try:
    from ArknightsUID.ArknightsUID.utils.http import download_bytes
    from ArknightsUID.ArknightsUID.utils.disk_cache import (
        cache_read,
        cache_write,
        is_fresh,
    )
    from ArknightsUID.ArknightsUID.utils.path import CACHE_BASE
except ModuleNotFoundError:
    from ArknightsUID.utils.http import download_bytes
    from ArknightsUID.utils.disk_cache import (
        cache_read,
        cache_write,
        is_fresh,
    )
    from ArknightsUID.utils.path import CACHE_BASE

STAGE_INFO_URL = "https://backend.yituliu.cn/stage/info"
DROP_MATRIX_URL = "https://cos.yituliu.cn/arknights/stage-drop/matrix.json"
_CACHE_DIR = CACHE_BASE / "yituliu"
_STAGE_CACHE = _CACHE_DIR / "stage_info.json"
_MATRIX_CACHE = _CACHE_DIR / "matrix.json"
# stage 元数据缓存 TTL：15 分钟
_TTL = 15 * 60
# 掉落矩阵缓存 TTL：1 小时（1.4MB 静态文件，低频更新）
_MATRIX_TTL = 3600

# stageType → 中文（一图流数据里的取值）
STAGE_TYPE_NAMES = {
    "MAIN": "主线",
    "ACT_PERM": "别传",
    "ACT": "限时活动",
    "SPECIAL": "资源收集",
    "MINI_STORY": "故事集",
}
DEFAULT_GROUPS = ("限时活动", "主线", "别传")


async def fetch_stage_info(force: bool = False) -> list[dict[str, Any]] | None:
    """拉取关卡效率数据（缓存优先，失败回退过期缓存）。"""
    if not force:
        payload, saved_ts = await cache_read(_STAGE_CACHE)
        if isinstance(payload, list) and payload and is_fresh(saved_ts, _TTL):
            return payload
    raw = await _download_json(STAGE_INFO_URL)
    data = raw.get("data") if isinstance(raw, dict) else None
    if isinstance(data, list) and data:
        await cache_write(_STAGE_CACHE, data)
        return data
    payload, _ = await cache_read(_STAGE_CACHE)
    return payload if isinstance(payload, list) else None


def _is_open(record: dict[str, Any], now: float | None = None) -> bool:
    """限时活动按时间窗过滤；永久类型恒开放。"""
    if record.get("stageType") != "ACT":
        return True
    now = time.time() if now is None else now
    # start/end 为毫秒时间戳（0/缺失视为不限）
    start = float(record.get("start") or 0)
    end = float(record.get("end") or 0)
    if start and now < start / 1000:
        return False
    if end and now > end / 1000:
        return False
    return True


def rank_stages(
    records: list[dict[str, Any]],
    group: str,
    top: int = 8,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """按分组取 spm 降序榜单（剔除无效率值/未开放关卡）。

    group 传中文分组名（主线/别传/限时活动/资源收集）或英文 stageType。
    """
    type_by_name = {v: k for k, v in STAGE_TYPE_NAMES.items()}
    stage_type = type_by_name.get(group, group.upper())
    now = time.time() if now is None else now
    rows = [
        record
        for record in records
        if record.get("stageType") == stage_type
        and float(record.get("spm") or 0) > 0
        and _is_open(record, now)
    ]
    rows.sort(
        key=lambda r: (
            -float(r.get("spm") or 0),
            str(r.get("stageCode", "")),
        )
    )
    return rows[:top]


def format_stage_board(
    records: list[dict[str, Any]], top: int = 8, now: float | None = None
) -> str:
    """默认榜单文案：限时活动 / 主线 / 别传 三组。"""
    now = time.time() if now is None else now
    lines = ["「明日方舟」刷图效率榜（一图流 spm，样本>300）"]
    has_any = False
    for group in DEFAULT_GROUPS:
        rows = rank_stages(records, group, top=top, now=now)
        if not rows:
            continue
        has_any = True
        lines.append(f"— {group} —")
        for record in rows:
            lines.append(
                f"  {record.get('stageCode', '?')} {record.get('zoneName', '')} "
                f"spm {float(record.get('spm') or 0):.2f} · {record.get('apCost', '?')} 理智"
            )
    if not has_any:
        lines.append("未获取到有效关卡数据。")
    lines.append("数据：ark.yituliu.cn")
    return "\n".join(lines)


# ==================== 材料掉率（COS 掉落矩阵） ====================


async def _download_json(url: str) -> Any | None:
    """下载 JSON（走共享 HTTP 层，大文件放宽超时）。失败返回 None。"""
    raw = await download_bytes(url, timeout=60.0)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·一图流] {url.rsplit('/', 1)[-1]} 响应非 JSON: "
            f"{exc.__class__.__name__}: {exc}"
        )
        return None


async def fetch_drop_matrix(force: bool = False) -> list[dict[str, Any]] | None:
    """拉取掉落矩阵（1 小时缓存，网络失败回退过期缓存）。

    结构：[{stageId, itemId, times, quantity, stdDev, start, end}]，
    同一 (stageId, itemId) 可能存在多个时间窗（取 end 最新，见 latest_matrix_by_item）。
    """
    if not force:
        payload, saved_ts = await cache_read(_MATRIX_CACHE)
        if isinstance(payload, list) and payload and is_fresh(
            saved_ts, _MATRIX_TTL
        ):
            return payload
    raw = await _download_json(DROP_MATRIX_URL)
    data = raw.get("matrix") if isinstance(raw, dict) else None
    if isinstance(data, list) and data:
        await cache_write(_MATRIX_CACHE, data)
        return data
    payload, _ = await cache_read(_MATRIX_CACHE)
    return payload if isinstance(payload, list) else None


def latest_matrix_by_item(
    matrix: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """itemId → 该材料的掉落记录（同关卡多时间窗只保留 end 最新一条）。"""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for record in matrix:
        if not isinstance(record, dict):
            continue
        key = (str(record.get("itemId", "")), str(record.get("stageId", "")))
        if key not in best or float(record.get("end") or 0) > float(
            best[key].get("end") or 0
        ):
            best[key] = record
    by_item: dict[str, list[dict[str, Any]]] = {}
    for (item_id, _stage_id), record in best.items():
        by_item.setdefault(item_id, []).append(record)
    return by_item


def item_drop_stats(
    item_id: str,
    matrix_by_item: dict[str, list[dict[str, Any]]],
    stage_meta: dict[str, dict[str, Any]],
    top: int = 8,
    min_times: int = 300,
) -> list[dict[str, Any]]:
    """材料的掉落关卡统计（按掉率降序）。

    返回 [{stage_code, zone, rate(每把期望件数), times, ap_expect(单件期望理智)}]，
    剔除样本不足与无关卡元数据的记录。
    """
    rows: list[dict[str, Any]] = []
    for record in matrix_by_item.get(item_id, []):
        times = float(record.get("times") or 0)
        quantity = float(record.get("quantity") or 0)
        if times < min_times or quantity <= 0:
            continue
        meta = stage_meta.get(str(record.get("stageId", "")))
        if not meta:
            continue
        rate = quantity / times
        ap_cost = float(meta.get("apCost") or 0)
        rows.append(
            {
                "stage_code": str(meta.get("stageCode", "?")),
                "zone": str(meta.get("zoneName", "")),
                "stage_type": str(meta.get("stageType", "")),
                "rate": rate,
                "times": int(times),
                "ap_expect": (ap_cost / rate) if rate > 0 else 0.0,
                "ap_cost": ap_cost,
            }
        )
    rows.sort(key=lambda r: -r["rate"])
    return rows[:top]
