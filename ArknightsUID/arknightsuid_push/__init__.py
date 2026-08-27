"""活动 / 公告查询 + 推送订阅（P4）。

- `ark活动`：进行中的限时活动 + 限定卡池清单（PRTS SMW，与日历同源）
- `ark公告 [编号]`：明日方舟官方公告列表（森空岛 web 公共接口）
- `订阅方舟活动` / `取消订阅方舟活动`（pm=3，群聊）：gs_subscribe 订阅
- 定时轮询（30 分钟）：新活动 / 新卡池 / 新公告 → 推送订阅群

推送状态（已见条目键）落 MAIN_PATH/push_state.json，条目上限防膨胀。
"""
from __future__ import annotations

import asyncio
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
    from ArknightsUID.ArknightsUID.utils.gamedata.charinfo import load_item_index
    from ArknightsUID.ArknightsUID.utils.gamedata.itemicon import item_icon_b64
    from ArknightsUID.ArknightsUID.utils.path import MAIN_PATH
    from ArknightsUID.ArknightsUID.utils.prts.client import (
        query_activities,
        query_gacha_pools,
    )
    from ArknightsUID.ArknightsUID.utils.skland.web import get_ark_ann_list
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
    from ArknightsUID.utils.gamedata.charinfo import load_item_index
    from ArknightsUID.utils.gamedata.itemicon import item_icon_b64
    from ArknightsUID.utils.path import MAIN_PATH
    from ArknightsUID.utils.prts.client import (
        query_activities,
        query_gacha_pools,
    )
    from ArknightsUID.utils.skland.web import get_ark_ann_list
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


def _activity_key(kind: str, title: str) -> str:
    return f"{kind}:{title}"


def load_push_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_push_state(state: dict) -> None:
    try:
        seen = state.get("seen")
        if isinstance(seen, list) and len(seen) > _STATE_LIMIT:
            state["seen"] = seen[-_STATE_LIMIT:]
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·推送] 状态写入失败: {exc.__class__.__name__}"
        )


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
    return "\n".join(lines)


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
        "活动",
        "方舟活动",
        "活动列表",
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
            {"title": pool.title, "start": pool.start, "end": pool.end}
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
    anns = await get_ark_ann_list()
    if not anns:
        return await _send_text(bot, ev, _format_ann_list(anns))
    context = {
        "version": ArknightsUID_version,
        "anns": [
            {"title": ann["title"], "date": ann.get("date", ""),
             "url": ann.get("url", "")}
            for ann in anns
        ],
    }
    await send_card_or_text(
        bot, ev, "ark_announce.html", context, _format_ann_list(anns)
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
        await collect_current_entries()
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
    """当前条目中未见过的（保留插入序）。"""
    seen_set = set(seen)
    return [items[0] for key, items in entries.items() if key not in seen_set]


@scheduler.scheduled_job("interval", minutes=30, id="ark_push_check")
async def check_ark_activities() -> None:
    """定时检测新活动/卡池/公告并推送订阅群。"""
    datas = await gs_subscribe.get_subscribe(TASK_NAME)
    if not datas:
        return
    logger.info("[ArknightsUID·推送] 定时任务：检查活动/卡池/公告更新..")

    entries = await collect_current_entries()
    if not entries:
        logger.info("[ArknightsUID·推送] 本次未拉到任何条目，跳过")
        return

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

    lines = ["「明日方舟」新动态"]
    for item in fresh[:8]:
        detail = f"（{item['detail']}）" if item.get("detail") else ""
        lines.append(f"[{item['kind']}] {item['title']}{detail}")
    message = "\n".join(lines)
    logger.info(f"[ArknightsUID·推送] 推送 {len(fresh)} 条新动态")

    for subscribe in datas:
        try:
            await subscribe.send(message)
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·推送] 推送失败 group={subscribe.group_id}: "
                f"{exc.__class__.__name__}"
            )
        await asyncio.sleep(random.uniform(1, 3))

    seen.extend(entries.keys())
    save_push_state({"seen": seen})
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


@push_sv.on_command(("合成", "合成树"), block=True)
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
