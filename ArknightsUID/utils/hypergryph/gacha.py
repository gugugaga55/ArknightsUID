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
import heapq
from collections import defaultdict
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

# 按池展示：每页卡池数与每池六星条目上限（防长图无限增长）
POOL_PAGE_SIZE = 3
_POOL_SIXES_MAX = 5

_LOGS_NAME = "gacha_logs.json"
# 历史补拉游标状态（保险丝中断处，按类别记下一页起点）
_SYNC_STATE_NAME = "gacha_sync_state.json"


def _sync_state_path(uid: str) -> Path:
    # 挂在 _logs_path 同目录：测试替换 _logs_path 时状态文件随之隔离
    return _logs_path(uid).parent / _SYNC_STATE_NAME


async def _write_gacha_json(path: Path, payload: Any) -> None:
    """线程写盘无法取消；锁持有者必须等到写完才传播取消。"""
    write = asyncio.create_task(write_player_json(path, payload))
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(write)
            break
        except asyncio.CancelledError as exc:
            if write.cancelled():
                raise
            cancelled = cancelled or exc
        except Exception as exc:
            if cancelled is not None:
                raise cancelled from exc
            raise
    if cancelled is not None:
        raise cancelled


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
        await _write_gacha_json(_sync_state_path(uid), {"floors": floors})
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


def _safe_ts_ms_raw(record: dict[str, Any]) -> int:
    """原始毫秒时间戳（无该字段或脏数据返回 0）。"""
    try:
        val = int(record.get("gacha_ts_ms") or 0)
    except (TypeError, ValueError):
        return 0
    return val if val > 10**12 else 0


def _safe_ts_ms(record: dict[str, Any]) -> int:
    """用于排序的毫秒时间戳：有 gacha_ts_ms 用精确值，否则秒×1000。"""
    raw_ms = _safe_ts_ms_raw(record)
    return raw_ms if raw_ms else _safe_ts(record) * 1000


def _cross_source_key(record: dict[str, Any]) -> tuple:
    """跨来源/同来源单条抽取的精确身份：pool_name + 秒 + 毫秒 + 位次 + 干员身份。

    不含 category（小黑盒没有官方 category）；官方同秒两次单抽（pos 同为 0
    但毫秒不同）靠 gacha_ts_ms 区分；跨源（官方有 ms / 小黑盒无 ms）与
    旧存量（无 ms）在 merge_records 第二阶段一对一归并。
    """
    char_id = str(record.get("char_id") or "")
    identity = char_id or _norm_char_name(str(record.get("name") or ""))
    return (
        str(record.get("pool_name") or ""),
        _safe_ts(record),
        _safe_ts_ms_raw(record),
        _safe_pos(record),
        identity,
    )


def _norm_char_name(name: str) -> str:
    """跨源干员名规范化：去空白。名称级匹配只做这一步轻归一，
    已知小黑盒别名映射在 heybox 适配器导入时处理（这里不做猜测规则）。"""
    return "".join(name.split())


def _is_official(record: dict[str, Any]) -> bool:
    """旧记录没有 source 字段时按官方历史记录处理。"""
    return str(record.get("source") or "official") == "official"


def _record_prefer_official(
    local: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """重复记录的取舍：优先保留官方副本，用另一来源补齐空字段。

    官方记录可能缺 pool_id/is_new 细节，小黑盒记录可能补得上；反向亦然。
    补齐只填空字段，不覆盖已有值。注意：basis 与本条记录自身的 rarity
    强绑定，绝不能直接把 0 基记录的 basis="0" 贴到 1 基存量记录（rarity=5
    五星会被错算成 5+1=6 六星）。
    """
    official, other = (
        (local, incoming) if _is_official(local) else (incoming, local)
    )
    if _is_official(local) == _is_official(incoming):
        # 同为官方（旧/新拉取）或同为小黑盒：取信息更全的一条（字段非空多者）
        local_full = sum(1 for v in local.values() if v not in ("", None, 0, False))
        incoming_full = sum(
            1 for v in incoming.values() if v not in ("", None, 0, False)
        )
        base, extra = (local, incoming) if local_full >= incoming_full else (incoming, local)
    else:
        base, extra = official, other
    merged = dict(base)
    had_rarity = merged.get("rarity") not in ("", None)
    for key, value in extra.items():
        if key == "basis":
            continue
        if merged.get(key) in ("", None) and value not in ("", None):
            merged[key] = value
    if merged.get("basis") in ("", None) and extra.get("basis") in ("0", "1"):
        if not had_rarity or merged.get("rarity") == extra.get("rarity"):
            merged["basis"] = extra["basis"]
        else:
            try:
                mr = int(merged.get("rarity") or 0)
                er = int(extra.get("rarity") or 0)
                if extra["basis"] == "0" and mr == er + 1:
                    merged["basis"] = "1"
                elif extra["basis"] == "1" and mr + 1 == er:
                    merged["basis"] = "0"
            except (TypeError, ValueError):
                pass
    return merged


def _normalize_record(raw: dict[str, Any], category: str) -> dict[str, Any]:
    raw_ts = raw.get("gachaTs")
    try:
        raw_ts_int = int(raw_ts)
    except (TypeError, ValueError):
        raw_ts_int = 0
    rec: dict[str, Any] = {
        "category": str(category),
        "char_id": str(raw.get("charId") or raw.get("charIdList") or ""),
        "name": str(raw.get("charName") or raw.get("name") or ""),
        "rarity": int(raw.get("rarity") or 0),
        "is_new": bool(raw.get("isNew")),
        "gacha_ts": _norm_ts(raw_ts),
        "pos": int(raw.get("pos") or 0),
        "pool_name": str(raw.get("poolName") or raw.get("pool") or ""),
        "pool_id": str(raw.get("poolId") or raw.get("pool_id") or ""),
        "source": "official",
    }
    if raw_ts_int > 10**12:
        rec["gacha_ts_ms"] = raw_ts_int
    return rec


def _normalize_rarity(record: dict[str, Any], legacy_one_based: bool) -> int:
    """逐条归一化到 1 基（六星=6）。

    记录带 `basis` 字段（拉取时会话级判定并持久化，审计 #13）：
    - "0"：0 基口径（0-5）→ +1
    - "1"：1 基口径（1-6）→ 原值
    - 无 basis（历史存量）：按无 basis 记录的全局口径（legacy_one_based，
      出现过 6 → 1 基）。
    """
    rarity = record.get("rarity", 0)
    basis = record.get("basis")
    if basis == "0":
        return rarity + 1
    if basis == "1":
        return rarity
    return rarity if legacy_one_based else rarity + 1


def _legacy_one_based(records: list[dict[str, Any]]) -> bool:
    """存量日志（仅统计无显式 basis 字段的记录）的全局量纲推断：出现过 6 → 1 基。"""
    return any(
        r.get("rarity") == 6
        for r in records
        if r.get("basis") not in ("0", "1")
    )


def _session_basis(
    records: list[dict[str, Any]],
    local: list[dict[str, Any]] | None = None,
) -> str:
    """本次拉取批次的量纲判定（会话级）：
    - 出现 0/1/2（三星在 0 基为 2，1 基寻访最低为 3）→ 必为 0 基
    - 出现 6 → 必为 1 基
    - 仅含 3-5（小批量单抽未出三星也未出 6）：若本地官方记录已确认为 1 基则继承 "1"，
      否则按官方主流量纲 "0"。
    """
    values = [r.get("rarity", 0) for r in records]
    if any(isinstance(v, int) and v <= 2 for v in values):
        return "0"
    if any(v == 6 for v in values):
        return "1"
    if local:
        official_local = [r for r in local if _is_official(r)]
        if official_local and not any(r.get("basis") == "0" for r in official_local):
            if any(r.get("basis") == "1" for r in official_local) or _legacy_one_based(
                official_local
            ):
                return "1"
    return "0"


def merge_records(
    local: list[dict[str, Any]], incoming: list[dict[str, Any]], *, truncate: bool = True
) -> list[dict[str, Any]]:
    """按跨源去重键合并，按时间新→旧排序，截断保留上限。

    先按池名、时间、位次和干员身份精确合并；同池同秒同名但位次或
    charId 缺失的跨源记录再一对一匹配。重复时优先保留官方副本，
    用另一来源补齐空字段；不同秒不做未经证实的近似匹配。
    """
    # 先按精确键合并，并保留每条记录已经匹配过的来源，防止十连中
    # 同名干员的第二条小黑盒记录再次匹配同一条官方记录。
    merged: dict[tuple, tuple[dict[str, Any], set[str]]] = {}
    for record in [*local, *incoming]:
        key = _cross_source_key(record)
        source = "official" if _is_official(record) else "heybox"
        if key in merged:
            previous, sources = merged[key]
            merged[key] = (_record_prefer_official(previous, record), sources | {source})
        else:
            merged[key] = (record, {source})

    entries = list(merged.values())
    removed: set[int] = set()

    # 兼容存量官方记录（无 gacha_ts_ms）与全量重拉记录（带 gacha_ts_ms）：
    # 同池同秒同位次同干员一对一升级合并，避免全量同步重复入库。
    official_no_ms: dict[tuple[str, int, int, str], list[int]] = defaultdict(list)
    official_with_ms: dict[tuple[str, int, int, str], list[int]] = defaultdict(list)
    for index, (record, sources) in enumerate(entries):
        if "official" not in sources:
            continue
        char_id = str(record.get("char_id") or "")
        identity = char_id or _norm_char_name(str(record.get("name") or ""))
        sig = (
            str(record.get("pool_name") or ""),
            _safe_ts(record),
            _safe_pos(record),
            identity,
        )
        if _safe_ts_ms_raw(record) == 0:
            official_no_ms[sig].append(index)
        else:
            official_with_ms[sig].append(index)
    for sig, legacy_indices in official_no_ms.items():
        ms_indices = official_with_ms.get(sig) or []
        for leg_idx, ms_idx in zip(legacy_indices, ms_indices):
            leg_rec, leg_src = entries[leg_idx]
            ms_rec, ms_src = entries[ms_idx]
            entries[ms_idx] = (
                _record_prefer_official(ms_rec, leg_rec),
                leg_src | ms_src,
            )
            removed.add(leg_idx)

    groups: dict[tuple[str, int, str], dict[str, list[int]]] = defaultdict(
        lambda: {"official": [], "heybox": []}
    )
    for index, (record, sources) in enumerate(entries):
        if index in removed:
            continue
        pool = str(record.get("pool_name") or "")
        name = _norm_char_name(str(record.get("name") or ""))
        ts = _safe_ts(record)
        if pool and name and ts and len(sources) == 1:
            groups[(pool, ts, name)][next(iter(sources))].append(index)

    for group in groups.values():
        available = set(group["official"])
        all_indices = list(group["official"])
        by_pos: dict[int, list[int]] = defaultdict(list)
        by_id: dict[str, list[int]] = defaultdict(list)
        by_id_pos: dict[tuple[str, int], list[int]] = defaultdict(list)
        heapq.heapify(all_indices)
        for index in group["official"]:
            record = entries[index][0]
            char_id = str(record.get("char_id") or "")
            pos = _safe_pos(record)
            heapq.heappush(by_pos[pos], index)
            heapq.heappush(by_id[char_id], index)
            heapq.heappush(by_id_pos[(char_id, pos)], index)

        def first_available(*heaps: list[int], remaining: set[int] = available) -> int | None:
            heads = []
            for heap in heaps:
                while heap and heap[0] not in remaining:
                    heapq.heappop(heap)
                if heap:
                    heads.append(heap[0])
            return min(heads) if heads else None

        for imported_index in sorted(
            group["heybox"], key=lambda index: not bool(entries[index][0].get("char_id"))
        ):
            imported = entries[imported_index][0]
            imported_id = str(imported.get("char_id") or "")
            # 同一秒内尽量先匹配相同位次，剩余条目一对一匹配；不同秒
            # 不做猜测，以免把两次真实抽取按宽泛时间窗合并。
            pos = _safe_pos(imported)
            if imported_id:
                chosen = first_available(by_id_pos.get((imported_id, pos), []))
                if chosen is None:
                    chosen = first_available(by_id.get(imported_id, []))
                if chosen is None:
                    chosen = first_available(by_id_pos.get(("", pos), []))
                if chosen is None:
                    chosen = first_available(by_id.get("", []))
            else:
                chosen = first_available(by_pos.get(pos, []))
                if chosen is None:
                    chosen = first_available(all_indices)
            if chosen is None:
                continue
            official = entries[chosen][0]
            entries[chosen] = (_record_prefer_official(official, imported), {"official", "heybox"})
            available.remove(chosen)
            removed.add(imported_index)
    result = sorted(
        (record for index, (record, _) in enumerate(entries) if index not in removed),
        key=lambda r: (-_safe_ts(r), -_safe_ts_ms(r), -_safe_pos(r)),
    )
    return result[:_KEEP_LIMIT] if truncate else result


def has_uncertain_cross_source_duplicates(records: list[dict[str, Any]]) -> bool:
    """不同秒但时间相近的跨源同名记录无法安全判重，提示用户复核。"""
    official: dict[tuple[str, int, str], set[str]] = defaultdict(set)
    for record in records:
        if not _is_official(record):
            continue
        pool = str(record.get("pool_name") or "")
        name = _norm_char_name(str(record.get("name") or ""))
        ts = _safe_ts(record)
        if pool and name and ts:
            official[(pool, ts, name)].add(str(record.get("char_id") or ""))
    for record in records:
        if _is_official(record):
            continue
        pool = str(record.get("pool_name") or "")
        name = _norm_char_name(str(record.get("name") or ""))
        ts = _safe_ts(record)
        char_id = str(record.get("char_id") or "")
        if not pool or not name or not ts:
            continue
        for delta in (-2, -1, 1, 2):
            ids = official.get((pool, ts + delta, name), set())
            if ids and (not char_id or "" in ids or char_id in ids):
                return True
    return False


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
    # write_player_json 使用线程写盘：取消等待不会停止线程。调用方持有
    # UID 锁时必须等线程结束，否则旧写入可能覆盖后续事务。
    try:
        await _write_gacha_json(_logs_path(uid), records)
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
    basis = _session_basis(incoming, local)
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


def _pity_key_resolver(records: list[dict[str, Any]]):
    """构造保底/垫刀分组键映射：
    - 常驻标准池（normal / heybox:rule:NORMAL）跨期跨源共享垫刀
    - 中坚寻访池（classic / heybox:rule:CLASSIC）跨期跨源共享垫刀
    - 限定/联动/庆典/单池（heybox:rule:LIMITED 等）各池独立保底，绝不跨限定池串联；
      若同一池同时有官方与小黑盒记录，则归并到同一池保底桶。
    """
    official_cate_by_pool: dict[str, set[str]] = defaultdict(set)
    pools_in_official_cate: dict[str, set[str]] = defaultdict(set)
    for r in records:
        if not _is_official(r):
            continue
        cate = str(r.get("category") or "")
        if not cate or cate.lower() in ("normal", "standard", "classic"):
            continue
        pid = str(r.get("pool_id") or r.get("pool_name") or "")
        if pid:
            official_cate_by_pool[pid].add(cate)
            pools_in_official_cate[cate].add(pid)
        pname = str(r.get("pool_name") or "")
        if pname and pname != pid:
            official_cate_by_pool[pname].add(cate)

    def _resolve(record: dict[str, Any]) -> str:
        cate = str(record.get("category") or "")
        low = cate.lower()
        if low in ("normal", "standard") or cate == "heybox:rule:NORMAL":
            return "shared:NORMAL"
        if low == "classic" or cate == "heybox:rule:CLASSIC":
            return "shared:CLASSIC"
        if cate.startswith("heybox:"):
            # 非跨池继承规则（LIMITED/LINKAGE/ATTAIN/SINGLE/BOOT/未知池）：按单池独立计垫刀
            pool_key = str(record.get("pool_id") or record.get("pool_name") or "")
            if pool_key:
                cates = official_cate_by_pool.get(pool_key)
                if cates and len(cates) == 1:
                    only_cate = next(iter(cates))
                    if len(pools_in_official_cate.get(only_cate, ())) <= 1:
                        return only_cate
                return f"heybox:pool:{pool_key}"
        return cate

    return _resolve


def analyze_gacha(records: list[dict[str, Any]]) -> dict[str, Any]:
    """统计：总抽数/六星数/六星率/平均六星/垫刀/最近六星/五星明细。

    五星口径（小黑盒式展示）：两个六星之间（同保底组内）出现的五星挂在
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

    # 按时间旧→新，按保底分组键计数：六星的垫刀数 = 同保底桶内上一个六星之后的抽数
    ordered = sorted(records, key=lambda r: (_safe_ts(r), _safe_ts_ms(r), _safe_pos(r)))
    pity_key_of = _pity_key_resolver(ordered)
    pulls_in_cate: dict[str, int] = {}
    last_six_in_cate: dict[str, int] = {}
    # 本段（上个六星之后）累积的五星，按类别暂存，遇六星结算挂到该六星
    pending_fives: dict[str, list[dict[str, Any]]] = {}
    pull_of_six: list[tuple[dict[str, Any], int, list[dict[str, Any]]]] = []
    for record in ordered:
        cate = pity_key_of(record)
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

    # 垫刀进度：最新有记录的保底组里，距最新六星的抽数
    pity = 0
    pity_fives: list[dict[str, Any]] = []
    if ordered:
        latest_cate = pity_key_of(ordered[-1])
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


# ---------------------------------------------------------------------------
# 按具体卡池分组（小黑盒式）：每池独立统计 + 六星条目 + 分段五星
# ---------------------------------------------------------------------------


def _pool_groups(
    records: list[dict[str, Any]], pools: list[Any] | None
) -> list[dict[str, Any]]:
    """记录 → 卡池分组（同步核心；pools 为 gamedata.gacha.GachaPoolInfo 列表）。

    分组键优先 pool_id；缺失时按「池名 + 时间」经卡池表补全（同名复刻池
    得到不同 pool_id，不会被无依据合并）；表不可用/未收录退回池名分组。
    返回组列表（未排序）：{pool_id, pool_name, rule_type, up_chars, up_char_ids, in_table, records}。
    """
    pools = pools or []
    pools_by_id = {p.pool_id: p for p in pools}
    pools_by_name: dict[str, list[Any]] = {}
    for p in pools:
        pools_by_name.setdefault(p.pool_name, []).append(p)
    match_memo: dict[tuple[str, int], Any] = {}

    distinct_ids_by_name: dict[str, set[str]] = defaultdict(set)
    has_idless_by_name: dict[str, bool] = defaultdict(bool)
    for r in records:
        pname = str(r.get("pool_name") or "")
        pid = str(r.get("pool_id") or "")
        if pname:
            if pid:
                distinct_ids_by_name[pname].add(pid)
            else:
                has_idless_by_name[pname] = True

    def _match(pool_name: str, ts: int):
        key = (pool_name, ts)
        if key in match_memo:
            return match_memo[key]
        cands = pools_by_name.get(pool_name) or []
        hit = None
        if cands:
            if ts:
                in_window = [
                    p
                    for p in cands
                    if p.open_time and p.end_time and p.open_time <= ts <= p.end_time
                ]
                if len(in_window) == 1:
                    hit = in_window[0]
                elif in_window:
                    hit = max(in_window, key=lambda p: p.open_time)
                elif len(cands) == 1:
                    # 唯一同名池时允许最近邻近似（保留越窗旧单池身份），
                    # 多期轮换同名池（如常驻标准寻访/复刻池）越窗绝不粘连到旧期
                    hit = cands[0]
            else:
                hit = cands[-1] if len(cands) == 1 else None
        match_memo[key] = hit
        return hit

    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records:
        pool_name = str(record.get("pool_name") or "")
        pool_id = str(record.get("pool_id") or "")
        ts = _safe_ts(record)
        own_meta = pools_by_id.get(pool_id) if pool_id else None
        meta = own_meta
        if meta is None and pool_name:
            fallback = _match(pool_name, ts)
            if fallback is None:
                # 仅当该池名下只有唯一未知 pool_id 且同时存在无 id 存量记录时，
                # 才折叠合并；若同一池名有多个不同 pool_id（如多期常驻标准寻访），
                # 必须保留各自的 pool_id 独立分组。
                if (
                    pool_id
                    and len(distinct_ids_by_name.get(pool_name, ())) == 1
                    and has_idless_by_name.get(pool_name, False)
                ):
                    pool_id = ""
            elif not pool_id:
                # 无 id 存量记录：按「池名+时间」补全分组身份
                meta = fallback
            # 自带 id 但表里查不到（新池未入表）且存在同名旧池：保留原
            # id 分组（复刻同名池不粘连），也不采纳 fallback 的池身份
        if meta is not None:
            pool_id = pool_id or meta.pool_id
            if not pool_name:
                pool_name = meta.pool_name
        # UP 名单只认可信来源：自带 id 命中池表（身份精确），或名字+时间
        # 匹配落在开池时间窗内。越窗最近邻只是分组近似，采纳它的 UP
        # 名单会把六星误标成非UP（复刻同名池张冠李戴）
        up_trusted = own_meta is not None or (
            meta is not None
            and ts
            and meta.open_time > 0
            and meta.end_time > 0
            and meta.open_time <= ts <= meta.end_time
        )
        key = pool_id or f"name:{pool_name}"
        group = groups.get(key)
        meta_up_ids = getattr(meta, "up_char_ids", set()) if meta else set()
        if group is None:
            group = {
                "pool_id": pool_id,
                "pool_name": pool_name,
                "rule_type": meta.rule_type if meta else "",
                "up_chars": set(meta.up_chars) if up_trusted else set(),
                "up_char_ids": set(meta_up_ids) if up_trusted else set(),
                "in_table": bool(up_trusted),
                "records": [],
            }
            groups[key] = group
            order.append(key)
        elif up_trusted and meta is not None:
            group["up_chars"] |= set(meta.up_chars)
            group["up_char_ids"] |= set(meta_up_ids)
            group["in_table"] = True
        group["records"].append(record)
    return [groups[key] for key in order]


def _pool_stats(group: dict[str, Any], legacy_one_based: bool) -> dict[str, Any]:
    """单池统计：总抽数/六星/平均/垫刀/首末日期 + 六星条目（含分段五星）。"""
    recs = group["records"]
    total = len(recs)
    ordered = sorted(recs, key=lambda r: (_safe_ts(r), _safe_ts_ms(r), _safe_pos(r)))
    six_count = 0
    pull_of_six: list[tuple[dict[str, Any], int, list[dict[str, Any]]]] = []
    pending_fives: list[dict[str, Any]] = []
    pulls = 0
    for record in ordered:
        pulls += 1
        rarity = _normalize_rarity(record, legacy_one_based)
        if rarity == 5:
            pending_fives.append(
                {
                    "name": record.get("name") or record.get("char_id", "?"),
                    "char_id": str(record.get("char_id") or ""),
                    "ts": _safe_ts(record),
                }
            )
        elif rarity >= 6:
            six_count += 1
            pull_of_six.append((record, pulls, pending_fives))
            pending_fives = []
            pulls = 0
    up_chars = group.get("up_chars") or set()
    up_char_ids = group.get("up_char_ids") or set()
    has_up_list = bool(up_chars or up_char_ids)

    def _date(ts: int, fmt: str) -> str:
        if not ts:
            return ""
        try:
            return beijing_datetime(ts).strftime(fmt)
        except (OverflowError, OSError, ValueError):
            return ""

    sixes = []
    for record, pull, fives in reversed(pull_of_six[-_POOL_SIXES_MAX:]):
        name = str(record.get("name") or record.get("char_id") or "?")
        cid = str(record.get("char_id") or "")
        sixes.append(
            {
                "name": name,
                "char_id": cid,
                "pull": pull,
                "ts": _safe_ts(record),
                "date": _date(_safe_ts(record), "%m-%d"),
                "is_new": bool(record.get("is_new")),
                # UP 名单明确才标 UP/非 UP（不按池名或稀有度猜歪没歪）
                "is_up": (
                    (cid in up_char_ids or name in up_chars)
                    if has_up_list
                    else None
                ),
                "fives": fives[:_FIVES_DISPLAY_MAX],
                "fives_extra": max(0, len(fives) - _FIVES_DISPLAY_MAX),
            }
        )
    return {
        "pool_id": group["pool_id"],
        "pool_name": group["pool_name"],
        "rule_type": group["rule_type"],
        "in_table": bool(group.get("in_table")),
        "total": total,
        "six_count": six_count,
        "avg_six": f"{total / six_count:.1f}" if six_count else "0.0",
        "pity": pulls,
        "pity_max": PITY_DISPLAY_MAX,
        "first_date": _date(_safe_ts(ordered[0]), "%y-%m-%d"),
        "last_date": _date(_safe_ts(ordered[-1]), "%y-%m-%d"),
        "sixes": sixes,
        "sixes_total": len(pull_of_six),
        "pity_fives": pending_fives[:_FIVES_DISPLAY_MAX],
        "pity_fives_extra": max(0, len(pending_fives) - _FIVES_DISPLAY_MAX),
        "last_ts": _safe_ts(ordered[-1]),
    }


async def analyze_gacha_pools(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按具体卡池分组的统计（卡池表可用时补 pool_id/UP 名单）。"""
    if not records:
        return []
    pools = []
    try:
        from ..gamedata.gacha import load_gacha_pools

        pools = await load_gacha_pools()
    except Exception:
        pools = []
    legacy_one_based = _legacy_one_based(records)
    stats = [
        _pool_stats(group, legacy_one_based)
        for group in _pool_groups(records, pools)
    ]
    # 卡池按最近一次抽取时间倒序
    stats.sort(key=lambda p: -p["last_ts"])
    return stats


async def build_gacha_pools_context(
    records: list[dict[str, Any]], page: int = 1, per_page: int = POOL_PAGE_SIZE
) -> dict[str, Any]:
    """抽卡卡按池区块的上下文：当前页池列表 + 分页信息。"""
    pools = await analyze_gacha_pools(records)
    pages = max(1, -(-len(pools) // per_page)) if pools else 1
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    return {
        "pools": pools[start : start + per_page],
        "all_pools": pools,
        "page": page,
        "pages": pages,
        "pool_total": len(pools),
        "pool_data_hint": (
            "部分卡池缺少 UP 数据，可尝试 ark刷新。"
            if any(
                six["is_up"] is None and not pool.get("in_table", False)
                for pool in pools
                for six in pool["sixes"]
            )
            else ""
        ),
    }


def format_gacha_text(
    name: str,
    uid_masked: str,
    records: list[dict[str, Any]],
    warning: str,
    *,
    pool_stats: list[dict[str, Any]] | None = None,
    page: int = 1,
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
        if pool_stats is None:
            pool_stats = [
                _pool_stats(group, _legacy_one_based(records))
                for group in _pool_groups(records, [])
            ]
            pool_stats.sort(key=lambda p: -p["last_ts"])
        per_text_page = 4
        pages = max(1, -(-len(pool_stats) // per_text_page)) if pool_stats else 1
        cur_page = max(1, min(page, pages))
        start = (cur_page - 1) * per_text_page
        for pool in pool_stats[start : start + per_text_page]:
            six_bits = []
            for s in pool["sixes"][:3]:
                bit = f"{s['name']}（第{s['pull']}抽"
                if s["date"]:
                    bit += f"，{s['date']}"
                fives_total = len(s.get("fives") or []) + int(
                    s.get("fives_extra") or 0
                )
                if fives_total:
                    names = "、".join(
                        str(f.get("name", "?")) for f in (s.get("fives") or [])[:4]
                    )
                    suffix = "等" if fives_total > 4 else ""
                    bit += f"，本段五星 ×{fives_total}：{names}{suffix}"
                six_bits.append(bit + "）")
            lines.append(
                f"【{pool['pool_name'] or '未知卡池'}】{pool['total']} 抽 | "
                f"六星 {pool['six_count']}（平均 {pool['avg_six']}）| "
                f"垫刀 {pool['pity']}"
            )
            if six_bits:
                lines.append(f"  六星：{'、'.join(six_bits)}")
        if len(pool_stats) > per_text_page:
            lines.append(
                f"（第 {cur_page}/{pages} 页，共 {len(pool_stats)} 个卡池，可用 页:2 翻页）"
            )
    if warning:
        lines.append(f"⚠️ {warning}")
    return "\n".join(lines)
