"""理智回满推送 + 专精训练完成推送（订阅 + 定时轮询 + 到点提醒）。

- `ark订阅理智 [序号|UID]`：订阅当前默认账号（或指定账号）的理智回满提醒
- `ark取消订阅理智 [序号|UID]`：取消（不带参数取消全部）
- 定时轮询（10 分钟）：逐订阅账号拉 player_info（带 5 分钟磁盘缓存），
  推算当前理智，回满即推送一次；用后（理智降回上限以下）自动重新武装
- `ark订阅训练 [序号|UID]` / `ark取消订阅训练`：专精训练完成提醒（30 分钟
  轮询训练室 remainSecs，归零推送一次；换干员开新一轮后自动重新武装）

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
from pathlib import Path

from gsuid_core.aps import scheduler
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        resolve_credential,
    )
    from ArknightsUID.ArknightsUID.utils.player import (
        TRADING_STRATEGY_NAMES,
        effective_ap,
        fetch_cached_player_info,
        format_training_remaining,
        parse_building,
        parse_player_info,
        training_remaining_secs,
    )
    from ArknightsUID.ArknightsUID.utils.skland.client import ark_attendance
except ModuleNotFoundError:
    from ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        resolve_credential,
    )
    from ArknightsUID.utils.player import (
        TRADING_STRATEGY_NAMES,
        effective_ap,
        fetch_cached_player_info,
        format_training_remaining,
        parse_building,
        parse_player_info,
        training_remaining_secs,
    )
    from ArknightsUID.utils.skland.client import ark_attendance

try:
    from ArknightsUID.ArknightsUID.utils.path import MAIN_PATH
except ModuleNotFoundError:
    from ArknightsUID.utils.path import MAIN_PATH

notify_sv = SV("ArknightsUID理智推送", priority=5)

TASK_NAME = "订阅理智推送"
_STATE_PATH: Path = MAIN_PATH / "sanity_push_state.json"
# 轮询间隔：理智 6 分钟回 1 点，10 分钟粒度平均多溢出 ~0.8 点，可接受
POLL_MINUTES = 10

TRAIN_TASK_NAME = "订阅训练推送"
_TRAIN_STATE_PATH: Path = MAIN_PATH / "training_push_state.json"
# 专精剩余时间预警窗口：进入最后一小时提醒一次「还差多久完成」
TRAIN_WARN_WINDOW_SECS = 3600
# 专精完成推送 + 剩余时间预警共用轮询：10 分钟粒度让「剩约X分钟」有读数
# 意义（player_info 磁盘缓存 5 分钟，与其他任务错峰命中）
TRAIN_POLL_MINUTES = 10


def _state_key(bot_id: str, user_id: str, uid: str) -> str:
    return f"{bot_id}|{user_id}|{uid}"


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


def evaluate_sanity_warn(
    ap: int, ap_max: int, warned: bool
) -> tuple[bool, bool]:
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


def format_sanity_warn_message(
    name: str, ap: int, ap_max: int, uid_masked: str
) -> str:
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


def _load_cred_warn_state() -> dict[str, dict]:
    try:
        state = json.loads(CRED_WARN_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {}


def _save_cred_warn_state(state: dict[str, dict]) -> None:
    try:
        CRED_WARN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_state_atomic(CRED_WARN_STATE_PATH, state)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·凭据预警] 状态写入失败: {exc.__class__.__name__}"
        )


async def maybe_warn_credential(subscribe, uid: str, error: str) -> None:
    """凭据不可用时向订阅会话提醒一次（7 天冷却，供各轮询任务共用）。

    subscribe 是 gs_subscribe 的订阅对象（.send/.bot_id/.user_id 可用）；
    发送失败不落时间戳，下轮还能重试。
    """
    key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
    last = float((_load_cred_warn_state().get(key) or {}).get("ts") or 0)
    if time.time() - last < CRED_WARN_COOLDOWN_SECS:
        return
    message = (
        "「明日方舟」登录状态不可用\n"
        f"UID {hide_uid(uid)} 的定时查询连续失败：{str(error)[:60]}\n"
        "请重新 ark绑定 / ark扫码登录（私聊）；7 天内不重复提醒。"
    )
    try:
        await subscribe.send(message)
        logger.info(
            f"[ArknightsUID·凭据预警] 已提醒 user={subscribe.user_id} "
            f"uid={hide_uid(uid)}"
        )
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·凭据预警] 提醒失败: {exc.__class__.__name__}"
        )
        return
    state = _load_cred_warn_state()
    state[key] = {"ts": time.time()}
    _save_cred_warn_state(state)


def _write_state_atomic(path: Path, state: dict) -> None:
    """临时文件 + os.replace 原子落盘：写一半崩溃不至于把状态文件写坏
    （坏文件会被当空态，导致全部订阅重复推送一次）。"""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, path)


def load_push_state() -> dict[str, dict]:
    try:
        state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {}


def save_push_state(state: dict[str, dict]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_state_atomic(_STATE_PATH, state)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·理智推送] 状态写入失败: {exc.__class__.__name__}"
        )


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


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


@notify_sv.on_command(
    (
        "订阅理智",
        "理智推送",
        "订阅理智提醒",
    ),
    block=True,
)
async def subscribe_sanity_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    # single 模式：同 user 一条/uid；重复订阅同 uid 只会刷新行
    await gs_subscribe.add_subscribe(
        "single", task_name=TASK_NAME, event=ev, uid=credential.uid
    )
    # 新订阅：清掉旧 notified 标记，保证"订阅时已回满"也能立即提醒一次
    state = load_push_state()
    state.pop(_state_key(ev.bot_id, ev.user_id, credential.uid), None)
    save_push_state(state)

    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot, ev,
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
    uid, index = _extract_target(ev.text)
    if uid or index:
        credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
        if isinstance(credential, str):
            return await _send_text(bot, ev, credential)
        target_uid = credential.uid
        await gs_subscribe.delete_subscribe(
            "single", task_name=TASK_NAME, event=ev, uid=target_uid
        )
        state = load_push_state()
        state.pop(_state_key(ev.bot_id, ev.user_id, target_uid), None)
        save_push_state(state)
        return await _send_text(bot, ev, "已取消该账号的理智回满推送。")

    await gs_subscribe.delete_subscribe(
        "single", task_name=TASK_NAME, event=ev
    )
    prefix = f"{ev.bot_id}|{ev.user_id}|"
    state = load_push_state()
    for key in [k for k in state if k.startswith(prefix)]:
        state.pop(key, None)
    save_push_state(state)
    await _send_text(bot, ev, "已取消全部理智回满推送。")


def format_push_message(name: str, ap_max: int, uid_masked: str) -> str:
    return (
        "「明日方舟」理智已回满\n"
        f"{name or '博士'}（UID {uid_masked}）{ap_max}/{ap_max}\n"
        "理智已满，再不消耗就要溢出了，快上号！"
    )


@scheduler.scheduled_job("interval", minutes=POLL_MINUTES, id="ark_sanity_push")
async def check_sanity_push() -> None:
    """定时检查各订阅账号理智，回满推送一次。"""
    datas = await gs_subscribe.get_subscribe(TASK_NAME)
    if not datas:
        return
    logger.info(f"[ArknightsUID·理智推送] 检查 {len(datas)} 个订阅..")

    # 只记本轮变更，落盘前重读合并——避免覆盖轮询期间 (退)订阅写盘的状态
    changes: dict[str, dict] = {}
    for subscribe in datas:
        uid = str(subscribe.uid or "")
        if not uid:
            continue
        key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
        prev = load_push_state().get(key, {})
        notified = bool(prev.get("notified"))
        warned = bool(prev.get("warned"))

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

        ap = effective_ap(data)
        is_full, should_push = evaluate_sanity(ap, data.ap_max, notified)
        should_warn, new_warned = evaluate_sanity_warn(ap, data.ap_max, warned)
        if should_push:
            message = format_push_message(
                data.name or credential.nickname, data.ap_max, hide_uid(uid)
            )
            try:
                await subscribe.send(message)
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
            try:
                await subscribe.send(message)
                logger.info(
                    f"[ArknightsUID·理智预警] 已推送 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} {ap}/{data.ap_max}"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·理智预警] 推送失败: {exc.__class__.__name__}"
                )
        if bool(is_full) != notified or new_warned != warned:
            # 整键覆盖：带上 prev 保住未变化的标志位（changes 在轮询末合并落盘）
            changes[key] = {**prev, "notified": is_full, "warned": new_warned}
        await asyncio.sleep(random.uniform(1, 2))

    if changes:
        fresh = load_push_state()
        fresh.update(changes)
        save_push_state(fresh)


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
    trainee_id: str, remaining: int, warned: bool, prev_trainee: str
) -> tuple[bool, bool]:
    """专精剩余时间预警状态机：进入最后一小时提醒一次「还差多久完成」。

    返回 (should_warn, new_warned)：
    - 未在训/剩余未知/剩余超过窗口：不提醒，warned 复位（新一轮重新武装）
    - 在窗口内且本轮未预警过：提醒一次
    - warned 换训干员自动失效（与完成推送同款的新一轮判定）
    """
    if not trainee_id or remaining < 0 or remaining > TRAIN_WARN_WINDOW_SECS:
        return False, False
    armed = warned and prev_trainee == trainee_id
    return (not armed), True


def format_training_warn_message(
    name: str, trainee_name: str, remaining: int, uid_masked: str
) -> str:
    return (
        "「明日方舟」专精训练即将完成\n"
        f"{name or '博士'}（UID {uid_masked}）训练室的 "
        f"{trainee_name or '干员'} 还剩{format_training_remaining(remaining)}完成\n"
        "可以准备上号领取，安排下一位了～"
    )


def _load_train_state() -> dict[str, dict]:
    try:
        state = json.loads(_TRAIN_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {}


def _save_train_state(state: dict[str, dict]) -> None:
    try:
        _TRAIN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_state_atomic(_TRAIN_STATE_PATH, state)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·训练推送] 状态写入失败: {exc.__class__.__name__}"
        )


@notify_sv.on_command(
    (
        "订阅训练",
        "订阅专精训练",
        "训练推送",
    ),
    block=True,
)
async def subscribe_training_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    await gs_subscribe.add_subscribe(
        "single", task_name=TRAIN_TASK_NAME, event=ev, uid=credential.uid
    )
    state = _load_train_state()
    state.pop(_state_key(ev.bot_id, ev.user_id, credential.uid), None)
    _save_train_state(state)

    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot, ev,
        f"已订阅专精训练完成推送：{shown}\n"
        f"训练完成会在这里提醒一次；换干员开新一轮后自动恢复提醒。\n"
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
    uid, index = _extract_target(ev.text)
    if uid or index:
        credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
        if isinstance(credential, str):
            return await _send_text(bot, ev, credential)
        target_uid = credential.uid
        await gs_subscribe.delete_subscribe(
            "single", task_name=TRAIN_TASK_NAME, event=ev, uid=target_uid
        )
        state = _load_train_state()
        state.pop(_state_key(ev.bot_id, ev.user_id, target_uid), None)
        _save_train_state(state)
        return await _send_text(bot, ev, "已取消该账号的专精训练推送。")

    await gs_subscribe.delete_subscribe(
        "single", task_name=TRAIN_TASK_NAME, event=ev
    )
    prefix = f"{ev.bot_id}|{ev.user_id}|"
    state = _load_train_state()
    for key in [k for k in state if k.startswith(prefix)]:
        state.pop(key, None)
    _save_train_state(state)
    await _send_text(bot, ev, "已取消全部专精训练推送。")


def format_training_push_message(
    name: str, trainee_name: str, uid_masked: str
) -> str:
    return (
        "「明日方舟」专精训练完成\n"
        f"{name or '博士'}（UID {uid_masked}）训练室的 "
        f"{trainee_name or '干员'} 专精训练已完成\n"
        "上号领取并安排下一位吧～"
    )


@scheduler.scheduled_job(
    "interval", minutes=TRAIN_POLL_MINUTES, id="ark_training_push"
)
async def check_training_push() -> None:
    """定时检查各订阅账号训练室，完成推送一次（换训干员后重新武装）。"""
    datas = await gs_subscribe.get_subscribe(TRAIN_TASK_NAME)
    if not datas:
        return
    logger.info(f"[ArknightsUID·训练推送] 检查 {len(datas)} 个订阅..")

    # 只记本轮变更，落盘前重读合并（与理智推送同款竞态防护）
    changes: dict[str, dict] = {}
    for subscribe in datas:
        uid = str(subscribe.uid or "")
        if not uid:
            continue
        key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
        prev = _load_train_state().get(key, {})
        prev_notified = bool(prev.get("notified"))
        prev_trainee = str(prev.get("trainee") or "")
        prev_warned = bool(prev.get("warned"))

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
        )
        new_state["warned"] = new_warned
        if should_push:
            message = format_training_push_message(
                (data.name if data else "") or credential.nickname,
                building.training_trainee,
                hide_uid(uid),
            )
            try:
                await subscribe.send(message)
                logger.info(
                    f"[ArknightsUID·训练推送] 已推送 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} trainee={building.training_trainee}"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·训练推送] 推送失败: {exc.__class__.__name__}"
                )
                # 发送失败不落 notified，下轮重试
                new_state = {"notified": False, "trainee": prev_trainee, "warned": False}
        elif should_warn:
            message = format_training_warn_message(
                (data.name if data else "") or credential.nickname,
                building.training_trainee,
                remaining,
                hide_uid(uid),
            )
            try:
                await subscribe.send(message)
                logger.info(
                    f"[ArknightsUID·训练预警] 已推送 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} remaining={remaining}s"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·训练预警] 推送失败: {exc.__class__.__name__}"
                )
                new_state["warned"] = False  # 发送失败下轮重试
        if new_state != prev:
            changes[key] = new_state
        await asyncio.sleep(random.uniform(1, 2))

    if changes:
        fresh = _load_train_state()
        fresh.update(changes)
        _save_train_state(fresh)


# ==================== 剿灭周提醒（周五晚未完成 → 提醒一次） ====================

CAMPAIGN_TASK_NAME = "订阅剿灭推送"


def _iso_week_key(now_ts: float | None = None) -> str:
    """ISO 周标识（如 2026-W34）：一周只提醒一次的状态键。"""
    from datetime import datetime

    moment = datetime.fromtimestamp(now_ts or time.time())
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


@notify_sv.on_command(("订阅剿灭", "剿灭推送"), block=True)
async def subscribe_campaign_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    await gs_subscribe.add_subscribe(
        "single", task_name=CAMPAIGN_TASK_NAME, event=ev, uid=credential.uid
    )
    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot, ev,
        f"已订阅剿灭周提醒：{shown}\n"
        f"每周五 20:00 检查本周合成玉，没拿满会提醒你。\n"
        f"取消：ark取消订阅剿灭",
    )


@notify_sv.on_command(("取消订阅剿灭", "退订剿灭推送"), block=True)
async def unsubscribe_campaign_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    if uid or index:
        credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
        if isinstance(credential, str):
            return await _send_text(bot, ev, credential)
        await gs_subscribe.delete_subscribe(
            "single", task_name=CAMPAIGN_TASK_NAME, event=ev, uid=credential.uid
        )
        return await _send_text(bot, ev, "已取消该账号的剿灭周提醒。")

    await gs_subscribe.delete_subscribe(
        "single", task_name=CAMPAIGN_TASK_NAME, event=ev
    )
    await _send_text(bot, ev, "已取消全部剿灭周提醒。")


def evaluate_campaign(
    campaign_current: int, campaign_total: int
) -> tuple[bool, int]:
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
                await subscribe.send(message)
                logger.info(
                    f"[ArknightsUID·剿灭提醒] 已推送 user={subscribe.user_id} "
                    f"uid={hide_uid(uid)} 剩余{remaining}"
                )
            except Exception as exc:
                logger.warning(
                    f"[ArknightsUID·剿灭提醒] 推送失败: {exc.__class__.__name__}"
                )
                continue  # 发送失败不落周标记：重启后的下次触发还能补发
        # 无论是否需要提醒，本周已检查过就落标记（未拿满也只提醒一次）
        state = load_push_state()
        state[key] = {**state.get(key, {}), "campaign_week": week_key}
        save_push_state(state)
        await asyncio.sleep(random.uniform(1, 2))


# ==================== 基建订阅推送（无人机/贸易/换班/公招） ====================

BUILDING_TASK_NAME = "订阅基建推送"
_BUILDING_STATE_PATH: Path = MAIN_PATH / "building_push_state.json"
# 基建事件均为小时级节奏，15 分钟轮询足够；公招 9 小时档更是绰绰有余
BUILDING_POLL_MINUTES = 15
# 公招完成去重：同一槽位同一 finishTs 只提醒一次（滚动保留最近的）
_RECRUIT_SEEN_KEEP = 16


def _load_building_state() -> dict[str, dict]:
    try:
        state = json.loads(_BUILDING_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {}


def _save_building_state(state: dict[str, dict]) -> None:
    try:
        _BUILDING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_state_atomic(_BUILDING_STATE_PATH, state)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·基建推送] 状态写入失败: {exc.__class__.__name__}"
        )


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


def evaluate_recruit(
    done_slots: list[int], seen: list[int]
) -> tuple[list[int], bool]:
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
        "订阅基建",
        "基建推送",
        "订阅基建推送",
    ),
    block=True,
)
async def subscribe_building_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    await gs_subscribe.add_subscribe(
        "single", task_name=BUILDING_TASK_NAME, event=ev, uid=credential.uid
    )
    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot, ev,
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
    uid, index = _extract_target(ev.text)
    if uid or index:
        credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
        if isinstance(credential, str):
            return await _send_text(bot, ev, credential)
        await gs_subscribe.delete_subscribe(
            "single", task_name=BUILDING_TASK_NAME, event=ev, uid=credential.uid
        )
        state = _load_building_state()
        state.pop(_state_key(ev.bot_id, ev.user_id, credential.uid), None)
        _save_building_state(state)
        return await _send_text(bot, ev, "已取消该账号的基建推送。")

    await gs_subscribe.delete_subscribe(
        "single", task_name=BUILDING_TASK_NAME, event=ev
    )
    prefix = f"{ev.bot_id}|{ev.user_id}|"
    state = _load_building_state()
    for key in [k for k in state if k.startswith(prefix)]:
        state.pop(key, None)
    _save_building_state(state)
    await _send_text(bot, ev, "已取消全部基建推送。")


@scheduler.scheduled_job(
    "interval", minutes=BUILDING_POLL_MINUTES, id="ark_building_push"
)
async def check_building_push() -> None:
    """定时检查各订阅账号基建：无人机/贸易/换班/公招四类事件各推一次。"""
    datas = await gs_subscribe.get_subscribe(BUILDING_TASK_NAME)
    if not datas:
        return
    logger.info(f"[ArknightsUID·基建推送] 检查 {len(datas)} 个订阅..")

    changes: dict[str, dict] = {}
    for subscribe in datas:
        uid = str(subscribe.uid or "")
        if not uid:
            continue
        key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
        prev = _load_building_state().get(key, {})

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

        new_state = dict(prev)
        nickname = data.name or credential.nickname

        # 无人机回满（无 building 时跳过基建三类，公招仍可判定）
        building = parse_building(data)
        if building is not None:
            labor_full, labor_push = evaluate_full(
                building.labor_value, building.labor_max,
                bool(prev.get("labor")),
            )
            if labor_push:
                try:
                    await subscribe.send(format_labor_push_message(
                        nickname, building.labor_value, building.labor_max,
                        hide_uid(uid),
                    ))
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
                    await subscribe.send(format_trading_push_message(
                        nickname, full_rows, hide_uid(uid),
                    ))
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
                    await subscribe.send(format_tired_push_message(
                        nickname,
                        [(c.name, c.room, c.morale) for c in building.tired_chars],
                        hide_uid(uid),
                    ))
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
                await subscribe.send(format_recruit_push_message(
                    nickname, len(fresh_done), hide_uid(uid),
                ))
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
            changes[key] = new_state
        await asyncio.sleep(random.uniform(1, 2))

    if changes:
        fresh = _load_building_state()
        fresh.update(changes)
        _save_building_state(fresh)


# ==================== 自动签到订阅（每日 08:05） ====================

SIGN_TASK_NAME = "订阅签到推送"
_SIGN_STATE_PATH: Path = MAIN_PATH / "sign_push_state.json"


def _load_sign_state() -> dict[str, dict]:
    try:
        state = json.loads(_SIGN_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {}


def _save_sign_state(state: dict[str, dict]) -> None:
    try:
        _SIGN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _write_state_atomic(_SIGN_STATE_PATH, state)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·签到推送] 状态写入失败: {exc.__class__.__name__}"
        )


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
        "签到推送",
    ),
    block=True,
)
async def subscribe_sign_push(bot: Bot, ev: Event) -> None:
    uid, index = _extract_target(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    await gs_subscribe.add_subscribe(
        "single", task_name=SIGN_TASK_NAME, event=ev, uid=credential.uid
    )
    shown = credential.nickname or hide_uid(credential.uid)
    await _send_text(
        bot, ev,
        f"已订阅每日自动签到：{shown}\n"
        f"每天 08:05 自动完成森空岛签到并回报结果（今日已签则静默）。\n"
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
        credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
        if isinstance(credential, str):
            return await _send_text(bot, ev, credential)
        await gs_subscribe.delete_subscribe(
            "single", task_name=SIGN_TASK_NAME, event=ev, uid=credential.uid
        )
        state = _load_sign_state()
        state.pop(_state_key(ev.bot_id, ev.user_id, credential.uid), None)
        _save_sign_state(state)
        return await _send_text(bot, ev, "已取消该账号的自动签到。")

    await gs_subscribe.delete_subscribe(
        "single", task_name=SIGN_TASK_NAME, event=ev
    )
    prefix = f"{ev.bot_id}|{ev.user_id}|"
    state = _load_sign_state()
    for key in [k for k in state if k.startswith(prefix)]:
        state.pop(key, None)
    _save_sign_state(state)
    await _send_text(bot, ev, "已取消全部自动签到。")


@scheduler.scheduled_job("cron", hour=8, minute=5, id="ark_sign_push")
async def check_sign_push() -> None:
    """每天 08:05 自动签到订阅账号并回报（失败/成功推送，已签静默）。"""
    datas = await gs_subscribe.get_subscribe(SIGN_TASK_NAME)
    if not datas:
        return
    logger.info(f"[ArknightsUID·签到推送] 处理 {len(datas)} 个订阅..")

    from datetime import date

    today = date.today().isoformat()
    from ..utils.reply_safety import safe_external_message

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
                await subscribe.send(push)
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
