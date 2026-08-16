"""抽卡记录数据服务（P3）：增量拉取、本地累积、统计分析。

本地存储复用 player_store（gacha_logs.json 已在 gzip 白名单），
增量策略：记录本地最新 gachaTs，翻页拉到旧记录即停，
全部网络失败时用本地缓存兜底出卡。

字段口径（gxy12345 apps/gacha.js）：
- history 元素：charId/charName/rarity/isNew/gachaTs(毫秒)/pos/poolName
- 分页游标：上一页最后一条的 gachaTs + pos，hasMore 控制循环
- rarity 量纲官方未文档化：批量内出现过 6 视为 1 基（六星=6），否则 0 基（六星=5）

卡池类型标签：优先 cate 的 name（官方口径，如「限定寻访」），记录内 poolName
（卡池副标题）作补充；weedy.prts 卡池表留作后续增强（开池时间等）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gsuid_core.logger import logger

from ..path import PLAYER_PATH
from ..player_store import assert_uid_segment, read_player_json, write_player_json

if TYPE_CHECKING:
    from ..enduid.adapter import ArkCredential

# 翻页保险丝：单类别最多拉 50 页（50*100=5000 抽）
_MAX_PAGES = 50
# 本地保留上限（官方接口本身只保留近期记录，超出仅防御）
_KEEP_LIMIT = 20000
# 保底显示上限（明日方舟六星概率 50 抽后递增，无硬保底，社区惯例显示 /80）
PITY_DISPLAY_MAX = 80

_LOGS_NAME = "gacha_logs.json"


def _logs_path(uid: str) -> Path:
    return PLAYER_PATH / assert_uid_segment(uid) / _LOGS_NAME


def _norm_ts(raw: Any) -> int:
    """毫秒/秒归一到秒。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value // 1000 if value > 10**12 else value


def _record_key(record: dict[str, Any]) -> tuple:
    return (
        str(record.get("category", "")),
        str(record.get("gacha_ts", "")),
        str(record.get("pos", "")),
        str(record.get("char_id", "")),
    )


def _normalize_record(raw: dict[str, Any], category: str) -> dict[str, Any]:
    return {
        "category": str(category),
        "char_id": str(raw.get("charId") or raw.get("charIdList") or ""),
        "name": str(raw.get("charName") or raw.get("name") or ""),
        "rarity": int(raw.get("rarity") or 0),
        "is_new": bool(raw.get("isNew")),
        "gacha_ts": _norm_ts(raw.get("gachaTs")),
        "pos": int(raw.get("pos") or 0),
        "pool_name": str(raw.get("poolName") or raw.get("pool") or ""),
    }


def _six_threshold(records: list[dict[str, Any]]) -> int:
    """六星判定阈值：批量里出现过 rarity==6 → 1 基（六=6），否则 0 基（六=5）。"""
    if any(r.get("rarity") == 6 for r in records):
        return 6
    return 5


def merge_records(
    local: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按去重键合并，按时间新→旧排序，截断保留上限。

    键从记录字段现算（本地文件里的记录无 _key，键也不落盘）。
    """
    merged: dict[tuple, dict[str, Any]] = {}
    for record in local:
        merged[_record_key(record)] = record
    for record in incoming:
        merged[_record_key(record)] = record
    result = sorted(
        merged.values(), key=lambda r: (-r.get("gacha_ts", 0), -r.get("pos", 0))
    )
    return result[:_KEEP_LIMIT]


async def load_local_logs(uid: str) -> list[dict[str, Any]]:
    # uid 非法（历史脏数据）当无本地记录处理，读路径不抛异常
    try:
        path = _logs_path(uid)
    except (ValueError, TypeError):
        return []
    data = await read_player_json(path)
    return data if isinstance(data, list) else []


async def save_local_logs(uid: str, records: list[dict[str, Any]]) -> None:
    try:
        await write_player_json(_logs_path(uid), records)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·抽卡] 本地记录写入失败 uid={uid}: {exc.__class__.__name__}"
        )


async def sync_gacha_logs(
    credential: "ArkCredential",
) -> tuple[list[dict[str, Any]], str]:
    """增量拉取并累积。返回 (records, 错误文案)；records 可能为本地兜底。

    网络全失败但本地有记录时返回 (本地, 警告文案)，调用方仍可出卡。
    """
    from .client import (
        get_gacha_categories,
        get_gacha_history_page,
        login_gacha_chain,
    )

    auth = await login_gacha_chain(credential)
    if isinstance(auth, str):
        local = await load_local_logs(credential.uid)
        if local:
            return local, f"{auth}（已用本地记录出卡，可能不是最新）"
        return [], auth

    categories = await get_gacha_categories(credential, auth)
    if categories is None:
        local = await load_local_logs(credential.uid)
        if local:
            return local, "抽卡接口请求失败（已用本地记录出卡，可能不是最新）。"
        return [], "抽卡接口请求失败。"

    local = await load_local_logs(credential.uid)
    local_max_ts = max((r.get("gacha_ts", 0) for r in local), default=0)

    incoming: list[dict[str, Any]] = []
    for cate in categories:
        category = str(cate.get("id") or "")
        if not category:
            continue
        gacha_ts: int | None = None
        pos: int | None = None
        for _ in range(_MAX_PAGES):
            page = await get_gacha_history_page(
                credential, auth, category, gacha_ts=gacha_ts, pos=pos
            )
            if page is None:
                break
            items = page.get("list") or []
            if not items:
                break
            stop = False
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                record = _normalize_record(raw, category)
                incoming.append(record)
                # 已翻到比本地最新更旧的记录 → 后面全是旧的，停止
                if local_max_ts and record["gacha_ts"] < local_max_ts:
                    stop = True
            last = items[-1] if isinstance(items[-1], dict) else {}
            gacha_ts = _norm_ts(last.get("gachaTs")) or None
            pos = last.get("pos")
            if stop or not page.get("hasMore"):
                break

    if not incoming and not local:
        return [], "未拉到任何抽卡记录（官方接口可能已过期清理，或该账号无记录）。"
    merged = merge_records(local, incoming)
    await save_local_logs(credential.uid, merged)
    return merged, ""


def analyze_gacha(records: list[dict[str, Any]]) -> dict[str, Any]:
    """统计：总抽数/六星数/六星率/平均六星/垫刀/最近六星。"""
    if not records:
        return {}
    threshold = _six_threshold(records)
    total = len(records)
    sixes = [r for r in records if r.get("rarity", 0) >= threshold]
    six_count = len(sixes)
    six_rate = f"{six_count * 100 / total:.2f}" if total else "0.00"
    avg_six = f"{total / six_count:.1f}" if six_count else "0.0"

    # 按时间旧→新，按类别计数：六星的垫刀数 = 同类别内上一个六星之后的抽数
    ordered = sorted(
        records, key=lambda r: (r.get("gacha_ts", 0), r.get("pos", 0))
    )
    pulls_in_cate: dict[str, int] = {}
    last_six_in_cate: dict[str, int] = {}
    pull_of_six: list[tuple[dict[str, Any], int]] = []
    for record in ordered:
        cate = str(record.get("category", ""))
        pulls_in_cate[cate] = pulls_in_cate.get(cate, 0) + 1
        if record.get("rarity", 0) >= threshold:
            pull = pulls_in_cate[cate] - last_six_in_cate.get(cate, 0)
            pull_of_six.append((record, pull))
            last_six_in_cate[cate] = pulls_in_cate[cate]
    recent = [
        {
            "name": r.get("name") or r.get("char_id", "?"),
            "pool": (r.get("pool_name") or "")[:10] or "—",
            "pull": pull,
            "ts": r.get("gacha_ts", 0),
            "category": str(r.get("category", "")),
        }
        for r, pull in reversed(pull_of_six[-6:])
    ]

    # 垫刀进度：最新有记录的类别里，距最新六星的抽数
    pity = 0
    if ordered:
        latest_cate = str(ordered[-1].get("category", ""))
        pity = pulls_in_cate.get(latest_cate, 0) - last_six_in_cate.get(latest_cate, 0)

    return {
        "total": total,
        "six_count": six_count,
        "six_rate": six_rate,
        "avg_six": avg_six,
        "pity": pity,
        "pity_max": PITY_DISPLAY_MAX,
        "recent": recent,
    }


def build_gacha_context(
    name: str, uid_masked: str, records: list[dict[str, Any]], version: str
) -> dict[str, Any] | None:
    stats = analyze_gacha(records)
    if not stats:
        return None
    context = dict(stats)
    context.update(
        {
            "name": name or "博士",
            "uid": uid_masked,
            "recent": stats["recent"],
            "version": version,
        }
    )
    return context


def format_gacha_text(
    name: str, uid_masked: str, records: list[dict[str, Any]], warning: str
) -> str:
    stats = analyze_gacha(records)
    lines = [f"「明日方舟」抽卡统计", f"UID：{uid_masked}"]
    if not stats:
        lines.append("暂无抽卡记录。")
    else:
        lines.append(
            f"总抽数 {stats['total']} | 六星 {stats['six_count']} "
            f"（{stats['six_rate']}%，平均 {stats['avg_six']} 抽/六星）"
        )
        lines.append(f"当前垫刀：{stats['pity']} 抽")
        for item in stats["recent"][:4]:
            ts = time.strftime("%y-%m-%d", time.localtime(item["ts"])) if item["ts"] else ""
            lines.append(
                f"  {item['name']}（{item['pool']}，第 {item['pull']} 抽）{ts}"
            )
    if warning:
        lines.append(f"⚠️ {warning}")
    return "\n".join(lines)
