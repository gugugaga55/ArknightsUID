from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.account_switch import (
        clear_default_uid,
        get_default_uid,
        set_default_uid,
    )
    from ArknightsUID.ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        ArkCredential,
        ark_attendance,
        fetch_player_info,
        hide_uid,
        hide_uid_for,
        resolve_accounts,
        resolve_credential,
    )
    from ArknightsUID.ArknightsUID.utils.gamedata.charinfo import (
        load_char_index,
        match_entries,
    )
    from ArknightsUID.ArknightsUID.utils.player import (
        BuildingData,
        CharEntry,
        PlayerData,
        TRADING_STRATEGY_NAMES,
        build_box_context,
        build_building_context,
        build_campaign_context,
        build_char_card_context,
        build_char_stat_context,
        build_daily_context,
        build_skin_context,
        build_status_context,
        effective_ap,
        fetch_cached_player_info,
        format_training_remaining,
        parse_building,
        parse_player_info,
        recruit_done_count,
        recruit_next_remaining,
        training_remaining_secs,
        training_status_text,
    )
    from ArknightsUID.ArknightsUID.utils.reply_safety import safe_external_message
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.utils.account_switch import (
        clear_default_uid,
        get_default_uid,
        set_default_uid,
    )
    from ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.utils.enduid.adapter import (
        ArkCredential,
        ark_attendance,
        fetch_player_info,
        hide_uid,
        hide_uid_for,
        resolve_accounts,
        resolve_credential,
    )
    from ArknightsUID.utils.gamedata.charinfo import (
        load_char_index,
        match_entries,
    )
    from ArknightsUID.utils.player import (
        BuildingData,
        CharEntry,
        PlayerData,
        TRADING_STRATEGY_NAMES,
        build_box_context,
        build_building_context,
        build_campaign_context,
        build_char_card_context,
        build_char_stat_context,
        build_daily_context,
        build_skin_context,
        build_status_context,
        effective_ap,
        fetch_cached_player_info,
        format_training_remaining,
        parse_building,
        parse_player_info,
        recruit_done_count,
        recruit_next_remaining,
        training_remaining_secs,
        training_status_text,
    )
    from ArknightsUID.utils.reply_safety import safe_external_message
    from ArknightsUID.version import ArknightsUID_version

account_sv = SV("ArknightsUID账号", priority=5)

# 外部消息脱敏统一实现在 utils/reply_safety；保留旧名供既有调用/测试使用
_safe_external_message = safe_external_message


def _extract_uid(text: str) -> str:
    match = re.search(r"\d{5,}", text)
    if match is None:
        return ""
    return match.group(0)


def _extract_index(text: str) -> int:
    if _extract_uid(text):
        return 0
    normalized = text.strip().replace("第", "").replace("号", "")
    match = re.search(r"(?:^|\s|#)(\d{1,2})(?:\s*)$", normalized)
    if match is None:
        return 0
    return int(match.group(1))


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


def _value(data: Any, key: str) -> Any:
    if isinstance(data, dict) and key in data:
        return data[key]
    return None


def _clip(text: Any, limit: int) -> str:
    """技能效果等长文本的单行截断。"""
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _path(data: Any, *keys: str) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _first_path(data: Any, paths: tuple[tuple[str, ...], ...]) -> Any:
    for path in paths:
        value = _path(data, *path)
        if value is not None and value != "":
            return value
    return None


def _find_first(data: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        for value in data.values():
            found = _find_first(value, keys, depth + 1)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for value in data[:20]:
            found = _find_first(value, keys, depth + 1)
            if found not in (None, ""):
                return found
    return None


def _format_accounts(
    uids: list[str],
    accounts: list[ArkCredential],
    default_uid: str = "",
    shown_uids: dict[str, str] | None = None,
) -> str:
    """账号列表文本。shown_uids: handler 侧按用户偏好预脱敏的 uid 映射。"""

    def _shown(uid: str) -> str:
        if shown_uids and uid in shown_uids:
            return shown_uids[uid]
        return hide_uid(uid)

    lines = ["「明日方舟」绑定账号列表"]
    if accounts:
        for index, account in enumerate(accounts, start=1):
            nickname = account.nickname if account.nickname else "未记录昵称"
            status = "可用" if account.usable else "凭据失效/缺失"
            mark = " ★默认" if account.uid == default_uid else ""
            lines.append(
                f"{index}. {nickname} | UID {_shown(account.uid)} | {status}{mark}"
            )
        lines.append("多账号查询：ark理智 序号 / ark理智 UID；ark切换 序号 设默认。")
    elif uids:
        lines.append("已找到 UID，但缺少对应凭据记录：")
        for index, uid in enumerate(uids, start=1):
            lines.append(f"{index}. UID {_shown(uid)}")
        lines.append("请重新使用 ark绑定 或 EndUID 登录森空岛。")
    else:
        lines.append("未找到明日方舟账号。")
    return "\n".join(lines)


def _format_api_error(action: str, response: dict[str, Any]) -> str | None:
    code = _value(response, "code")
    status = _value(response, "status")
    message = _value(response, "message")
    if message is None:
        message = _value(response, "msg")
    message = _safe_external_message(message)

    if code == 10001 and message:
        return f"{action}返回：{message}"
    if code not in (None, 0):
        return f"{action}失败：code={code}，message={message or '无'}"
    if status not in (None, 0):
        return f"{action}失败：status={status}，message={message or '无'}"
    return None


def _format_timestamp(value: Any) -> str:
    if not isinstance(value, (int, float)) or value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _format_player_info(
    credential: ArkCredential,
    response: dict[str, Any],
    uid_display: str | None = None,
) -> str:
    api_error = _format_api_error("查询森空岛玩家信息", response)
    if api_error is not None:
        return api_error

    data = _value(response, "data")
    if data is None:
        data = response

    nickname = _first_path(
        data,
        (
            ("status", "name"),
            ("status", "nickName"),
            ("player", "nickName"),
            ("player", "nickname"),
            ("base", "nickName"),
            ("base", "nickname"),
        ),
    )
    level = _first_path(
        data,
        (
            ("status", "level"),
            ("player", "level"),
            ("base", "level"),
        ),
    )
    progress = _first_path(
        data,
        (
            ("status", "mainStageProgress"),
            ("player", "mainStageProgress"),
            ("base", "mainStageProgress"),
        ),
    )
    ap_current = _first_path(
        data,
        (
            ("status", "ap", "current"),
            ("status", "ap", "currentAp"),
            ("ap", "current"),
            ("ap", "currentAp"),
        ),
    )
    ap_max = _first_path(
        data,
        (
            ("status", "ap", "max"),
            ("status", "ap", "maxAp"),
            ("ap", "max"),
            ("ap", "maxAp"),
        ),
    )
    recover_time = _first_path(
        data,
        (
            ("status", "ap", "completeRecoveryTime"),
            ("status", "ap", "completeRecoveryTimestamp"),
            ("ap", "completeRecoveryTime"),
            ("ap", "completeRecoveryTimestamp"),
        ),
    )

    shown_name = str(nickname or credential.nickname or "未记录昵称")
    shown_uid = uid_display if uid_display is not None else hide_uid(credential.uid)
    lines = [f"「明日方舟」{shown_name}", f"UID：{shown_uid}"]
    if level is not None:
        lines.append(f"等级：{level}")
    if progress is not None:
        lines.append(f"主线进度：{progress}")
    if ap_current is not None or ap_max is not None:
        cur = "?" if ap_current is None else str(ap_current)
        max_ap = "?" if ap_max is None else str(ap_max)
        lines.append(f"理智：{cur}/{max_ap}")
    recover = _format_timestamp(recover_time)
    if recover:
        lines.append(f"理智回满：{recover}")

    daily_current = _find_first(data, ("dailyCurrent", "dailyTaskCurrent"))
    daily_total = _find_first(data, ("dailyTotal", "dailyTaskTotal"))
    if daily_current is not None and daily_total is not None:
        lines.append(f"日常：{daily_current}/{daily_total}")

    weekly_current = _find_first(data, ("weeklyCurrent", "weeklyTaskCurrent"))
    weekly_total = _find_first(data, ("weeklyTotal", "weeklyTaskTotal"))
    if weekly_current is not None and weekly_total is not None:
        lines.append(f"周常：{weekly_current}/{weekly_total}")

    recruit = _find_first(data, ("recruit", "recruiting", "recruitStatus"))
    if isinstance(recruit, (str, int, float)):
        lines.append(f"公开招募：{recruit}")

    if len(lines) <= 2:
        lines.append("森空岛请求成功，但当前版本尚未识别详细字段。")
    return "\n".join(lines)


def _format_attendance(
    credential: ArkCredential,
    response: dict[str, Any],
    uid_display: str | None = None,
) -> str:
    code = _value(response, "code")
    message = _value(response, "message")
    if message is None:
        message = _value(response, "msg")
    message = _safe_external_message(message)
    shown_uid = uid_display if uid_display is not None else hide_uid(credential.uid)
    if code == 0:
        return f"「明日方舟」签到请求成功\nUID：{shown_uid}\n{message or 'OK'}"
    return f"「明日方舟」签到失败\nUID：{shown_uid}\ncode={code}，message={message or '无'}"


@account_sv.on_command(
    (
        "账号",
        "我的方舟",
        "方舟账号",
        "绑定列表",
        "方舟绑定",
    ),
    block=True,
)
async def show_ark_accounts(bot: Bot, ev: Event) -> None:
    result = await resolve_accounts(ev.user_id, ev.bot_id)
    if not result.installed:
        return await _send_text(bot, ev, result.reason)
    default_uid = get_default_uid(ev.bot_id, ev.user_id)
    all_uids = [a.uid for a in result.accounts] + [
        u for u in result.uids if u not in {a.uid for a in result.accounts}
    ]
    shown_uids = {
        uid: await hide_uid_for(uid, ev.user_id, ev.bot_id) for uid in all_uids
    }
    context = {
        "version": ArknightsUID_version,
        "accounts": [
            {
                "index": index,
                "nickname": account.nickname if account.nickname else "未记录昵称",
                "uid": shown_uids.get(account.uid, hide_uid(account.uid)),
                "usable": bool(account.usable),
                "is_default": account.uid == default_uid,
            }
            for index, account in enumerate(result.accounts, start=1)
        ],
        "orphan_uids": (
            []
            if result.accounts
            else [shown_uids.get(uid, hide_uid(uid)) for uid in result.uids]
        ),
    }
    await send_card_or_text(
        bot, ev, "ark_accounts.html", context,
        _format_accounts(
            result.uids, result.accounts, default_uid, shown_uids=shown_uids
        ),
    )


@account_sv.on_command(
    (
        "切换账号",
        "方舟切换",
        "切换",
    ),
    block=True,
)
async def switch_ark_account(bot: Bot, ev: Event) -> None:
    """ark切换 <序号|UID>：设置默认查询账号（不带参数时查看当前默认）。"""
    result = await resolve_accounts(ev.user_id, ev.bot_id)
    if not result.installed or not result.accounts:
        reason = result.reason or "未找到明日方舟账号，请先使用 ark绑定 登录。"
        return await _send_text(bot, ev, reason)

    default_uid = get_default_uid(ev.bot_id, ev.user_id)
    query = ev.text.strip()
    if not query:
        current = "未设置（默认用第一个可用账号）"
        for index, account in enumerate(result.accounts, start=1):
            if account.uid == default_uid:
                current = (
                    f"{index}. {account.nickname or '未记录昵称'}"
                    f"（UID {await hide_uid_for(account.uid, ev.user_id, ev.bot_id)}）"
                )
        return await _send_text(
            bot, ev,
            f"当前默认账号：{current}\n"
            f"用法：ark切换 序号 / ark切换 UID（序号见 ark账号）",
        )

    target: ArkCredential | None = None
    uid = _extract_uid(query)
    if uid:
        for account in result.accounts:
            if account.uid == uid:
                target = account
                break
        if target is None:
            shown = await hide_uid_for(uid, ev.user_id, ev.bot_id)
            return await _send_text(
                bot, ev, f"未找到 UID {shown} 对应的账号，可用账号见 ark账号。"
            )
    else:
        match = re.fullmatch(r"第?(\d{1,2})号?", query)
        if match is None:
            return await _send_text(
                bot, ev, "用法：ark切换 序号 / ark切换 UID（序号见 ark账号）。"
            )
        index = int(match.group(1))
        if not 1 <= index <= len(result.accounts):
            return await _send_text(
                bot, ev, f"序号超出范围（1-{len(result.accounts)}），可用账号见 ark账号。"
            )
        target = result.accounts[index - 1]

    if set_default_uid(ev.bot_id, ev.user_id, target.uid):
        shown = await hide_uid_for(target.uid, ev.user_id, ev.bot_id)
        await _send_text(
            bot, ev,
            f"已切换默认账号：{target.nickname or '未记录昵称'}（UID {shown}）\n"
            f"之后 ark理智 / ark基建 等查询默认走该账号；ark切换 序号 可再改。",
        )
    else:
        await _send_text(bot, ev, "默认账号保存失败，请稍后重试。")


async def _load_player_data(credential: ArkCredential) -> PlayerData | str:
    """带缓存的 player_info 获取 + 解析。失败返回中文错误文案。"""
    response = await fetch_cached_player_info(credential)
    if isinstance(response, str):
        return response
    data = parse_player_info(response)
    if data is None:
        return "森空岛玩家数据解析失败（接口返回异常），请稍后重试。"
    return data


def _format_campaign(
    credential: ArkCredential, data: PlayerData, uid_display: str | None = None
) -> str:
    shown_uid = uid_display if uid_display is not None else hide_uid(credential.uid)
    lines = [f"「明日方舟」剿灭作战", f"UID：{shown_uid}"]
    if data.campaign_total > 0:
        lines.append(f"本周合成玉：{data.campaign_current}/{data.campaign_total}")
    for zone, kills in sorted(
        data.campaign_records.items(), key=lambda kv: -kv[1]
    )[:6]:
        lines.append(f"{zone}：击杀 {kills}")
    if len(lines) <= 2:
        lines.append("未识别到剿灭详细字段（等待数据补全）。")
    return "\n".join(lines)


def _format_char_stat(
    credential: ArkCredential, data: PlayerData, uid_display: str | None = None
) -> str:
    if not data.chars:
        return "未识别到干员练度数据。"
    e2 = sum(1 for c in data.chars if c.evolve_phase >= 2)
    shown_uid = uid_display if uid_display is not None else hide_uid(credential.uid)
    lines = [
        "「明日方舟」练度统计",
        f"UID：{shown_uid}",
        f"持有 {len(data.chars)} | 精二 {e2}",
    ]
    by_profession: dict[str, list[CharEntry]] = {}
    for char in data.chars:
        by_profession.setdefault(char.profession or "UNKNOWN", []).append(char)
    for prof in sorted(by_profession):
        members = by_profession[prof]
        prof_e2 = sum(1 for c in members if c.evolve_phase >= 2)
        label = members[0].profession_name or prof
        lines.append(f"{label}：{len(members)}（精二 {prof_e2}）")
    return "\n".join(lines)


def _format_box(
    credential: ArkCredential, data: PlayerData, uid_display: str | None = None
) -> str:
    if not data.chars:
        return "未识别到干员数据。"
    total = len(data.chars)
    shown_uid = uid_display if uid_display is not None else hide_uid(credential.uid)
    lines = [
        "「明日方舟」BOX 分析",
        f"UID：{shown_uid}",
        f"持有干员：{total}",
    ]
    for star in (6, 5, 4):
        count = sum(1 for c in data.chars if c.rarity_display == star)
        lines.append(f"{star}★：{count}")
    low = sum(1 for c in data.chars if c.rarity_display <= 3)
    lines.append(f"3★及以下：{low}")
    return "\n".join(lines)


async def _char_skill_effects(entry: CharEntry) -> dict[str, dict]:
    """练度卡技能效果增强：PRTS 当前级/满级描述（页面缓存 24h）。

    练度卡是玩家数据卡，wiki 只是增强信息：任何失败返回空 dict，
    模板对缺省只展示专精点，不阻塞出卡。
    """
    try:
        from ..utils.gamedata.charinfo import load_skill_names
        from ..utils.prts.client import (
            final_level,
            query_operator,
            select_owner_level,
        )

        skill_names = await load_skill_names()
        op = await query_operator(entry.name or entry.char_id)
        if not op or not op.skills:
            return {}
        specialize_by_name = {
            skill_names.get(sk.skill_id) or sk.skill_id: sk.specialize_level
            for sk in entry.skills
        }
        effects: dict[str, dict] = {}
        for s in op.skills:
            if s.name not in specialize_by_name:
                continue
            current = select_owner_level(
                s.levels, specialize_by_name[s.name], entry.main_skill_lvl
            )
            final = final_level(s.levels)
            effects[s.name] = {
                "current": current.description if current else "",
                "current_level": current.level if current else "",
                "final": (
                    final.description
                    if final is not None and final is not current
                    else ""
                ),
                "final_level": (
                    final.level if final is not None and final is not current else ""
                ),
            }
        return effects
    except Exception:
        logger.debug("[ArknightsUID] 练度卡技能效果获取失败", exc_info=True)
        return {}


def _format_char_card_text(
    entry: CharEntry, skill_effects: dict[str, dict] | None = None
) -> str:
    lines = [
        f"「明日方舟」{entry.display_name}",
        f"{entry.rarity_display}★ {entry.profession_name}",
        f"等级 {entry.level}（精英化 {entry.evolve_phase}，潜能 {entry.potential_rank}）",
        f"信赖 {entry.favor_percent}% | 主技能 {entry.main_skill_lvl}",
    ]
    if entry.skills:
        spec = "/".join(str(s.specialize_level) for s in entry.skills)
        lines.append(f"技能专精：{spec}")
    if skill_effects:
        for name, eff in skill_effects.items():
            if eff.get("current"):
                lines.append(
                    f"  {name} 当前{eff['current_level']}：{_clip(eff['current'], 120)}"
                )
            if eff.get("final"):
                lines.append(
                    f"  {name} {eff['final_level']}：{_clip(eff['final'], 120)}"
                )
    if entry.equips:
        equips = "、".join(f"{eq.equip_id} Lv{eq.level}" for eq in entry.equips)
        lines.append(f"模组：{equips}")
    return "\n".join(lines)


# 阿米娅三形态直查（社区习惯叫法 → charId，客观角色事实）
_AMIYA_FORM_IDS = {
    "近卫阿米娅": "char_1001_amiya2",
    "阿米娅近卫": "char_1001_amiya2",
    "医疗阿米娅": "char_1037_amiya3",
    "阿米娅医疗": "char_1037_amiya3",
}


def _find_char_offline(data: PlayerData, query: str) -> CharEntry | None:
    """gamedata 索引不可用时的兜底：直接对玩家持有干员名做精确/唯一前缀匹配。"""
    if not query or not data.chars:
        return None
    exact = [c for c in data.chars if c.name == query]
    if len(exact) == 1:
        return exact[0]
    prefix = [c for c in data.chars if c.name.startswith(query)]
    if len(prefix) == 1:
        return prefix[0]
    return None


async def _resolve_char_entry(data: PlayerData, query: str) -> CharEntry | str:
    """干员名/别名 → 玩家持有干员。失败返回中文提示。"""
    query = query.strip()
    if not query:
        return "请输入干员名，如：ark练度卡 阿米娅"

    amiya_char_id = _AMIYA_FORM_IDS.get(query)
    if amiya_char_id:
        for char in data.chars:
            if char.char_id == amiya_char_id:
                return char
        return "你还没有获得该形态的阿米娅。"

    index = await load_char_index()
    if index:
        matched = match_entries(query, index)
        if len(matched) == 1:
            char_id = str(matched[0]["char_id"])
            for char in data.chars:
                if char.char_id == char_id:
                    return char
            return f"你还没有获得干员「{matched[0].get('name', query)}」。"
        if len(matched) > 1:
            names = "、".join(str(m.get("name", "")) for m in matched[:8])
            return f"「{query}」匹配到多名干员：{names}，请写全名。"

    offline = _find_char_offline(data, query)
    if offline is not None:
        return offline
    return f"未找到干员「{query}」（首次使用会下载干员数据，可稍后重试）。"


@account_sv.on_command(
    (
        "理智",
        "方舟理智",
        "便签",
    ),
    block=True,
)
async def show_ark_status(bot: Bot, ev: Event) -> None:
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    loaded = await _load_player_data(credential)
    if isinstance(loaded, str):
        return await _send_text(bot, ev, loaded)
    data = loaded
    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)

    context = build_status_context(data, ArknightsUID_version, shown)
    if context is not None:
        return await send_card_or_text(bot, ev, "ark_status.html", context, "")
    # 数据不足（缺昵称/理智）回退旧文本格式：用原始响应走既有格式化
    response = await fetch_player_info(credential)
    if isinstance(response, str):
        return await _send_text(bot, ev, response)
    await _send_text(
        bot, ev, _format_player_info(credential, response, uid_display=shown)
    )


@account_sv.on_command(
    (
        "每日",
        "日常",
        "方舟每日",
        "方舟日常",
    ),
    block=True,
)
async def show_ark_daily(bot: Bot, ev: Event) -> None:
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    loaded = await _load_player_data(credential)
    if isinstance(loaded, str):
        return await _send_text(bot, ev, loaded)
    data = loaded
    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)

    context = build_daily_context(data, ArknightsUID_version, shown)
    text = (
        f"「明日方舟」日常进度\n"
        f"UID：{shown}\n"
        f"每日：{data.daily_done}/{data.daily_total}\n"
        f"周常：{data.weekly_done}/{data.weekly_total}\n"
        f"剿灭合成玉：{data.campaign_current}/{data.campaign_total}"
    )
    if context is not None:
        return await send_card_or_text(bot, ev, "ark_daily.html", context, text)
    await _send_text(bot, ev, text)


@account_sv.on_command(
    (
        "剿灭",
        "方舟剿灭",
    ),
    block=True,
)
async def show_ark_campaign(bot: Bot, ev: Event) -> None:
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    loaded = await _load_player_data(credential)
    if isinstance(loaded, str):
        return await _send_text(bot, ev, loaded)
    data = loaded
    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)

    context = build_campaign_context(data, ArknightsUID_version, shown)
    if context is not None:
        return await send_card_or_text(
            bot, ev, "ark_campaign.html", context,
            _format_campaign(credential, data, uid_display=shown),
        )
    await _send_text(bot, ev, _format_campaign(credential, data, uid_display=shown))


# 注册顺序约束：练度卡 必须先于「练度统计/练度」注册（startswith 匹配下
# 短词「练度」会抢走「ark练度卡」，实机踩坑：练度卡曾渲染成练度统计卡）。
@account_sv.on_command(
    (
        "练度卡",
        "干员练度卡",
    ),
    block=True,
)
async def show_ark_char_card(bot: Bot, ev: Event) -> None:
    name, uid, index = _parse_char_query(ev.text)
    try:
        from ..utils.user_alias import apply_user_alias

        name = await apply_user_alias(ev.bot_id, ev.user_id, name)
    except Exception:
        pass
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    loaded = await _load_player_data(credential)
    if isinstance(loaded, str):
        return await _send_text(bot, ev, loaded)
    data = loaded
    if not data.chars:
        return await _send_text(bot, ev, "未识别到干员练度数据。")

    resolved = await _resolve_char_entry(data, name)
    if isinstance(resolved, str):
        return await _send_text(bot, ev, resolved)
    entry = resolved

    # PRTS 技能效果增强（当前级/满级描述；失败静默降级为纯专精点展示）
    skill_effects = await _char_skill_effects(entry)
    context = await build_char_card_context(
        entry, ArknightsUID_version, skill_effects
    )
    await send_card_or_text(
        bot, ev, "ark_char_card.html", context,
        _format_char_card_text(entry, skill_effects),
    )


@account_sv.on_command(
    (
        "练度统计",
        "练度",
    ),
    block=True,
)
async def show_ark_char_stat(bot: Bot, ev: Event) -> None:
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    loaded = await _load_player_data(credential)
    if isinstance(loaded, str):
        return await _send_text(bot, ev, loaded)
    data = loaded
    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)

    context = build_char_stat_context(data, ArknightsUID_version, shown)
    if context is not None:
        # 职业行挂 PRTS 职业图标（失败空串降级）
        try:
            from ..utils.gamedata.itemicon import profession_icon_b64

            for p in context.get("professions", []):
                p["icon"] = await profession_icon_b64(str(p.get("name", "")))
        except Exception:
            pass
        return await send_card_or_text(
            bot, ev, "ark_char_stat.html", context,
            _format_char_stat(credential, data, uid_display=shown),
        )
    await _send_text(
        bot, ev, _format_char_stat(credential, data, uid_display=shown)
    )


@account_sv.on_command(
    (
        "BOX分析",
        "box分析",
        "方舟BOX",
        "BOX",
        "box",
    ),
    block=True,
)
async def show_ark_box(bot: Bot, ev: Event) -> None:
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    loaded = await _load_player_data(credential)
    if isinstance(loaded, str):
        return await _send_text(bot, ev, loaded)
    data = loaded
    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)

    context = build_box_context(data, ArknightsUID_version, shown)
    if context is not None:
        # 六星头像墙：等级前 10 的六星（当前穿戴皮肤头像），失败降级为空不显示
        try:
            from ..utils.path import AVATAR_CACHE_PATH
            from ..utils.player.context import skin_avatar_url
            from ..utils.render import get_image_b64_with_cache

            top_six = sorted(
                (c for c in data.chars if c.rarity_display == 6),
                key=lambda c: (-c.level, c.display_name),
            )[:10]
            six_rows = []
            for c in top_six:
                b64 = await get_image_b64_with_cache(
                    skin_avatar_url(c.skin_id), AVATAR_CACHE_PATH
                )
                six_rows.append({"name": c.display_name, "avatar": b64})
            context["six_rows"] = six_rows
        except Exception:
            context["six_rows"] = []
        return await send_card_or_text(
            bot, ev, "ark_box_analysis.html", context,
            _format_box(credential, data, uid_display=shown),
        )
    await _send_text(bot, ev, _format_box(credential, data, uid_display=shown))


def _parse_char_query(text: str) -> tuple[str, str, int]:
    """练度卡参数解析 → (干员名, uid, 序号)。

    尾部 1-2 位数字仅在前面还有名字时才视为账号序号
    （「练度卡 银灰 2」→ 银灰/2号账号；「练度卡 42」→ 干员别名 42）。
    """
    uid = _extract_uid(text)
    cleaned = text.strip()
    if uid:
        cleaned = cleaned.replace(uid, "", 1).strip()
    index = 0
    match = re.search(r"\s(?:第)?(\d{1,2})(?:号)?\s*$", cleaned)
    if match and cleaned[: match.start()].strip():
        index = int(match.group(1))
        cleaned = cleaned[: match.start()].strip()
    return cleaned, uid, index


@account_sv.on_command(
    (
        "签到",
        "方舟签到",
    ),
    block=True,
)
async def send_ark_attendance(bot: Bot, ev: Event) -> None:
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    response = await ark_attendance(credential)
    if isinstance(response, str):
        return await _send_text(bot, ev, response)
    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    await _send_text(
        bot, ev, _format_attendance(credential, response, uid_display=shown)
    )


def _format_building(
    credential: ArkCredential,
    data: PlayerData,
    building: BuildingData,
    uid_display: str | None = None,
) -> str:
    shown_uid = uid_display if uid_display is not None else hide_uid(credential.uid)
    lines = ["「明日方舟」基建总览", f"UID：{shown_uid}"]
    lines.append(
        f"控制中枢 Lv{building.control_level} · 无人机 "
        f"{building.labor_value}/{building.labor_max}"
        + (f" · 发电 ×{len(building.powers)}" if building.powers else "")
    )
    for index, room in enumerate(building.manufactures, start=1):
        label = "制造站" if index == 1 else f"制造站{index}"
        product = f"配方#{room.formula_id}" if room.formula_id else "未在生产"
        speed = f" · 效率{round(room.speed * 100)}%" if room.speed > 0 else ""
        lines.append(f"{label} Lv{room.level}：{product}{speed}")
    for index, room in enumerate(building.tradings, start=1):
        label = "贸易站" if index == 1 else f"贸易站{index}"
        strategy = TRADING_STRATEGY_NAMES.get(room.strategy, room.strategy or "订单")
        lines.append(
            f"{label} Lv{room.level}：{strategy}订单 {room.stock_count}/{room.stock_limit}"
        )
    for index, room in enumerate(building.dormitories, start=1):
        lines.append(f"宿舍{index}：氛围 {room.comfort}/5000")
    if building.clue_own or building.clue_board:
        board = "、".join(building.clue_board) if building.clue_board else "空"
        lines.append(f"会客室：线索 {building.clue_own} · 线索板：{board}")
    if building.training_trainee:
        status = training_status_text(training_remaining_secs(building))
        suffix = f"（{status}）" if status else ""
        lines.append(f"训练室：{building.training_trainee} 专精训练中{suffix}")
    if data.recruit_slots:
        done = recruit_done_count(data.recruit_slots)
        nxt = recruit_next_remaining(data.recruit_slots)
        if done:
            lines.append(f"公开招募：{done} 个槽位已可领取")
        elif nxt is not None:
            lines.append(
                f"公开招募：招募中，最早剩{format_training_remaining(nxt)}可领取"
            )
        else:
            lines.append("公开招募：全部槽位空闲")
    if building.tired_chars:
        names = "、".join(
            f"{c.name}({c.room} {c.morale})" for c in building.tired_chars[:6]
        )
        more = f" 等{len(building.tired_chars)}人" if len(building.tired_chars) > 6 else ""
        lines.append(f"待换班：{names}{more}")
    else:
        lines.append("待换班：无（在岗干员心情充足）")
    return "\n".join(lines)


@account_sv.on_command(
    (
        "基建",
        "方舟基建",
    ),
    block=True,
)
async def show_ark_building(bot: Bot, ev: Event) -> None:
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    loaded = await _load_player_data(credential)
    if isinstance(loaded, str):
        return await _send_text(bot, ev, loaded)
    data = loaded

    building = parse_building(data)
    if building is None:
        return await _send_text(
            bot, ev, "未识别到基建数据（森空岛返回缺失 building 字段）。"
        )
    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)

    context = await build_building_context(
        data, building, ArknightsUID_version, shown
    )
    if context is not None:
        # 房间成员挂初始头像（EndUID 基建卡同款展示），失败空串降级
        try:
            from ..utils.player.context import char_avatar_b64

            for room in context.get("manufactures", []) + context.get("tradings", []):
                for m in room.get("members", []):
                    m["avatar"] = await char_avatar_b64(str(m.get("char_id", "")))
        except Exception:
            pass
    await send_card_or_text(
        bot, ev, "ark_building.html", context or {},
        _format_building(credential, data, building, uid_display=shown),
    )


def _format_skin_collection(
    credential: ArkCredential, summary: dict, uid_display: str | None = None
) -> str:
    shown_uid = uid_display if uid_display is not None else hide_uid(credential.uid)
    lines = [
        "「明日方舟」皮肤收集",
        f"UID：{shown_uid}",
        (
            f"已收集 {summary['owned']}/{summary['total']}"
            f"（{summary['pct']}%），缺 {summary['missing_count']} 件"
        ),
    ]
    if summary["missing"]:
        lines.append("未收集（按系列排序，前 12 件）：")
        for row in summary["missing"]:
            group = f"〔{row['group']}〕" if row["group"] else ""
            lines.append(f"  {row['char']} · {group}{row['name']}")
    else:
        lines.append("全皮肤收集完成，博士氪度惊人！")
    lines.append("目录口径：ArknightsGameData 时装（@ 皮肤）× 可获得干员")
    return "\n".join(lines)


@account_sv.on_command(
    (
        "皮肤收集",
        "时装",
        "方舟皮肤",
        "皮肤",
    ),
    block=True,
)
async def show_ark_skins(bot: Bot, ev: Event) -> None:
    """ark皮肤 → 皮肤收集进度卡（player_info.skins × skin_table 目录）。"""
    uid = _extract_uid(ev.text)
    index = _extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    loaded = await _load_player_data(credential)
    if isinstance(loaded, str):
        return await _send_text(bot, ev, loaded)
    data = loaded
    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)

    context = await build_skin_context(data, ArknightsUID_version, shown)
    if context is None:
        # 目录不可用（skin_table 拉取失败）时仍可给出持有计数的降级文案
        return await _send_text(
            bot, ev,
            f"皮肤目录不可用（游戏数据拉取失败），请稍后再试。\n"
            f"当前记录持有时装 {len(data.owned_skin_ids)} 件。",
        )
    await send_card_or_text(
        bot, ev, "ark_skin.html", context,
        _format_skin_collection(credential, context, uid_display=shown),
    )
