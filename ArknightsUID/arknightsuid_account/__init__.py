from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        ArkCredential,
        ark_attendance,
        fetch_player_info,
        hide_uid,
        resolve_accounts,
        resolve_credential,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.enduid.adapter import (
        ArkCredential,
        ark_attendance,
        fetch_player_info,
        hide_uid,
        resolve_accounts,
        resolve_credential,
    )

account_sv = SV("ArknightsUID账号", priority=5)


def _extract_uid(text: str) -> str:
    match = re.search(r"\d{5,}", text)
    if match is None:
        return ""
    return match.group(0)


def _extract_index(text: str) -> int:
    if _extract_uid(text):
        return 0
    normalized = text.strip().replace("第", "").replace("号", "")
    match = re.search(r"(?:^|\s|#)(\d{1,2})(?:\s*)$", normalized)
    if match is None:
        return 0
    return int(match.group(1))


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


def _value(data: Any, key: str) -> Any:
    if isinstance(data, dict) and key in data:
        return data[key]
    return None


def _path(data: Any, *keys: str) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _first_path(data: Any, paths: tuple[tuple[str, ...], ...]) -> Any:
    for path in paths:
        value = _path(data, *path)
        if value is not None and value != "":
            return value
    return None


def _find_first(data: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        for value in data.values():
            found = _find_first(value, keys, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for value in data[:20]:
            found = _find_first(value, keys, depth + 1)
            if found not in (None, ""):
                return found
    return None


def _format_accounts(
    uids: list[str],
    accounts: list[ArkCredential],
) -> str:
    lines = ["「明日方舟」EndUID 账号复用状态"]
    if accounts:
        for index, account in enumerate(accounts, start=1):
            nickname = account.nickname if account.nickname else "未记录昵称"
            status = "可用" if account.usable else "凭据失效/缺失"
            lines.append(
                f"{index}. {nickname} | UID {hide_uid(account.uid)} | {status}"
            )
        lines.append("多账号查询：ark理智 序号 / ark理智 UID；签到同理。")
    elif uids:
        lines.append("已找到 UID，但缺少对应凭据记录：")
        for index, uid in enumerate(uids, start=1):
            lines.append(f"{index}. UID {hide_uid(uid)}")
        lines.append("请重新使用 EndUID 登录森空岛。")
    else:
        lines.append("未找到明日方舟账号。")
    return "\n".join(lines)


def _format_api_error(action: str, response: dict[str, Any]) -> str | None:
    code = _value(response, "code")
    status = _value(response, "status")
    message = _value(response, "message")
    if message is None:
        message = _value(response, "msg")

    if code == 10001 and message:
        return f"{action}返回：{message}"
    if code not in (None, 0):
        return f"{action}失败：code={code}，message={message or '无'}"
    if status not in (None, 0):
        return f"{action}失败：status={status}，message={message or '无'}"
    return None


def _format_timestamp(value: Any) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _format_player_info(
    credential: ArkCredential,
    response: dict[str, Any],
) -> str:
    api_error = _format_api_error("查询森空岛玩家信息", response)
    if api_error is not None:
        return api_error

    data = _value(response, "data")
    if data is None:
        data = response

    nickname = _first_path(
        data,
        (
            ("status", "name"),
            ("status", "nickName"),
            ("player", "nickName"),
            ("player", "nickname"),
            ("base", "nickName"),
            ("base", "nickname"),
        ),
    )
    level = _first_path(
        data,
        (
            ("status", "level"),
            ("player", "level"),
            ("base", "level"),
        ),
    )
    progress = _first_path(
        data,
        (
            ("status", "mainStageProgress"),
            ("player", "mainStageProgress"),
            ("base", "mainStageProgress"),
        ),
    )
    ap_current = _first_path(
        data,
        (
            ("status", "ap", "current"),
            ("status", "ap", "currentAp"),
            ("ap", "current"),
            ("ap", "currentAp"),
        ),
    )
    ap_max = _first_path(
        data,
        (
            ("status", "ap", "max"),
            ("status", "ap", "maxAp"),
            ("ap", "max"),
            ("ap", "maxAp"),
        ),
    )
    recover_time = _first_path(
        data,
        (
            ("status", "ap", "completeRecoveryTime"),
            ("status", "ap", "completeRecoveryTimestamp"),
            ("ap", "completeRecoveryTime"),
            ("ap", "completeRecoveryTimestamp"),
        ),
    )

    shown_name = str(nickname or credential.nickname or "未记录昵称")
    lines = [f"「明日方舟」{shown_name}", f"UID：{hide_uid(credential.uid)}"]
    if level is not None:
        lines.append(f"等级：{level}")
    if progress is not None:
        lines.append(f"主线进度：{progress}")
    if ap_current is not None or ap_max is not None:
        cur = "?" if ap_current is None else str(ap_current)
        max_ap = "?" if ap_max is None else str(ap_max)
        lines.append(f"理智：{cur}/{max_ap}")
    recover = _format_timestamp(recover_time)
    if recover:
        lines.append(f"理智回满：{recover}")

    daily_current = _find_first(data, ("dailyCurrent", "dailyTaskCurrent"))
    daily_total = _find_first(data, ("dailyTotal", "dailyTaskTotal"))
    if daily_current is not None and daily_total is not None:
        lines.append(f"日常：{daily_current}/{daily_total}")

    weekly_current = _find_first(data, ("weeklyCurrent", "weeklyTaskCurrent"))
    weekly_total = _find_first(data, ("weeklyTotal", "weeklyTaskTotal"))
    if weekly_current is not None and weekly_total is not None:
        lines.append(f"周常：{weekly_current}/{weekly_total}")

    recruit = _find_first(data, ("recruit", "recruiting", "recruitStatus"))
    if isinstance(recruit, (str, int, float)):
        lines.append(f"公开招募：{recruit}")

    if len(lines) <= 2:
        lines.append("森空岛请求成功，但当前版本尚未识别详细字段。")
    return "\n".join(lines)


def _format_attendance(
    credential: ArkCredential,
    response: dict[str, Any],
) -> str:
    code = _value(response, "code")
    message = _value(response, "message")
    if message is None:
        message = _value(response, "msg")
    if code == 0:
        return f"「明日方舟」签到请求成功\nUID：{hide_uid(credential.uid)}\n{message or 'OK'}"
    return f"「明日方舟」签到失败\nUID：{hide_uid(credential.uid)}\ncode={code}，message={message or '无'}"


@account_sv.on_command(
    (
        "账号",
        "我的方舟",
        "方舟账号",
        "绑定列表",
        "方舟绑定",
    ),
    block=True,
)
async def show_ark_accounts(bot: Bot, ev: Event) -> None:
    result = await resolve_accounts(ev.user_id, ev.bot_id)
    if not result.installed:
        return await _send_text(bot, ev, result.reason)
    await _send_text(bot, ev, _format_accounts(result.uids, result.accounts))


@account_sv.on_command(
    (
        "理智",
        "每日",
        "日常",
        "方舟理智",
        "方舟每日",
        "方舟日常",
    ),
    block=True,
)
async def show_ark_daily(bot: Bot, ev: Event) -> None:
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    response = await fetch_player_info(credential)
    if isinstance(response, str):
        return await _send_text(bot, ev, response)
    await _send_text(bot, ev, _format_player_info(credential, response))


@account_sv.on_command(
    (
        "签到",
        "方舟签到",
    ),
    block=True,
)
async def send_ark_attendance(bot: Bot, ev: Event) -> None:
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    response = await ark_attendance(credential)
    if isinstance(response, str):
        return await _send_text(bot, ev, response)
    await _send_text(bot, ev, _format_attendance(credential, response))
