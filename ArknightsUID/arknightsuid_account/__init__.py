from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        ArkCredential,
        ark_attendance,
        fetch_player_info,
        hide_uid,
        resolve_accounts,
        resolve_credential,
    )
    from ArknightsUID.ArknightsUID.utils.gamedata.charinfo import (
        load_char_index,
        match_entries,
    )
    from ArknightsUID.ArknightsUID.utils.player import (
        CharEntry,
        PlayerData,
        build_box_context,
        build_campaign_context,
        build_char_card_context,
        build_char_stat_context,
        build_daily_context,
        build_status_context,
        effective_ap,
        fetch_cached_player_info,
        parse_player_info,
    )
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.utils.enduid.adapter import (
        ArkCredential,
        ark_attendance,
        fetch_player_info,
        hide_uid,
        resolve_accounts,
        resolve_credential,
    )
    from ArknightsUID.utils.gamedata.charinfo import (
        load_char_index,
        match_entries,
    )
    from ArknightsUID.utils.player import (
        CharEntry,
        PlayerData,
        build_box_context,
        build_campaign_context,
        build_char_card_context,
        build_char_stat_context,
        build_daily_context,
        build_status_context,
        effective_ap,
        fetch_cached_player_info,
        parse_player_info,
    )
    from ArknightsUID.version import ArknightsUID_version

account_sv = SV("ArknightsUID账号", priority=5)

_sensitive_fields = (
    r"cookie|cred|token|hg_token|skland_user_id"
    # 扩充常见高敏感字段，覆盖驼峰/下划线/空格变体，避免外部消息夹带泄漏。
    r"|access[_ ]?token|refresh[_ ]?token"
    r"|secret|password|passwd"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b(?:{_sensitive_fields})\b\s*[:=]\s*"
    rf"(?:\"[^\"]*\"|'[^']*'|[^,，;；\s]+)"
)
_SENSITIVE_FIELD_RE = re.compile(rf"(?i)\b(?:{_sensitive_fields})\b")
_SENTINEL_VALUE_RE = re.compile(r"(?i)\bSENTINEL_[A-Z0-9_]*\b")


def _safe_external_message(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    text = _SENSITIVE_ASSIGNMENT_RE.sub("已隐藏敏感内容", text)
    text = _SENSITIVE_FIELD_RE.sub("已隐藏敏感内容", text)
    text = _SENTINEL_VALUE_RE.sub("已隐藏敏感内容", text)
    text = re.sub(r"(?:已隐藏敏感内容[\s,，;；]*){2,}", "已隐藏敏感内容", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text or "已隐藏敏感内容"


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
) -> str:
    lines = ["「明日方舟」绑定账号列表"]
    if accounts:
        for index, account in enumerate(accounts, start=1):
            nickname = account.nickname if account.nickname else "未记录昵称"
            status = "可用" if account.usable else "凭据失效/缺失"
            mark = " ★默认" if account.uid == default_uid else ""
            lines.append(
                f"{index}. {nickname} | UID {hide_uid(account.uid)} | {status}{mark}"
            )
        lines.append("多账号查询：ark理智 序号 / ark理智 UID；ark切换 序号 设默认。")
    elif uids:
        lines.append("已找到 UID，但缺少对应凭据记录：")
        for index, uid in enumerate(uids, start=1):
            lines.append(f"{index}. UID {hide_uid(uid)}")
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
    lines = [f"「明日方舟」{shown_name}", f"UID：{hide_uid(credential.uid)}"]
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
) -> str:
    code = _value(response, "code")
    message = _value(response, "message")
    if message is None:
        message = _value(response, "msg")
    message = _safe_external_message(message)
    if code == 0:
        return f"「明日方舟」签到请求成功\nUID：{hide_uid(credential.uid)}\n{message or 'OK'}"
    return f"「明日方舟」签到失败\nUID：{hide_uid(credential.uid)}\ncode={code}，message={message or '无'}"


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
    context = {
        "version": ArknightsUID_version,
        "accounts": [
            {
                "index": index,
                "nickname": account.nickname if account.nickname else "未记录昵称",
                "uid": hide_uid(account.uid),
                "usable": bool(account.usable),
            }
            for index, account in enumerate(result.accounts, start=1)
        ],
        "orphan_uids": (
            [] if result.accounts else [hide_uid(uid) for uid in result.uids]
        ),
    }
    await send_card_or_text(
        bot, ev, "ark_accounts.html", context,
        _format_accounts(result.uids, result.accounts),
    )


async def _load_player_data(credential: ArkCredential) -> PlayerData | str:
    """带缓存的 player_info 获取 + 解析。失败返回中文错误文案。"""
    response = await fetch_cached_player_info(credential)
    if isinstance(response, str):
        return response
    data = parse_player_info(response)
    if data is None:
        return "森空岛玩家数据解析失败（接口返回异常），请稍后重试。"
    return data


def _format_campaign(credential: ArkCredential, data: PlayerData) -> str:
    lines = [f"「明日方舟」剿灭作战", f"UID：{hide_uid(credential.uid)}"]
    if data.campaign_total > 0:
        lines.append(f"本周合成玉：{data.campaign_current}/{data.campaign_total}")
    for zone, kills in sorted(
        data.campaign_records.items(), key=lambda kv: -kv[1]
    )[:6]:
        lines.append(f"{zone}：击杀 {kills}")
    if len(lines) <= 2:
        lines.append("未识别到剿灭详细字段（等待数据补全）。")
    return "\n".join(lines)


def _format_char_stat(credential: ArkCredential, data: PlayerData) -> str:
    if not data.chars:
        return "未识别到干员练度数据。"
    e2 = sum(1 for c in data.chars if c.evolve_phase >= 2)
    lines = [
        "「明日方舟」练度统计",
        f"UID：{hide_uid(credential.uid)}",
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


def _format_box(credential: ArkCredential, data: PlayerData) -> str:
    if not data.chars:
        return "未识别到干员数据。"
    total = len(data.chars)
    lines = [
        "「明日方舟」BOX 分析",
        f"UID：{hide_uid(credential.uid)}",
        f"持有干员：{total}",
    ]
    for star in (6, 5, 4):
        count = sum(1 for c in data.chars if c.rarity_display == star)
        lines.append(f"{star}★：{count}")
    low = sum(1 for c in data.chars if c.rarity_display <= 3)
    lines.append(f"3★及以下：{low}")
    return "\n".join(lines)


def _format_char_card_text(entry: CharEntry) -> str:
    lines = [
        f"「明日方舟」{entry.display_name}",
        f"{entry.rarity_display}★ {entry.profession_name}",
        f"等级 {entry.level}（精英化 {entry.evolve_phase}，潜能 {entry.potential_rank}）",
        f"信赖 {entry.favor_percent}% | 主技能 {entry.main_skill_lvl}",
    ]
    if entry.skills:
        spec = "/".join(str(s.specialize_level) for s in entry.skills)
        lines.append(f"技能专精：{spec}")
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

    context = build_status_context(
        data, ArknightsUID_version, hide_uid(credential.uid)
    )
    if context is not None:
        return await send_card_or_text(bot, ev, "ark_status.html", context, "")
    # 数据不足（缺昵称/理智）回退旧文本格式：用原始响应走既有格式化
    response = await fetch_player_info(credential)
    if isinstance(response, str):
        return await _send_text(bot, ev, response)
    await _send_text(bot, ev, _format_player_info(credential, response))


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

    context = build_daily_context(
        data, ArknightsUID_version, hide_uid(credential.uid)
    )
    text = (
        f"「明日方舟」日常进度\n"
        f"UID：{hide_uid(credential.uid)}\n"
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

    context = build_campaign_context(
        data, ArknightsUID_version, hide_uid(credential.uid)
    )
    if context is not None:
        return await send_card_or_text(
            bot, ev, "ark_campaign.html", context, _format_campaign(credential, data)
        )
    await _send_text(bot, ev, _format_campaign(credential, data))


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

    context = build_char_stat_context(
        data, ArknightsUID_version, hide_uid(credential.uid)
    )
    if context is not None:
        return await send_card_or_text(
            bot, ev, "ark_char_stat.html", context, _format_char_stat(credential, data)
        )
    await _send_text(bot, ev, _format_char_stat(credential, data))


@account_sv.on_command(
    (
        "BOX",
        "box",
        "BOX分析",
        "box分析",
        "方舟BOX",
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

    context = build_box_context(
        data, ArknightsUID_version, hide_uid(credential.uid)
    )
    if context is not None:
        return await send_card_or_text(
            bot, ev, "ark_box_analysis.html", context, _format_box(credential, data)
        )
    await _send_text(bot, ev, _format_box(credential, data))


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
        "练度卡",
        "干员练度卡",
    ),
    block=True,
)
async def show_ark_char_card(bot: Bot, ev: Event) -> None:
    name, uid, index = _parse_char_query(ev.text)
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

    context = await build_char_card_context(entry, ArknightsUID_version)
    await send_card_or_text(
        bot, ev, "ark_char_card.html", context, _format_char_card_text(entry)
    )


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
    await _send_text(bot, ev, _format_attendance(credential, response))
