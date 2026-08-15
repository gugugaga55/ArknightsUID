"""抽卡记录 / 礼包码兑换命令（P3，鹰角 OAuth 链）。

命令（前缀 ark/方舟/明日方舟）：
- 抽卡记录 / 抽卡分析 [序号|UID]：增量拉取官方寻访记录并出统计卡
- 兑换码 <code>：官网礼包码兑换（纯文本回复）
"""
from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        resolve_credential,
    )
    from ArknightsUID.ArknightsUID.utils.hypergryph.client import (
        exchange_gift_code,
        login_gacha_chain,
    )
    from ArknightsUID.ArknightsUID.utils.hypergryph.gacha import (
        build_gacha_context,
        format_gacha_text,
        sync_gacha_logs,
    )
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        resolve_credential,
    )
    from ArknightsUID.utils.hypergryph.client import (
        exchange_gift_code,
        login_gacha_chain,
    )
    from ArknightsUID.utils.hypergryph.gacha import (
        build_gacha_context,
        format_gacha_text,
        sync_gacha_logs,
    )
    from ArknightsUID.version import ArknightsUID_version

gacha_sv = SV("ArknightsUID抽卡", priority=5)


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


def _gift_reply(response: dict) -> str:
    code = response.get("code")
    message = str(response.get("message") or response.get("msg") or "")
    if message:
        message = message[:120]
    if code == 0:
        return f"兑换码提交成功\n{message or 'OK'}"
    return f"兑换失败：code={code}，{message or '无详情'}"


@gacha_sv.on_command(
    (
        "抽卡记录",
        "抽卡分析",
        "寻访记录",
    ),
    block=True,
)
async def show_gacha_report(bot: Bot, ev: Event) -> None:
    uid = extract_uid(ev.text)
    index = extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    records, warning = await sync_gacha_logs(credential)
    if not records:
        return await _send_text(bot, ev, warning or "暂无抽卡记录。")

    nickname = credential.nickname or ""
    context = build_gacha_context(
        nickname, hide_uid(credential.uid), records, ArknightsUID_version
    )
    text = format_gacha_text(
        nickname, hide_uid(credential.uid), records, warning
    )
    if context is not None:
        return await send_card_or_text(
            bot, ev, "ark_gacha_card.html", context, text
        )
    await _send_text(bot, ev, text)


@gacha_sv.on_command(
    (
        "兑换码",
        "礼包码",
    ),
    block=True,
)
async def redeem_gift_code(bot: Bot, ev: Event) -> None:
    code = ev.text.strip().split()[0] if ev.text.strip() else ""
    if not code:
        return await _send_text(bot, ev, "用法：ark兑换码 <礼包码>")
    # 兑换码本身是敏感输入（防止误转发/回显），群聊不回显码值
    shown = code if not ev.group_id else "****"

    uid = extract_uid(ev.text.replace(code, "", 1))
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, 0)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    auth = await login_gacha_chain(credential)
    if isinstance(auth, str):
        return await _send_text(bot, ev, auth)

    try:
        response = await exchange_gift_code(credential, auth, code)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·兑换] 兑换请求异常: {exc.__class__.__name__}"
        )
        response = None
    if response is None:
        return await _send_text(bot, ev, f"兑换请求失败（{shown}），请稍后重试。")
    await _send_text(bot, ev, _gift_reply(response))
