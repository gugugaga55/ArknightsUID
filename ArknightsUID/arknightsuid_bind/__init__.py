from __future__ import annotations

import asyncio

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV
from gsuid_core.utils.cookie_manager.qrlogin import get_qrcode_base64

try:
    from ArknightsUID.ArknightsUID.utils.account_switch import clear_default_uid
    from ArknightsUID.ArknightsUID.utils.arg_utils import extract_uid
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import hide_uid
    from ArknightsUID.ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH
    from ArknightsUID.ArknightsUID.utils.skland import auth as ark_auth
    from ArknightsUID.ArknightsUID.utils.skland import store as ark_store
except ModuleNotFoundError:
    from ArknightsUID.utils.account_switch import clear_default_uid
    from ArknightsUID.utils.arg_utils import extract_uid
    from ArknightsUID.utils.enduid.adapter import hide_uid
    from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH
    from ArknightsUID.utils.skland import auth as ark_auth
    from ArknightsUID.utils.skland import store as ark_store

# area="DIRECT" 静默屏蔽群聊派发；handler 内 if ev.group_id 是额外保险
# （防止插件 area 被配成 ALL 绕过 SV 级限制）
bind_sv = SV("ArknightsUID绑定", area="DIRECT", priority=5)

_PRIVATE_ONLY_HINT = "绑定/登录涉及凭据安全，请私聊我使用本命令。"


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


def _format_bind_success(roles: list[dict]) -> str:
    lines = ["「明日方舟」登录成功，已写入账号："]
    for role in roles:
        lines.append(f"- {role['nickname']}（UID {hide_uid(role['role_id'])}）")
    lines.append("现在可使用 ark理智 / ark每日 / ark肉鸽 等命令。")
    return "\n".join(lines)


@bind_sv.on_command(("绑定", "绑定登录"), block=True)
async def bind_credential(bot: Bot, ev: Event) -> None:
    if ev.group_id:
        return await _send_text(bot, ev, _PRIVATE_ONLY_HINT)

    kind, value = ark_auth.parse_credential(ev.text)
    if kind is None:
        return await _send_text(
            bot, ev, "请输入 32 位 cred 或 24 位 token，例如：ark绑定 <你的cred>"
        )

    hg_token = ""
    skland_user_id = ""
    if kind == "token":
        cred_info, err = await ark_auth.get_cred_by_token(value)
        if err == ark_auth.CRED_ERR_UNAVAILABLE:
            return await _send_text(
                bot, ev, "当前服务暂时无法使用 token 登录，请改用 cred 或 ark扫码登录。"
            )
        if not cred_info or not cred_info.get("cred"):
            return await _send_text(bot, ev, "token 兑换 cred 失败，请检查 token 是否正确。")
        cred = cred_info["cred"]
        skland_user_id = cred_info.get("skland_user_id", "")
        hg_token = value  # token 即 hg_token，抽卡链要用
    else:
        cred = value

    roles, err = await ark_store.persist_ark_login(
        ev.user_id, ev.bot_id, ev.group_id, cred,
        hg_token=hg_token, skland_user_id=skland_user_id,
    )
    if err:
        return await _send_text(bot, ev, err)
    await _send_text(bot, ev, _format_bind_success(roles or []))


@bind_sv.on_fullmatch(("扫码登录", "扫码登陆"), block=True)
async def scan_login(bot: Bot, ev: Event) -> None:
    if ev.group_id:
        return await _send_text(bot, ev, _PRIVATE_ONLY_HINT)

    scan_id = await ark_auth.get_scan_id()
    if not scan_id:
        return await _send_text(bot, ev, "获取扫码 ID 失败，请稍后重试。")

    scan_url = f"hypergryph://scan_login?scanId={scan_id}"
    qr_path = CACHE_PATH / "qr" / f"ark_scan_{ev.user_id}.png"
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        qr_img = await get_qrcode_base64(scan_url, qr_path, ev.bot_id)
    except Exception:
        return await _send_text(bot, ev, "二维码生成失败，请稍后重试。")
    await bot.send(qr_img)
    await _send_text(bot, ev, "请使用森空岛 APP 扫码确认登录（100 秒内有效）……")

    # 轮询扫码状态：2s/次，最多 50 次（照搬 EndUID，不加长）
    scan_code = ""
    for _ in range(50):
        await asyncio.sleep(2)
        scan_code = await ark_auth.get_scan_status(scan_id) or ""
        if scan_code:
            break
    if not scan_code:
        return await _send_text(bot, ev, "扫码超时或未确认，请重新使用 ark扫码登录。")

    token_info = await ark_auth.get_token_by_scan_code(scan_code)
    if not token_info or not token_info.get("token"):
        return await _send_text(bot, ev, "扫码换取 token 失败，请重试。")

    cred_info, err = await ark_auth.get_cred_by_token(token_info["token"])
    if err == ark_auth.CRED_ERR_UNAVAILABLE:
        # 扫码链路最终同样要过 OAuth token 兑换，405 受限时重扫也无用
        return await _send_text(
            bot, ev, "当前服务暂时无法完成扫码登录，请稍后再试或改用 ark绑定 <cred>。"
        )
    if not cred_info or not cred_info.get("cred"):
        return await _send_text(bot, ev, "换取 cred 失败，请重试。")

    roles, err = await ark_store.persist_ark_login(
        ev.user_id, ev.bot_id, ev.group_id, cred_info["cred"],
        hg_token=token_info["token"],
        skland_user_id=cred_info.get("skland_user_id", ""),
    )
    if err:
        return await _send_text(bot, ev, err)
    await _send_text(bot, ev, _format_bind_success(roles or []))


@bind_sv.on_command(("删除登录", "解除绑定"), block=True)
async def remove_login(bot: Bot, ev: Event) -> None:
    if ev.group_id:
        return await _send_text(bot, ev, _PRIVATE_ONLY_HINT)

    uid = extract_uid(ev.text)
    if uid:
        await ark_store.remove_ark_login(ev.user_id, ev.bot_id, uid=uid)
        # 默认账号恰好指向被删 UID 时一并清除，防止 resolve 落到不存在的默认
        if clear_default_uid(ev.bot_id, ev.user_id, uid=uid):
            await _send_text(
                bot, ev,
                f"已删除 UID {hide_uid(uid)} 的明日方舟登录（该账号原为默认账号，默认已重置）。",
            )
        else:
            await _send_text(bot, ev, f"已删除 UID {hide_uid(uid)} 的明日方舟登录。")
    else:
        count = await ark_store.remove_ark_login(ev.user_id, ev.bot_id)
        clear_default_uid(ev.bot_id, ev.user_id)
        await _send_text(bot, ev, f"已删除 {count} 个明日方舟登录。")


@bind_sv.on_fullmatch(("查看绑定", "我的绑定"), block=True)
async def show_bind(bot: Bot, ev: Event) -> None:
    users = await ark_store.list_local_accounts(ev.user_id, ev.bot_id)
    if not users:
        return await _send_text(
            bot, ev, "本地未绑定明日方舟账号。请私聊使用 ark绑定 或 ark扫码登录。"
        )
    lines = ["「明日方舟」本地绑定账号："]
    for index, row in enumerate(users, start=1):
        nick = (row.nickname or "") or "未记录"
        usable = bool(row.cookie) and (row.cookie_status or "") != "无效"
        status = "可用" if usable else "凭据失效/缺失"
        lines.append(f"{index}. {nick} | UID {hide_uid(row.uid)} | {status}")
    await _send_text(bot, ev, "\n".join(lines))
