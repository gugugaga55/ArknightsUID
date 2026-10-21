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

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gsuid_core.logger import logger

from ..time_utils import beijing_datetime
from ..path import PLAYER_PATH
from ..player_store import assert_uid_segment, read_player_json, write_player_json

if TYPE_CHECKING:
    from ..enduid.adapter import ArkCredential

# 翻页保险丝：单类别最多拉 50 页（50*100=5000 抽）
_MAX_PAGES = 50
# 本地保留上限（官方接口本身只保留近期记录，超出仅防御）
_KEEP_LIMIT = 20000
# 第 51 抽起每抽 +2 个百分点，第 99 抽六星概率达到 100%。
PITY_DISPLAY_MAX = 99

# 单个六星段/垫刀段展示的五星头像上限（超出折叠为 +N）
_FIVES_DISPLAY_MAX = 12

_LOGS_NAME = "gacha_logs.json"
# 历史补拉游标状态（保险丝中断处，按类别记下一页起点）
_SYNC_STATE_NAME = "gacha_sync_state.json"


def _sync_state_path(uid: str) -> Path:
    # 挂在 _logs_path 同目录：测试替换 _logs_path 时状态文件随之隔离
    return _logs_path(uid).parent / _SYNC_STATE_NAME


async def _load_sync_floors(uid: str) -> dict[str, dict[str, Any]]:
    """读取每类别的续拉游标；空/坏文件按无游标处理（读路径不抛）。"""
    data = await read_player_json(_sync_state_path(uid))
    floors = data.get("floors") if isinstance(data, dict) else None
    if not isinstance(floors, dict):
        return {}
    return {
        str(cate): cursor
        for cate, cursor in floors.items()
        if isinstance(cursor, dict) and _norm_ts(cursor.get("ts")) > 0
    }


async def _save_sync_floors(uid: str, floors: dict[str, dict[str, Any]]) -> None:
    if not floors:
        # 全部翻到底：清掉状态文件，不留空壳
        try:
            _sync_state_path(uid).unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        await write_player_json(_sync_state_path(uid), {"floors": floors})
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·抽卡] 补拉游标写入失败 uid={uid}: {exc.__class__.__name__}"
        )


# 同 UID 同步 single-flight（审计 #11）：两个同步任务并发读同一份旧日志、
# 各自拉取合并写回，后写者会覆盖先写者新增的记录且检查点已推进、永久丢失
_sync_locks: dict[str, asyncio.Lock] = {}
_sync_locks_guard = asyncio.Lock()


async def _uid_sync_lock(uid: str) -> asyncio.Lock:
    async with _sync_locks_guard:
        lock = _sync_locks.get(uid)
        if lock is None:
            lock = asyncio.Lock()
            _sync_locks[uid] = lock
        return lock


def _logs_path(uid: str) -> Path:
    return PLAYER_PATH / assert_uid_segment(uid) / _LOGS_NAME


def _norm_ts(raw: Any) -> int:
    """毫秒/秒归一到秒。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value // 1000 if value > 10**12 else value


def _safe_ts(record: dict[str, Any]) -> int:
    """记录时间戳（秒）：脏数据（null/"N/A"/超范围）按 0，绝不抛异常。

    审计 #39：本地日志的威胁模型含手工编辑/半损坏写盘（见 load_local_logs），
    此前排序键 `-r.get("gacha_ts", 0)` 在键存在但值为 null 时得 -None，
    一条脏记录就把抽卡记录/年度报告整条命令拖垮且无从自救。
    """
    return _norm_ts(record.get("gacha_ts") or 0)


def _safe_pos(record: dict[str, Any]) -> int:
    """记录位次：脏数据按 0（口径同 _safe_ts）。"""
    try:
        return int(record.get("pos") or 0)
    except (TypeError, ValueError):
        return 0


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


def _normalize_rarity(record: dict[str, Any], legacy_one_based: bool) -> int:
    """逐条归一化到 1 基（六星=6）。

    记录带 `basis` 字段（拉取时会话级判定并持久化，审计 #13）：
    - "0"：0 基口径（0-5）→ +1
    - "1"：1 基口径（1-6）→ 原值
    - 无 basis（历史存量）：按整份日志的全局口径（legacy_one_based，
      出现过 6 → 1 基）——跨口径混存的旧数据无法逐条判定，全局口径是
      与旧版一致的最优近似；新拉取的记录从此不再有该歧义。
    """
    rarity = record.get("rarity", 0)
    basis = record.get("basis")
    if basis == "0":
        return rarity + 1
    if basis == "1":
        return rarity
    # 存量记录：整份日志同一口径（0 基 +1 / 1 基原值），
    # 六星判定结果与旧版全局阈值完全一致
    return rarity if legacy_one_based else rarity + 1


def _legacy_one_based(records: list[dict[str, Any]]) -> bool:
    """存量日志（无 basis 字段）的全局量纲推断：出现过 6 → 1 基。"""
    return any(r.get("rarity") == 6 for r in records)


def _session_basis(records: list[dict[str, Any]]) -> str:
    """本次拉取批次的量纲判定（会话级）：出现过 0 → 0 基；出现过 6 → 1 基；
    都无（3-5 集合，两种口径同形）→ 按官方主流量纲 0 基。"""
    values = [r.get("rarity", 0) for r in records]
    if any(v == 6 for v in values):
        return "1"
    return "0"


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
    result = sorted(merged.values(), key=lambda r: (-_safe_ts(r), -_safe_pos(r)))
    return result[:_KEEP_LIMIT]


async def load_local_logs(uid: str) -> list[dict[str, Any]]:
    # uid 非法（历史脏数据）当无本地记录处理，读路径不抛异常
    try:
        path = _logs_path(uid)
    except (ValueError, TypeError):
        return []
    data = await read_player_json(path)
    if not isinstance(data, list):
        return []
    # 审计 #27：手工编辑/损坏的日志可能是 [null, "x"]——非 dict 元素
    # 过滤掉，别让下游 .get() 抛异常拖垮整条命令
    return [r for r in data if isinstance(r, dict)]


async def save_local_logs(uid: str, records: list[dict[str, Any]]) -> bool:
    try:
        await write_player_json(_logs_path(uid), records)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·抽卡] 本地记录写入失败 uid={uid}: {exc.__class__.__name__}: {exc}"
        )
        return False
    return True


async def sync_gacha_logs(
    credential: ArkCredential, *, full: bool = False
) -> tuple[list[dict[str, Any]], str]:
    """增量拉取并累积。返回 (records, 错误文案)；records 可能为本地兜底。

    full=True 从首页重新遍历上游保留的记录，并与本地合并（不删除旧存档）。
    网络全失败但本地有记录时返回 (本地, 警告文案)，调用方仍可出卡。
    同 UID single-flight（审计 #11）：并发调用排队串行执行，后到者
    在锁内重新走全流程（读到前者刚写的日志，增量从新检查点继续）。
    """
    async with await _uid_sync_lock(credential.uid):
        return await _sync_gacha_logs_locked(credential, full=full)


async def _sync_gacha_logs_locked(
    credential: ArkCredential, *, full: bool = False
) -> tuple[list[dict[str, Any]], str]:
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
    floors = await _load_sync_floors(credential.uid)
    cursor_errors: set[str] = set()

    async def _page_category(
        category: str,
        start: dict[str, Any] | None,
        stop_below_ts: int,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        """单段翻页 → (结局, 记录, 续拉游标)。

        结局：
        - "done"    翻到底（hasMore=false / 空页）
        - "overlap" 已翻到比 stop_below_ts 更旧的记录（与本地检查点衔接）
        - "cursor"  游标失效/未推进或 hasMore=true 却空页：保留记录并提示缺口
        - "fused"   保险丝（_MAX_PAGES）耗尽：段内无断层但历史没翻完
        - "failed"  中途请求失败：调用方整批丢弃（审计 #12）
        """
        records: list[dict[str, Any]] = []
        gacha_ts: int | None = int(start["ts"]) if start else None
        if gacha_ts is None or gacha_ts <= 0:
            gacha_ts = None
        pos = start.get("pos") if start else None
        for _ in range(_MAX_PAGES):
            page = await get_gacha_history_page(
                credential, auth, category, gacha_ts=gacha_ts, pos=pos
            )
            if page is None:
                return "failed", records, None
            items = page.get("list") or []
            if not items:
                if page.get("hasMore"):
                    cursor_errors.add(category)
                    return "cursor", records, None
                return "done", records, None
            stop = False
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                record = _normalize_record(raw, category)
                records.append(record)
                # 已翻到比停止线更旧的记录 → 后面全是已同步的旧数据
                if stop_below_ts and record["gacha_ts"] < stop_below_ts:
                    stop = True
            if stop:
                return "overlap", records, None
            if not page.get("hasMore"):
                return "done", records, None
            last = items[-1] if isinstance(items[-1], dict) else {}
            next_ts = last.get("gachaTs")
            if not _norm_ts(next_ts) or (int(next_ts), last.get("pos")) == (
                gacha_ts,
                pos,
            ):
                # 无可用的下一页游标，继续请求会从头或重复拉当前页。
                cursor_errors.add(category)
                return "cursor", records, None
            # 游标属于上游协议，保留毫秒精度；仅本地统计字段归一到秒。
            gacha_ts = int(next_ts)
            pos = last.get("pos")
        return "fused", records, {"ts": gacha_ts, "pos": pos}

    async def fetch_category(
        category: str,
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any] | None]:
        """单类别同步 → (记录, 是否完整入批, 续拉游标)。

        停止阈值取**本类别**的本地最新时间戳——若拿全类别最大值当
        阈值，某类目会被另一类目的更新记录过早截断（单周期新增超过
        一页 100 条时，第二页起的新增记录永久丢失且不自愈）。

        审计 #39：保险丝（50 页）耗尽不再当"完整"。旧版把耗尽当完整
        落盘，检查点推进后断层以下的历史永久拉不到且无警告。现在
        中断处留**续拉游标**（下一页起点），段内记录从最新/上次游标
        连续翻下来、无内部断层，照常并入；下轮从游标继续补拉，
        翻到底自动清除游标。
        """
        local_max_ts = max(
            (_safe_ts(r) for r in local if r.get("category") == category),
            default=0,
        )
        floor = floors.get(category)
        if full or (floor is not None and int(floor["ts"]) < 10**12):
            # 旧版把毫秒游标截成秒，精度不可恢复，重新全量翻页修复历史缺口。
            floor = None
            local_max_ts = 0

        outcome, records, cursor = await _page_category(category, None, local_max_ts)
        if outcome == "failed":
            return [], False, floor  # 增量段失败：本类整批丢弃，游标不动
        complete = True
        if outcome == "fused":
            if floor is not None:
                # 单轮新增超 5000 且没衔接到旧游标（中间可能有未拉
                # 断层）：保守丢弃，下轮重试；新抽记录晚一轮并入
                return [], False, floor
            # 首轮即超量：记录从最新连续下来，无断层，入批并留游标
            return records, True, cursor

        if floor is not None:
            # 补拉段：从游标继续往旧翻（无停止线，只认 hasMore/保险丝）
            outcome_b, records_b, cursor_b = await _page_category(category, floor, 0)
            if outcome_b != "failed":
                records = records + records_b
                floor = cursor_b if outcome_b == "fused" else None
                # 补拉失败（failed）：增量段照常并入，游标不动，下轮再补
        else:
            floor = None
        return records, complete, floor

    category_ids = [str(cate.get("id") or "") for cate in categories if cate.get("id")]
    # 各类别并发拉取（此前串行：3 类 × 最多 50 页 × 25s 超时）。
    # 不完整类目整批丢弃（审计 #12），完整类目照常合并
    incoming: list[dict[str, Any]] = []
    incomplete: list[str] = []
    new_floors: dict[str, dict[str, Any]] = dict(floors)
    for category, (records, complete, floor) in zip(
        category_ids,
        await asyncio.gather(*(fetch_category(category) for category in category_ids)),
    ):
        if not complete:
            # 首页就失败（records 为空）同样算未并入——统计静默变旧
            # 比报一声"中断"更误导用户
            incomplete.append(category)
            continue
        incoming.extend(records)
        if floor is None:
            new_floors.pop(category, None)
        else:
            new_floors[category] = floor

    cursor_warning = (
        f"卡池（{'、'.join(sorted(cursor_errors))}）分页游标异常，历史未完整拉取；"
        "已保留拉到的记录，请稍后使用 ark抽卡记录 全量 重试。"
        if cursor_errors
        else ""
    )
    if not incoming and not local:
        if cursor_warning:
            return [], cursor_warning
        if incomplete:
            # 全部类别首页即失败：是拉取故障，不是"无记录"
            return [], (
                f"卡池（{'、'.join(incomplete)}）拉取失败，本次未并入任何记录"
                "（已有本地统计不受影响），请稍后重试。"
            )
        return [], "未拉到任何抽卡记录（官方接口可能已过期清理，或该账号无记录）。"

    # 会话级量纲判定并持久化到每条记录（审计 #13）：统计时逐条按 basis
    # 归一化，跨口径混存的日志不再靠全局猜测错算六星
    basis = _session_basis(incoming)
    for record in incoming:
        record["basis"] = basis

    merged = merge_records(local, incoming)
    saved = await save_local_logs(credential.uid, merged)
    if saved:
        await _save_sync_floors(credential.uid, new_floors)
    else:
        new_floors = floors
    warning = ""
    if incomplete:
        warning = (
            f"部分卡池（{'、'.join(incomplete)}）拉取中断，本次未并入，"
            "下轮自动补齐；统计可能不是最新。"
        )
    elif new_floors:
        backfilling = "、".join(sorted(new_floors))
        warning = (
            f"卡池（{backfilling}）历史记录较多，正在分批补拉"
            f"（每轮最多 {_MAX_PAGES} 页），期间统计不含最早期记录。"
        )
    save_warning = (
        "本地记录保存失败，本次结果仅供展示；补拉进度未提交，请稍后重试。"
        if not saved
        else ""
    )
    return merged, "\n".join(
        part for part in (warning, cursor_warning, save_warning) if part
    )


def analyze_gacha(records: list[dict[str, Any]]) -> dict[str, Any]:
    """统计：总抽数/六星数/六星率/平均六星/垫刀/最近六星/五星明细。

    五星口径（小黑盒式展示）：两个六星之间（同类别内）出现的五星挂在
    该段末尾的六星条目下（recent[].fives）；当前垫刀段（最新六星之后）
    的五星归入 pity_fives；均按抽取顺序（旧→新）保留，超长截断 +N。
    """
    if not records:
        return {}
    legacy_one_based = _legacy_one_based(records)
    total = len(records)
    sixes = [r for r in records if _normalize_rarity(r, legacy_one_based) >= 6]
    six_count = len(sixes)
    six_rate = f"{six_count * 100 / total:.2f}" if total else "0.00"
    avg_six = f"{total / six_count:.1f}" if six_count else "0.0"
    five_count = sum(1 for r in records if _normalize_rarity(r, legacy_one_based) == 5)
    five_rate = f"{five_count * 100 / total:.2f}" if total else "0.00"

    # 按时间旧→新，按类别计数：六星的垫刀数 = 同类别内上一个六星之后的抽数
    ordered = sorted(records, key=lambda r: (_safe_ts(r), _safe_pos(r)))
    pulls_in_cate: dict[str, int] = {}
    last_six_in_cate: dict[str, int] = {}
    # 本段（上个六星之后）累积的五星，按类别暂存，遇六星结算挂到该六星
    pending_fives: dict[str, list[dict[str, Any]]] = {}
    pull_of_six: list[tuple[dict[str, Any], int, list[dict[str, Any]]]] = []
    for record in ordered:
        cate = str(record.get("category", ""))
        pulls_in_cate[cate] = pulls_in_cate.get(cate, 0) + 1
        rarity = _normalize_rarity(record, legacy_one_based)
        if rarity == 5:
            pending_fives.setdefault(cate, []).append(
                {
                    "name": record.get("name") or record.get("char_id", "?"),
                    "ts": _safe_ts(record),
                }
            )
        elif rarity >= 6:
            pull = pulls_in_cate[cate] - last_six_in_cate.get(cate, 0)
            pull_of_six.append((record, pull, pending_fives.pop(cate, [])))
            last_six_in_cate[cate] = pulls_in_cate[cate]
    recent = [
        {
            "name": r.get("name") or r.get("char_id", "?"),
            "pool": (r.get("pool_name") or "")[:10] or "—",
            "pull": pull,
            "ts": _safe_ts(r),
            "date": (
                beijing_datetime(_safe_ts(r)).strftime("%m-%d")
                if _safe_ts(r)
                else ""
            ),
            "category": str(r.get("category", "")),
            "fives": fives[:_FIVES_DISPLAY_MAX],
            "fives_extra": max(0, len(fives) - _FIVES_DISPLAY_MAX),
        }
        for r, pull, fives in reversed(pull_of_six[-6:])
    ]

    # 垫刀进度：最新有记录的类别里，距最新六星的抽数
    pity = 0
    pity_fives: list[dict[str, Any]] = []
    if ordered:
        latest_cate = str(ordered[-1].get("category", ""))
        pity = pulls_in_cate.get(latest_cate, 0) - last_six_in_cate.get(latest_cate, 0)
        pity_fives = pending_fives.get(latest_cate, [])

    return {
        "total": total,
        "six_count": six_count,
        "six_rate": six_rate,
        "avg_six": avg_six,
        "five_count": five_count,
        "five_rate": five_rate,
        "pity": pity,
        "pity_max": PITY_DISPLAY_MAX,
        "pity_fives": pity_fives[:_FIVES_DISPLAY_MAX],
        "pity_fives_extra": max(0, len(pity_fives) - _FIVES_DISPLAY_MAX),
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
    lines = ["「明日方舟」抽卡统计", f"UID：{uid_masked}"]
    if not stats:
        lines.append("暂无抽卡记录。")
    else:
        lines.append(
            f"总抽数 {stats['total']} | 六星 {stats['six_count']} "
            f"（{stats['six_rate']}%，平均 {stats['avg_six']} 抽/六星）"
            f" | 五星 {stats['five_count']}（{stats['five_rate']}%）"
        )
        lines.append(f"当前垫刀：{stats['pity']} 抽")
        for item in stats["recent"][:4]:
            ts = (
                time.strftime("%y-%m-%d", time.localtime(item["ts"]))
                if item["ts"]
                else ""
            )
            lines.append(
                f"  {item['name']}（{item['pool']}，第 {item['pull']} 抽）{ts}"
            )
            fives = item.get("fives") or []
            if fives:
                total_fives = len(fives) + int(item.get("fives_extra") or 0)
                names = "、".join(str(f.get("name", "?")) for f in fives[:4])
                suffix = f" 等{total_fives}个" if total_fives > 4 else ""
                lines.append(f"    本段五星 ×{total_fives}：{names}{suffix}")
    if warning:
        lines.append(f"⚠️ {warning}")
    return "\n".join(lines)
