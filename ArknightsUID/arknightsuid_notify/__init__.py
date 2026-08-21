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
        effective_ap,
        fetch_cached_player_info,
        parse_building,
        parse_player_info,
        training_remaining_secs,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        resolve_credential,
    )
    from ArknightsUID.utils.player import (
        effective_ap,
        fetch_cached_player_info,
        parse_building,
        parse_player_info,
        training_remaining_secs,
    )

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
# 专精训练动辄数小时，30 分钟粒度的完成提醒误差可接受（player_info 5 分钟缓存）
TRAIN_POLL_MINUTES = 30


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

        credential = await resolve_credential(
            subscribe.user_id, subscribe.bot_id, uid=uid
        )
        if isinstance(credential, str):
            logger.warning(
                f"[ArknightsUID·训练推送] 账号不可用 user={subscribe.user_id}: {credential}"
            )
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

        should_push, new_state = evaluate_training(
            building.training_trainee_id,
            training_remaining_secs(building),
            prev_notified,
            prev_trainee,
        )
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
                new_state = {"notified": False, "trainee": prev_trainee}
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
