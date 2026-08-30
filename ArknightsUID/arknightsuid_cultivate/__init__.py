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
        hide_uid_for,
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
        hide_uid_for,
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


def _format_result(credential, result: dict, shown_uid: str = "") -> str:
    char = result["char"]
    gap = result["gap"]
    current = result["current"]
    uid_line = shown_uid or hide_uid(credential.uid)
    lines = [
        f"「明日方舟」养成计算 · {char.display_name}",
        f"UID：{uid_line}",
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


async def _resolve_query_to_char_id(
    query: str, bot_id: str, user_id: str
) -> tuple[str, str]:
    """查询词 → charId（用户别名 → 阿米娅形态 → 索引 → charId 兜底）。

    返回 (char_id, 错误文案)，两者互斥（错误时 char_id 为空串）。
    """
    # 用户自定义别名优先（精确整词替换；char_xxx 直查不受影响）
    try:
        from ..utils.user_alias import apply_user_alias

        query = await apply_user_alias(bot_id, user_id, query)
    except Exception:
        pass

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
                return "", f"「{query}」匹配到多名干员：{names}，请写全名。"
        except Exception:
            char_id = ""
    if not char_id and not alias_loaded and not query.startswith("char_"):
        return "", (
            "干员索引尚未就绪（首次使用需下载游戏数据），请稍后重试；"
            "也可直接用 charId 查询（如 ark养成 char_123_silverash）。"
        )
    if not char_id:
        # 原词兜底当 charId 试（如 char_123_silverash）；森空岛接口查询串
        # 刻意不编码，非白名单字符会污染请求参数，直接拒绝
        if not _CHAR_ID_RE.fullmatch(query):
            return "", (
                f"未找到干员「{query}」。若要用 charId 查询，"
                "仅支持字母/数字/下划线（如 ark养成 char_123_silverash）。"
            )
        char_id = query
    return char_id, ""


# 注册顺序约束：养成计划 必须先于「养成计算/养成」注册（startswith 匹配下
# 短词「养成」会抢走「ark养成计划」，实机踩坑：计划请求曾落到养成计算）。


@cultivate_sv.on_command(
    (
        "养成计划",
        "养成规划",
        "多干员养成",
    ),
    block=True,
)
async def show_cultivation_plan(bot: Bot, ev: Event) -> None:
    """ark养成计划 <干员名>… → 多干员到毕业的合并材料缺口 + 刷图建议。"""
    text = ev.text.strip()
    uid = extract_uid(text)
    if uid:
        text = text.replace(uid, "", 1).strip()
    index = extract_index(text)
    if index:
        import re as _re

        text = _re.sub(r"\s(?:第)?\d{1,2}(?:号)?\s*$", "", text).strip()

    names = _split_plan_names(text)
    if not names:
        return await _send_text(
            bot, ev,
            "用法：ark养成计划 <干员名> [干员名 …]（最多 6 名，空格/顿号分隔，"
            "如 ark养成计划 银灰 史尔特尔 42）",
        )
    if len(names) > _PLAN_MAX_CHARS:
        names = names[:_PLAN_MAX_CHARS]

    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    results: list[dict] = []
    unresolved: list[str] = []
    for name in names:
        char_id, resolve_error = await _resolve_query_to_char_id(
            name, ev.bot_id, ev.user_id
        )
        if not char_id:
            unresolved.append(f"{name}（{resolve_error}）")
            continue
        result = await calculate_cultivation(
            credential, char_id, max_rows=999, keep_zero=True
        )
        if isinstance(result, str):
            unresolved.append(f"{name}（{result}）")
            continue
        results.append(result)

    if not results:
        return await _send_text(
            bot, ev, "没有可计算的干员：\n" + "\n".join(unresolved)
        )

    from ..utils.cultivate import merge_cultivation_results

    merged = merge_cultivation_results(results)
    if unresolved:
        merged["warnings"] = list(merged.get("warnings") or []) + [
            f"未计入：{'；'.join(unresolved)}"
        ]

    farm_rows = await _build_farm_rows(merged, credential)

    gap = merged
    for row in gap["materials"] + gap["exp_books"]:
        row["icon"] = await item_icon_b64(row["name"])
    if gap["gold_lack"]:
        gap["gold_icon"] = await item_icon_b64("龙门币")

    shown_uid = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    context = {
        "version": ArknightsUID_version,
        "uid": shown_uid,
        "merged": merged,
        "farm_rows": farm_rows,
    }
    await send_card_or_text(
        bot, ev, "ark_cultivate_plan.html", context,
        _format_plan_result(credential, merged, farm_rows, shown_uid),
    )


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

    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    char_id, resolve_error = await _resolve_query_to_char_id(
        query, ev.bot_id, ev.user_id
    )
    if not char_id:
        return await _send_text(bot, ev, resolve_error)

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
        bot, ev, "ark_cultivate.html", context,
        _format_result(
            credential, result,
            await hide_uid_for(credential.uid, ev.user_id, ev.bot_id),
        ),
    )


# ---------------------------------------------------------------------------
# 多干员合并养成规划（ark养成计划）：<干员名>… → 合并缺口 + 刷图建议
# ---------------------------------------------------------------------------

_PLAN_MAX_CHARS = 6


def _split_plan_names(text: str) -> list[str]:
    """干员名切分：空白 / 、 / ，/, 分隔，去空保序去重。"""
    import re as _re

    parts = _re.split(r"[\s、，,]+", (text or "").strip())
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return seen


def _format_plan_result(
    credential, merged: dict, farm_rows: list[dict], shown_uid: str = ""
) -> str:
    names = "、".join(str(c["name"]) for c in merged["chars"])
    uid_line = shown_uid or hide_uid(credential.uid)
    lines = [
        "「明日方舟」养成规划（合并缺口）",
        f"UID：{uid_line}",
        f"干员：{names}",
        "目标：精2 · 技能7 · 专精3 · 模组满级（各干员当前进度到毕业）",
    ]
    if merged["gold_lack"]:
        lines.append(f"龙门币：共差 {merged['gold_lack']:,}")
    if merged["materials"]:
        lines.append(f"材料缺口（{merged['materials_total']} 种，合并后）：")
        for row in merged["materials"][:12]:
            lines.append(
                f"  {row['rarity'] + ' ' if row['rarity'] else ''}{row['name']}："
                f"缺 {row['lack']}（需 {row['need']}，有 {row['owned']}）"
            )
    else:
        lines.append("材料缺口：无（均已毕业或仓库充足）")
    for row in merged["exp_books"]:
        lines.append(f"  {row['name']}：缺 {row['lack']}")
    if farm_rows:
        lines.append("刷图建议（缺口的直刷 vs 合成，前 5 种）：")
        for row in farm_rows:
            lines.append(f"  {row['name']}：{row['advice']}")
    for warning in merged.get("warnings", [])[:4]:
        lines.append(f"⚠️ {warning}")
    lines.append("注：不含等级经验曲线；仓库持有为各干员合并计算只扣一次。")
    return "\n".join(lines)


def _farm_advice_text(result: dict) -> str:
    """craft_vs_farm 结果 → 一句话建议（把数 = 缺口 ÷ 每把期望件数）。"""
    farm = result.get("farm")
    farm_sanity = result.get("farm_sanity")
    craft_sanity = result.get("craft_sanity")
    if farm is None and craft_sanity is None:
        return "无直刷/合成数据"
    if farm is None and craft_sanity is not None:
        return f"不可直刷，合成（叶子直刷约 {craft_sanity} 理智）"
    stage = str(farm.get("stage_code") or "?")
    rate = float(farm.get("rate") or 0)
    need = int(result.get("need") or 0)
    runs = None
    if rate > 0 and need > 0:
        import math as _math

        runs = _math.ceil(need / rate)
    direct = f"直刷 {stage}"
    if runs is not None and farm_sanity is not None:
        direct += f"（约 {runs} 把，{farm_sanity} 理智）"
    elif farm_sanity is not None:
        direct += f"（约 {farm_sanity} 理智）"
    if craft_sanity is not None and farm_sanity is not None:
        better = "合成更省" if craft_sanity < farm_sanity else "直刷更省"
        return f"{direct}；合成（叶子约 {craft_sanity} 理智）→ {better}"
    return direct


async def _build_farm_rows(
    merged: dict, credential
) -> list[dict]:
    """对缺口前 5 材料生成刷图建议（一图流/配方不可用时静默跳过）。"""
    targets = merged["materials"][:5]
    if not targets:
        return []
    try:
        from ArknightsUID.ArknightsUID.utils.gamedata.craft import (
            craft_vs_farm,
            load_formulas,
        )
        from ArknightsUID.ArknightsUID.utils.yituliu import (
            fetch_drop_matrix,
            fetch_stage_info,
            latest_matrix_by_item,
        )
        from ArknightsUID.ArknightsUID.utils.gamedata.charinfo import load_item_index
    except ModuleNotFoundError:
        from ArknightsUID.utils.gamedata.craft import craft_vs_farm, load_formulas
        from ArknightsUID.utils.yituliu import (
            fetch_drop_matrix,
            fetch_stage_info,
            latest_matrix_by_item,
        )
        from ArknightsUID.utils.gamedata.charinfo import load_item_index

    item_index = await load_item_index()
    matrix = await fetch_drop_matrix()
    stages = await fetch_stage_info()
    if not item_index or not matrix or not stages:
        return []
    matrix_by_item = latest_matrix_by_item(matrix)
    stage_meta = {str(s.get("stageId", "")): s for s in stages}
    formulas = await load_formulas()

    rows: list[dict] = []
    for row in targets:
        item_id = str(row.get("id") or "")
        if not item_id:
            continue
        result = craft_vs_farm(
            item_id, int(row.get("lack") or 1), formulas, item_index,
            matrix_by_item, stage_meta,
        )
        rows.append(
            {
                "name": result["name"],
                "lack": row.get("lack"),
                "advice": _farm_advice_text(result),
            }
        )
    return rows
