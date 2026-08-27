"""用户级参数配置（对齐 EndUID end_user_config）。

当前支持：
- `ark设置隐藏UID` / `ark取消隐藏UID`：写入 ArkUser.hide_uid_self_value
  （"on" 强制隐藏 / "off" 强制不隐藏 / "" 跟随全局 HideUid 配置），
  对 ark账号 / ark理智 / ark日常 / ark基建 等查询输出即时生效。
"""
from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.database.models import (
        ArkBind,
        ArkUser,
    )
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        hide_uid_for,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.database.models import (
        ArkBind,
        ArkUser,
    )
    from ArknightsUID.utils.enduid.adapter import (
        hide_uid_for,
    )

GAME_TITLE = "「明日方舟」"

user_config_sv = SV("ArknightsUID用户配置", priority=5)


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


async def _resolve_bound_uid(ev: Event) -> str:
    return await ArkBind.get_bound_uid(ev.user_id, ev.bot_id) or ""


async def _set_hide_uid_pref(ev: Event, uid: str, value: str) -> str:
    """value: "on"/"off"。要求该 uid 存在本地 ArkUser 行（ark绑定 登录过）。"""
    rows = await ArkUser.select_rows(uid=uid, user_id=ev.user_id, bot_id=ev.bot_id)
    if not rows:
        return (
            f"{GAME_TITLE} 该账号未通过「ark绑定」登录，"
            "用户级设置仅在本地登录的账号上生效。"
        )
    updated = await ArkUser.update_data_by_data(
        select_data={
            "user_id": ev.user_id,
            "bot_id": ev.bot_id,
            "uid": uid,
        },
        update_data={"hide_uid_self_value": value},
    )
    # 基类约定：成功返回 0，失败返回 -1
    if updated != 0:
        return f"{GAME_TITLE} 设置失败，请稍后重试。"
    action = "已开启" if value == "on" else "已关闭"
    shown = await hide_uid_for(uid, ev.user_id, ev.bot_id)
    return f"{GAME_TITLE} {action}隐藏UID！\nUID[{shown}]"


@user_config_sv.on_command(("设置隐藏UID", "设置隐藏uid"), block=True)
async def set_hide_uid(bot: Bot, ev: Event) -> None:
    uid = await _resolve_bound_uid(ev)
    if not uid:
        return await _send_text(
            bot, ev, f"{GAME_TITLE} 未绑定账号，请先使用「ark绑定」登录。"
        )
    try:
        msg = await _set_hide_uid_pref(ev, uid, "on")
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·用户配置] 设置隐藏UID失败: {exc.__class__.__name__}"
        )
        msg = f"{GAME_TITLE} 设置失败，请稍后重试。"
    await _send_text(bot, ev, msg)


@user_config_sv.on_command(
    ("取消隐藏UID", "取消隐藏uid", "设置取消隐藏UID", "设置取消隐藏uid"),
    block=True,
)
async def unset_hide_uid(bot: Bot, ev: Event) -> None:
    uid = await _resolve_bound_uid(ev)
    if not uid:
        return await _send_text(
            bot, ev, f"{GAME_TITLE} 未绑定账号，请先使用「ark绑定」登录。"
        )
    try:
        msg = await _set_hide_uid_pref(ev, uid, "off")
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·用户配置] 取消隐藏UID失败: {exc.__class__.__name__}"
        )
        msg = f"{GAME_TITLE} 设置失败，请稍后重试。"
    await _send_text(bot, ev, msg)
