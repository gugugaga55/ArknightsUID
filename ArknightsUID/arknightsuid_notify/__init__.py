"""理智回满推送（订阅 + 定时轮询 + 到点提醒）。

- `ark订阅理智 [序号|UID]`：订阅当前默认账号（或指定账号）的理智回满提醒
- `ark取消订阅理智 [序号|UID]`：取消（不带参数取消全部）
- 定时轮询（10 分钟）：逐订阅账号拉 player_info（带 5 分钟磁盘缓存），
  推算当前理智，回满即推送一次；用后（理智降回上限以下）自动重新武装

推送状态（notified 标记）落 MAIN_PATH/sanity_push_state.json，
键为 f"{bot_id}|{user_id}|{uid}"。订阅本身存 gs_subscribe（single 模式，
uid = 游戏 UID，同用户可多账号分别订阅）。
"""
from __future__ import annotations

import asyncio
import json
import random
import re
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
        parse_player_info,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        resolve_credential,
    )
    from ArknightsUID.utils.player import (
        effective_ap,
        fetch_cached_player_info,
        parse_player_info,
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
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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

    state = load_push_state()
    dirty = False
    for subscribe in datas:
        uid = str(subscribe.uid or "")
        if not uid:
            continue
        key = _state_key(subscribe.bot_id, subscribe.user_id, uid)
        notified = bool(state.get(key, {}).get("notified"))

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
        if bool(is_full) != notified:
            state[key] = {"notified": is_full}
            dirty = True
        await asyncio.sleep(random.uniform(1, 2))

    if dirty:
        save_push_state(state)
