"""养成计算命令（P3）：ark养成 <干员> —— 当前练度到毕业的材料缺口。

数据：森空岛 cultivate 接口 ×2 + player_info 进度 + ArknightsGameData 物品名。
UI：ark_cultivate.html（v3 浅色终端，已落地），渲染失败回退纯文本。
"""

from __future__ import annotations

import re

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ArknightsUID.utils.arg_utils import split_account
from ArknightsUID.utils.card import send_card_or_text
from ArknightsUID.utils.cultivate import (
    CultivationGoal,
    calculate_cultivation,
    merge_cultivation_results,
    parse_cultivation_goal,
)
from ArknightsUID.utils.enduid.adapter import (
    ArkCredential,
    hide_uid,
    hide_uid_for,
)
from ArknightsUID.utils.gamedata.charinfo import resolve_query
from ArknightsUID.utils.gamedata.itemicon import item_icon_b64
from ArknightsUID.utils.image import gather_b64
from ArknightsUID.utils.player.context import char_avatar_b64, char_portrait_b64
from ArknightsUID.version import ArknightsUID_version

cultivate_sv = SV("ArknightsUID养成", priority=5)

_CHAR_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


# 文本回复公共实现在 utils.card.send_text；保留模块级别名供调用点/测试 patch
from ArknightsUID.utils.card import send_text as _send_text  # noqa: E402

# 凭据解析 + 失败即回复的统一入口（原各处四行样板）
from ArknightsUID.utils.card import resolve_or_reply  # noqa: E402


def _format_result(
    credential, result: dict, shown_uid: str = "", farm_rows: list[dict] | None = None
) -> str:
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
        f"目标：{result.get('goal_label', '毕业')}",
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
        reached = "已毕业" if result.get("full_goal", True) else "已达目标"
        lines.append(f"材料缺口：无（{reached}或仓库充足）")
    for row in gap["exp_books"]:
        lines.append(f"  {row['name']}：缺 {row['lack']}")
    if farm_rows:
        lines.append("刷图建议（各材料单独估算）：")
        for row in farm_rows:
            lines.append(f"  {row['name']}：{row['advice']}")
    lines.append("不含干员等级经验与升级龙门币。")
    for warning in result.get("warnings", []):
        lines.append(f"⚠️ {warning}")
    return "\n".join(lines)


async def _resolve_query_to_char_id(
    query: str, bot_id: str, user_id: str
) -> tuple[str, str]:
    """查询词 → charId（统一管线见 charinfo.resolve_query）。

    返回 (char_id, 错误文案)，两者互斥（错误时 char_id 为空串）。
    """
    res = await resolve_query(query, bot_id=bot_id, user_id=user_id)
    if res.unique:
        return res.char_id, ""
    if res.candidates:
        return "", res.ambiguous_message(query)
    if not res.index_ready and not query.startswith("char_"):
        return "", (
            "干员索引尚未就绪（首次使用需下载游戏数据），请稍后重试；"
            "也可直接用 charId 查询（如 ark养成 char_123_silverash）。"
        )
    # 原词兜底当 charId 试（如 char_123_silverash）；森空岛接口查询串
    # 刻意不编码，非白名单字符会污染请求参数，直接拒绝
    if not _CHAR_ID_RE.fullmatch(query):
        return "", (
            f"未找到干员「{query}」。若要用 charId 查询，"
            "仅支持字母/数字/下划线（如 ark养成 char_123_silverash）。"
        )
    return query, ""


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
    await _show_cultivation(bot, ev, plan_view=True)


@cultivate_sv.on_command(("养成计算", "养成", "yc"), block=True)
async def show_cultivation(bot: Bot, ev: Event) -> None:
    await _show_cultivation(bot, ev)


async def _show_cultivation(bot: Bot, ev: Event, *, plan_view: bool = False) -> None:
    try:
        text, uid, index = split_account(ev.text or "", legacy_index=False)
        if not uid and not index:
            text, index = _split_index(text)
        targets = _parse_targets(text)
    except ValueError as exc:
        return await _send_text(bot, ev, str(exc))
    if not targets:
        return await _send_text(
            bot, ev, "用法：ark养成 银灰 [精2] / ark养成 银灰 伊内丝。"
            "每人指定不同目标时用分号分隔，例如：ark养成 银灰 精2；伊内丝 3技能 专3。",
        )
    if len(targets) > 1 and not plan_view and all(goal is None for _, goal in targets):
        if not re.search(r"[、，,；;]", text):
            # 旧单人入口允许包含空格的个人别名，优先判定整串。
            whole = await resolve_query(text, bot_id=ev.bot_id, user_id=ev.user_id)
            if whole.unique:
                targets = [(text, None)]
    if len(targets) > _PLAN_MAX_CHARS:
        return await _send_text(bot, ev, f"一次最多计算 {_PLAN_MAX_CHARS} 名干员。")

    credential = await resolve_or_reply(bot, ev, uid, index)
    if credential is None:
        return
    plan_view = plan_view or len(targets) > 1
    results: list[dict] = []
    unresolved: list[str] = []
    seen: dict[str, CultivationGoal | None] = {}
    inventory_response = None

    for name, goal in targets:
        char_id, error = await _resolve_query_to_char_id(name, ev.bot_id, ev.user_id)
        if not char_id:
            unresolved.append(f"{name}（{error}）")
            continue
        if char_id in seen:
            if seen[char_id] != goal:
                return await _send_text(bot, ev, f"「{name}」有两个不同目标，请只保留一个。")
            continue
        seen[char_id] = goal
        first_in_batch = inventory_response is None
        if plan_view and first_in_batch:
            from ArknightsUID.utils.enduid.adapter import fetch_cultivate_player

            inventory_response = await fetch_cultivate_player(credential)
        result = await calculate_cultivation(
            credential, char_id, keep_zero=plan_view,
            inventory_response=inventory_response,
            force_refresh=first_in_batch, goal=goal,
        )
        if isinstance(result, str):
            unresolved.append(f"{name}（{result}）")
        else:
            results.append(result)
    if not results:
        return await _send_text(bot, ev, "没有可计算的干员：\n" + "\n".join(unresolved))

    if plan_view:
        await _send_cultivation_plan(bot, ev, credential, results, unresolved)
    else:
        await _send_single_cultivation(bot, ev, credential, results[0])


def _parse_targets(text: str) -> list[tuple[str, CultivationGoal | None]]:
    targets = []
    for part in re.split(r"[、，,；;]+", text):
        if not part.strip():
            continue
        name, goal = parse_cultivation_goal(part)
        if not name:
            raise ValueError("请在目标前写干员名，例如「银灰 精2」。")
        if goal is not None:
            targets.append((name, goal))
        else:
            targets.extend((name, None) for name in _split_plan_names(name))
    return list(dict.fromkeys(targets))


async def _send_cultivation_plan(
    bot: Bot, ev: Event, credential: ArkCredential,
    results: list[dict], unresolved: list[str],
) -> None:
    merged = merge_cultivation_results(results)
    if unresolved:
        merged["warnings"].append(f"未计入：{'；'.join(unresolved)}")
    _add_farm_inventory(merged, results)
    farm_rows = await _build_farm_rows(merged, credential)
    await _apply_material_icons(merged, farm_rows)
    avatars = await gather_b64(
        char_avatar_b64, [str(c.get("char_id") or "") for c in merged["chars"]]
    )
    for row, avatar in zip(merged["chars"], avatars):
        row["avatar"] = avatar
    uid = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    context = {
        "version": ArknightsUID_version, "uid": uid, "merged": merged, "farm_rows": farm_rows,
    }
    await send_card_or_text(
        bot, ev, "ark_cultivate_plan.html", context,
        _format_plan_result(credential, merged, farm_rows, uid),
    )


async def _send_single_cultivation(
    bot: Bot, ev: Event, credential: ArkCredential, result: dict,
) -> None:
    char = result["char"]
    gap = result["gap"]
    _add_farm_inventory(gap, [result])
    farm_rows = await _build_farm_rows(gap, credential)
    await _apply_material_icons(gap, farm_rows)
    context = {
        "version": ArknightsUID_version,
        "name": char.display_name,
        "portrait": await char_portrait_b64(char.char_id, char.rarity_display),
        "result": result,
        "farm_rows": farm_rows,
    }
    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    await send_card_or_text(
        bot, ev, "ark_cultivate.html", context, _format_result(credential, result, shown, farm_rows),
    )


async def _apply_material_icons(gap: dict, farm_rows: list[dict]) -> None:
    rows = gap["materials"] + gap["exp_books"] + farm_rows
    icons = await gather_b64(item_icon_b64, [str(row["name"]) for row in rows])
    for row, icon in zip(rows, icons):
        row["icon"] = icon
    if gap["gold_lack"]:
        gap["gold_icon"] = await item_icon_b64("龙门币")


def _add_farm_inventory(gap: dict, results: list[dict]) -> None:
    # 先为全部养成目标预留直用材料，避免再把它们算成可合成的库存。
    inventory = results[0].get("inventory")
    if inventory is None:
        return
    remaining = inventory.copy()
    for result in results:
        for item_id, quantity in result.get("need", {}).items():
            remaining[item_id] = max(0, remaining.get(item_id, 0) - quantity)
    gap["farm_inventory"] = remaining


# ---------------------------------------------------------------------------
# 多干员合并养成规划（ark养成计划）：<干员名>… → 合并缺口 + 刷图建议
# ---------------------------------------------------------------------------

_PLAN_MAX_CHARS = 6

# 尾部账号序号：带「第/号」标记，或一位数字（1-9）。两位裸数字
# （如 42=史尔特尔，帮助文案明示支持）在干员语境里是数字别名，
# 不当序号——否则 resolve_credential(index=N) 必然报"未找到第 N
# 个账号"（审计 #39）。要多位序号请用「第 N / N号」显式标记。
_TRAILING_INDEX_RE = re.compile(r"\s(第?\d{1,2}号?)\s*$")


def _split_index(text: str) -> tuple[str, int]:
    """拆出尾部账号序号 → (剩余文本, 序号)；无序号返回 (原文, 0)。"""
    match = _TRAILING_INDEX_RE.search(text or "")
    if not match:
        return text, 0
    token = match.group(1)
    num = token.replace("第", "").replace("号", "")
    if len(num) > 1 and "第" not in token and "号" not in token:
        return text, 0
    return text[: match.start()].strip(), int(num)


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
        *[f"  {c['name']}：{c.get('goal_label', '毕业')}" for c in merged["chars"]],
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
        lines.append("材料缺口：无（均已达目标或仓库充足）")
    for row in merged["exp_books"]:
        lines.append(f"  {row['name']}：缺 {row['lack']}")
    if farm_rows:
        lines.append("刷图建议（缺口前 5 种，各材料单独估算）：")
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
    craftable = result.get("stock_craftable")
    if craftable is not None:
        missing = "、".join(
            f"{row['name']}×{row['count']}" + (f"刷 {row['stage']}" if row["stage"] else "")
            for row in result["craft_missing_rows"][:3]
        )
        if len(result["craft_missing_rows"]) > 3:
            missing += "等"
        estimate = f"（约 {craft_sanity:g} 理智）" if craft_sanity is not None else "（部分缺掉率）"
        advice = f"库存子材料可合 {craftable}/{result['need']} 件"
        if missing:
            advice += f"；合成还缺 {missing}{estimate}"
        if farm and (craft_sanity is None or farm_sanity <= craft_sanity):
            advice += f"；也可直刷 {farm['stage_code']}（约 {farm_sanity:g} 理智）"
        return advice
    if farm is None and craft_sanity is None:
        return "有加工站配方，部分子材料缺掉率" if result.get("craft") else "无直刷/合成数据"
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


async def _build_farm_rows(merged: dict, credential) -> list[dict]:
    """对缺口前 5 材料生成刷图建议（一图流/配方不可用时静默跳过）。"""
    targets = merged["materials"][:5]
    if not targets:
        return []
    from ArknightsUID.utils.gamedata.craft import (
        craft_vs_farm,
        load_formulas,
    )
    from ArknightsUID.utils.yituliu import (
        fetch_drop_matrix,
        fetch_stage_info,
        latest_matrix_by_item,
    )
    from ArknightsUID.utils.gamedata.charinfo import load_item_index

    item_index = await load_item_index()
    matrix = await fetch_drop_matrix()
    stages = await fetch_stage_info()
    if not item_index:
        return []
    matrix_by_item = latest_matrix_by_item(matrix or [])
    stage_meta = {str(s.get("stageId", "")): s for s in stages or []}
    formulas = await load_formulas()
    if not matrix_by_item and not formulas:
        return []

    rows: list[dict] = []
    for row in targets:
        item_id = str(row.get("id") or "")
        if not item_id:
            continue
        result = craft_vs_farm(
            item_id,
            int(row.get("lack") or 1),
            formulas,
            item_index,
            matrix_by_item,
            stage_meta,
            inventory=merged.get("farm_inventory"),
        )
        rows.append(
            {
                "name": result["name"],
                "lack": row.get("lack"),
                "advice": _farm_advice_text(result),
            }
        )
    return rows
