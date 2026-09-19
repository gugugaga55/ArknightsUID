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

from ArknightsUID.utils.account_switch import get_default_uid
from ArknightsUID.utils.database.models import ArkUser
from ArknightsUID.utils.enduid.adapter import (
    hide_uid_for,
    resolve_accounts,
)

GAME_TITLE = "「明日方舟」"

user_config_sv = SV("ArknightsUID用户配置", priority=5)

# 文本回复公共实现在 utils.card.send_text；保留模块级别名供调用点/测试 patch
from ArknightsUID.utils.card import send_text as _send_text  # noqa: E402


async def _resolve_bound_uid(ev: Event) -> str:
    """当前实际查询账号的 UID，与查询命令同口径（ark切换 的默认账号
    优先，否则首个可用账号）。

    此前用 ArkBind 首个绑定行（旧方法 get_bound_uid，已删）恒返回第一个
    UID：多账号下设置会写到别的账号行上，用户明确要求的隐藏在其后查询
    里不生效。
    """
    result = await resolve_accounts(ev.user_id, ev.bot_id)
    if not result.accounts:
        return ""
    default_uid = get_default_uid(ev.bot_id, ev.user_id)
    if default_uid:
        for account in result.accounts:
            if account.uid == default_uid:
                return default_uid
    for account in result.accounts:
        if account.usable:
            return account.uid
    return result.accounts[0].uid


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


# ---------------------------------------------------------------------------
# 状态卡背景图（ark设置背景 / ark设置全局背景）：EndUID「体力背景」同思路。
# 附图发送即设；「重置」清除。用户级按 bot+user 存储，全局级仅 superuser
# 可设（SV pm=1 注册级权限），优先级：用户上传/干员立绘 → 全局 → 随机立绘。
# 干员立绘：`ark设置背景 阿米娅`（跟随游戏内当前皮肤）或
# `ark设置背景 阿米娅 报童`（钉死指定时装皮肤），与上传图同槽位后写覆盖。
# ---------------------------------------------------------------------------
def _event_image_urls(ev: Event) -> list[str]:
    """消息里的图片 URL（公招截图同口径：image_list 优先，回退 image）。"""
    return [
        url
        for url in (ev.image_list or ([ev.image] if ev.image else []))
        if isinstance(url, str) and url.strip()
    ]


async def _save_bg_reply(bot: Bot, ev: Event, path, label: str, op_path=None) -> None:
    from ArknightsUID.utils.background import delete_bg, save_bg_from_url

    text = (ev.text or "").strip()
    if text in ("重置", "清除", "删除"):
        ok = delete_bg(path)
        if op_path is not None:
            # 用户槽位：干员立绘偏好与上传图同清
            ok = delete_bg(op_path) or ok
        await _send_text(
            bot,
            ev,
            f"{GAME_TITLE} 已清除{label}背景。"
            if ok
            else f"{GAME_TITLE} 没有已设置的{label}背景。",
        )
        return

    urls = _event_image_urls(ev)
    if not urls:
        await _send_text(
            bot,
            ev,
            f"用法：ark设置{label}背景 + 图片（与图片同条消息发送）；"
            "ark设置背景 阿米娅 可用干员皮肤立绘（追加皮肤名可指定时装）；"
            f"ark设置{label}背景 重置 可恢复默认。",
        )
        return
    if await save_bg_from_url(urls[0], path):
        if op_path is not None:
            # 同槽位后写覆盖：上传图生效时清掉干员立绘偏好
            delete_bg(op_path)
        await _send_text(
            bot, ev, f"{GAME_TITLE} {label}背景已更新（ark理智 / ark日常 生效）。"
        )
    else:
        await _send_text(bot, ev, f"{GAME_TITLE} 背景保存失败（仅支持图片）。")


async def _set_operator_bg(bot: Bot, ev: Event, text: str) -> None:
    """ark设置背景 <干员名> [皮肤名]：把状态卡背景设为干员（皮肤）立绘。"""
    from ArknightsUID.utils.background import save_op_bg
    from ArknightsUID.utils.gamedata.charinfo import load_char_index, resolve_query
    from ArknightsUID.utils.gamedata.skins import load_skin_index, match_skin

    # 皮肤名拆分：先整体当干员名解析（「纯烬艾雅法拉」这类全名优先），
    # 不中再按最后一个 空格/·/全角空格 拆成 干员 + 皮肤名
    query, skin_query = text, ""
    res = await resolve_query(query, bot_id=ev.bot_id, user_id=ev.user_id)
    if not res.unique:
        for sep in (" ", "·", "　"):
            if sep in text:
                head, tail = text.rsplit(sep, 1)
                res = await resolve_query(
                    head.strip(), bot_id=ev.bot_id, user_id=ev.user_id
                )
                if res.unique:
                    query, skin_query = head.strip(), tail.strip()
                break

    if not res.unique:
        if res.candidates:
            await _send_text(bot, ev, f"{GAME_TITLE} {res.ambiguous_message(query)}")
        elif not res.index_ready:
            await _send_text(bot, ev, f"{GAME_TITLE} 干员数据未就绪，请稍后重试。")
        else:
            await _send_text(bot, ev, f"{GAME_TITLE} 没有找到干员「{query}」。")
        return

    name = res.name or query
    skin_id, label = "", name
    if skin_query:
        catalog = await load_skin_index()
        skin_id, skin_name, all_names = match_skin(catalog, res.char_id, skin_query)
        if not skin_id:
            options = "、".join(all_names) if all_names else "（该干员没有时装皮肤）"
            await _send_text(
                bot,
                ev,
                f"{GAME_TITLE} 没有找到{name}的皮肤「{skin_query}」。可选：{options}",
            )
            return
        label = f"{name}·{skin_name}"

    rarity = 0
    index = await load_char_index()
    entry = index.get(res.char_id)
    if entry:
        rarity = int(entry.get("rarity") or 0)

    save_op_bg(
        ev.bot_id,
        ev.user_id,
        char_id=res.char_id,
        skin_id=skin_id,
        name=label,
        rarity=rarity,
    )
    await _send_text(
        bot,
        ev,
        f"{GAME_TITLE} 状态卡背景已设置为 {label}"
        "（ark理智 / ark日常 生效；ark设置背景 重置 可恢复）。",
    )


@user_config_sv.on_command(("设置背景", "设置状态背景"), block=True)
async def set_user_background(bot: Bot, ev: Event) -> None:
    """用户级状态卡背景（仅影响自己）：附图设图片，写干员名设皮肤立绘。"""
    from ArknightsUID.utils.background import user_bg_path, user_op_bg_path

    text = (ev.text or "").strip()
    if text and text not in ("重置", "清除", "删除") and not _event_image_urls(ev):
        await _set_operator_bg(bot, ev, text)
        return
    await _save_bg_reply(
        bot,
        ev,
        user_bg_path(ev.bot_id, ev.user_id),
        "",
        op_path=user_op_bg_path(ev.bot_id, ev.user_id),
    )


admin_config_sv = SV("ArknightsUID管理员配置", priority=5, pm=1)


@admin_config_sv.on_command(("设置全局背景", "设置全局状态背景"), block=True)
async def set_admin_background(bot: Bot, ev: Event) -> None:
    """全局默认状态卡背景（superuser；未设个人背景的用户生效）。"""
    from ArknightsUID.utils.background import admin_bg_path

    await _save_bg_reply(bot, ev, admin_bg_path(ev.bot_id), "全局")
