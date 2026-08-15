from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.ArknightsUID.utils.dict_utils import path
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        fetch_player_info,
        resolve_credential,
    )
    from ArknightsUID.ArknightsUID.utils.rogue import flatten_rogue, parse_rogue
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.utils.dict_utils import path
    from ArknightsUID.utils.enduid.adapter import (
        fetch_player_info,
        resolve_credential,
    )
    from ArknightsUID.utils.rogue import flatten_rogue, parse_rogue
    from ArknightsUID.version import ArknightsUID_version

rogue_sv = SV("ArknightsUID肉鸽", priority=5)

# 集成战略主题名（rogue_id → 官方主题名，客观游戏数据）
ROGUE_THEME_NAMES = {
    "rogue_1": "刻俄柏的灰蕈迷境",
    "rogue_2": "傀影与猩红孤钻",
    "rogue_3": "水月与深蓝之树",
    "rogue_4": "探索者的银凇止境",
    "rogue_5": "萨卡兹的无终奇语",
}


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


def _format_rogue(response: object) -> str:
    """把 player_info 信封里的 rogue 字段格式化为纯文本。

    records 精确展示（rogueId/通关/等级/收藏/蜡烛/奖章，来自真实 dump）；
    无 records 时用 flatten_rogue 通用遍历兜底。
    """
    if isinstance(response, str):
        # adapter 失败时返回中文错误串
        return response
    rogue = path(response, "data", "rogue")
    if not isinstance(rogue, dict) or not rogue:
        return "未识别到肉鸽（集成战略）详细字段（等待数据补全）。"
    info = parse_rogue(rogue)
    lines = ["「明日方舟」集成战略（肉鸽）"]
    if info and info.records:
        for r in info.records:
            lines.append(
                f"[{r.rogue_id}] 通关 {r.clear_time} | 声望 {r.bp_level} | "
                f"收藏 {r.relic_cnt} | 蜡烛 {r.bank.current}/{r.bank.record} | "
                f"奖章 {r.medal.current}/{r.medal.total}"
            )
        return "\n".join(lines)
    # 无 records 但有 rogue dict：通用遍历兜底
    flat = flatten_rogue(rogue)
    if not flat:
        lines.append("已获取肉鸽数据，但当前版本未识别可展示字段。")
    else:
        for label, val in flat[:25]:
            lines.append(f"{label}：{val}")
    return "\n".join(lines)


def _build_rogue_context(response: object) -> dict | None:
    """把 player_info 信封组装成 ark_rogue.html 的渲染上下文。

    无 records 时返回 None（调用方回退纯文本）。
    """
    rogue = path(response, "data", "rogue")
    if not isinstance(rogue, dict) or not rogue:
        return None
    info = parse_rogue(rogue)
    if not info or not info.records:
        return None
    name = path(response, "data", "status", "nickName") or "博士"
    uid = path(response, "data", "status", "uid") or ""
    return {
        "name": name,
        "uid": uid,
        "records": [
            {
                "rogue_id": r.rogue_id,
                "name": ROGUE_THEME_NAMES.get(r.rogue_id, r.rogue_id),
                "clear_time": r.clear_time,
                "bp_level": r.bp_level,
                "relic_cnt": r.relic_cnt,
                "bank_current": r.bank.current,
                "bank_record": r.bank.record,
                "medal_current": r.medal.current,
                "medal_total": r.medal.total,
            }
            for r in info.records
        ],
        "version": ArknightsUID_version,
    }


@rogue_sv.on_command(("肉鸽", "集成战略"), block=True)
async def send_rogue_info(bot: Bot, ev: Event) -> None:
    uid = extract_uid(ev.text)
    index = extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid=uid, index=index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)
    response = await fetch_player_info(credential)

    # 前端卡片路径：模板缺失/渲染失败时回退纯文本（与 wiki 模块同模式）
    context = _build_rogue_context(response)
    if context is not None:
        img = None
        try:
            from ..utils.render import ark_templates, render_html

            img = await render_html(ark_templates, "ark_rogue.html", context)
        except Exception:
            img = None
        if img:
            return await bot.send(img, at_sender=bool(ev.group_id))

    await _send_text(bot, ev, _format_rogue(response))
