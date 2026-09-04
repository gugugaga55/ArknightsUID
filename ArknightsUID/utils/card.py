"""消息发送共享工具：纯文本回复 + 出图命令的回退。

send_text 是全部业务模块（wiki/account/cultivate/…）文本回复的单一实现
（此前 10 处逐字重复的私有 _send_text）；send_card_or_text 是"有模板渲染
出图、失败回退纯文本"的统一入口。各模块保留 `_send_text = send_text`
别名，供既有调用点与测试 patch 不动。
"""

from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from jinja2 import TemplateNotFound

from .enduid.adapter import ArkCredential, resolve_credential


async def send_text(bot: Bot, ev: Event, msg: str) -> None:
    """文本回复：群聊加前导空格并 @发送者（防空格被吞），私聊原样发送。"""
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


async def resolve_or_reply(
    bot: Bot, ev: Event, uid: str = "", index: int | None = None
) -> ArkCredential | None:
    """命令前置：解析凭据；失败时已把原因回复给用户并返回 None。

    收编各处理器里逐字重复 22 处的「resolve_credential → isinstance(str)
    → _send_text」四行样板。调用方约定：

        credential = await resolve_or_reply(bot, ev, uid, index)
        if credential is None:
            return

    resolve_credential 模块级导入：adapter 本身轻量（EndUID 探测惰性），
    且保持模块属性可被测试 patch。
    """
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        await send_text(bot, ev, credential)
        return None
    return credential


async def send_card_or_text(
    bot: Bot,
    ev: Event,
    template_name: str,
    context: dict,
    text: str,
    extra_images: list[bytes] | None = None,
) -> None:
    """渲染 template_name 出图；模板缺失/渲染失败回退发 text。

    render 模块（含 playwright/fastapi）惰性导入：纯文本路径不付重依赖成本，
    单测在无渲染引擎环境仍可走回退分支。出图直接 bot.send(bytes) 不带
    at_sender（图片无需 @，与文本路径不同，非 bug）。

    extra_images：随卡片一并追加发送的独立图片（如公告超长图，
    对齐 EndUID ann_detail_card 的长图单独发送）；bot.send(list) 会逐张发出。
    出图成功时拼在卡片后一起发；回退文本时先发文本再逐张发图。
    """
    img = None
    try:
        from .render import ark_templates, render_html

        img = await render_html(ark_templates, template_name, context)
    except TemplateNotFound:
        logger.debug(f"[ArknightsUID] 模板未就绪，回退纯文本：{template_name}")
        img = None
    except Exception:
        logger.exception(f"[ArknightsUID] 卡片渲染失败，回退纯文本：{template_name}")
        img = None
    extra_images = extra_images or []
    if img:
        if extra_images:
            await bot.send([img, *extra_images], at_sender=bool(ev.group_id))
        else:
            await bot.send(img, at_sender=bool(ev.group_id))
    else:
        await send_text(bot, ev, text)
        for extra in extra_images:
            await bot.send(extra)
