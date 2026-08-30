"""活动 / 公告查询 + 推送订阅（P4）。

- `ark活动`：进行中的限时活动 + 限定卡池清单（PRTS SMW，与日历同源）
- `ark公告 [编号]`：明日方舟官方公告列表（森空岛 web 公共接口）
- `订阅方舟活动` / `取消订阅方舟活动`（pm=3，群聊）：gs_subscribe 订阅
- `订阅公告` / `取消订阅公告`（群聊/私聊）：仅官方公告的专项推送，
  与「订阅方舟活动」（活动+卡池+公告合并）互不影响、各自独立基线
- 定时轮询（间隔/开关见插件配置 AnnCheckMinute / AnnPushEnable）：新活动 / 新卡池 / 新公告 → 推送订阅群

推送状态（已见条目键）落 MAIN_PATH/push_state.json（活动合并订阅）与
ann_push_state.json（公告专项订阅），条目上限防膨胀。
"""
from __future__ import annotations

import asyncio
import math
import os
import random
import time

from gsuid_core.aps import scheduler
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.subscribe import gs_subscribe
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.ArknightsUID.utils.config import ArkConfig
    from ArknightsUID.ArknightsUID.utils.gamedata.charinfo import load_item_index
    from ArknightsUID.ArknightsUID.utils.gamedata.itemicon import item_icon_b64
    from ArknightsUID.ArknightsUID.utils.path import MAIN_PATH
    from ArknightsUID.ArknightsUID.utils.prts.client import (
        query_activities,
        query_gacha_pools,
    )
    from ArknightsUID.ArknightsUID.utils.skland.web import (
        get_ann_detail,
        get_ark_ann_list,
    )
    from ArknightsUID.ArknightsUID.utils.yituliu import (
        fetch_drop_matrix,
        fetch_stage_info,
        format_stage_board,
        item_drop_stats,
        latest_matrix_by_item,
        rank_stages,
    )
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.utils.config import ArkConfig
    from ArknightsUID.utils.gamedata.charinfo import load_item_index
    from ArknightsUID.utils.gamedata.itemicon import item_icon_b64
    from ArknightsUID.utils.path import MAIN_PATH
    from ArknightsUID.utils.prts.client import (
        query_activities,
        query_gacha_pools,
    )
    from ArknightsUID.utils.skland.web import (
        get_ann_detail,
        get_ark_ann_list,
    )
    from ArknightsUID.utils.yituliu import (
        fetch_drop_matrix,
        fetch_stage_info,
        format_stage_board,
        item_drop_stats,
        latest_matrix_by_item,
        rank_stages,
    )
    from ArknightsUID.version import ArknightsUID_version

import json
from pathlib import Path

push_sv = SV("ArknightsUID活动", priority=5)
push_sub_sv = SV("订阅方舟活动", pm=3)

TASK_NAME = "订阅方舟活动"
_STATE_PATH: Path = MAIN_PATH / "push_state.json"
# 已见条目键上限（超出裁剪最旧的，防状态文件膨胀）
_STATE_LIMIT = 400

# 公告专项订阅（仅公告，不掺活动/卡池；session 模式群聊/私聊各一条）
ANN_TASK_NAME = "订阅方舟公告"
_ANN_STATE_PATH: Path = MAIN_PATH / "ann_push_state.json"
# 公告推送单轮上限：超过的留下轮增量推（与活动合并推送同款防刷屏）
_ANN_PUSH_LIMIT = 5


def _activity_key(kind: str, title: str) -> str:
    return f"{kind}:{title}"


# 推送状态读-改-写锁：定时任务在"load → 发送(await) → save"整段持有，
# 防止两轮推送并发重复推/互相覆盖；命令侧写盘由 save_* 的合并语义兜底
# （多进程部署仍需外部协调，见 save_push_state 注释）
_push_state_lock = asyncio.Lock()


def _session_key(subscribe) -> str:
    """订阅会话标识：群聊按群、私聊按用户。"""
    if getattr(subscribe, "group_id", None):
        return f"group:{subscribe.group_id}"
    return f"direct:{getattr(subscribe, 'uid', '')}"


async def _deliver_per_session(
    datas,
    keys: list[str],
    build_message,
    delivered: dict[str, list[str]],
    log_tag: str,
) -> tuple[dict[str, list[str]], bool]:
    """按订阅会话投递条目（审计 #9：部分成功不再造成永久漏发）。

    delivered[key] = 已确认收到的会话集合（跨轮持久化在状态文件里）。
    某条目对某会话已投递则跳过——A 成功 B 失败时，下轮只给 B 补发，
    不给 A 重复刷屏；条目对全部会话投递完成后由调用方推进全局 seen。

    返回 (更新后的 delivered, 本轮是否有任何发送成功)。
    """
    any_sent = False
    for subscribe in datas:
        session = _session_key(subscribe)
        pending = [
            key for key in keys
            if session not in (delivered.get(key) or [])
        ]
        if not pending:
            continue
        try:
            await subscribe.send(build_message(pending))
            any_sent = True
            for key in pending:
                delivered.setdefault(key, []).append(session)
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·{log_tag}] 推送失败 session={session}: "
                f"{exc.__class__.__name__}"
            )
        await asyncio.sleep(random.uniform(1, 3))
    return delivered, any_sent


def _merge_seen_state(current_state: dict, incoming: dict) -> dict:
    """落盘前合并：fresh 磁盘状态优先，incoming 的 seen 追加并集（保序）。

    旧快照整文件覆盖会丢掉"轮询期间其他写入者（命令/另一轮任务）刚标的
    条目"——seen 是标记集合，并集语义下重复无害、丢失即漏推/重推。
    """
    merged = dict(current_state) if isinstance(current_state, dict) else {}
    if isinstance(incoming, dict):
        merged.update({k: v for k, v in incoming.items() if k not in ("seen", "delivered")})
        # delivered（per-session 投递进度，审计 #9）：按条目并集合并
        incoming_delivered = incoming.get("delivered")
        if isinstance(incoming_delivered, dict):
            merged_delivered = dict(merged.get("delivered") or {})
            for key, sessions in incoming_delivered.items():
                if not isinstance(sessions, list):
                    continue
                current_sessions = merged_delivered.get(key) or []
                merged_delivered[key] = list(
                    dict.fromkeys([*current_sessions, *sessions])
                )
            merged["delivered"] = merged_delivered
        incoming_seen = incoming.get("seen")
        if isinstance(incoming_seen, list):
            fresh_seen = merged.get("seen")
            seen_list = list(fresh_seen) if isinstance(fresh_seen, list) else []
            seen_set = {str(key) for key in seen_list}
            for key in incoming_seen:
                if str(key) not in seen_set:
                    seen_set.add(str(key))
                    seen_list.append(key)
            merged["seen"] = seen_list
    return merged


def load_push_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_push_state(state: dict) -> None:
    from ..utils.file_lock import file_locked, unique_tmp

    try:
        # 读-合并-写三步全在跨进程文件锁内（审计 #10：多 worker 部署
        # 下另一进程的旧快照不能再整文件覆盖；合并语义见 _merge_seen_state）
        with file_locked(_STATE_PATH):
            state = _merge_seen_state(load_push_state(), state)
            seen = state.get("seen")
            if isinstance(seen, list) and len(seen) > _STATE_LIMIT:
                state["seen"] = seen[-_STATE_LIMIT:]
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # 唯一 tmp + os.replace 原子写：崩溃不留半文件（半文件会被当
            # 无基线，下轮重建 seen 基线漏推一轮）
            tmp = unique_tmp(_STATE_PATH)
            tmp.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, _STATE_PATH)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·推送] 状态写入失败: {exc.__class__.__name__}"
        )


def _load_ann_state() -> dict:
    try:
        state = json.loads(_ANN_STATE_PATH.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _save_ann_state(state: dict) -> None:
    from ..utils.file_lock import file_locked, unique_tmp

    try:
        # 读-合并-写三步全在跨进程文件锁内（审计 #10，语义同 save_push_state）
        with file_locked(_ANN_STATE_PATH):
            state = _merge_seen_state(_load_ann_state(), state)
            seen = state.get("seen")
            if isinstance(seen, list) and len(seen) > _STATE_LIMIT:
                state["seen"] = seen[-_STATE_LIMIT:]
            _ANN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = unique_tmp(_ANN_STATE_PATH)
            tmp.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, _ANN_STATE_PATH)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·公告推送] 状态写入失败: {exc.__class__.__name__}"
        )


def diff_new_announcements(
    anns: list[dict[str, str]], seen: list[str]
) -> list[dict[str, str]]:
    """公告列表中未见过的（保持新→旧原序；无 id 的条目跳过）。"""
    seen_set = {str(key) for key in seen}
    return [
        ann for ann in anns
        if ann.get("id") and str(ann["id"]) not in seen_set
    ]


def format_ann_push_message(fresh: list[dict[str, str]]) -> str:
    """新公告 → 推送文案（标题+日期+链接，单轮最多 5 条）。"""
    lines = ["「明日方舟」新公告"]
    for ann in fresh[:_ANN_PUSH_LIMIT]:
        date = f"（{ann['date']}）" if ann.get("date") else ""
        lines.append(f"《{ann['title']}》{date}")
        if ann.get("url"):
            lines.append(f"  {ann['url']}")
    if len(fresh) > _ANN_PUSH_LIMIT:
        lines.append(f"……（本轮共 {len(fresh)} 条，其余下轮继续推）")
    lines.append("发送「ark公告」查看列表与正文。")
    return "\n".join(lines)


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


def _format_activities(
    activities: list, pools: list
) -> str:
    lines = ["「明日方舟」进行中 / 即将开始"]
    if activities:
        lines.append("— 限时活动 —")
        for act in activities:
            lines.append(f"  {act.title}（{act.start} ~ {act.end}）")
    if pools:
        lines.append("— 限定卡池 —")
        for pool in pools:
            lines.append(f"  {pool.title}（{pool.start} ~ {pool.end}）")
    if len(lines) == 1:
        lines.append("暂无进行中的活动或卡池。")
    lines.append("数据：PRTS Wiki")
    return "\n".join(lines)


def _format_ann_list(anns: list[dict[str, str]]) -> str:
    if not anns:
        return "公告获取失败（森空岛 web 接口不可用或分类待校正），请稍后重试。"
    lines = ["「明日方舟」官方公告（森空岛）"]
    for index, ann in enumerate(anns, start=1):
        date = f" · {ann['date']}" if ann.get("date") else ""
        lines.append(f"{index}. {ann['title']}{date}")
        if ann.get("url"):
            lines.append(f"   {ann['url']}")
    lines.append("发送「ark公告 编号」查看对应公告正文。")
    return "\n".join(lines)


# 文本详情：段落/单段长度上限（聊天窗口可读性）
_DETAIL_MAX_PARAS = 20
_DETAIL_PARA_CHARS = 200


def _format_ann_detail(detail: dict) -> str:
    lines = ["「明日方舟」公告详情（森空岛）", detail.get("title", "")]
    meta = [
        m for m in (
            detail.get("userName", ""),
            detail.get("date", ""),
            detail.get("userIpLocation", ""),
        ) if m
    ]
    if meta:
        lines.append("  " + " · ".join(meta))
    paras = detail.get("paragraphs") or []
    for para in paras[:_DETAIL_MAX_PARAS]:
        lines.append(para[:_DETAIL_PARA_CHARS] + ("…" if len(para) > _DETAIL_PARA_CHARS else ""))
    if len(paras) > _DETAIL_MAX_PARAS:
        lines.append(f"……（共 {len(paras)} 段）")
    images = detail.get("images") or []
    videos = detail.get("videos") or []
    extra = []
    if images:
        extra.append(f"{len(images)} 图")
    if videos:
        extra.append(f"{len(videos)} 视频")
    if extra:
        lines.append("（含 " + "+".join(extra) + "，超长原图已单独发送，完整内容见链接）")
    if detail.get("id"):
        lines.append(f"https://www.skland.com/article?id={detail['id']}")
    return "\n".join(lines)


# 公告配图高宽比阈值：超过视为超长图（维护公告整图），不内嵌卡片，
# 下载后按原画质单独追发（对齐 EndUID ann_detail_card 的 5 倍阈值）
_ANN_LONG_IMAGE_RATIO = 5.0
# 详情卡内嵌配图上限（防纯图帖把卡片撑爆渲染）；追发超长图上限（防刷屏）
_ANN_CARD_IMAGE_LIMIT = 12
_ANN_LONG_IMAGE_LIMIT = 6
# 公告图宽高合理上限：超过按无效处理（接口异常/脏数据，不进卡片布局）
_ANN_IMG_MAX_SIDE = 100_000


def _safe_img_side(value: object) -> int:
    """公告图宽高安全解析：仅接受有限数值（含数字字符串），异常回 0。

    接口是外部输入：None/列表/字典/"auto"/NaN 等此前会让 int() 直接
    抛 ValueError/TypeError，公告命令整体失败。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        number = float(value.strip())
    else:
        return 0
    if not math.isfinite(number) or number <= 0 or number > _ANN_IMG_MAX_SIDE:
        return 0
    return int(number)


def split_ann_images(detail: dict) -> tuple[list[dict], list[str]]:
    """公告配图 →（内嵌卡片的普通图, 单独追发的超长图直链）。

    与视频封面重复的图片跳过（EndUID 同规则）；无尺寸信息的图按普通图
    处理（内嵌显示宽度由卡片约束）。宽高经 _safe_img_side 归一，脏数据
    不再让命令抛异常。
    """
    video_cover_urls = {
        str(v.get("coverUrl") or "") for v in detail.get("videos") or []
    }
    normal: list[dict] = []
    longs: list[str] = []
    for img in detail.get("images") or []:
        if not isinstance(img, dict):
            continue
        url = str(img.get("url") or "")
        if not url or url in video_cover_urls:
            continue
        width = _safe_img_side(img.get("width"))
        height = _safe_img_side(img.get("height"))
        if width > 0 and height / width > _ANN_LONG_IMAGE_RATIO:
            longs.append(url)
        else:
            normal.append({"url": url, "width": width, "height": height})
    return normal[:_ANN_CARD_IMAGE_LIMIT], longs[:_ANN_LONG_IMAGE_LIMIT]


async def _announce_image_b64(
    url: str, quality: int = 80, cover_size: tuple | None = None
) -> str:
    """公告配图直链 → base64（下载缓存 + webp 压缩；失败空串降级）。

    cover_size 传 (宽, 高) 时中心裁切到该尺寸（列表封面缩略用，EndUID 同参）。
    """
    if not url:
        return ""
    try:
        from ..utils.render import get_image_b64_with_cache
        from ..utils.resource.RESOURCE_PATH import CACHE_PATH

        return await get_image_b64_with_cache(
            url, CACHE_PATH / "announce", quality=quality, cover_size=cover_size
        )
    except Exception as e:
        logger.debug(f"[ArknightsUID] 公告配图下载失败: {e}")
        return ""


async def _announce_long_image_bytes(url: str) -> bytes:
    """超长公告图 → 独立追发用图片 bytes（下载缓存 + JPEG；失败空跳过）。"""
    if not url:
        return b""
    try:
        from gsuid_core.utils.image.convert import convert_img

        from ..utils.image import pic_download_from_url
        from ..utils.resource.RESOURCE_PATH import CACHE_PATH

        img = await pic_download_from_url(CACHE_PATH / "announce", url)
        return await convert_img(img)
    except Exception as e:
        logger.warning(
            f"[ArknightsUID] 公告超长图处理失败: {e.__class__.__name__}"
        )
        return b""


async def _show_ann_detail(bot: Bot, ev: Event, index: int) -> None:
    anns = await get_ark_ann_list()
    if not anns:
        return await _send_text(bot, ev, "公告列表获取失败，请稍后重试。")
    if not 1 <= index <= len(anns):
        return await _send_text(
            bot, ev, f"编号超出范围（1~{len(anns)}），请先发送「ark公告」查看列表。"
        )
    ann = anns[index - 1]
    detail = await get_ann_detail(ann["id"])
    if not detail:
        return await _send_text(
            bot, ev, f"公告详情获取失败：{ann['title']}\n{ann.get('url', '')}"
        )
    # 配图三路（对齐 EndUID）：普通图全部内嵌卡片、视频封面作封面位、
    # 超长图（维护整图）下载后按原画质在卡片后单独追发
    normal_images, long_urls = split_ann_images(detail)
    card_images: list[dict] = []
    for img in normal_images:
        b64 = await _announce_image_b64(img["url"])
        if b64:
            card_images.append(
                {"b64": b64, "width": img["width"], "height": img["height"]}
            )
    video_covers: list[dict] = []
    for video in detail.get("videos") or []:
        b64 = await _announce_image_b64(str(video.get("coverUrl") or ""))
        if b64:
            video_covers.append({"b64": b64})
    avatar_b64 = await _announce_image_b64(
        str(detail.get("userAvatar") or ""), quality=70
    )
    long_images = [
        await _announce_long_image_bytes(url) for url in long_urls
    ]
    long_images = [img for img in long_images if img]
    context = {
        "version": ArknightsUID_version,
        "index": index,
        "title": detail.get("title", ""),
        "date": detail.get("date", ""),
        "author": detail.get("userName", ""),
        "avatar": avatar_b64,
        "ip": detail.get("userIpLocation", ""),
        "url": f"https://www.skland.com/article?id={detail.get('id', '')}",
        "paragraphs": [
            p[:400] for p in (detail.get("paragraphs") or [])[:30]
        ],
        "images": card_images,
        "videos": video_covers,
        "longImageCount": len(long_images),
        "imageCount": len(detail.get("images") or []),
        "videoCount": len(detail.get("videos") or []),
    }
    await send_card_or_text(
        bot, ev,
        "ark_announce_detail.html",
        context,
        _format_ann_detail(detail),
        extra_images=long_images,
    )


async def _activity_banner_b64(url: str) -> str:
    """活动标题图直链 → base64（下载缓存 + webp 压缩；失败空串降级）。"""
    if not url:
        return ""
    try:
        from ..utils.render import get_image_b64_with_cache
        from ..utils.resource.RESOURCE_PATH import CACHE_PATH

        return await get_image_b64_with_cache(
            url, CACHE_PATH / "activity", quality=82
        )
    except Exception as e:
        logger.debug(f"[ArknightsUID] 活动标题图下载失败: {e}")
        return ""


@push_sv.on_command(
    (
        "活动列表",
        "方舟活动",
        "活动",
    ),
    block=True,
)
async def show_ark_activities(bot: Bot, ev: Event) -> None:
    activities = await query_activities()
    pools = await query_gacha_pools()
    # 官方标题图（PRTS 活动页信息框，批量两次 API；失败按无图降级）
    banner_urls: dict[str, str] = {}
    try:
        from ..utils.prts.client import activity_banner_urls

        banner_urls = await activity_banner_urls([a.title for a in activities])
    except Exception as e:
        logger.debug(f"[ArknightsUID] 活动标题图拉取失败: {e}")
    activity_rows = []
    for act in activities:
        activity_rows.append(
            {
                "title": act.title,
                "start": act.start,
                "end": act.end,
                "image": await _activity_banner_b64(
                    banner_urls.get(act.title, "")
                ),
            }
        )
    context = {
        "version": ArknightsUID_version,
        "activities": activity_rows,
        "pools": [
            {
                "title": pool.title,
                "start": pool.start,
                "end": pool.end,
                # 卡池横幅：SMW 结果键即文件页（文件:xxx.jpg）→ md5 散列直链
                "image": await _activity_banner_b64(pool.image_url),
            }
            for pool in pools
        ],
    }
    await send_card_or_text(
        bot, ev, "ark_activity.html", context,
        _format_activities(activities, pools),
    )


@push_sv.on_command(
    (
        "公告",
        "方舟公告",
        "官方公告",
    ),
    block=True,
)
async def show_ark_announcements(bot: Bot, ev: Event) -> None:
    index_text = ev.text.strip()
    if index_text.isdigit():
        return await _show_ann_detail(bot, ev, int(index_text))
    anns = await get_ark_ann_list()
    if not anns:
        return await _send_text(bot, ev, _format_ann_list(anns))
    # 列表卡内嵌官方封面 + 作者头像（对齐 EndUID ann_list_card：
    # 封面中心裁切 400x200 缩略；纯文本公告无封面时按空串降级为纯文字行）
    ann_rows: list[dict] = []
    for ann in anns:
        ann_rows.append(
            {
                "title": ann["title"],
                "date": ann.get("date", ""),
                "url": ann.get("url", ""),
                "author": ann.get("author", ""),
                "cover": await _announce_image_b64(
                    ann.get("cover", ""), quality=60, cover_size=(400, 200)
                ),
                "avatar": await _announce_image_b64(
                    ann.get("avatar", ""), quality=70
                ),
            }
        )
    context = {
        "version": ArknightsUID_version,
        "anns": ann_rows,
    }
    await send_card_or_text(
        bot, ev, "ark_announce.html", context, _format_ann_list(anns)
    )


# 注册顺序约束：计划（含别名 刷图计划）必须先于 刷图推荐（含别名 刷图）注册，
# 否则「ark刷图计划」会被短词「刷图」按 startswith 抢走。


@push_sv.on_command(("计划", "刷图计划"), block=True)
async def show_farm_plan(bot: Bot, ev: Event) -> None:
    """ark计划 材料:数量 [...] → 直刷 vs 合成 规划（去哪刷/多少理智）。"""
    text = ev.text.strip()
    targets = _parse_plan_targets(text)
    if not targets:
        return await _send_text(
            bot, ev,
            "用法：ark计划 材料:数量 [材料:数量 …]（如 ark计划 聚酸酯块:10 龙门币:20000）",
        )

    try:
        from ArknightsUID.utils.gamedata.craft import craft_vs_farm, load_formulas
    except ModuleNotFoundError:
        from ArknightsUID.ArknightsUID.utils.gamedata.craft import (
            craft_vs_farm,
            load_formulas,
        )

    item_index = await load_item_index()
    matrix = await fetch_drop_matrix()
    stages = await fetch_stage_info()
    if not item_index or not matrix or not stages:
        return await _send_text(bot, ev, "数据源不可用（gamedata/一图流拉取失败），请稍后再试。")

    from ..utils.yituliu import latest_matrix_by_item

    matrix_by_item = latest_matrix_by_item(matrix)
    stage_meta = {str(s.get("stageId", "")): s for s in stages}
    formulas = await load_formulas()

    lines: list[str] = ["「明日方舟」刷图规划"]
    total_farm = 0.0
    unknown: list[str] = []
    card_rows: list[dict] = []
    for name, count in targets[:6]:
        item_id = _match_item_id(name, item_index)
        if not item_id:
            unknown.append(name)
            continue
        result = craft_vs_farm(
            item_id, count, formulas, item_index, matrix_by_item, stage_meta
        )
        head = f"◆ {result['name']} ×{count}"
        options: list[str] = []
        farm_row = None
        craft_row = None
        if result["farm"]:
            options.append(
                f"直刷 {result['farm']['stage_code']}（{result['farm']['ap_expect']:.0f} 理智/件"
                f"≈{result['farm_sanity']:.0f} 理智，需 {count / result['farm']['rate']:.0f} 把）"
            )
            farm_row = {
                "stage_code": result["farm"]["stage_code"],
                "ap_expect": result["farm"]["ap_expect"],
                "sanity": result["farm_sanity"],
                "times": count / result["farm"]["rate"],
            }
            if result["farm_sanity"] is not None:
                total_farm += result["farm_sanity"]
        if result["craft"] is not None:
            if result["craft_sanity"] is not None:
                leaves = " + ".join(
                    f"{item_index[lid]['name']}×{ln}"
                    if lid in item_index else lid
                    for lid, ln in result["craft_leaves"].items()
                )
                options.append(
                    f"合成（叶子材料 {leaves} ≈{result['craft_sanity']:.0f} 理智）"
                )
                craft_row = {
                    "sanity": result["craft_sanity"],
                    "leaves": leaves,
                }
            else:
                options.append("合成可行，但部分子材料无掉落数据")
                craft_row = {"sanity": None, "leaves": "部分子材料无掉落数据"}
        note = "" if options else "没有样本充足的掉落关卡"
        if not options:
            lines.append(f"{head}：没有样本充足的掉落关卡")
        else:
            lines.append(f"{head}：" + "｜".join(options))
        card_rows.append(
            {
                "name": result["name"],
                "count": count,
                "icon": "",
                "farm": farm_row,
                "craft": craft_row,
                "note": note,
            }
        )
    if unknown:
        lines.append(f"未识别：{'、'.join(unknown)}（支持精确材料名）")
    lines.append(f"直刷合计 ≈{total_farm:.0f} 理智")
    lines.append("数据：ArknightsGameData + ark.yituliu.cn（样本>300 关卡）")
    # 材料图标（失败空串降级）
    try:
        from ..utils.gamedata.itemicon import item_icon_b64

        for row in card_rows:
            row["icon"] = await item_icon_b64(row["name"])
    except Exception as e:
        logger.debug(f"[ArknightsUID] 计划卡材料图标获取失败: {e}")
    await send_card_or_text(
        bot, ev,
        "ark_farm_plan.html",
        {
            "rows": card_rows,
            "total_farm": total_farm,
            "unknown": unknown,
            "version": ArknightsUID_version,
        },
        "\n".join(lines),
    )


@push_sv.on_command(
    (
        "刷图推荐",
        "刷图",
    ),
    block=True,
)
async def show_stage_recommend(bot: Bot, ev: Event) -> None:
    query = ev.text.strip()
    records = await fetch_stage_info()
    if not records:
        return await _send_text(bot, ev, "一图流关卡数据获取失败，请稍后重试。")
    if query:
        rows = rank_stages(records, query)
        if not rows:
            return await _send_text(
                bot, ev, f"未找到分组「{query}」，可选：主线 / 别传 / 限时活动 / 资源收集"
            )
        lines = [f"「明日方舟」{query} · 刷图效率榜（一图流 spm）"]
        for record in rows:
            lines.append(
                f"  {record.get('stageCode', '?')} {record.get('zoneName', '')} "
                f"spm {float(record.get('spm') or 0):.2f} · {record.get('apCost', '?')} 理智"
            )
        max_spm = max(float(r.get("spm") or 0) for r in rows) or 1.0
        context = {
            "version": ArknightsUID_version,
            "group": query,
            "rows": [
                {
                    "stage_code": str(r.get("stageCode", "?")),
                    "zone": str(r.get("zoneName", "")),
                    "spm": float(r.get("spm") or 0),
                    "ap_cost": r.get("apCost", "?"),
                    "spm_pct": round(float(r.get("spm") or 0) / max_spm * 100),
                }
                for r in rows
            ],
        }
        return await send_card_or_text(
            bot, ev, "ark_stage_rank.html", context, "\n".join(lines)
        )
    await _send_text(bot, ev, format_stage_board(records))


@push_sv.on_command(("材料掉率", "掉率"), block=True)
async def show_drop_rate(bot: Bot, ev: Event) -> None:
    query = ev.text.strip()
    if not query:
        return await _send_text(
            bot, ev, "用法：ark材料掉率 <材料名>（如 ark材料掉率 固源岩组）"
        )

    item_index = await load_item_index()
    item_id = ""
    if item_index:
        # 精确名 → 唯一前缀
        exact = [
            iid for iid, info in item_index.items() if info.get("name") == query
        ]
        if len(exact) == 1:
            item_id = exact[0]
        else:
            prefix = [
                iid
                for iid, info in item_index.items()
                if str(info.get("name", "")).startswith(query)
            ]
            if len(prefix) == 1:
                item_id = prefix[0]
            elif len(prefix) > 1:
                names = "、".join(
                    str(item_index[i].get("name", "")) for i in prefix[:8]
                )
                return await _send_text(
                    bot, ev, f"「{query}」匹配到多种材料：{names}，请写全名。"
                )
    if not item_id:
        return await _send_text(
            bot, ev,
            f"未找到材料「{query}」（物品数据首次使用需联网下载，可稍后重试）。",
        )

    matrix = await fetch_drop_matrix()
    stages = await fetch_stage_info()
    if not matrix or not stages:
        return await _send_text(bot, ev, "掉落数据获取失败（一图流），请稍后重试。")

    stage_meta = {str(s.get("stageId", "")): s for s in stages}
    stats = item_drop_stats(
        item_id, latest_matrix_by_item(matrix), stage_meta, top=8
    )
    item_name = str((item_index.get(item_id) or {}).get("name", "") or query)
    if not stats:
        return await _send_text(
            bot, ev, f"「{item_name}」没有样本充足的掉落关卡（样本>300 才收录）。"
        )

    lines = [
        f"「明日方舟」{item_name} · 掉落关卡（一图流，按掉率降序）",
        "  关卡 · 每把期望 · 单件理智 · 样本",
    ]
    for row in stats:
        lines.append(
            f"  {row['stage_code']} {row['zone'][:8]} · "
            f"{row['rate']:.3f} 件 · {row['ap_expect']:.0f} 理智/件 · "
            f"{row['times']:,} 把"
        )
    lines.append("数据：ark.yituliu.cn（COS 掉落矩阵）")

    max_rate = max(r["rate"] for r in stats) or 1.0
    context = {
        "version": ArknightsUID_version,
        "item_name": item_name,
        "item_icon": await item_icon_b64(item_name),
        "rows": [
            {
                "stage_code": r["stage_code"],
                "zone": r["zone"],
                "rate": r["rate"],
                "ap_expect": r["ap_expect"],
                "times": r["times"],
                "rate_pct": round(r["rate"] / max_rate * 100),
            }
            for r in stats
        ],
    }
    await send_card_or_text(
        bot, ev, "ark_drop_rate.html", context, "\n".join(lines)
    )


@push_sub_sv.on_fullmatch(("订阅方舟活动", "订阅活动"))
async def subscribe_ark_activities(bot: Bot, ev: Event) -> None:
    if ev.group_id is None:
        return await bot.send("请在群聊中订阅")

    data = await gs_subscribe.get_subscribe(TASK_NAME)
    resubscribed = False
    if data:
        for subscribe in data:
            if subscribe.group_id == ev.group_id:
                await gs_subscribe.delete_subscribe("session", TASK_NAME, ev)
                resubscribed = True
                break
    await gs_subscribe.add_subscribe(
        "session", task_name=TASK_NAME, event=ev, extra_message=""
    )
    # 首次订阅即初始化基线，避免把存量活动当新活动轰炸
    state = load_push_state()
    if "seen" not in state or not state.get("seen"):
        entries = await collect_current_entries()
        if entries:
            # 必须落盘才成基线——此前返回值被丢弃，基线实际未建立
            save_push_state({"seen": list(entries.keys())})
    await bot.send(
        "已重新订阅方舟活动推送！" if resubscribed else "成功订阅方舟活动推送！"
    )


@push_sub_sv.on_fullmatch(("取消订阅方舟活动", "取消订阅活动", "退订方舟活动"))
async def unsubscribe_ark_activities(bot: Bot, ev: Event) -> None:
    if ev.group_id is None:
        return await bot.send("请在群聊中取消订阅")
    data = await gs_subscribe.get_subscribe(TASK_NAME)
    if data:
        for subscribe in data:
            if subscribe.group_id == ev.group_id:
                await gs_subscribe.delete_subscribe("session", TASK_NAME, ev)
                return await bot.send("成功取消订阅方舟活动！")
    await bot.send("未曾订阅方舟活动！")


@push_sv.on_command(
    (
        "订阅公告",
        "订阅方舟公告",
    ),
    block=True,
)
async def subscribe_ark_announcements(bot: Bot, ev: Event) -> None:
    """订阅官方公告专项推送（session 模式：群聊按群、私聊按人各一条）。"""
    await gs_subscribe.add_subscribe(
        "session", task_name=ANN_TASK_NAME, event=ev, extra_message=""
    )
    # 首次订阅建基线：存量公告不当新公告轰炸（已有基线则不动）
    if not _load_ann_state().get("seen"):
        try:
            current = await get_ark_ann_list(limit=10)
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·公告推送] 基线拉取失败: {exc.__class__.__name__}"
            )
            current = []
        if current:
            _save_ann_state({"seen": [a["id"] for a in current if a.get("id")]})

    lines = ["已订阅官方公告推送（仅公告；活动/卡池请用 ark订阅方舟活动）"]
    try:
        latest = await get_ark_ann_list(limit=3)
    except Exception:
        latest = []
    if latest:
        lines.append("最近公告：")
        for index, ann in enumerate(latest, start=1):
            date = f" · {ann['date']}" if ann.get("date") else ""
            lines.append(f"{index}. {ann['title']}{date}")
    lines.append("新公告发布后会在这里推送。取消：ark取消订阅公告")
    await _send_text(bot, ev, "\n".join(lines))


@push_sv.on_command(
    (
        "取消订阅公告",
        "取消方舟公告",
        "退订公告推送",
        "退订方舟公告",
    ),
    block=True,
)
async def unsubscribe_ark_announcements(bot: Bot, ev: Event) -> None:
    data = await gs_subscribe.get_subscribe(ANN_TASK_NAME) or []
    # session 模式：群聊按 group_id 定位本群订阅，私聊按 user_id
    mine = [
        subscribe
        for subscribe in data
        if (
            ev.group_id and subscribe.group_id == ev.group_id
        ) or (
            not ev.group_id and subscribe.user_id == ev.user_id
        )
    ]
    if not mine:
        return await _send_text(bot, ev, "未曾订阅官方公告。")
    await gs_subscribe.delete_subscribe("session", ANN_TASK_NAME, ev)
    await _send_text(bot, ev, "已取消官方公告推送。")


async def collect_current_entries() -> dict[str, list[dict[str, str]]]:
    """汇总当前活动/卡池/公告 → {key: 展示行}。单项失败跳过（不阻塞推送）。"""
    entries: dict[str, list[dict[str, str]]] = {}
    try:
        for act in await query_activities(limit=20):
            entries.setdefault(_activity_key("活动", act.title), []).append(
                {"kind": "活动", "title": act.title,
                 "detail": f"{act.start} ~ {act.end}"}
            )
    except Exception as exc:
        logger.warning(f"[ArknightsUID·推送] 活动拉取失败: {exc.__class__.__name__}")
    try:
        for pool in await query_gacha_pools(limit=10):
            entries.setdefault(_activity_key("卡池", pool.title), []).append(
                {"kind": "卡池", "title": pool.title,
                 "detail": f"{pool.start} ~ {pool.end}"}
            )
    except Exception as exc:
        logger.warning(f"[ArknightsUID·推送] 卡池拉取失败: {exc.__class__.__name__}")
    try:
        for ann in await get_ark_ann_list(limit=10):
            if ann.get("id"):
                key = _activity_key("公告", ann["id"])
                entries.setdefault(key, []).append(
                    {"kind": "公告", "title": ann["title"], "detail": ""}
                )
    except Exception as exc:
        logger.warning(f"[ArknightsUID·推送] 公告拉取失败: {exc.__class__.__name__}")
    return entries


def diff_new_entries(
    entries: dict[str, list[dict[str, str]]], seen: list[str]
) -> list[dict[str, str]]:
    """当前条目中未见过的（保留插入序；条目附带其去重 key）。"""
    seen_set = set(seen)
    return [
        {**items[0], "key": key}
        for key, items in entries.items() if key not in seen_set
    ]


def _push_check_minutes() -> int:
    """AnnCheckMinute 配置 → 轮询间隔（分钟），异常值回落默认 30。"""
    try:
        value = int(ArkConfig.get_config("AnnCheckMinute").data or 30)
    except Exception:
        value = 30
    return value if value > 0 else 30


def _push_enabled() -> bool:
    """AnnPushEnable 配置 → 推送总开关（False 时定时任务空转）。"""
    try:
        return bool(ArkConfig.get_config("AnnPushEnable").data)
    except Exception:
        return True


async def _push_new_announcements(datas) -> None:
    """公告专项订阅：新公告 → 逐订阅推送（独立 seen 基线，与活动订阅互不影响）。"""
    try:
        anns = await get_ark_ann_list(limit=10)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·公告推送] 公告拉取失败: {exc.__class__.__name__}"
        )
        return
    if not anns:
        return

    async with _push_state_lock:
        state = _load_ann_state()
        seen: list[str] = [str(key) for key in (state.get("seen") or [])]
        if not seen:
            # 无基线（首次运行/状态丢失）：只建基线不推送
            _save_ann_state({"seen": [a["id"] for a in anns if a.get("id")]})
            logger.info("[ArknightsUID·公告推送] 基线初始化完成，下轮开始检测")
            return

        fresh = diff_new_announcements(anns, seen)
        if not fresh:
            logger.info("[ArknightsUID·公告推送] 无新增")
            return

        # 单轮最多推 5 条；其余留下轮增量推
        fresh_ids = [str(ann["id"]) for ann in fresh[:_ANN_PUSH_LIMIT]]
        logger.info(
            f"[ArknightsUID·公告推送] 推送 {len(fresh_ids)} 条新公告"
            f"（会话 {len(datas)} 个）"
        )
        delivered: dict[str, list[str]] = {
            str(k): list(v or [])
            for k, v in (state.get("delivered") or {}).items()
            if isinstance(v, list)
        }
        delivered_before = {
            k: list(v) for k, v in delivered.items()
        }

        def _build_ann_message(ids: list[str]) -> str:
            return format_ann_push_message(
                [a for a in fresh if str(a["id"]) in ids]
            )

        delivered, _any = await _deliver_per_session(
            datas, fresh_ids, _build_ann_message, delivered, "公告推送"
        )
        # 条目对全部会话投递完成 → 推进全局 seen 并清掉其投递进度；
        # 部分完成 → seen 不动、delivered 落盘，下轮只补发未覆盖会话
        sessions = {_session_key(sub) for sub in datas}
        done = [
            key for key in fresh_ids
            if sessions and sessions.issubset(set(delivered.get(key) or []))
        ]
        if done or delivered != delivered_before:
            _save_ann_state({
                "seen": seen + done,
                "delivered": {
                    k: v for k, v in delivered.items() if k not in done
                },
            })
        else:
            # 全部投递失败且无新进度：不落盘（seen/delivered 均未变），
            # 下轮整批重试
            logger.warning(
                "[ArknightsUID·公告推送] 全部订阅推送失败，本轮不落盘，下轮重试"
            )


@scheduler.scheduled_job("interval", minutes=_push_check_minutes(), id="ark_push_check")
async def check_ark_activities() -> None:
    """定时检测新活动/卡池/公告并推送订阅群。"""
    if not _push_enabled():
        logger.debug("[ArknightsUID·推送] AnnPushEnable 已关闭，跳过本轮检查")
        return
    datas = await gs_subscribe.get_subscribe(TASK_NAME)
    ann_datas = await gs_subscribe.get_subscribe(ANN_TASK_NAME)
    if not datas and not ann_datas:
        return
    logger.info("[ArknightsUID·推送] 定时任务：检查活动/卡池/公告更新..")

    if ann_datas:
        await _push_new_announcements(ann_datas)
    if not datas:
        return

    entries = await collect_current_entries()
    if not entries:
        logger.info("[ArknightsUID·推送] 本次未拉到任何条目，跳过")
        return

    async with _push_state_lock:
        state = load_push_state()
        seen: list[str] = list(state.get("seen") or [])
        if not seen:
            # 无基线（首次运行/状态丢失）：只建基线不推送
            save_push_state({"seen": list(entries.keys())})
            logger.info("[ArknightsUID·推送] 基线初始化完成，下轮开始检测")
            return

        fresh = diff_new_entries(entries, seen)
        if not fresh:
            logger.info("[ArknightsUID·推送] 无新增")
            return

        # 单轮最多推 8 条；seen 只标实际推过的 key（多余的留下轮增量推，
        # 否则一次性全标会让第 9 条起永久漏推）
        seen_set = set(seen)
        fresh_keys = [k for k in entries.keys() if k not in seen_set]
        pushed_keys = fresh_keys[:8]

        fresh_by_key = {item["key"]: item for item in fresh if item.get("key")}
        logger.info(
            f"[ArknightsUID·推送] 推送 {len(pushed_keys)} 条新动态"
            f"（会话 {len(datas)} 个）"
        )

        def _build_entry_message(keys: list[str]) -> str:
            lines = ["「明日方舟」新动态"]
            for key in keys:
                item = fresh_by_key.get(key)
                if not item:
                    continue
                detail = f"（{item['detail']}）" if item.get("detail") else ""
                lines.append(f"[{item['kind']}] {item['title']}{detail}")
            return "\n".join(lines)

        delivered: dict[str, list[str]] = {
            str(k): list(v or [])
            for k, v in (state.get("delivered") or {}).items()
            if isinstance(v, list)
        }
        delivered_before = {
            k: list(v) for k, v in delivered.items()
        }
        delivered, _any = await _deliver_per_session(
            datas, pushed_keys, _build_entry_message, delivered, "推送"
        )
        # 条目对全部会话投递完成 → 推进全局 seen；部分完成 → 下轮补发
        sessions = {_session_key(sub) for sub in datas}
        done = [
            key for key in pushed_keys
            if sessions and sessions.issubset(set(delivered.get(key) or []))
        ]
        if done or delivered != delivered_before:
            save_push_state({
                "seen": seen + done,
                "delivered": {
                    k: v for k, v in delivered.items() if k not in done
                },
            })
        logger.info("[ArknightsUID·推送] 推送完毕")


# ---------------------------------------------------------------------------
# 材料合成树（ark合成）与刷图规划（ark计划）
# ---------------------------------------------------------------------------


def _match_item_id(query: str, item_index: dict) -> str | None:
    """材料名 → itemId：精确 → 唯一前缀。歧义/无匹配返回 None。"""
    exact = [iid for iid, info in item_index.items() if info.get("name") == query]
    if len(exact) == 1:
        return exact[0]
    prefix = [
        iid for iid, info in item_index.items()
        if str(info.get("name", "")).startswith(query)
    ]
    return prefix[0] if len(prefix) == 1 else None


def _parse_plan_targets(text: str) -> list[tuple[str, int]]:
    """「材料:数量 材料:数量 …」→ [(name, count)]；数量默认 1。"""
    targets: list[tuple[str, int]] = []
    for token in (text or "").split():
        if ":" in token or "：" in token:
            name, _, count_text = token.replace("：", ":").partition(":")
        else:
            name, count_text = token, "1"
        try:
            count = max(1, int(count_text))
        except ValueError:
            count = 1
        if name:
            targets.append((name, count))
    return targets


@push_sv.on_command(("合成树", "合成"), block=True)
async def show_craft_tree(bot: Bot, ev: Event) -> None:
    """ark合成 材料 → 加工站合成树（含子材料递归）+ 直刷对比。"""
    query = ev.text.strip()
    if not query:
        return await _send_text(bot, ev, "用法：ark合成 材料名（如 ark合成 聚酸酯块）")

    try:
        from ArknightsUID.utils.gamedata.craft import (
            craft_tree,
            format_craft_tree,
            load_formulas,
        )
    except ModuleNotFoundError:
        from ArknightsUID.ArknightsUID.utils.gamedata.craft import (
            craft_tree,
            format_craft_tree,
            load_formulas,
        )

    item_index = await load_item_index()
    if not item_index:
        return await _send_text(bot, ev, "物品数据不可用（gamedata 拉取失败），请稍后再试。")
    item_id = _match_item_id(query, item_index)
    if not item_id:
        return await _send_text(bot, ev, f"未找到材料「{query}」（或匹配到多种，请写全名）。")

    formulas = await load_formulas()
    if not formulas or item_id not in formulas:
        return await _send_text(
            bot, ev, f"「{item_index[item_id]['name']}」不可合成（无加工站配方）。"
        )

    tree = craft_tree(item_id, formulas, item_index, need=1)
    lines = [f"「明日方舟」{item_index[item_id]['name']} · 合成树"]
    lines.extend(format_craft_tree(tree, item_index))
    lines.append("数据：ArknightsGameData（workshopFormulas）；合成需加工站 Lv3")

    # 卡片行：树展平为带缩进层级的节点列表（图标仅首两层，避免逐叶下载）
    from ..utils.gamedata.itemicon import item_icon_b64

    card_rows: list[dict] = []

    async def _walk(node: dict, depth: int) -> None:
        costs = node.get("costs") or []
        if depth <= 1 and costs:
            for cost in costs:
                cost["icon"] = await item_icon_b64(cost.get("name") or "")
        card_rows.append(
            {
                "depth": depth,
                "name": node.get("name") or node.get("item_id"),
                "need": node.get("need") or 0,
                "craftable": bool(node.get("craftable")),
                "output": node.get("output") or 0,
                "gold": node.get("gold") or 0,
                "costs": costs,
            }
        )
        for child in node.get("children") or []:
            await _walk(child, depth + 1)

    await _walk(tree, 0)
    await send_card_or_text(
        bot, ev,
        "ark_craft.html",
        {
            "name": item_index[item_id]["name"],
            "need": 1,
            "icon": await item_icon_b64(item_index[item_id]["name"]),
            "rows": card_rows,
            "version": ArknightsUID_version,
        },
        "\n".join(lines),
    )
