"""理智回满推送 + 专精训练完成推送（订阅 + 定时轮询 + 到点提醒）。

- `ark订阅理智 [序号|UID]`：订阅当前默认账号（或指定账号）的理智回满提醒
- `ark取消订阅理智 [序号|UID]`：取消（不带参数取消全部）
- 定时轮询（10 分钟）：逐订阅账号拉 player_info（带 5 分钟磁盘缓存），
  推算当前理智，回满即推送一次；用后（理智降回上限以下）自动重新武装
- `ark订阅训练 [序号|UID] [提前量]` / `ark取消订阅训练`：专精训练剩余时间
  预警 + 完成提醒。提前量如「5小时」「90分」「2小时30分」控制预警提前多久
  触发（默认 1 小时）；订阅回执即时播报当前在训干员与剩余时间

推送状态（notified 标记）落 MAIN_PATH/sanity_push_state.json 与
training_push_state.json，键为 f"{bot_id}|{user_id}|{uid}"。订阅本身存
gs_subscribe（single 模式，uid = 游戏 UID，同用户可多账号分别订阅）。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from gsuid_core.aps import scheduler
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.sv import SV

from ArknightsUID.utils.async_locks import KeyedLocks
from ArknightsUID.utils.time_utils import beijing_datetime

from ArknightsUID.utils.config import ArkConfig
from ArknightsUID.utils.enduid.adapter import (
    hide_uid,
    resolve_credential,
)
from ArknightsUID.utils.player import (
    TRADING_STRATEGY_NAMES,
    drop_cached_player_info,
    effective_ap,
    fetch_cached_player_info,
    format_training_remaining,
    parse_building,
    parse_player_info,
    training_remaining_secs,
)
from ArknightsUID.utils.skland.client import ark_attendance
from ArknightsUID.utils.path import MAIN_PATH

notify_sv = SV("ArknightsUID理智推送", priority=5)

TASK_NAME = "订阅理智推送"
_STATE_PATH: Path = MAIN_PATH / "sanity_push_state.json"
# 轮询间隔：理智 6 分钟回 1 点，10 分钟粒度平均多溢出 ~0.8 点，可接受
POLL_MINUTES = 10

TRAIN_TASK_NAME = "订阅训练推送"
_TRAIN_STATE_PATH: Path = MAIN_PATH / "training_push_state.json"
# 专精剩余时间预警默认窗口：进入最后一小时提醒一次「还差多久完成」；
# 订阅命令可带提前量覆盖（如 ark订阅训练 5小时），存训练状态 warn_window
TRAIN_WARN_WINDOW_SECS = 3600
# 提前量合法范围：1 分钟 ~ 7 天（专精最长 24h，留调度余量）
TRAIN_LEAD_MIN_SECS = 60
TRAIN_LEAD_MAX_SECS = 7 * 86400
# 提前量时间词：「5小时」「5.5小时」「300分」「2小时30分」「1天2小时」；
# 所有匹配片段求和并从原文移除，剩余文本继续走账号定位（序号|UID）解析
_LEAD_SPEC_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(天|小时|時|时|h|H|分钟|分鐘|分|m|M)")
_LEAD_UNIT_SECS = {
    "天": 86400,
    "小时": 3600,
    "時": 3600,
    "时": 3600,
    "h": 3600,
    "H": 3600,
    "分钟": 60,
    "分鐘": 60,
    "分": 60,
    "m": 60,
    "M": 60,
}
# 专精完成推送 + 剩余时间预警共用轮询：10 分钟粒度让「剩约X分钟」有读数
# 意义（player_info 磁盘缓存 5 分钟，与其他任务错峰命中）
TRAIN_POLL_MINUTES = 10


def parse_training_lead(text: str) -> tuple[int | None, str]:
    """订阅命令文本 → (提前量秒数, 抽掉时间词后的文本)。

    无时间词或越界（<1 分钟 / >7 天）返回 (None, 原文)，调用方回落默认
    窗口；时间词从文本移除后继续按「序号|UID」规则定位账号。
    """
    query = text or ""
    matched = _LEAD_SPEC_RE.findall(query)
    if not matched:
        return None, query
    total = sum(float(value) * _LEAD_UNIT_SECS[unit] for value, unit in matched)
    total = int(round(total))
    if not TRAIN_LEAD_MIN_SECS <= total <= TRAIN_LEAD_MAX_SECS:
        return None, query
    return total, _LEAD_SPEC_RE.sub(" ", query).strip()


def format_lead_secs(secs: int) -> str:
    """提前量秒 → 简洁文案（5小时 / 1小时30分钟 / 45分钟）。"""
    minutes = max(0, int(secs)) // 60
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if mins:
        parts.append(f"{mins}分钟")
    return "".join(parts) or "0分钟"


def _state_key(bot_id: str, user_id: str, uid: str) -> str:
    return f"{bot_id}|{user_id}|{uid}"


async def _alive_state_keys(task_name: str) -> set[str]:
    """当前订阅关系对应的存活状态键集合（轮询末尾落盘前重读）。

    轮询在网络 await 期间，用户可能执行退订（pop 状态键）：落盘时
    不在磁盘态也不在此集合里的键不写回，避免孤儿键/覆盖新订阅的
    复位态（三条推送链共用口径）。
    """
    alive: set[str] = set()
    for sub in await gs_subscribe.get_subscribe(task_name) or []:
        uid = str(sub.uid or "")
        if uid:
            alive.add(_state_key(sub.bot_id, sub.user_id, uid))
    return alive


def evaluate_sanity(ap: int, ap_max: int, notified: bool) -> tuple[bool, bool]:
    """理智推送状态机的纯函数核心。

    返回 (is_full, should_push)：
    - is_full：当前理智是否已达上限
    - should_push：是否需要推送（回满且本轮未通知过）
    调用方负责落盘 notified = is_full（未回满时自动复位，用理智后重新武装）。
    """
    if ap_max <= 0:
        return False, False
    is_full = ap >= ap_max
    return is_full, is_full and not notified


# 理智预警阈值：达到上限的 85% 先提醒一次（回满前还有 ~2-4 小时）
SANITY_WARN_RATIO = 0.85


def evaluate_sanity_warn(ap: int, ap_max: int, warned: bool) -> tuple[bool, bool]:
    """预警状态机（与回满推送互补）：返回 (should_warn, new_warned)。

    - 进入 [85%, 100%) 区间且本轮未预警过 → should_warn
    - warned 落盘规则：仅在区间内为 True——低于 85%（用理智后）或已回满
      （由回满推送接管）自动复位，下次回充周期重新武装。
    """
    if ap_max <= 0:
        return False, False
    ratio = ap / ap_max
    in_band = SANITY_WARN_RATIO <= ratio < 1
    return in_band and not warned, in_band


def format_sanity_warn_message(name: str, ap: int, ap_max: int, uid_masked: str) -> str:
    remaining = max(0, ap_max - ap)
    return (
        "「明日方舟」理智即将回满（85% 预警）\n"
        f"{name or '博士'}（UID {uid_masked}）{ap}/{ap_max}\n"
        f"距回满还差 {remaining} 点，安排一下上号时间～"
    )


# ==================== 登录失效主动提醒（凭据健康） ====================

# 凭据不可用（token/cred 失效）时主动提醒一次，7 天冷却防轰炸
CRED_WARN_STATE_PATH: Path = MAIN_PATH / "cred_warn_state.json"
CRED_WARN_COOLDOWN_SECS = 7 * 86400


class _CheckedSubscribe:
    """订阅发送包装：把 gsuid_core send 的 -1 返回转成异常。

    send 在 bot 离线（WS_BOT_ID 失效）时返回 -1 而不抛异常；各推送链
    若直接当成功记账，"已推送"标志照常落盘，该周期永久漏发（审计 #39）。
    统一经此包装走既有 except 失败路径（失败不落 notified / 冷却时间戳）。
    """

    def __init__(self, subscribe):
        self._subscribe = subscribe

    async def send(self, message):
        sent = await self._subscribe.send(message)
        if sent == -1:
            raise RuntimeError("bot 会话不可用（send 返回 -1）")


def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


# 各轮询链"负责"的状态字段：轮末变更判定与基底比对只看这些字段，
# 行内其他字段（warn_window 等用户配置、campaign_week 等其他功能）
# 的变化不属于轮询，不触发也不阻断落盘。
_TRAIN_POLL_FIELDS = ("notified", "trainee", "warned")
_SANITY_POLL_FIELDS = ("notified", "warned")


def _poll_view(row: dict, fields: tuple[str, ...]) -> dict:
    """行中轮询负责字段的视图（缺失键取 None），用于与轮前快照比较。"""
    return {f: row.get(f) for f in fields}


# 订阅代次（第三轮复核）：订阅/退订命令每次执行都更新目标键的代次；
# 轮询与在途命令落盘前比对代次——代次变化说明有订阅命令在快照之后
# 执行过（含值完全相同的重订阅、只含其他功能字段的行、尚无状态行的
# 首次订阅），旧快照一律弃写。存独立文件：状态行本身可能被清掉或
# 从未存在，行内版本字段覆盖不到这些交错。
_SUB_EPOCH_PATH: Path = MAIN_PATH / "push_sub_epoch.json"


def _epoch_key(task_name: str, key: str) -> str:
    return f"{task_name}|{key}"


def _read_epoch(task_name: str, key: str) -> int:
    return int(_load_state(_SUB_EPOCH_PATH).get(_epoch_key(task_name, key)) or 0)


def _bump_epoch(task_name: str, key: str) -> bool:
    """更新订阅代次。写失败（磁盘故障等）不抛出——调用方按返回值决定是否
    继续状态写入；返回 False 表示本轮竞态保护降级（在途旧快照可能多存活
    一轮），已落 warning 便于定位。"""
    try:

        def merge() -> dict:
            fresh = _load_state(_SUB_EPOCH_PATH)
            fresh[_epoch_key(task_name, key)] = time.time_ns()
            return fresh

        return _write_state_atomic(_SUB_EPOCH_PATH, {}, merge_from=merge)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·推送] 订阅代次更新失败 {key}: {exc.__class__.__name__}"
        )
        return False


def _intent_key(task_name: str, key: str) -> str:
    return f"intent|{task_name}|{key}"


def _cancel_scope_key(task_name: str, key: str) -> str:
    return f"cancel-all|{task_name}|{key.rsplit('|', 1)[0]}"


def _intent_current(task_name: str, key: str, ticket: int) -> bool:
    """操作身份独立于通知状态行；退订清行不能使旧操作重新有效。"""
    records = _load_state(_SUB_EPOCH_PATH)
    row = records.get(_intent_key(task_name, key)) or {}
    return (
        row.get("ticket") == ticket
        and row.get("active") is True
        and ticket > int(records.get(_cancel_scope_key(task_name, key)) or 0)
    )


def _record_intent(
    task_name: str, key: str, ticket: int, *, active: bool, pending: bool = False
) -> bool:
    """意图与代次一起原子写入；后发操作和取消记录都能拒绝旧操作。"""
    accepted = False

    def merge() -> dict:
        nonlocal accepted
        fresh = _load_state(_SUB_EPOCH_PATH)
        previous = fresh.get(_intent_key(task_name, key)) or {}
        floor = max(
            int(previous.get("ticket") or 0),
            int(fresh.get(_cancel_scope_key(task_name, key)) or 0),
        )
        if ticket < floor:
            return fresh
        fresh[_intent_key(task_name, key)] = {
            "ticket": ticket,
            "active": active,
            "pending": pending,
            "resume_active": (
                previous.get("resume_active")
                if previous.get("pending")
                else previous.get("active")
            ),
        }
        fresh[_epoch_key(task_name, key)] = time.time_ns()
        accepted = True
        return fresh

    try:
        written = _write_state_atomic(_SUB_EPOCH_PATH, {}, merge_from=merge)
        return bool(written) and accepted
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·推送] 订阅意图保存失败: {exc.__class__.__name__}"
        )
        return False


@contextmanager
def _intent_guard(task_name: str, key: str, ticket: int):
    """异常/取消只结束自身仍未提交的意图，不回滚更新操作或取消墓碑。"""
    try:
        yield
    finally:

        def merge() -> dict:
            fresh = _load_state(_SUB_EPOCH_PATH)
            row = fresh.get(_intent_key(task_name, key)) or {}
            if row.get("ticket") == ticket and row.get("pending"):
                # 保持最新票水位，避免旧命令借恢复动作重新有效。
                fresh[_intent_key(task_name, key)] = {
                    **row,
                    "pending": False,
                    # None 是升级前未知状态：是否有订阅由数据库查询结果决定。
                    "active": row.get("resume_active"),
                }
                fresh[_epoch_key(task_name, key)] = time.time_ns()
            return fresh

        row = _load_state(_SUB_EPOCH_PATH).get(_intent_key(task_name, key)) or {}
        if row.get("ticket") == ticket and row.get("pending"):
            try:
                _write_state_atomic(_SUB_EPOCH_PATH, {}, merge_from=merge)
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·推送] 未完成订阅恢复失败: {exc.__class__.__name__}"
                )


_subscription_locks = KeyedLocks()


def _subscription_lock(task_name: str, ev: Event) -> asyncio.Lock:
    """同任务同用户的数据库添加/删除互斥；网络查询在锁外执行。"""
    return _subscription_locks.get(f"{task_name}|{ev.bot_id}|{ev.user_id}")


def _record_cancel_all(task_name: str, ev: Event, ticket: int) -> bool:
    """用户级取消墓碑覆盖无状态行、尚未解析完账号的在途命令。"""
    scope = _cancel_scope_key(task_name, _state_key(ev.bot_id, ev.user_id, ""))

    def merge() -> dict:
        fresh = _load_state(_SUB_EPOCH_PATH)
        fresh[scope] = max(ticket, int(fresh.get(scope) or 0))
        return fresh

    try:
        return _write_state_atomic(_SUB_EPOCH_PATH, {}, merge_from=merge)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·推送] 取消意图保存失败: {exc.__class__.__name__}"
        )
        return False


def _poll_current(task_name: str, key: str, epoch: int, snapshot: dict) -> bool:
    records = _load_state(_SUB_EPOCH_PATH)
    intent = records.get(_intent_key(task_name, key)) or {}
    scope = _cancel_scope_key(task_name, key)
    cancelled = int(records.get(scope) or 0)
    return (
        int(records.get(_epoch_key(task_name, key)) or 0) == epoch
        and records.get(scope) == snapshot.get(scope)
        and (
            not intent
            or (
                intent.get("active") is not False
                and not intent.get("pending")
                and int(intent.get("ticket") or 0) > cancelled
            )
        )
    )


def _task_state_path(task_name: str) -> Path:
    """有轮询竞态的三个任务（理智/训练/基建）→ 状态文件路径。"""
    if task_name == TASK_NAME:
        return _STATE_PATH
    if task_name == TRAIN_TASK_NAME:
        return _TRAIN_STATE_PATH
    return _BUILDING_STATE_PATH


def _clear_subscription_state(task_name: str, ev: Event, uid: str) -> None:
    if task_name == TASK_NAME:
        _reset_sanity_state(ev.bot_id, ev.user_id, uid)
        return
    path = _task_state_path(task_name)
    state = _load_state(path)
    state.pop(_state_key(ev.bot_id, ev.user_id, uid), None)
    _save_state(path, state, "退订")


async def _cancel_subscription(
    task_name: str, ev: Event, ticket: int, uid: str = ""
) -> bool:
    key = _state_key(ev.bot_id, ev.user_id, uid)
    saved = (
        _record_intent(task_name, key, ticket, active=False)
        if uid
        else _record_cancel_all(task_name, ev, ticket)
    )
    if not saved:
        return False
    async with _subscription_lock(task_name, ev):
        if uid:
            targets = [uid]
        else:
            # 删除前枚举；墓碑已记录，枚举不到的在途操作也会失效。
            targets = [
                str(s.uid)
                for s in await gs_subscribe.get_subscribe(task_name) or []
                if s.uid and s.bot_id == ev.bot_id and s.user_id == ev.user_id
            ]
            path = _task_state_path(task_name)
            prefix = f"{ev.bot_id}|{ev.user_id}|"
            targets += [
                k[len(prefix) :] for k in _load_state(path) if k.startswith(prefix)
            ]
        for target_uid in dict.fromkeys(targets):
            target_key = _state_key(ev.bot_id, ev.user_id, target_uid)
            row = (
                _load_state(_SUB_EPOCH_PATH).get(_intent_key(task_name, target_key))
                or {}
            )
            if int(row.get("ticket") or 0) > ticket:
                continue
            await gs_subscribe.delete_subscribe(
                "single", task_name=task_name, event=ev, uid=target_uid
            )
            _clear_subscription_state(task_name, ev, target_uid)
            _bump_epoch(task_name, target_key)
    return True


def _save_state(
    path: Path, state: dict[str, dict], label: str, merge_from=None
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_state_atomic(path, state, merge_from=merge_from)
    except Exception as exc:
        logger.warning(f"[ArknightsUID·{label}] 状态写入失败: {exc.__class__.__name__}")


def _load_cred_warn_state() -> dict[str, dict]:
    return _load_state(CRED_WARN_STATE_PATH)


def _save_cred_warn_state(state: dict[str, dict]) -> None:
    _save_state(CRED_WARN_STATE_PATH, state, "凭据预警")


# 凭据预警的按键锁：同一 (bot, user, uid) 的「冷却重读 → 发送 → 成功记账」
# 必须串行——理智/训练/基建/签到多条轮询链可能同时发现同一凭据失效，若
# 各自通过冷却检查后再各自发送，同一用户会收到多条重复提醒。锁内重读冷却、
# 发送、成功后才写时间戳；发送失败/-1/任务取消都不记账（保留重试语义）。
# 锁只在等待/执行期间持有强引用，不随历史账号数量积累。
_cred_warn_locks = KeyedLocks()


async def maybe_warn_credential(subscribe, uid: str, error: str) -> None:
    """凭据不可用时向订阅会话提醒一次（7 天冷却，供各轮询任务共用）。

    subscribe 是 gs_subscribe 的订阅对象（.send/.bot_id/.user_id 可用）；
    发送失败不落时间戳，下轮还能重试。同键并发在锁内串行，
    冷却检查在锁内重读；不同键互不阻塞。
    """
    key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
    async with _cred_warn_locks.get(key):
        last = float((_load_cred_warn_state().get(key) or {}).get("ts") or 0)
        if time.time() - last < CRED_WARN_COOLDOWN_SECS:
            return
        message = (
            "「明日方舟」登录状态不可用\n"
            f"UID {hide_uid(uid)} 的定时查询连续失败：{str(error)[:60]}\n"
            "请重新 ark绑定 / ark扫码登录（私聊）；7 天内不重复提醒。"
        )
        try:
            await _CheckedSubscribe(subscribe).send(message)
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·凭据预警] 提醒失败: {exc.__class__.__name__}"
            )
            return
        logger.info(
            f"[ArknightsUID·凭据预警] 已提醒 user={subscribe.user_id} "
            f"uid={hide_uid(uid)}"
        )
        state = _load_cred_warn_state()
        state[key] = {"ts": time.time()}
        _save_cred_warn_state(state)


def _write_state_atomic(path: Path, state: dict, merge_from=None) -> bool:
    """跨进程文件锁 + 唯一 tmp + os.replace 原子落盘（审计 #10）。

    merge_from 传入读函数时执行"锁内重读 → 键级合并 → 写"：轮询任务
    在网络 await 期间，其他进程/命令写入的键不会被本进程的旧快照覆盖
    （退订复活/丢键）；写一半崩溃也不至于把状态文件写坏（坏文件会被
    当空态，导致全部订阅重复推送一次）。
    """
    from ArknightsUID.utils.file_lock import (
        file_locked,
        unique_tmp,
    )

    with file_locked(path):
        if merge_from is not None:
            try:
                fresh = merge_from()
            except Exception as exc:
                # 合并基底读取失败时放弃本轮写入：硬写会把磁盘上未涉及
                # 的键（含窗口内其他写者的更新）整批丢掉，下轮重试即可
                logger.warning(
                    f"[ArknightsUID·推送] 状态合并失败，本轮不落盘: "
                    f"{exc.__class__.__name__}"
                )
                return False
            if isinstance(fresh, dict):
                fresh.update(state)
                state = fresh
        tmp = unique_tmp(path)
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    return True


def load_push_state() -> dict[str, dict]:
    return _load_state(_STATE_PATH)


def save_push_state(state: dict[str, dict], merge_from=None) -> None:
    _save_state(_STATE_PATH, state, "理智推送", merge_from=merge_from)


def _extract_target(text: str) -> tuple[str, int]:
    """订阅命令参数 → (uid, 序号)。与 account 模块同规则：长数字=UID，短数字=序号。"""
    query = text.strip()
    uid_match = re.search(r"\d{5,}", query)
    if uid_match:
        return uid_match.group(0), 0
    index_match = re.fullmatch(r"第?(\d{1,2})号?", query)
    if index_match:
        return "", int(index_match.group(1))
    return "", 0


# 文本回复公共实现在 utils.card.send_text；保留模块级别名供调用点/测试 patch
from ArknightsUID.utils.card import send_text as _send_text  # noqa: E402

# 凭据解析 + 失败即回复的统一入口（原各处四行样板）
from ArknightsUID.utils.card import resolve_or_reply  # noqa: E402


def _reset_sanity_state(bot_id: str, user_id: str, uid: str = "") -> None:
    """仅重置理智字段，保留同文件中剿灭周提醒等其他功能的状态。

    同时无条件更新订阅代次（第三轮复核）：初始行可能只有 campaign_week
    或根本不存在，复位未必改变行内容——值比较无法识别这类复位，在途
    轮询靠代次识别并弃写旧快照。
    """

    def merge() -> dict:
        state = load_push_state()
        keys = (
            [_state_key(bot_id, user_id, uid)]
            if uid
            else [key for key in state if key.startswith(f"{bot_id}|{user_id}|")]
        )
        for key in keys:
            row = state.get(key)
            if row is None:
                continue
            row.pop("notified", None)
            row.pop("warned", None)
            if not row:
                state.pop(key)
        return state

    save_push_state({}, merge_from=merge)
    if uid:
        _bump_epoch(TASK_NAME, _state_key(bot_id, user_id, uid))
    else:
        for key in load_push_state():
            if key.startswith(f"{bot_id}|{user_id}|"):
                _bump_epoch(TASK_NAME, key)


async def _run_subscribe_command(
    bot: Bot,
    ev: Event,
    task_name: str,
    text: str,
    commit: Callable[..., Awaitable[bool]],
    prepare: Callable[..., Awaitable[Any]] | None = None,
) -> tuple[Any, Any] | None:
    """理智/训练/基建订阅命令的公共骨架（操作票流程，第四轮复核）。

    取票 → 解析账号 → 意图落盘(pending) → 守卫内锁前 prepare（任务专属
    查询）→ 锁内 commit（任务专属的加库与状态提交）→ 返回
    (credential, prepare 结果)；任一失败路径已向用户回复原因，调用方直接
    返回。剿灭/签到等无状态竞态的订阅不经此骨架。
    """
    ticket = time.time_ns()
    uid, index = _extract_target(text)
    credential = await resolve_or_reply(bot, ev, uid, index)
    if credential is None:
        return None
    key = _state_key(ev.bot_id, ev.user_id, credential.uid)
    if not _record_intent(task_name, key, ticket, active=True, pending=True):
        await _send_text(
            bot, ev, "订阅保存失败或已有更新操作，本次未生效；请稍后重试。"
        )
        return None
    with _intent_guard(task_name, key, ticket):
        prepared = await prepare(credential) if prepare is not None else None
        completed = False
        async with _subscription_lock(task_name, ev):
            completed = await commit(key, ticket, credential, prepared)
        if not completed:
            await _send_text(
                bot, ev, "订阅保存未完成或已有更新操作，本次未生效；请重新订阅。"
            )
            return None
    return credential, prepared


@notify_sv.on_command(
    (
        "订阅理智提醒",
        "订阅理智",
    ),
    block=True,
)
async def subscribe_sanity_push(bot: Bot, ev: Event) -> None:
    async def commit(key: str, ticket: int, credential, _prepared) -> bool:
        if not _intent_current(TASK_NAME, key, ticket):
            return False
        await gs_subscribe.add_subscribe(
            "single", task_name=TASK_NAME, event=ev, uid=credential.uid
        )
        if not _intent_current(TASK_NAME, key, ticket):
            return False
        _reset_sanity_state(ev.bot_id, ev.user_id, credential.uid)
        return _record_intent(TASK_NAME, key, ticket, active=True)

    result = await _run_subscribe_command(
        bot, ev, TASK_NAME, ev.text or "", commit=commit
    )
    if result is None:
        return
    credential, _ = result
    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot,
        ev,
        f"已订阅理智回满推送：{shown}\n"
        f"理智回满时会在这里提醒你一次，用理智后自动恢复提醒。\n"
        f"取消：ark取消订阅理智",
    )


@notify_sv.on_command(
    (
        "取消订阅理智",
        "取消理智推送",
        "退订理智推送",
    ),
    block=True,
)
async def unsubscribe_sanity_push(bot: Bot, ev: Event) -> None:
    ticket = time.time_ns()
    uid, index = _extract_target(ev.text)
    if uid or index:
        credential = await resolve_or_reply(bot, ev, uid, index)
        if credential is None:
            return
        uid = credential.uid
    if not await _cancel_subscription(TASK_NAME, ev, ticket, uid):
        await _send_text(
            bot, ev, "退订保存失败或已有更新操作，本次未生效；请稍后重试。"
        )
        return
    message = "已取消该账号的理智回满推送。" if uid else "已取消全部理智回满推送。"
    await _send_text(bot, ev, message)


def format_push_message(name: str, ap_max: int, uid_masked: str) -> str:
    return (
        "「明日方舟」理智已回满\n"
        f"{name or '博士'}（UID {uid_masked}）{ap_max}/{ap_max}\n"
        "理智已满，再不消耗就要溢出了，快上号！"
    )


@scheduler.scheduled_job("interval", minutes=POLL_MINUTES, id="ark_sanity_push")
async def check_sanity_push() -> None:
    """定时检查各订阅账号理智，回满推送一次。"""
    _t0 = time.monotonic()
    # 查询前记录代次，查询结果返回期间的变更也必须使旧关系失效。
    epoch_snapshot = _load_state(_SUB_EPOCH_PATH)
    datas = await gs_subscribe.get_subscribe(TASK_NAME)
    if not datas:
        return
    logger.info(f"[ArknightsUID·理智推送] 检查 {len(datas)} 个订阅..")

    # 只记本轮变更，落盘前重读合并——避免覆盖轮询期间 (退)订阅写盘的状态
    changes: dict[str, tuple[dict, dict, int]] = {}
    for subscribe in datas:
        uid = str(subscribe.uid or "")
        if not uid:
            continue
        key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
        prev = load_push_state().get(key, {})
        epoch_base = int(epoch_snapshot.get(_epoch_key(TASK_NAME, key)) or 0)
        notified = bool(prev.get("notified"))
        warned = bool(prev.get("warned"))
        prev_notified, prev_warned = notified, warned

        credential = await resolve_credential(
            subscribe.user_id, subscribe.bot_id, uid=uid
        )
        if isinstance(credential, str):
            logger.warning(
                f"[ArknightsUID·理智推送] 账号不可用 user={subscribe.user_id}: {credential}"
            )
            await maybe_warn_credential(subscribe, uid, credential)
            continue

        response = await fetch_cached_player_info(credential)
        if isinstance(response, str):
            logger.warning(
                f"[ArknightsUID·理智推送] 拉取失败 uid={hide_uid(uid)}: {response}"
            )
            continue
        data = parse_player_info(response)
        if data is None or data.ap_max <= 0:
            continue

        # 发送前验证（第四轮复核）：处理期间订阅被重订阅换会话或退订
        # → 本轮跳过该订阅，不向旧会话发送、不落旧标记
        if not _poll_current(TASK_NAME, key, epoch_base, epoch_snapshot):
            continue

        ap = effective_ap(data)
        is_full, should_push = evaluate_sanity(ap, data.ap_max, notified)
        should_warn, new_warned = evaluate_sanity_warn(ap, data.ap_max, warned)
        # 发送失败的标志不落盘（下轮重试），与训练/基建推送口径一致；
        # 否则回满当轮 bot 掉线 → notified=True 落盘 → 该周期永久漏发。
        # 另一标志与本轮发送成败无关，照常推进（否则预警轮不复位
        # notified，下轮回满会被误判"已通知过"而漏发）。
        if should_push:
            message = format_push_message(
                data.name or credential.nickname, data.ap_max, hide_uid(uid)
            )
            warned = new_warned
            try:
                await _CheckedSubscribe(subscribe).send(message)
                notified = is_full
                logger.info(
                    f"[ArknightsUID·理智推送] 已推送 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} {ap}/{data.ap_max}"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·理智推送] 推送失败: {exc.__class__.__name__}"
                )
        elif should_warn:
            message = format_sanity_warn_message(
                data.name or credential.nickname, ap, data.ap_max, hide_uid(uid)
            )
            notified = is_full
            try:
                await _CheckedSubscribe(subscribe).send(message)
                warned = new_warned
                logger.info(
                    f"[ArknightsUID·理智预警] 已推送 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} {ap}/{data.ap_max}"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·理智预警] 推送失败: {exc.__class__.__name__}"
                )
        else:
            notified = is_full
            warned = new_warned
        # 与**进入本轮时**的快照比较（notified/warned 已被更新为最新值，
        # 直接比会永相等导致落盘被跳过）；changes 只记本轮变化的两字段，
        # 基底 prev 与订阅代次单独存（轮末做字段级合并 + 代次比对）
        if notified != prev_notified or warned != prev_warned:
            changes[key] = (
                {"notified": notified, "warned": warned},
                prev,
                epoch_base,
            )
        await asyncio.sleep(random.uniform(1, 2))

    if changes:
        # 退订不复活：落盘前重读订阅列表，窗口内被退订命令 pop 掉的键
        # 不再写回（否则留下孤儿键/覆盖新订阅的复位态）。
        # 过滤与字段级合并都在 merge 回调里——它在 _write_state_atomic 的
        # 跨进程锁内对**最新磁盘态**执行，窗口外的并发写入不丢（审计 #10）
        alive = await _alive_state_keys(TASK_NAME)

        def _merge_sanity() -> dict:
            fresh = load_push_state()
            for key, (patch, prev_base, epoch_base) in changes.items():
                if key not in alive:
                    continue
                # 订阅代次变化：轮询处理期间有订阅/退订命令完成（复位、
                # 同设置重订阅等）→ 整键弃写，下一轮基于新状态重新评估
                if not _poll_current(TASK_NAME, key, epoch_base, epoch_snapshot):
                    continue
                row = fresh.get(key)
                if row is None:
                    # 读时存在、写时已消失：重订阅复位/退订清掉了键——
                    # 不得用旧快照重建（否则恢复复位前的"已通知"标记）；
                    # 仅新订阅（读时也无行）首次落点
                    if not prev_base:
                        fresh[key] = {**prev_base, **patch}
                    continue
                # 基底比对按理智自有字段：campaign_week 等其他功能字段的
                # 变化不代表理智状态被改写，照常字段级合并（并保留它们）
                if _poll_view(row, _SANITY_POLL_FIELDS) != _poll_view(
                    prev_base, _SANITY_POLL_FIELDS
                ):
                    continue
                fresh[key] = {**row, **patch}
            return fresh

        save_push_state({}, merge_from=_merge_sanity)
    _elapsed = time.monotonic() - _t0
    if _elapsed > 480:
        # 超过轮询间隔(10min)的 80%：下一轮会被调度器 max_instances=1
        # skip（漏检一轮）——账号过多或凭据异常时在此留痕
        logger.warning(
            f"[ArknightsUID·理智推送] 单轮耗时 {_elapsed:.0f}s，接近轮询间隔，"
            "下一轮可能被跳过（账号过多/凭据异常）"
        )


# ==================== 专精训练完成推送 ====================


def evaluate_training(
    trainee_id: str, remaining: int, prev_notified: bool, prev_trainee: str
) -> tuple[bool, dict]:
    """训练推送状态机的纯函数核心。返回 (should_push, new_state)。

    - 无在训干员：复位状态（新一轮训练可再次触发）
    - 剩余未知（remainSecs<0，接口未给/空闲哨兵穿透）：状态保持，不推送
    - 换了在训干员：视为新一轮，notified 复位
    - 剩余归零且未通知过：推送一次；完成后停在 notified，直到换人/空闲
    """
    if not trainee_id:
        return False, {"notified": False, "trainee": ""}
    if remaining < 0:
        return False, {"notified": prev_notified, "trainee": prev_trainee}
    notified = prev_notified and prev_trainee == trainee_id
    done = remaining <= 0
    return done and not notified, {"notified": done, "trainee": trainee_id}


def evaluate_training_warn(
    trainee_id: str,
    remaining: int,
    warned: bool,
    prev_trainee: str,
    window_secs: int = TRAIN_WARN_WINDOW_SECS,
) -> tuple[bool, bool]:
    """专精剩余时间预警状态机：进入提前量窗口提醒一次「还差多久完成」。

    window_secs 为该订阅的自定义提前量（默认最后 1 小时）。
    返回 (should_warn, new_warned)：
    - 未在训/剩余未知/剩余超过窗口：不提醒，warned 复位（新一轮重新武装）
    - 在窗口内且本轮未预警过：提醒一次
    - warned 换训干员自动失效（与完成推送同款的新一轮判定）
    """
    if window_secs <= 0:
        window_secs = TRAIN_WARN_WINDOW_SECS
    if not trainee_id or remaining < 0 or remaining > window_secs:
        return False, False
    armed = warned and prev_trainee == trainee_id
    return (not armed), True


def format_training_warn_message(
    name: str, trainee_name: str, remaining: int, uid_masked: str
) -> str:
    return (
        "「明日方舟」专精训练剩余时间提醒\n"
        f"{name or '博士'}（UID {uid_masked}）训练室的 "
        f"{trainee_name or '干员'} 还剩{format_training_remaining(remaining)}完成\n"
        "可以准备上号领取，安排下一位了～"
    )


def _load_train_state() -> dict[str, dict]:
    return _load_state(_TRAIN_STATE_PATH)


def _save_train_state(state: dict[str, dict]) -> None:
    _save_state(_TRAIN_STATE_PATH, state, "训练推送")


@notify_sv.on_command(
    (
        "订阅专精训练",
        "订阅训练",
        "训练推送",
    ),
    block=True,
)
async def subscribe_training_push(bot: Bot, ev: Event) -> None:
    # 操作票（第四轮复核）：查询前先取票——两条订阅并发时，后发起者票大，
    # 完成顺序无论谁先，最终以后发起者的设置为准（用户最新意图）
    lead_secs, target_text = parse_training_lead(ev.text)

    async def prepare(credential) -> dict:
        window = lead_secs or TRAIN_WARN_WINDOW_SECS
        lead_note = ""
        if lead_secs is None and _LEAD_SPEC_RE.search(ev.text or ""):
            # 用户确实写了时间词但越界（<1 分钟 / >7 天）：静默回落默认
            # 会误导（以为 8 天生效了），回执里明确告知
            lead_note = (
                f"\n⚠️ 提前量需在 1 分钟~7 天之间，已按默认 "
                f"{format_lead_secs(TRAIN_WARN_WINDOW_SECS)} 处理。"
            )

        # 订阅即播报当前训练室状态（还剩多久/何时到点）；回执已说过的信息
        # 不再让预警重复一遍——剩余已进窗口时直接把 warned 置位
        status_line = "训练室状态未知（稍后轮询会自动补上）。"
        state_value: dict = {
            "notified": False,
            "trainee": "",
            "warn_window": window,
        }
        try:
            response = await fetch_cached_player_info(credential)
            if not isinstance(response, str):
                data = parse_player_info(response)
                building = parse_building(data) if data is not None else None
                if building is not None:
                    remaining = training_remaining_secs(building)
                    if building.training_trainee_id and remaining > 0:
                        finish_at = beijing_datetime(time.time() + remaining).strftime(
                            "%m-%d %H:%M"
                        )
                        status_line = (
                            f"当前在训：{building.training_trainee}，"
                            f"剩{format_training_remaining(remaining)}完成"
                            f"（预计 {finish_at} 到点）"
                        )
                        state_value["trainee"] = building.training_trainee_id
                        state_value["warned"] = remaining <= window
                    elif building.training_trainee_id and remaining == 0:
                        status_line = f"当前在训：{building.training_trainee}，专精已完成，可上号领取"
                        state_value["trainee"] = building.training_trainee_id
                    else:
                        status_line = "训练室当前空闲，开训后自动开始提醒。"
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·训练推送] 订阅时查询训练室失败: {exc.__class__.__name__}"
            )
        return {
            "state": state_value,
            "status_line": status_line,
            "window": window,
            "lead_note": lead_note,
        }

    async def commit(key: str, ticket: int, credential, prepared) -> bool:
        # 意图身份在 await 之后再校验；无需借用另一条命令建立的关系。
        # op 水位兜底：新命令的状态写盘成功但意图落盘失败时，旧票不能
        # 覆盖新配置（此时意图文件仍认旧票为当前）。
        current_row = _load_train_state().get(key) or {}
        if not (
            _intent_current(TRAIN_TASK_NAME, key, ticket)
            and int(current_row.get("op") or 0) <= ticket
        ):
            return False
        await gs_subscribe.add_subscribe(
            "single", task_name=TRAIN_TASK_NAME, event=ev, uid=credential.uid
        )
        if not _intent_current(TRAIN_TASK_NAME, key, ticket):
            return False
        state = _load_train_state()
        state[key] = {**prepared["state"], "op": ticket}
        _save_train_state(state)
        return _record_intent(TRAIN_TASK_NAME, key, ticket, active=True)

    result = await _run_subscribe_command(
        bot, ev, TRAIN_TASK_NAME, target_text, prepare=prepare, commit=commit
    )
    if result is None:
        return
    credential, prepared = result
    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot,
        ev,
        f"已订阅专精训练推送：{shown}\n"
        f"{prepared['status_line']}\n"
        f"剩余不足{format_lead_secs(prepared['window'])}时提醒一次，完成时再提醒一次"
        "（换干员自动恢复）。"
        f"{prepared['lead_note']}\n"
        f"取消：ark取消订阅训练",
    )


@notify_sv.on_command(
    (
        "取消订阅训练",
        "取消训练推送",
        "退订训练推送",
    ),
    block=True,
)
async def unsubscribe_training_push(bot: Bot, ev: Event) -> None:
    ticket = time.time_ns()
    uid, index = _extract_target(ev.text)
    if uid or index:
        credential = await resolve_or_reply(bot, ev, uid, index)
        if credential is None:
            return
        uid = credential.uid
    if not await _cancel_subscription(TRAIN_TASK_NAME, ev, ticket, uid):
        await _send_text(
            bot, ev, "退订保存失败或已有更新操作，本次未生效；请稍后重试。"
        )
        return
    message = "已取消该账号的专精训练推送。" if uid else "已取消全部专精训练推送。"
    await _send_text(bot, ev, message)


def format_training_push_message(name: str, trainee_name: str, uid_masked: str) -> str:
    return (
        "「明日方舟」专精训练完成\n"
        f"{name or '博士'}（UID {uid_masked}）训练室的 "
        f"{trainee_name or '干员'} 专精训练已完成\n"
        "上号领取并安排下一位吧～"
    )


@scheduler.scheduled_job("interval", minutes=TRAIN_POLL_MINUTES, id="ark_training_push")
async def check_training_push() -> None:
    _t0 = time.monotonic()
    """定时检查各订阅账号训练室，完成推送一次（换训干员后重新武装）。"""
    # 查询前记录代次，查询结果返回期间的变更也必须使旧关系失效。
    epoch_snapshot = _load_state(_SUB_EPOCH_PATH)
    datas = await gs_subscribe.get_subscribe(TRAIN_TASK_NAME)
    if not datas:
        return
    logger.info(f"[ArknightsUID·训练推送] 检查 {len(datas)} 个订阅..")

    # 只记本轮变更，落盘前重读合并（与理智推送同款竞态防护）
    changes: dict[str, tuple[dict, int, dict]] = {}
    for subscribe in datas:
        uid = str(subscribe.uid or "")
        if not uid:
            continue
        key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
        prev = _load_train_state().get(key, {})
        epoch_base = int(epoch_snapshot.get(_epoch_key(TRAIN_TASK_NAME, key)) or 0)
        prev_notified = bool(prev.get("notified"))
        prev_trainee = str(prev.get("trainee") or "")
        prev_warned = bool(prev.get("warned"))
        warn_window = int(prev.get("warn_window") or 0) or TRAIN_WARN_WINDOW_SECS

        credential = await resolve_credential(
            subscribe.user_id, subscribe.bot_id, uid=uid
        )
        if isinstance(credential, str):
            logger.warning(
                f"[ArknightsUID·训练推送] 账号不可用 user={subscribe.user_id}: {credential}"
            )
            await maybe_warn_credential(subscribe, uid, credential)
            continue

        response = await fetch_cached_player_info(credential)
        if isinstance(response, str):
            logger.warning(
                f"[ArknightsUID·训练推送] 拉取失败 uid={hide_uid(uid)}: {response}"
            )
            continue
        data = parse_player_info(response)
        building = parse_building(data) if data is not None else None
        if building is None:
            continue

        # 发送前验证（第四轮复核）：处理期间订阅被重订阅换会话或退订
        # → 本轮跳过该订阅，不向旧会话发送、不落旧标记
        if not _poll_current(TRAIN_TASK_NAME, key, epoch_base, epoch_snapshot):
            continue

        remaining = training_remaining_secs(building)
        should_push, new_state = evaluate_training(
            building.training_trainee_id,
            remaining,
            prev_notified,
            prev_trainee,
        )
        should_warn, new_warned = evaluate_training_warn(
            building.training_trainee_id,
            remaining,
            prev_warned,
            prev_trainee,
            window_secs=warn_window,
        )
        new_state["warned"] = new_warned
        # 字段所有权：warn_window 是用户配置（只由订阅命令写），轮询不回写；
        # 本轮预警判定用进入本轮时的快照值（见上 evaluate_training_warn）。
        if should_push:
            message = format_training_push_message(
                (data.name if data else "") or credential.nickname,
                building.training_trainee,
                hide_uid(uid),
            )
            try:
                await _CheckedSubscribe(subscribe).send(message)
                logger.info(
                    f"[ArknightsUID·训练推送] 已推送 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} trainee={building.training_trainee}"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·训练推送] 推送失败: {exc.__class__.__name__}"
                )
                # 发送失败不落 notified，下轮重试
                new_state = {
                    "notified": False,
                    "trainee": prev_trainee,
                    "warned": False,
                }
        elif should_warn:
            message = format_training_warn_message(
                (data.name if data else "") or credential.nickname,
                building.training_trainee,
                remaining,
                hide_uid(uid),
            )
            try:
                await _CheckedSubscribe(subscribe).send(message)
                logger.info(
                    f"[ArknightsUID·训练预警] 已推送 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} remaining={remaining}s"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·训练预警] 推送失败: {exc.__class__.__name__}"
                )
                new_state["warned"] = False  # 发送失败下轮重试
        # 只比较轮询负责的字段（P3）：prev 含 warn_window 等非轮询字段，
        # 整行比较在 new_state 去掉 warn_window 后永不相等 → 每轮误判
        # 变化、重写状态文件。changes 携带 (比较基底, 订阅代次, 新状态)，
        # 供轮末检测命令侧的中途写入（改提前量/重订阅复位）
        if new_state != _poll_view(prev, _TRAIN_POLL_FIELDS):
            changes[key] = (prev, epoch_base, new_state)
        await asyncio.sleep(random.uniform(1, 2))

    if changes:
        # 跨进程锁内重读合并 + 退订不复活（审计 #10，对齐理智推送）：
        # merge 回调在文件锁内对最新磁盘态过滤——轮询 await 期间被
        # 退订命令 pop 掉的键不写回（否则留下孤儿键/覆盖新订阅的复位态）。
        # 字段所有权 + 比较基底：warn_window 是用户配置（只由订阅命令写），
        # 通知标记的复位同样属于命令意图。磁盘行相对本轮基底 prev 已被
        # 改写（改提前量 / 退订后重订阅 / 其他写者更新同键）时，本轮评估
        # 基于旧快照——整键弃写，下一轮基于新状态重新评估，绝不回写。
        alive = await _alive_state_keys(TRAIN_TASK_NAME)

        def _merge_train() -> dict:
            fresh = _load_train_state()
            for key, (base, epoch_base, new_state) in changes.items():
                # 订阅代次变化：轮询处理期间有订阅/退订命令完成（含值相同
                # 的重订阅）→ 本轮评估基于旧快照，整键弃写
                if not _poll_current(TRAIN_TASK_NAME, key, epoch_base, epoch_snapshot):
                    continue
                row = fresh.get(key)
                if row is None:
                    # 读时不存在、写时仍不存在：新订阅的首轮落点
                    # （alive 过滤防退订复活；base 非空说明中途被删，不重建）
                    if not base and key in alive:
                        fresh[key] = new_state
                    continue
                if row != base:
                    continue
                fresh[key] = {**row, **new_state}
            return fresh

        _write_state_atomic(_TRAIN_STATE_PATH, {}, merge_from=_merge_train)
    _elapsed = time.monotonic() - _t0
    if _elapsed > 480:
        logger.warning(
            f"[ArknightsUID·训练推送] 单轮耗时 {_elapsed:.0f}s，接近轮询间隔，"
            "下一轮可能被跳过（账号过多/凭据异常）"
        )


# ==================== 剿灭周提醒（周五晚未完成 → 提醒一次） ====================

CAMPAIGN_TASK_NAME = "订阅剿灭推送"


def _iso_week_key(now_ts: float | None = None) -> str:
    """ISO 周标识（如 2026-W34）：一周只提醒一次的状态键。"""
    moment = beijing_datetime(now_ts)
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


@notify_sv.on_command(("订阅剿灭",), block=True)
async def subscribe_campaign_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    credential = await resolve_or_reply(bot, ev, uid, index)
    if credential is None:
        return

    await gs_subscribe.add_subscribe(
        "single", task_name=CAMPAIGN_TASK_NAME, event=ev, uid=credential.uid
    )
    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot,
        ev,
        f"已订阅剿灭周提醒：{shown}\n"
        f"每周五 20:00 检查本周合成玉，没拿满会提醒你。\n"
        f"取消：ark取消订阅剿灭",
    )


@notify_sv.on_command(("取消订阅剿灭", "退订剿灭推送"), block=True)
async def unsubscribe_campaign_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    if uid or index:
        credential = await resolve_or_reply(bot, ev, uid, index)
        if credential is None:
            return
        await gs_subscribe.delete_subscribe(
            "single", task_name=CAMPAIGN_TASK_NAME, event=ev, uid=credential.uid
        )
        return await _send_text(bot, ev, "已取消该账号的剿灭周提醒。")

    await gs_subscribe.delete_subscribe(
        "single", task_name=CAMPAIGN_TASK_NAME, event=ev
    )
    await _send_text(bot, ev, "已取消全部剿灭周提醒。")


def evaluate_campaign(campaign_current: int, campaign_total: int) -> tuple[bool, int]:
    """剿灭提醒判定：未拿满 → (需提醒, 剩余合成玉)。"""
    if campaign_total <= 0:
        return False, 0
    remaining = max(0, campaign_total - campaign_current)
    return remaining > 0, remaining


@scheduler.scheduled_job("cron", day_of_week="fri", hour=20, id="ark_campaign_push")
async def check_campaign_push() -> None:
    """周五 20:00 检查订阅账号的本周剿灭进度，未拿满提醒一次。"""
    datas = await gs_subscribe.get_subscribe(CAMPAIGN_TASK_NAME)
    if not datas:
        return
    week_key = _iso_week_key()
    state = load_push_state()
    # 每账号每周只提醒一次：{key: {"campaign_week": "2026-W34"}}
    for subscribe in datas:
        uid = str(subscribe.uid or "")
        if not uid:
            continue
        key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
        if (state.get(key) or {}).get("campaign_week") == week_key:
            continue

        credential = await resolve_credential(
            subscribe.user_id, subscribe.bot_id, uid=uid
        )
        if isinstance(credential, str):
            await maybe_warn_credential(subscribe, uid, credential)
            continue
        response = await fetch_cached_player_info(credential, force=True)
        if isinstance(response, str):
            continue
        data = parse_player_info(response)
        if data is None:
            continue
        should_push, remaining = evaluate_campaign(
            data.campaign_current, data.campaign_total
        )
        if should_push:
            message = (
                "「明日方舟」剿灭周提醒\n"
                f"{data.name or credential.nickname}（UID {hide_uid(uid)}）"
                f"本周合成玉 {data.campaign_current}/{data.campaign_total}\n"
                f"还有 {remaining} 合成玉没拿，周末别忘了打剿灭！"
            )
            try:
                await _CheckedSubscribe(subscribe).send(message)
                logger.info(
                    f"[ArknightsUID·剿灭提醒] 已推送 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} 剩余{remaining}"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·剿灭提醒] 推送失败: {exc.__class__.__name__}"
                )
                continue  # 发送失败不落周标记：重启后的下次触发还能补发

        # 无论是否需要提醒，本周已检查过就落标记（未拿满也只提醒一次）。
        # 键级合并写：整文件覆盖会把轮询窗口内其他任务/进程刚写入的键
        # （如理智推送的 notified）回滚（审计 #39）
        # 显式绑定循环变量（B023）：闭包在本迭代内即被同步消费，
        # 默认参绑定消除对晚绑定的依赖
        def _merge_campaign(key=key, week_key=week_key) -> dict:
            fresh = load_push_state()
            fresh[key] = {**fresh.get(key, {}), "campaign_week": week_key}
            return fresh

        save_push_state({}, merge_from=_merge_campaign)
        await asyncio.sleep(random.uniform(1, 2))


# ==================== 基建订阅推送（无人机/贸易/换班/公招） ====================

BUILDING_TASK_NAME = "订阅基建推送"
_BUILDING_STATE_PATH: Path = MAIN_PATH / "building_push_state.json"
# 基建事件均为小时级节奏，15 分钟轮询足够；公招 9 小时档更是绰绰有余
BUILDING_POLL_MINUTES = 15
# 公招完成去重：同一槽位同一 finishTs 只提醒一次（滚动保留最近的）
_RECRUIT_SEEN_KEEP = 16


def _load_building_state() -> dict[str, dict]:
    return _load_state(_BUILDING_STATE_PATH)


def _save_building_state(state: dict[str, dict]) -> None:
    _save_state(_BUILDING_STATE_PATH, state, "基建推送")


def evaluate_full(current: int, limit: int, notified: bool) -> tuple[bool, bool]:
    """满仓类事件状态机（无人机/贸易订单同型）：返回 (is_full, should_push)。

    - limit 无效（0/负）：不判定不推送
    - 满 → 推送一次（notified 落 True）；降回未满自动复位重新武装
    """
    if limit <= 0:
        return False, False
    is_full = current >= limit
    return is_full, is_full and not notified


def evaluate_trading_full(
    rooms: list[tuple[int, int]], notified: bool
) -> tuple[bool, bool]:
    """贸易订单满：任一房间 待交付数 >= 上限 即满（rooms: [(stock, limit)]）。"""
    full_rooms = [
        (stock, limit) for stock, limit in rooms if limit > 0 and stock >= limit
    ]
    is_full = bool(full_rooms)
    return is_full, is_full and not notified


def evaluate_tired(tired_count: int, notified: bool) -> tuple[bool, bool]:
    """低心情换班：有待换班干员 → 提醒一次；换完班（清零）自动复位。"""
    is_tired = tired_count > 0
    return is_tired, is_tired and not notified


def evaluate_recruit(done_slots: list[int], seen: list[int]) -> tuple[list[int], bool]:
    """公招完成：已到点槽位的 finishTs 去重（seen 内不重复提醒）。

    返回 (本轮新完成的 finishTs 列表, should_push)。
    """
    fresh = sorted({ts for ts in done_slots if ts not in set(seen)})
    return fresh, bool(fresh)


def format_labor_push_message(
    name: str, value: int, max_value: int, uid_masked: str
) -> str:
    return (
        "「明日方舟」无人机已回满\n"
        f"{name or '博士'}（UID {uid_masked}）{value}/{max_value}\n"
        "无人机满仓不再回复，上号用掉（加速制造/贸易/剿灭撤退）～"
    )


def format_trading_push_message(
    name: str, full_rooms: list[tuple[str, str, int, int]], uid_masked: str
) -> str:
    rooms_text = "、".join(
        f"{label}{strategy}订单 {stock}/{limit}"
        for label, strategy, stock, limit in full_rooms
    )
    return (
        "「明日方舟」贸易站订单已满\n"
        f"{name or '博士'}（UID {uid_masked}）{rooms_text}\n"
        "订单满后贸易站停摆，上号交付（可用无人机加速）～"
    )


def format_tired_push_message(
    name: str, tired_chars: list[tuple[str, str, float]], uid_masked: str
) -> str:
    head = "、".join(
        f"{char_name}({room} {morale}点)" for char_name, room, morale in tired_chars[:6]
    )
    more = f" 等{len(tired_chars)}人" if len(tired_chars) > 6 else ""
    return (
        "「明日方舟」基建干员心情过低\n"
        f"{name or '博士'}（UID {uid_masked}）待换班：{head}{more}\n"
        "心情低于 12 点效率受损，上号换班/安排宿舍休息～"
    )


def format_recruit_push_message(name: str, done_count: int, uid_masked: str) -> str:
    return (
        "「明日方舟」公开招募完成\n"
        f"{name or '博士'}（UID {uid_masked}）有 {done_count} 个公招标位可领取\n"
        "上号领取并安排下一轮招募（记得刷新标签）～"
    )


@notify_sv.on_command(
    (
        "订阅基建推送",
        "订阅基建",
    ),
    block=True,
)
async def subscribe_building_push(bot: Bot, ev: Event) -> None:
    async def commit(key: str, ticket: int, credential, _prepared) -> bool:
        if not _intent_current(BUILDING_TASK_NAME, key, ticket):
            return False
        await gs_subscribe.add_subscribe(
            "single", task_name=BUILDING_TASK_NAME, event=ev, uid=credential.uid
        )
        if not _intent_current(BUILDING_TASK_NAME, key, ticket):
            return False
        return _record_intent(BUILDING_TASK_NAME, key, ticket, active=True)

    result = await _run_subscribe_command(
        bot, ev, BUILDING_TASK_NAME, ev.text or "", commit=commit
    )
    if result is None:
        return
    credential, _ = result
    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot,
        ev,
        f"已订阅基建推送：{shown}\n"
        f"无人机回满 / 贸易订单满 / 干员心情过低 / 公招完成时各提醒一次，\n"
        f"处理后自动恢复提醒（专精训练另有 ark订阅训练，含剩余时间预警）。\n"
        f"取消：ark取消订阅基建",
    )


@notify_sv.on_command(
    (
        "取消订阅基建",
        "取消基建推送",
        "退订基建推送",
    ),
    block=True,
)
async def unsubscribe_building_push(bot: Bot, ev: Event) -> None:
    ticket = time.time_ns()
    uid, index = _extract_target(ev.text)
    if uid or index:
        credential = await resolve_or_reply(bot, ev, uid, index)
        if credential is None:
            return
        uid = credential.uid
    if not await _cancel_subscription(BUILDING_TASK_NAME, ev, ticket, uid):
        await _send_text(
            bot, ev, "退订保存失败或已有更新操作，本次未生效；请稍后重试。"
        )
        return
    message = "已取消该账号的基建推送。" if uid else "已取消全部基建推送。"
    await _send_text(bot, ev, message)


@scheduler.scheduled_job(
    "interval", minutes=BUILDING_POLL_MINUTES, id="ark_building_push"
)
async def check_building_push() -> None:
    _t0 = time.monotonic()
    """定时检查各订阅账号基建：无人机/贸易/换班/公招四类事件各推一次。"""
    # 查询前记录代次，查询结果返回期间的变更也必须使旧关系失效。
    epoch_snapshot = _load_state(_SUB_EPOCH_PATH)
    datas = await gs_subscribe.get_subscribe(BUILDING_TASK_NAME)
    if not datas:
        return
    logger.info(f"[ArknightsUID·基建推送] 检查 {len(datas)} 个订阅..")

    changes: dict[str, tuple[dict, int, dict]] = {}
    for subscribe in datas:
        uid = str(subscribe.uid or "")
        if not uid:
            continue
        key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
        prev = _load_building_state().get(key, {})
        epoch_base = int(epoch_snapshot.get(_epoch_key(BUILDING_TASK_NAME, key)) or 0)

        credential = await resolve_credential(
            subscribe.user_id, subscribe.bot_id, uid=uid
        )
        if isinstance(credential, str):
            logger.warning(
                f"[ArknightsUID·基建推送] 账号不可用 user={subscribe.user_id}: {credential}"
            )
            await maybe_warn_credential(subscribe, uid, credential)
            continue

        response = await fetch_cached_player_info(credential)
        if isinstance(response, str):
            logger.warning(
                f"[ArknightsUID·基建推送] 拉取失败 uid={hide_uid(uid)}: {response}"
            )
            continue
        data = parse_player_info(response)
        if data is None:
            continue

        # 发送前验证（第四轮复核）：处理期间订阅被重订阅换会话或退订
        # → 本轮跳过该订阅，不向旧会话发送、不落旧标记
        if not _poll_current(BUILDING_TASK_NAME, key, epoch_base, epoch_snapshot):
            continue

        new_state = dict(prev)
        nickname = data.name or credential.nickname

        # 无人机回满（无 building 时跳过基建三类，公招仍可判定）
        building = parse_building(data)
        if building is not None:
            labor_full, labor_push = evaluate_full(
                building.labor_value,
                building.labor_max,
                bool(prev.get("labor")),
            )
            if labor_push:
                try:
                    await _CheckedSubscribe(subscribe).send(
                        format_labor_push_message(
                            nickname,
                            building.labor_value,
                            building.labor_max,
                            hide_uid(uid),
                        )
                    )
                    logger.info(
                        f"[ArknightsUID·基建推送] 无人机满 user={subscribe.user_id} "
                        f"uid={hide_uid(uid)}"
                    )
                except Exception as exc:
                    logger.warning(
                        f"[ArknightsUID·基建推送] 无人机满推送失败: {exc.__class__.__name__}"
                    )
                    labor_full = bool(prev.get("labor"))  # 发送失败保持旧值
            new_state["labor"] = labor_full

            trading_rooms = [
                (room.stock_count, room.stock_limit) for room in building.tradings
            ]
            trading_full, trading_push = evaluate_trading_full(
                trading_rooms, bool(prev.get("trading"))
            )
            if trading_push:
                full_rows = [
                    (
                        "贸易站" if i == 1 else f"贸易站{i}",
                        TRADING_STRATEGY_NAMES.get(room.strategy, "订单"),
                        room.stock_count,
                        room.stock_limit,
                    )
                    for i, room in enumerate(building.tradings, start=1)
                    if room.stock_limit > 0 and room.stock_count >= room.stock_limit
                ]
                try:
                    await _CheckedSubscribe(subscribe).send(
                        format_trading_push_message(
                            nickname,
                            full_rows,
                            hide_uid(uid),
                        )
                    )
                    logger.info(
                        f"[ArknightsUID·基建推送] 贸易满 user={subscribe.user_id} "
                        f"uid={hide_uid(uid)}"
                    )
                except Exception as exc:
                    logger.warning(
                        f"[ArknightsUID·基建推送] 贸易满推送失败: {exc.__class__.__name__}"
                    )
                    trading_full = bool(prev.get("trading"))
            new_state["trading"] = trading_full

            tired_full, tired_push = evaluate_tired(
                len(building.tired_chars), bool(prev.get("tired"))
            )
            if tired_push:
                try:
                    await _CheckedSubscribe(subscribe).send(
                        format_tired_push_message(
                            nickname,
                            [(c.name, c.room, c.morale) for c in building.tired_chars],
                            hide_uid(uid),
                        )
                    )
                    logger.info(
                        f"[ArknightsUID·基建推送] 低心情 user={subscribe.user_id} "
                        f"uid={hide_uid(uid)} n={len(building.tired_chars)}"
                    )
                except Exception as exc:
                    logger.warning(
                        f"[ArknightsUID·基建推送] 低心情推送失败: {exc.__class__.__name__}"
                    )
                    tired_full = bool(prev.get("tired"))
            new_state["tired"] = tired_full

        # 公招完成（data.recruit 顶层字段，与 building 无关）
        done_slots = [
            slot.finish_ts
            for slot in data.recruit_slots
            if slot.recruiting and slot.remaining_secs() == 0
        ]
        fresh_done, recruit_push = evaluate_recruit(
            done_slots, list(prev.get("recruit_seen") or [])
        )
        if recruit_push:
            try:
                await _CheckedSubscribe(subscribe).send(
                    format_recruit_push_message(
                        nickname,
                        len(fresh_done),
                        hide_uid(uid),
                    )
                )
                logger.info(
                    f"[ArknightsUID·基建推送] 公招完成 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} n={len(fresh_done)}"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·基建推送] 公招推送失败: {exc.__class__.__name__}"
                )
                fresh_done = []
        if fresh_done:
            seen = list(prev.get("recruit_seen") or []) + fresh_done
            new_state["recruit_seen"] = seen[-_RECRUIT_SEEN_KEEP:]

        if new_state != prev:
            # 携带 (比较基底, 订阅代次, 新状态)：退订→重订阅（含尚无状态行
            # 的首次订阅）会更新代次，轮末据此弃写，不压掉新订阅的首轮提醒
            changes[key] = (prev, epoch_base, new_state)
        await asyncio.sleep(random.uniform(1, 2))

    if changes:
        # 跨进程锁内重读合并 + 退订不复活（审计 #10，同训练推送）
        alive = await _alive_state_keys(BUILDING_TASK_NAME)

        def _merge_building() -> dict:
            fresh = _load_building_state()
            for key, (base, epoch_base, new_state) in changes.items():
                # 订阅代次变化：轮询处理期间有订阅/退订命令完成 → 弃写
                if not _poll_current(
                    BUILDING_TASK_NAME, key, epoch_base, epoch_snapshot
                ):
                    continue
                row = fresh.get(key)
                if row is None:
                    # 读时存在、写时已消失：退订→重订阅清掉了键——不得用
                    # 旧标记重建；仅新订阅（读时也无行且代次未变）首次落点
                    if not base and key in alive:
                        fresh[key] = new_state
                    continue
                if row != base:
                    continue
                fresh[key] = {**row, **new_state}
            return fresh

        _write_state_atomic(_BUILDING_STATE_PATH, {}, merge_from=_merge_building)
    _elapsed = time.monotonic() - _t0
    if _elapsed > 720:  # 轮询间隔 15min 的 80%
        logger.warning(
            f"[ArknightsUID·基建推送] 单轮耗时 {_elapsed:.0f}s，接近轮询间隔，"
            "下一轮可能被跳过（账号过多/凭据异常）"
        )


# ==================== 自动签到订阅（每日 08:05） ====================

SIGN_TASK_NAME = "订阅签到推送"
_SIGN_STATE_PATH: Path = MAIN_PATH / "sign_push_state.json"


def _load_sign_state() -> dict[str, dict]:
    return _load_state(_SIGN_STATE_PATH)


def _save_sign_state(state: dict[str, dict]) -> None:
    _save_state(_SIGN_STATE_PATH, state, "签到推送")


def evaluate_sign_result(response: dict | None) -> str:
    """签到响应 → 事件类型：success / already / fail（纯函数）。

    - code == 0：success（无论 message 文案）
    - 非 0 且 message 含「已签到/已成功签到」类字样：already（今日已签）
    - 其余/响应为空：fail
    """
    if not isinstance(response, dict):
        return "fail"
    if response.get("code") == 0:
        return "success"
    message = str(response.get("message") or response.get("msg") or "")
    if "已签" in message:
        return "already"
    return "fail"


def format_sign_push_message(
    name: str, uid_masked: str, outcome: str, message: str
) -> str:
    if outcome == "success":
        return (
            "「明日方舟」自动签到完成\n"
            f"{name or '博士'}（UID {uid_masked}）{message or '签到成功'}"
        )
    return (
        "「明日方舟」自动签到失败\n"
        f"{name or '博士'}（UID {uid_masked}）{message or '无详情'}\n"
        "可手动 ark签到 重试。"
    )


@notify_sv.on_command(
    (
        "订阅签到",
        "自动签到",
    ),
    block=True,
)
async def subscribe_sign_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    credential = await resolve_or_reply(bot, ev, uid, index)
    if credential is None:
        return

    await gs_subscribe.add_subscribe(
        "single", task_name=SIGN_TASK_NAME, event=ev, uid=credential.uid
    )
    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot,
        ev,
        f"已订阅每日自动签到：{shown}\n"
        f"每天 {_SIGN_HOUR:02d}:{_SIGN_MINUTE:02d} 自动完成森空岛签到并回报结果（今日已签则静默）。\n"
        f"取消：ark取消订阅签到",
    )


@notify_sv.on_command(
    (
        "取消订阅签到",
        "取消自动签到",
        "退订签到推送",
    ),
    block=True,
)
async def unsubscribe_sign_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    if uid or index:
        credential = await resolve_or_reply(bot, ev, uid, index)
        if credential is None:
            return
        await gs_subscribe.delete_subscribe(
            "single", task_name=SIGN_TASK_NAME, event=ev, uid=credential.uid
        )
        state = _load_sign_state()
        state.pop(_state_key(ev.bot_id, ev.user_id, credential.uid), None)
        _save_sign_state(state)
        return await _send_text(bot, ev, "已取消该账号的自动签到。")

    await gs_subscribe.delete_subscribe("single", task_name=SIGN_TASK_NAME, event=ev)
    prefix = f"{ev.bot_id}|{ev.user_id}|"
    state = _load_sign_state()
    for key in [k for k in state if k.startswith(prefix)]:
        state.pop(key, None)
    _save_sign_state(state)
    await _send_text(bot, ev, "已取消全部自动签到。")


def _sign_schedule() -> tuple[int, int]:
    """SignTime 配置 [时, 分] → cron 参数，非法值回落 08:05。"""
    try:
        raw = ArkConfig.get_config("SignTime").data or ["8", "5"]
        hour, minute = int(raw[0]), int(raw[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except Exception:
        pass
    return 8, 5


_SIGN_HOUR, _SIGN_MINUTE = _sign_schedule()


@scheduler.scheduled_job(
    "cron", hour=_SIGN_HOUR, minute=_SIGN_MINUTE, id="ark_sign_push"
)
async def check_sign_push() -> None:
    """每天按 SignTime 配置自动签到订阅账号并回报（失败/成功推送，已签静默）。"""
    datas = await gs_subscribe.get_subscribe(SIGN_TASK_NAME)
    if not datas:
        return
    logger.info(f"[ArknightsUID·签到推送] 处理 {len(datas)} 个订阅..")

    today = beijing_datetime().date().isoformat()
    from ArknightsUID.utils.reply_safety import safe_external_message

    for subscribe in datas:
        uid = str(subscribe.uid or "")
        if not uid:
            continue
        key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
        if (_load_sign_state().get(key) or {}).get("date") == today:
            continue

        credential = await resolve_credential(
            subscribe.user_id, subscribe.bot_id, uid=uid
        )
        if isinstance(credential, str):
            await maybe_warn_credential(subscribe, uid, credential)
            continue

        response = await ark_attendance(credential)
        # 签到改变账号状态：清 player_info 本地缓存，避免 TTL 窗口内查旧数据
        # （"already" 也清——签到可能发生在缓存写入之后）
        if isinstance(response, dict):
            await drop_cached_player_info(uid)
        outcome = evaluate_sign_result(response)
        message = ""
        if isinstance(response, dict):
            message = safe_external_message(
                str(response.get("message") or response.get("msg") or "")
            )[:80]
        if outcome == "already":
            logger.info(
                f"[ArknightsUID·签到推送] 今日已签 user={subscribe.user_id} "
                f"uid={hide_uid(uid)}"
            )
        else:
            push = format_sign_push_message(
                credential.nickname, hide_uid(uid), outcome, message
            )
            try:
                await _CheckedSubscribe(subscribe).send(push)
                logger.info(
                    f"[ArknightsUID·签到推送] {outcome} user={subscribe.user_id} "
                    f"uid={hide_uid(uid)}"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·签到推送] 推送失败: {exc.__class__.__name__}"
                )
                continue  # 发送失败不落日期标记，重启后补跑
        state = _load_sign_state()
        state[key] = {"date": today}
        _save_sign_state(state)
        await asyncio.sleep(random.uniform(1, 2))


# ---------------------------------------------------------------------------
# ark全部签到（pm=1）：对所有可用登录手动执行一次签到（对齐 EndUID）
# ---------------------------------------------------------------------------
sign_all_sv = SV("ArknightsUID全部签到", pm=1, priority=5)

# 单次手动全签的账号上限（防误装了大量凭据时刷接口）
_SIGN_ALL_LIMIT = 50


@sign_all_sv.on_fullmatch(("全部签到", "签到全部"), block=True)
async def sign_all_accounts(bot: Bot, ev: Event) -> None:
    from ArknightsUID.utils.database.models import ArkUser

    try:
        rows = await ArkUser.select_rows(cookie_status="")
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·签到] 全部签到查询失败: {exc.__class__.__name__}"
        )
        rows = []

    targets: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        uid = (getattr(row, "uid", "") or "").strip()
        user_id = getattr(row, "user_id", "") or ""
        bot_id = getattr(row, "bot_id", "") or ""
        if not uid or (user_id, bot_id, uid) in seen:
            continue
        seen.add((user_id, bot_id, uid))
        targets.append((user_id, bot_id, uid))
    if not targets:
        return await _send_text(bot, ev, "没有可用登录（需先 ark绑定）。")

    await _send_text(
        bot, ev, f"开始为 {min(len(targets), _SIGN_ALL_LIMIT)} 个账号签到…"
    )
    ok = failed = skipped = 0
    for user_id, bot_id, uid in targets[:_SIGN_ALL_LIMIT]:
        credential = await resolve_credential(user_id, bot_id, uid=uid)
        if isinstance(credential, str):
            failed += 1
            logger.warning(
                f"[ArknightsUID·签到] 全部签到账号不可用 uid={hide_uid(uid)}"
            )
            continue
        response = await ark_attendance(credential)
        if isinstance(response, str):
            failed += 1
            continue
        # 签到改变账号状态：清 player_info 本地缓存（同上方自动签到）
        await drop_cached_player_info(uid)
        # 10001 既可能是"今日已签"也可能是 cred 失效（request_util 注释
        # 自证歧义，审计 #31）：按 message 区分，失效账号计入失败而不是
        # 被全绿统计掩盖
        outcome = evaluate_sign_result(response)
        if outcome == "success":
            ok += 1
        elif outcome == "already":
            skipped += 1
        else:
            failed += 1
        await asyncio.sleep(random.uniform(1.0, 2.0))

    lines = [
        "「明日方舟」全部签到完成：",
        f"成功 {ok} · 已签跳过 {skipped} · 失败 {failed}",
    ]
    if len(targets) > _SIGN_ALL_LIMIT:
        lines.append(f"（共 {len(targets)} 个账号，仅处理前 {_SIGN_ALL_LIMIT} 个）")
    await _send_text(bot, ev, "\n".join(lines))
