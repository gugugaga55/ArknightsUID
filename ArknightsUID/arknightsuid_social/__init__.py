"""游戏好友与森空岛公开头像查询。"""

from __future__ import annotations

import math
import re

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ArknightsUID.utils.card import resolve_or_reply, send_card_or_text, send_text
from ArknightsUID.utils.enduid.adapter import hide_uid, hide_uid_for
from ArknightsUID.utils.image import data_uri_bytes, gather_b64
from ArknightsUID.utils.path import AVATAR_CACHE_PATH
from ArknightsUID.utils.render import get_image_b64_with_cache
from ArknightsUID.utils.reply_safety import safe_external_message
from ArknightsUID.utils.skland.catalog import (
    ARK_AVATAR_CATEGORY_ID,
    AVATAR_PAGE_SIZE,
    load_avatar_catalog,
)
from ArknightsUID.utils.skland.friends import FRIEND_PAGE_SIZE, fetch_friends
from ArknightsUID.utils.time_utils import beijing_datetime
from ArknightsUID.version import ArknightsUID_version

social_sv = SV("ArknightsUID好友与头像", priority=5)


def parse_friend_args(text: str) -> tuple[str, int, int, str]:
    """账号参数与本地页码分开；搜索内容不会被识别为另一个账号 UID。

    显式 UID/账号前缀优先于位数启发式（"UID:1234" 是 UID，"账号:100000000"
    是序号）；裸数字沿用"5 位以上=UID"的既有启发式。
    """
    keyword = ""
    search = re.search(r"(?:^|\s)搜索[:：](.*)$", text)
    if search:
        keyword = search.group(1).strip()
        text = text[: search.start()].strip()
    page = 1
    match = re.search(r"(?:^|\s)(?:页(\d+)|第(\d+)页)(?=\s|$)", text)
    if match:
        page = int(match.group(1) or match.group(2))
        text = (text[: match.start()] + text[match.end() :]).strip()
    stripped = text.strip()
    prefix_match = re.match(r"^(账号|UID)[:：]?\s*", stripped, flags=re.IGNORECASE)
    prefix = prefix_match.group(1).upper() if prefix_match else ""
    selector = stripped[prefix_match.end() :] if prefix_match else stripped
    if page < 1 or (selector and (not selector.isdigit() or int(selector) < 1)):
        raise ValueError("用法：ark好友 [账号序号|UID] [页N] [搜索:关键词]")
    if not selector:
        uid, index = "", 0
    elif prefix == "UID":
        uid, index = selector, 0
    elif prefix == "账号":
        uid, index = "", int(selector)
    else:
        uid = selector if len(selector) >= 5 else ""
        index = int(selector) if selector and not uid else 0
    return uid, index, page, keyword


def parse_avatar_args(text: str, *, detail: bool = False) -> tuple[int, int]:
    category = ARK_AVATAR_CATEGORY_ID
    number = 0 if detail else 1
    positional = False
    for token in text.split():
        if token.startswith("分类"):
            category = int(token[2:].lstrip(":："))
        elif not detail and token.startswith("页"):
            number = int(token[1:])
        elif token.isdigit() and not positional:
            number = int(token)
            positional = True
        else:
            raise ValueError("参数格式不符")
    if category < 1 or number < 1:
        raise ValueError("分类、页码或头像 ID 必须大于 0")
    return category, number


def _slice_page(rows: list, page: int, size: int) -> tuple[list, int]:
    pages = max(1, math.ceil(len(rows) / size))
    if page > pages:
        raise ValueError(f"页码超出范围，共 {pages} 页。")
    return rows[(page - 1) * size : page * size], pages


async def _images(urls: list[str]) -> list[str]:
    return await gather_b64(
        lambda url: get_image_b64_with_cache(url, AVATAR_CACHE_PATH), urls
    )


def _online_time(timestamp: int) -> str:
    if timestamp <= 0:
        return "未提供"
    try:
        return beijing_datetime(timestamp).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "未提供"


@social_sv.on_command(("好友列表", "游戏好友", "好友"), block=True)
async def show_game_friends(bot: Bot, ev: Event) -> None:
    try:
        uid, index, page, keyword = parse_friend_args(ev.text or "")
    except ValueError as exc:
        return await send_text(bot, ev, str(exc))
    credential = await resolve_or_reply(bot, ev, uid, index)
    if credential is None:
        return
    result = await fetch_friends(credential)
    if isinstance(result, str):
        return await send_text(bot, ev, safe_external_message(result))
    friends = result.friends
    if keyword:
        friends = [
            friend
            for friend in friends
            if keyword.casefold() in friend.name.casefold()
            or keyword in friend.uid
            or keyword.casefold() in friend.skland_nickname.casefold()
        ]
    try:
        selected, pages = _slice_page(friends, page, FRIEND_PAGE_SIZE)
    except ValueError as exc:
        return await send_text(bot, ev, str(exc))
    portraits = await _images([friend.avatar_url for friend in selected])
    rows = [
        {
            "name": safe_external_message(friend.name),
            "uid": hide_uid(friend.uid),
            "level": friend.level,
            "avatar": portrait,
            "last_online": _online_time(friend.last_online_ts),
            "skland_nickname": safe_external_message(friend.skland_nickname)
            or "未授权",
        }
        for friend, portrait in zip(selected, portraits)
    ]
    account_uid = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    note = "" if result.complete else "接口返回的好友尚不完整，本次只展示已返回的记录。"
    hint = "翻页：ark好友 [相同账号参数] 页N；筛选：搜索:关键词"
    context = {
        "kind": "friends",
        "rows": rows,
        "name": safe_external_message(credential.nickname),
        "uid": account_uid,
        "total": len(friends),
        "page": page,
        "pages": pages,
        "keyword": bool(keyword),
        "note": note,
        "hint": hint,
        "version": ArknightsUID_version,
    }
    lines = [
        f"「明日方舟」{context['name'] or '博士'} · 游戏好友",
        f"UID：{account_uid}",
        f"{'匹配' if keyword else '已返回'} {len(friends)} 人 · 第 {page}/{pages} 页",
    ]
    for row in rows:
        lines.append(
            f"{row['name']} · Lv{row['level']} · UID {row['uid']}\n"
            f"最近在线：{row['last_online']} · 森空岛：{row['skland_nickname']}"
        )
    if not rows:
        lines.append(
            "未返回好友记录。"
            if not result.complete
            else ("未找到匹配的好友。" if keyword else "当前账号暂无游戏好友。")
        )
    lines.extend(part for part in (note, hint) if part)
    await send_card_or_text(bot, ev, "ark_social.html", context, "\n".join(lines))


@social_sv.on_command("头像分类", block=True)
async def show_avatar_categories(bot: Bot, ev: Event) -> None:
    categories = await load_avatar_catalog()
    if isinstance(categories, str):
        return await send_text(bot, ev, categories)
    lines = ["「森空岛」官方头像分类"]
    lines.extend(f"{row['id']} · {row['title']}" for row in categories)
    lines.append("查看：ark头像列表 分类:N（默认明日方舟）")
    await send_text(bot, ev, "\n".join(lines))


async def _avatar_rows(category: int) -> tuple[str, list[dict]] | str:
    categories = await load_avatar_catalog()
    if isinstance(categories, str):
        return categories
    title = next((row["title"] for row in categories if row["id"] == category), None)
    if title is None:
        return "未找到该头像分类，请用 ark头像分类 查看可用分类。"
    rows = await load_avatar_catalog(category)
    return rows if isinstance(rows, str) else (title, rows)


@social_sv.on_command(("头像列表", "头像目录"), block=True)
async def show_avatar_catalog(bot: Bot, ev: Event) -> None:
    try:
        category, page = parse_avatar_args(ev.text or "")
    except ValueError:
        return await send_text(bot, ev, "用法：ark头像列表 [页码] [分类:N]")
    result = await _avatar_rows(category)
    if isinstance(result, str):
        return await send_text(bot, ev, result)
    title, all_rows = result
    try:
        selected, pages = _slice_page(all_rows, page, AVATAR_PAGE_SIZE)
    except ValueError as exc:
        return await send_text(bot, ev, str(exc))
    pictures = await _images([row["url"] for row in selected])
    rows = [
        {"id": row["id"], "avatar": picture} for row, picture in zip(selected, pictures)
    ]
    hint = f"翻页：ark头像列表 页N 分类:{category}；原图：ark头像 ID 分类:{category}"
    context = {
        "kind": "avatars",
        "rows": rows,
        "name": title,
        "total": len(all_rows),
        "page": page,
        "pages": pages,
        "note": "",
        "hint": hint,
        "version": ArknightsUID_version,
    }
    lines = [f"「森空岛」{title}头像 · 共 {len(all_rows)} 个 · 第 {page}/{pages} 页"]
    lines.extend(f"ID {row['id']} · {row['url']}" for row in selected)
    if not selected:
        lines.append("该分类暂无头像。")
    lines.append(hint)
    await send_card_or_text(bot, ev, "ark_social.html", context, "\n".join(lines))


@social_sv.on_command("头像", block=True)
async def show_avatar_image(bot: Bot, ev: Event) -> None:
    try:
        category, avatar_id = parse_avatar_args(ev.text or "", detail=True)
    except ValueError:
        return await send_text(bot, ev, "用法：ark头像 <ID> [分类:N]")
    result = await _avatar_rows(category)
    if isinstance(result, str):
        return await send_text(bot, ev, result)
    _, rows = result
    avatar = next((row for row in rows if row["id"] == avatar_id), None)
    if avatar is None:
        return await send_text(bot, ev, "未找到该头像 ID，请用 ark头像列表 查看目录。")
    picture = await get_image_b64_with_cache(avatar["url"], AVATAR_CACHE_PATH)
    if not picture:
        return await send_text(bot, ev, "头像图片暂时无法下载，请稍后重试。")
    await bot.send(data_uri_bytes(picture))
