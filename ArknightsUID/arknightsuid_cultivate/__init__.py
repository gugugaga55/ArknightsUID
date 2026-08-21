"""养成计算命令（P3）：ark养成 <干员> —— 当前练度到毕业的材料缺口。

数据：森空岛 cultivate 接口 ×2 + player_info 进度 + ArknightsGameData 物品名。
UI：ark_cultivate.html（v3 浅色终端，已落地），渲染失败回退纯文本。
"""
from __future__ import annotations

import re

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.ArknightsUID.utils.cultivate import calculate_cultivation
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        resolve_credential,
    )
    from ArknightsUID.ArknightsUID.utils.gamedata.charinfo import (
        load_char_index,
        match_entries,
    )
    from ArknightsUID.ArknightsUID.utils.gamedata.itemicon import item_icon_b64
    from ArknightsUID.ArknightsUID.utils.player.context import char_portrait_b64
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.utils.cultivate import calculate_cultivation
    from ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        resolve_credential,
    )
    from ArknightsUID.utils.gamedata.charinfo import (
        load_char_index,
        match_entries,
    )
    from ArknightsUID.utils.gamedata.itemicon import item_icon_b64
    from ArknightsUID.utils.player.context import char_portrait_b64
    from ArknightsUID.version import ArknightsUID_version

cultivate_sv = SV("ArknightsUID养成", priority=5)

# 阿米娅三形态直查（与 account 模块同源的角色事实）
_AMIYA_FORM_IDS = {
    "近卫阿米娅": "char_1001_amiya2",
    "阿米娅近卫": "char_1001_amiya2",
    "医疗阿米娅": "char_1037_amiya3",
    "阿米娅医疗": "char_1037_amiya3",
}

# charId 兜底白名单：原词透传给森空岛接口前必须匹配（该接口查询串不编码，
# 非 ASCII/特殊字符会污染请求参数）
_CHAR_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


def _format_result(credential, result: dict) -> str:
    char = result["char"]
    gap = result["gap"]
    current = result["current"]
    lines = [
        f"「明日方舟」养成计算 · {char.display_name}",
        f"UID：{hide_uid(credential.uid)}",
        (
            f"当前：精{current['evolve_phase']} · 主技能{current['main_skill_lvl']} · "
            f"专精{'/'.join(str(m) for m in current['masteries']) or '-'}"
        ),
        "目标：精2 · 技能7 · 专精3 · 模组满级",
    ]
    if gap["gold_lack"]:
        lines.append(f"龙门币：还差 {gap['gold_lack']:,}")
    if gap["materials"]:
        lines.append(f"材料缺口（{gap['materials_total']} 种）：")
        for row in gap["materials"]:
            rarity = f"{row['rarity']} " if row["rarity"] else ""
            lines.append(
                f"  {rarity}{row['name']}：缺 {row['lack']}（需 {row['need']}，有 {row['owned']}）"
            )
    else:
        lines.append("材料缺口：无（已毕业或仓库充足）")
    for row in gap["exp_books"]:
        lines.append(f"  {row['name']}：缺 {row['lack']}")
    lines.append("注：不含等级经验曲线与龙门币等级消耗；刷图推荐暂未接入。")
    for warning in result.get("warnings", []):
        lines.append(f"⚠️ {warning}")
    return "\n".join(lines)


@cultivate_sv.on_command(
    (
        "养成计算",
        "养成",
    ),
    block=True,
)
async def show_cultivation(bot: Bot, ev: Event) -> None:
    query = ev.text.strip()
    uid = extract_uid(query)
    if uid:
        query = query.replace(uid, "", 1).strip()
    index = extract_index(query)
    if index:
        import re as _re

        query = _re.sub(r"\s(?:第)?\d{1,2}(?:号)?\s*$", "", query).strip()

    if not query:
        return await _send_text(
            bot, ev, "用法：ark养成 <干员名>（如 ark养成 银灰 / ark养成 42）"
        )

    # 用户自定义别名优先（精确整词替换；char_xxx 直查不受影响）
    try:
        from ..utils.user_alias import apply_user_alias

        query = await apply_user_alias(ev.bot_id, ev.user_id, query)
    except Exception:
        pass

    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    # 别名 → charId（阿米娅形态直查 → gamedata 索引 → 原词兜底）
    alias_loaded = False
    char_id = _AMIYA_FORM_IDS.get(query)
    if not char_id:
        try:
            alias_index = await load_char_index()
            alias_loaded = bool(alias_index)
            matched = match_entries(query, alias_index) if alias_index else []
            if len(matched) == 1:
                char_id = str(matched[0]["char_id"])
            elif len(matched) > 1:
                names = "、".join(str(m.get("name", "")) for m in matched[:8])
                return await _send_text(
                    bot, ev, f"「{query}」匹配到多名干员：{names}，请写全名。"
                )
        except Exception:
            char_id = ""
    if not char_id and not alias_loaded and not query.startswith("char_"):
        return await _send_text(
            bot, ev,
            "干员索引尚未就绪（首次使用需下载游戏数据），请稍后重试；"
            "也可直接用 charId 查询（如 ark养成 char_123_silverash）。",
        )
    if not char_id:
        # 原词兜底当 charId 试（如 char_123_silverash）；森空岛接口查询串
        # 刻意不编码，非白名单字符会污染请求参数，直接拒绝
        if not _CHAR_ID_RE.fullmatch(query):
            return await _send_text(
                bot, ev,
                f"未找到干员「{query}」。若要用 charId 查询，"
                "仅支持字母/数字/下划线（如 ark养成 char_123_silverash）。",
            )
        char_id = query

    result = await calculate_cultivation(credential, char_id)
    if isinstance(result, str):
        return await _send_text(bot, ev, result)

    # 图文并茂：材料/经验书/龙门币行挂上 PRTS 带框道具图标（失败降级空串）
    gap = result["gap"]
    for row in gap["materials"] + gap["exp_books"]:
        row["icon"] = await item_icon_b64(row["name"])
    if gap["gold_lack"]:
        gap["gold_icon"] = await item_icon_b64("龙门币")

    context = {
        "version": ArknightsUID_version,
        "name": result["char"].display_name,
        "portrait": await char_portrait_b64(char_id, result["char"].rarity_display),
        "result": result,
    }
    await send_card_or_text(
        bot, ev, "ark_cultivate.html", context, _format_result(credential, result)
    )
