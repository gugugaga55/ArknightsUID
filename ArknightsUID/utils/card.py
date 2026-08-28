"""出图命令的共享回退工具：有模板渲染出图，失败回退纯文本。

与 wiki 模块私有 _render_or_text 同模式，抽公共供 account/help/gacha
等业务模块复用（wiki 保持私有副本不动，避免 churn）。
"""
from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from jinja2 import TemplateNotFound


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
        suffix = (" " if ev.group_id else "") + text
        await bot.send(suffix, at_sender=bool(ev.group_id))
        for extra in extra_images:
            await bot.send(extra)
