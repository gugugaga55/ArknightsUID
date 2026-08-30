"""抽卡记录 / 礼包码兑换 / 攒抽规划命令（P3，鹰角 OAuth 链）。

命令（前缀 ark/方舟/明日方舟）：
- 抽卡记录 / 抽卡分析 [序号|UID]：增量拉取官方寻访记录并出统计卡
- 兑换码 <code>：官网礼包码兑换（纯文本回复）
- 攒抽 / 抽卡规划 [参数]：攒抽规划卡（自动垫刀 + 自动当前卡池，
  参数 玉: 石: 单: 十: 垫刀: 池: 类型:单UP|双UP 天数: 目标抽数）
"""
from __future__ import annotations

import asyncio
from typing import Any

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        hide_uid_for,
        resolve_credential,
    )
    from ArknightsUID.ArknightsUID.utils.gacha_plan import (
        build_plan_context,
        build_summary,
        format_plan_text,
        parse_plan_args,
    )
    from ArknightsUID.ArknightsUID.utils.hypergryph.client import (
        exchange_gift_code,
        login_gacha_chain,
    )
    from ArknightsUID.ArknightsUID.utils.gamedata.charinfo import load_char_index
    from ArknightsUID.ArknightsUID.utils.hypergryph.gacha import (
        analyze_gacha,
        build_gacha_context,
        format_gacha_text,
        load_local_logs,
        save_local_logs,
        sync_gacha_logs,
    )
    from ArknightsUID.ArknightsUID.utils.player.context import char_avatar_b64
    from ArknightsUID.ArknightsUID.utils.prts.client import query_gacha_pools
    from ArknightsUID.ArknightsUID.utils.reply_safety import safe_external_message
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.utils.enduid.adapter import (
        hide_uid,
        hide_uid_for,
        resolve_credential,
    )
    from ArknightsUID.utils.gacha_plan import (
        build_plan_context,
        build_summary,
        format_plan_text,
        parse_plan_args,
    )
    from ArknightsUID.utils.hypergryph.client import (
        exchange_gift_code,
        login_gacha_chain,
    )
    from ArknightsUID.utils.gamedata.charinfo import load_char_index
    from ArknightsUID.utils.hypergryph.gacha import (
        analyze_gacha,
        build_gacha_context,
        format_gacha_text,
        load_local_logs,
        save_local_logs,
        sync_gacha_logs,
    )
    from ArknightsUID.utils.player.context import char_avatar_b64
    from ArknightsUID.utils.prts.client import query_gacha_pools
    from ArknightsUID.utils.reply_safety import safe_external_message
    from ArknightsUID.version import ArknightsUID_version

gacha_sv = SV("ArknightsUID抽卡", priority=5)


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


_PLAN_USAGE = (
    "用法：ark攒抽 [玉:数量] [石:数量] [单:数量] [十:数量] "
    "[垫刀:数量] [类型:单UP|双UP] [池:关键词] [天数:N] [目标抽数]\n"
    "示例：ark攒抽 玉:60000 石:30 垫刀:20"
)


def _gift_reply(response: dict) -> str:
    code = response.get("code")
    # 上游 message 可能回显请求细节，统一脱敏后再进回复
    message = safe_external_message(
        str(response.get("message") or response.get("msg") or "")
    )
    if message:
        message = message[:120]
    if code == 0:
        return f"兑换码提交成功\n{message or 'OK'}"
    return f"兑换失败：code={code}，{message or '无详情'}"


@gacha_sv.on_command(
    (
        "抽卡记录",
        "抽卡分析",
        "寻访记录",
    ),
    block=True,
)
async def show_gacha_report(bot: Bot, ev: Event) -> None:
    uid = extract_uid(ev.text)
    index = extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    records, warning = await sync_gacha_logs(credential)
    if not records:
        return await _send_text(bot, ev, warning or "暂无抽卡记录。")

    nickname = credential.nickname or ""
    shown_uid = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    context = build_gacha_context(
        nickname, shown_uid, records, ArknightsUID_version
    )
    text = format_gacha_text(nickname, shown_uid, records, warning)
    if context is not None:
        # 最近六星逐个挂初始头像（gamedata 索引精确名反查，失败空串降级）
        try:
            char_index = await load_char_index()
            if char_index:
                for row in context.get("recent", []):
                    hit = next(
                        (cid for cid, info in char_index.items()
                         if info.get("name") == row.get("name")),
                        "",
                    )
                    row["avatar"] = await char_avatar_b64(hit) if hit else ""
        except Exception:
            pass
        return await send_card_or_text(
            bot, ev, "ark_gacha_card.html", context, text
        )
    await _send_text(bot, ev, text)


@gacha_sv.on_command(
    (
        "兑换码",
        "礼包码",
    ),
    block=True,
)
async def redeem_gift_code(bot: Bot, ev: Event) -> None:
    code = ev.text.strip().split()[0] if ev.text.strip() else ""
    if not code:
        return await _send_text(bot, ev, "用法：ark兑换码 <礼包码>")
    # 兑换码本身是敏感输入（防止误转发/回显），群聊不回显码值
    shown = code if not ev.group_id else "****"

    uid = extract_uid(ev.text.replace(code, "", 1))
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, 0)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    auth = await login_gacha_chain(credential)
    if isinstance(auth, str):
        return await _send_text(bot, ev, auth)

    try:
        response = await exchange_gift_code(credential, auth, code)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·兑换] 兑换请求异常: {exc.__class__.__name__}"
        )
        response = None
    if response is None:
        return await _send_text(bot, ev, f"兑换请求失败（{shown}），请稍后重试。")
    await _send_text(bot, ev, _gift_reply(response))


async def _auto_pity(credential) -> int:
    """读本地抽卡记录算当前垫刀（不联网拉新，无记录返回 0）。"""
    try:
        records = await load_local_logs(credential.uid)
        stats = analyze_gacha(records)
        return int(stats.get("pity") or 0)
    except Exception:
        return 0


async def _pick_pool(keyword: str) -> tuple[dict | None, str]:
    """选规划目标卡池：关键词模糊匹配，缺省取下一个结束的池。

    返回 (pool, err_text)；err_text 非空时 pool 为 None。
    """
    try:
        pools = await query_gacha_pools(limit=8)
    except Exception as exc:
        logger.warning(f"[ArknightsUID·攒抽] 卡池数据拉取失败: {exc}")
        return None, ""
    if not pools:
        return None, ""
    def to_dict(pool) -> dict:
        return {"title": pool.title, "end": pool.end, "end_ts": pool.end_ts}

    if not keyword:
        return to_dict(pools[0]), ""
    for pool in pools:
        if keyword in pool.title:
            return to_dict(pool), ""
    names = "、".join(p.title for p in pools[:5])
    return None, f"未找到包含「{keyword}」的进行中/即将开启卡池。当前：{names}"


@gacha_sv.on_command(
    (
        "攒抽规划",
        "抽卡规划",
        "攒抽",
    ),
    block=True,
)
async def plan_gacha_draws(bot: Bot, ev: Event) -> None:
    args = parse_plan_args(ev.text)
    if args.errors:
        return await _send_text(
            bot, ev, _PLAN_USAGE + "\n" + "；".join(args.errors)
        )

    # 参数 token（key:value）与 1-4 位独立数字（=目标抽数）不参与
    # UID/序号提取，否则 `玉:60000` 的数字会被误认成 UID。
    # 5 位以上纯数字是 UID（extract_uid 口径）必须保留（审计 #23）：
    # 旧版 `not t.isdigit()` 会把裸 UID 删掉，`ark攒抽 149809765`
    # 静默落到默认账号、垫刀数据查错人
    positional = " ".join(
        t for t in ev.text.split()
        if ":" not in t and "：" not in t
        and not (t.isascii() and t.isdigit() and len(t) <= 4)
    )
    uid = extract_uid(positional)
    index = extract_index(positional)
    # 单次解析凭据：读本地垫刀 + 展示身份共用
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        pity, name, uid_masked = 0, "", ""
    else:
        pity = await _auto_pity(credential)
        name = credential.nickname
        uid_masked = await hide_uid_for(
            credential.uid, ev.user_id, ev.bot_id
        )
    if args.pity is not None:
        pity = args.pity

    pool, pool_err = await _pick_pool(args.pool_keyword)
    if pool_err:
        return await _send_text(bot, ev, pool_err)

    # 概率 DP 纯 CPU（双UP双限定实测 ~0.4s）：下放线程池避免卡事件循环
    summary = await asyncio.to_thread(build_summary, args, pool, pity)

    context = build_plan_context(summary, name, uid_masked, ArknightsUID_version)
    text = format_plan_text(summary, name, uid_masked)
    if not uid_masked:
        # 未绑定：垫刀必然 0，提示可绑定后自动读垫刀
        context["notes"] = list(context["notes"]) + [
            "未绑定账号：绑定后自动读取本地抽卡记录的当前垫刀"
        ]
        text += "\n提示：绑定账号后可自动读取当前垫刀（ark绑定 / ark扫码登录）"
    await send_card_or_text(bot, ev, "ark_gacha_plan.html", context, text)


# ---------------------------------------------------------------------------
# ark年度：本地抽卡记录年度报告（纯文本娱乐总结，不联网）
# ---------------------------------------------------------------------------


def _annual_report(records: list[dict], year: str) -> str | None:
    """按年汇总：总抽数/六星数/六星率/最长垫刀/月份分布/新干员。"""
    from collections import Counter
    from datetime import datetime

    from ..utils.hypergryph.gacha import _legacy_one_based, _normalize_rarity

    _legacy = _legacy_one_based(records)

    def _year_of(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y") if ts else ""

    year_records = sorted(
        [r for r in records if _year_of(int(r.get("gacha_ts") or 0)) == year],
        key=lambda r: (r.get("gacha_ts", 0), r.get("pos", 0)),
    )
    if not year_records:
        return None

    total = len(year_records)
    sixes = [
        r for r in year_records
        if _normalize_rarity(r, _legacy) >= 6
    ]
    six_count = len(sixes)
    new_chars = [r for r in sixes if r.get("is_new")]

    # 垫刀 = 同 category 内上一个六星后的抽数；跨年界沿用同规则
    pity_by_cate: dict[str, int] = {}
    worst_pity = 0
    worst_record: dict | None = None
    for record in year_records:
        cate = str(record.get("category", ""))
        pity_by_cate[cate] = pity_by_cate.get(cate, 0) + 1
        if _normalize_rarity(record, _legacy) >= 6:
            pity = pity_by_cate[cate]
            if pity > worst_pity:
                worst_pity, worst_record = pity, record
            pity_by_cate[cate] = 0

    months = Counter(
        datetime.fromtimestamp(int(r.get("gacha_ts") or 0)).month
        for r in year_records
    )
    busiest = max(months.items(), key=lambda kv: kv[1])[0]

    lines = [
        f"「明日方舟」{year} 年寻访年度报告",
        f"总抽数：{total}（六星 {six_count}，六星率 "
        f"{six_count * 100 / total:.2f}%）",
        f"平均 {total / six_count:.1f} 抽一个六星" if six_count else "全年无六星……节哀",
        f"六星新干员：{len(new_chars)} 位"
        + (f"（{'、'.join(str(r['name']) for r in new_chars[:8])}）"
           if new_chars else ""),
    ]
    if worst_record is not None:
        # 计数含出货那一抽（"第 N 抽"口径）；文案写"前垫"须减 1，
        # 否则首个六星会显示"前垫了 1 抽"
        lines.append(
            f"最非时刻：{worst_record['name']} 第 {worst_pity} 抽才出货"
        )
    lines.append(f"最肝月份：{busiest} 月（{months[busiest]} 抽）")
    lines.append("（基于本地已同步记录；ark抽卡记录 先同步最新数据）")
    return "\n".join(lines)


@gacha_sv.on_command(("年度报告", "抽卡年度", "年度"), block=True)
async def send_annual_report(bot: Bot, ev: Event) -> None:
    from datetime import datetime

    args = (ev.text or "").strip().split()
    year = args[0] if args and args[0].isdigit() and len(args[0]) == 4 else str(
        datetime.now().year
    )
    uid = extract_uid(ev.text or "")
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, 0)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    records = await load_local_logs(credential.uid)
    if not records:
        return await _send_text(
            bot, ev,
            f"本地没有抽卡记录（{year} 年）。先用 ark抽卡记录 同步，再来看年度报告。",
        )
    report = _annual_report(records, year)
    if report is None:
        return await _send_text(
            bot, ev, f"{year} 年没有本地记录（可用年份见 ark抽卡分析）。"
        )
    await _send_text(bot, ev, report)


# ---------------------------------------------------------------------------
# 寻访模拟（ark十连 / ark单抽）：官方概率 + 十连保底的纯本地娱乐模拟
# ---------------------------------------------------------------------------


def _simulate_context(
    results: list[dict], summary: dict, count: int
) -> dict[str, Any]:
    """模拟结果 → 卡片上下文（profession 代码转中文名）。"""
    from ..utils.player.parse import PROFESSION_NAMES

    rows = []
    for row in results:
        rows.append(
            {
                "rarity": row["rarity"],
                "name": row["name"],
                "char_id": row.get("char_id") or "",
                "profession": PROFESSION_NAMES.get(
                    row.get("profession") or "", ""
                ),
                "avatar": "",
                "guarantee": bool(row.get("guarantee")),
            }
        )
    return {
        "count": count,
        "results": rows,
        "summary": summary,
        "version": ArknightsUID_version,
    }


def _format_sim_text(summary: dict, results: list[dict]) -> str:
    from ..utils.gamedata.gacha_sim import format_pulls_text

    lines = ["「明日方舟」寻访模拟（概率同官方，仅供娱乐）"]
    lines.append(
        f"六星 {summary.get(6, 0)} | 五星 {summary.get(5, 0)} | "
        f"四星 {summary.get(4, 0)} | 三星 {summary.get(3, 0)}"
    )
    lines.append(format_pulls_text(results))
    lines.append("不区分常驻/限定卡池，不累计真实垫刀；真实记录看 ark抽卡记录。")
    return "\n".join(lines)


async def _run_gacha_simulation(
    bot: Bot, ev: Event, count: int
) -> None:
    import random as _random

    try:
        from ArknightsUID.utils.gamedata.gacha_sim import (
            build_draw_pools,
            simulate_pulls,
            summarize_pulls,
        )
    except ModuleNotFoundError:
        from ArknightsUID.ArknightsUID.utils.gamedata.gacha_sim import (
            build_draw_pools,
            simulate_pulls,
            summarize_pulls,
        )

    char_index = await load_char_index()
    if not char_index:
        return await _send_text(
            bot, ev, "干员索引尚未就绪（首次使用需下载游戏数据），请稍后重试。"
        )
    pools = build_draw_pools(char_index)
    if not any(pools.values()):
        return await _send_text(bot, ev, "干员池构建失败，请稍后重试。")

    results = simulate_pulls(pools, count, _random.Random())
    summary = summarize_pulls(results)
    context = _simulate_context(results, summary, count)

    # 逐抽头像（hycdn 初始头像，失败空串降级）
    try:
        for row in context["results"]:
            row["avatar"] = await char_avatar_b64(str(row.get("char_id") or ""))
    except Exception:
        pass
    await send_card_or_text(
        bot, ev, "ark_gacha_sim.html", context, _format_sim_text(summary, results)
    )


@gacha_sv.on_command(("十连", "模拟十连", "方舟十连"), block=True)
async def simulate_ten_pulls(bot: Bot, ev: Event) -> None:
    await _run_gacha_simulation(bot, ev, 10)


@gacha_sv.on_command(("单抽", "模拟单抽", "方舟单抽"), block=True)
async def simulate_single_pull(bot: Bot, ev: Event) -> None:
    await _run_gacha_simulation(bot, ev, 1)


# ---------------------------------------------------------------------------
# 抽卡记录导出（ark导出抽卡）：本地全量记录 → 通用 JSON 存档文件
# ---------------------------------------------------------------------------


def build_export_payload(
    uid: str, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """本地记录 → 自描述 JSON 存档（时间旧→新，字段名明确，含原始量纲）。"""
    from datetime import datetime, timezone

    ordered = sorted(
        records, key=lambda r: (int(r.get("gacha_ts", 0)), int(r.get("pos", 0)))
    )
    export_records = []
    for record in ordered:
        ts = int(record.get("gacha_ts", 0))
        export_records.append(
            {
                "time": datetime.fromtimestamp(
                    ts, tz=timezone.utc
                ).isoformat(timespec="seconds") if ts else "",
                "ts": ts,
                "name": str(record.get("name") or ""),
                "char_id": str(record.get("char_id") or ""),
                "rarity": int(record.get("rarity", 0)),
                "is_new": bool(record.get("is_new")),
                "pool": str(record.get("pool_name") or ""),
                "category": str(record.get("category") or ""),
                "pos": int(record.get("pos", 0)),
            }
        )
    return {
        "info": {
            "app": "ArknightsUID",
            "game": "arknights",
            "uid": uid,
            "exported_at": datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds"
            ),
            "count": len(export_records),
            # rarity 为官方 inquiry 接口原始量纲（0 基或 1 基并存，
            # 见 utils/hypergryph/gacha._normalize_rarity 的说明）
            "rarity_basis": "official-inquiry-raw",
        },
        "records": export_records,
    }


@gacha_sv.on_command(("删除抽卡记录", "清空抽卡记录"), block=True)
async def delete_gacha_records(bot: Bot, ev: Event) -> None:
    """隐私清理：删除该账号的本地寻访记录存档（官方服务器数据不受影响）。"""
    uid = extract_uid(ev.text)
    index = extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    records = await load_local_logs(credential.uid)
    if not records:
        return await _send_text(bot, ev, f"UID {shown} 本地没有抽卡记录。")
    await save_local_logs(credential.uid, [])
    await _send_text(
        bot, ev,
        f"已删除 UID {shown} 的 {len(records)} 条本地抽卡记录。\n"
        "（仅清本地存档；如需重新统计，ark抽卡记录 会从官方接口重新拉取。）",
    )


@gacha_sv.on_command(("导出抽卡", "抽卡导出", "导出寻访记录"), block=True)
async def export_gacha_records(bot: Bot, ev: Event) -> None:
    # 导出文件含完整 UID 与全量抽卡历史（时间/卡池/干员），发到群里等于
    # 把账号标识和消费记录公开给全体群成员——仅允许私聊使用
    if ev.group_id:
        return await _send_text(
            bot, ev,
            "抽卡存档含完整 UID 与全部寻访历史，请在私聊中使用 ark导出抽卡。",
        )
    uid = extract_uid(ev.text)
    index = extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)

    records = await load_local_logs(credential.uid)
    if not records:
        return await _send_text(
            bot, ev,
            "本地没有抽卡记录可导出。先 ark抽卡记录 同步官方寻访记录。",
        )

    payload = build_export_payload(credential.uid, records)
    import json as _json

    content = _json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    file_name = f"ark_gacha_{hide_uid(credential.uid).replace('*', '_')}.json"
    summary = (
        f"已导出 {len(records)} 条寻访记录（{file_name}）。\n"
        "通用 JSON 存档：含 info（uid/时间/量纲说明）与逐条记录\n"
        "（时间/池名/干员/稀有度/是否新获）。如需最新数据先 ark抽卡记录 同步。"
    )
    try:
        from gsuid_core.segment import MessageSegment

        await bot.send(MessageSegment.file(content, file_name))
        await _send_text(bot, ev, summary)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·抽卡导出] 文件发送失败: {exc.__class__.__name__}"
        )
        await _send_text(
            bot, ev,
            f"文件发送失败（{exc.__class__.__name__}），改发存档路径：\n"
            f"{summary}",
        )
