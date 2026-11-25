"""抽卡记录 / 礼包码兑换 / 攒抽规划命令（P3，鹰角 OAuth 链）。

命令（前缀 ark/方舟/明日方舟）：
- 抽卡记录 / 抽卡分析 [序号|UID] [页:N]：增量拉取官方寻访记录并出
  按卡池分组的统计卡（每页 3 个池，页:N 翻页）
- 导入抽卡 / 小黑盒导入 <链接>：私聊导入小黑盒导出链接（与官方
  增量同步合并去重）
- 兑换码 <code>：官网礼包码兑换（纯文本回复）
- 攒抽 / 抽卡规划 [参数]：攒抽规划卡（自动垫刀 + 自动当前卡池，
  参数 玉: 石: 单: 十: 垫刀: 池: 类型:单UP|双UP 天数: 目标抽数）
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC
from typing import Any

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ArknightsUID.utils.arg_utils import (
    extract_index,
    extract_uid,
    split_account,
    split_page,
)
from ArknightsUID.utils.card import send_card_or_text
from ArknightsUID.utils.enduid.adapter import (
    hide_uid,
    hide_uid_for,
    resolve_accounts,
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
from ArknightsUID.utils.hypergryph import gacha as gacha_service
from ArknightsUID.utils.hypergryph.gacha import (
    analyze_gacha as analyze_gacha,
    build_gacha_context,
    build_gacha_pools_context,
    format_gacha_text,
    has_uncertain_cross_source_duplicates,
    load_local_logs,
    merge_records,
    save_local_logs,
    sync_gacha_logs,
    _uid_sync_lock,
)
from ArknightsUID.utils.hypergryph.heybox import (
    convert_heybox_records,
    fetch_heybox_export,
)
from ArknightsUID.utils.player.context import char_avatar_b64
from ArknightsUID.utils.prts.client import query_gacha_pools
from ArknightsUID.utils.time_utils import beijing_datetime
from ArknightsUID.utils.reply_safety import safe_external_message
from ArknightsUID.version import ArknightsUID_version

# 凭据解析 + 失败即回复的统一入口（原各处四行样板）
from ArknightsUID.utils.card import resolve_or_reply  # noqa: E402

gacha_sv = SV("ArknightsUID抽卡", priority=5)


# 文本回复公共实现在 utils.card.send_text；保留模块级别名供调用点/测试 patch
from ArknightsUID.utils.card import send_text as _send_text  # noqa: E402



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


# 分页 token：显式 页:2 / 页2 / 页 2 / 第2页（裸数字可能表示 UID/账号序号，不承担分页）
_RE_PAGE = re.compile(r"(?:页\s*[:：]?\s*(\d+)|第\s*(\d+)\s*页)")
# 小黑盒导出链接使用 ASCII；中文路径可使用 URL 百分号编码。
_RE_URL = re.compile(r"https?://[^\s\u3400-\u9fff\u3000-\u303f\uff00-\uffef]+")
_CROSS_SOURCE_WARNING = "部分小黑盒与官方记录时间相近，可能重复，请核对统计。"


def _extract_page(text: str) -> int:
    try:
        _, page = split_page(text or "")
        return page
    except ValueError:
        match = _RE_PAGE.search(text or "")
        return int(match.group(1) or match.group(2)) if match else 1


def _extract_url(text: str) -> str:
    match = _RE_URL.search(text or "")
    if match is None:
        return ""
    return match.group(0).rstrip("，。,.！!？?；;）)】>\"'")


async def _bake_avatars(context: dict) -> None:
    """recent/pity_fives/卡池区块的头像挂载：gamedata 索引精确名反查
    charId，受限并发批量取（不再逐条串行下载），失败空串降级。"""
    try:
        from ArknightsUID.utils.image import gather_b64

        char_index = await load_char_index()
        if not char_index:
            return
        name_to_cid = {
            str(info.get("name") or ""): cid
            for cid, info in char_index.items()
            if isinstance(info, dict) and info.get("name")
        }
        rows: list[dict] = list(context.get("recent", [])) + list(
            context.get("pity_fives", [])
        )
        for pool in context.get("pools", []):
            for six in pool.get("sixes", []):
                rows.append(six)
                rows.extend(six.get("fives", []))
            rows.extend(pool.get("pity_fives", []))
        targets: list[dict] = []
        for row in rows:
            row.setdefault("avatar", "")
            cid = str(row.get("char_id") or "") or name_to_cid.get(
                str(row.get("name") or ""), ""
            )
            if cid:
                row["char_id"] = cid
                targets.append(row)
        avatars = await gather_b64(
            char_avatar_b64, [str(row["char_id"]) for row in targets]
        )
        for row, avatar in zip(targets, avatars):
            row["avatar"] = avatar
    except Exception:
        logger.debug("[ArknightsUID·抽卡] 头像挂载失败（降级无头像）", exc_info=True)


@gacha_sv.on_command(
    (
        "抽卡记录",
        "抽卡分析",
        "寻访记录",
        "ck",
    ),
    block=True,
)
async def show_gacha_report(bot: Bot, ev: Event) -> None:
    try:
        rest, page = split_page(ev.text or "")
    except ValueError as exc:
        return await _send_text(bot, ev, str(exc))
    tokens = rest.split()
    full = "全量" in tokens
    acct_text = " ".join(tok for tok in tokens if tok != "全量")
    try:
        extra, uid, index = split_account(acct_text)
    except ValueError as exc:
        return await _send_text(bot, ev, str(exc))
    if extra:
        uid = extract_uid(acct_text)
        index = extract_index(acct_text)
    credential = await resolve_or_reply(bot, ev, uid, index)
    if credential is None:
        return

    if full:
        records, warning = await sync_gacha_logs(credential, full=True)
    else:
        records, warning = await sync_gacha_logs(credential)
    if not records:
        return await _send_text(bot, ev, warning or "暂无抽卡记录。")
    if has_uncertain_cross_source_duplicates(records):
        warning = "\n".join(filter(None, (
            warning, _CROSS_SOURCE_WARNING
        )))

    nickname = credential.nickname or ""
    shown_uid = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    context = build_gacha_context(nickname, shown_uid, records, ArknightsUID_version)
    if context is not None:
        # 按具体卡池分组的小黑盒式区块（每页 3 池）+ 分页信息
        pools_ctx = await build_gacha_pools_context(records, page=page)
        text = format_gacha_text(
            nickname,
            shown_uid,
            records,
            warning,
            pool_stats=pools_ctx.get("all_pools"),
            page=page,
        )
        if pools_ctx.get("pool_data_hint"):
            text += "\n" + pools_ctx["pool_data_hint"]
        context.update(pools_ctx)
        context["warning"] = warning or ""
        await _bake_avatars(context)
        return await send_card_or_text(bot, ev, "ark_gacha_card.html", context, text)
    text = format_gacha_text(nickname, shown_uid, records, warning, page=page)
    await _send_text(bot, ev, text)


@gacha_sv.on_command(
    (
        "兑换码",
        "礼包码",
    ),
    block=True,
)
async def redeem_gift_code(bot: Bot, ev: Event) -> None:
    try:
        raw, uid, index = split_account(
            ev.text or "", legacy_uid=False, legacy_index=False
        )
    except ValueError as exc:
        return await _send_text(bot, ev, str(exc))
    tokens = raw.split()
    all_accounts = "全部" in tokens
    if all_accounts:
        if uid or index:
            return await _send_text(bot, ev, "「全部」不能和账号选择器同时使用。")
        tokens = [token for token in tokens if token != "全部"]
    elif (
        not uid and not index and len(tokens) == 2 and "\n" not in raw
        and re.fullmatch(r"[0-9]{5,}", tokens[1])
    ):
        # 保留旧的「一个码 + 裸 UID」，即使 UID 不存在也不能落到默认账号。
        uid = tokens.pop()

    codes = list(dict.fromkeys(tokens))
    if not codes or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", code) for code in codes):
        return await _send_text(
            bot, ev, "用法：ark兑换码 码1 [码2 …] [2号|UID:数字|全部]（空格或换行分隔）"
        )
    if len(codes) > 10:
        return await _send_text(bot, ev, "一次最多兑换 10 个不同的码，请分批发送。")
    if all_accounts:
        result = await resolve_accounts(ev.user_id, ev.bot_id)
        targets = list(enumerate(result.accounts, 1))
        if not targets:
            return await _send_text(bot, ev, result.reason or "没有可兑换的账号，请先 ark登录。")
        if sum(account.usable for _, account in targets) * len(codes) > 30:
            return await _send_text(bot, ev, "一次最多提交 30 个账号与兑换码组合，请分批发送。")
    else:
        credential = await resolve_or_reply(bot, ev, uid, index)
        if credential is None:
            return
        targets = [(index, credential)]

    batch = all_accounts or len(codes) > 1
    redact_codes = (
        re.compile("|".join(map(re.escape, sorted(codes, key=len, reverse=True))), re.IGNORECASE)
        if ev.group_id else None
    )
    lines = ["「明日方舟」兑换结果"] if batch else []
    for number, credential in targets:
        if batch:
            name = credential.nickname or await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
            lines.append(f"{number}号 · {name}" if number else name)
        if not credential.usable:
            lines.append("跳过（登录失效）")
            continue
        try:
            auth = await login_gacha_chain(credential)
        except Exception as exc:
            logger.warning(f"[ArknightsUID·兑换] 登录失败: {type(exc).__name__}")
            auth = "登录失败，请稍后重试。"
        if isinstance(auth, str):
            lines.append(safe_external_message(redact_codes.sub("****", auth) if redact_codes else auth))
            continue
        for position, code in enumerate(codes, 1):
            try:
                response = await exchange_gift_code(credential, auth, code)
            except Exception as exc:
                logger.warning(f"[ArknightsUID·兑换] 兑换请求异常: {type(exc).__name__}")
                response = None
            if response and redact_codes:
                response = {
                    **response,
                    "message": redact_codes.sub(
                        "****", str(response.get("message") or response.get("msg") or ""),
                    ),
                    "msg": "",
                }
            outcome = _gift_reply(response) if response else "兑换请求失败，请稍后重试。"
            if batch:
                outcome = " · ".join(outcome.splitlines())
                lines.append(f"第 {position} 个码：{outcome}")
            else:
                lines.append(outcome)
    await _send_text(bot, ev, "\n".join(lines))


@gacha_sv.on_command(("导入抽卡", "小黑盒导入", "小黑盒抽卡导入"), block=True)
async def import_heybox_gacha(bot: Bot, ev: Event) -> None:
    """小黑盒导出链接导入：下载 → 校验 → 转换 → 与本地合并去重 → 官方增量同步。

    链接含 UID 与完整抽卡历史，仅限私聊；不在任何日志/回复中回显链接，
    也不保存链接本身。官方同步失败不回滚已保存的小黑盒记录。
    """
    if ev.group_id:
        return await _send_text(
            bot,
            ev,
            "导入链接包含完整 UID 与抽卡历史，请在私聊中使用：ark导入抽卡 <链接>",
        )
    url = _extract_url(ev.text or "")
    if not url:
        return await _send_text(
            bot,
            ev,
            "用法：ark导入抽卡 <链接>\n"
            "链接来自小黑盒 App → 抽卡分析 → 数据管理 → 导出数据并复制链接。",
        )
    # 先分离 URL，再从剩余文本解析账号——URL 里的数字不能被当成 UID/序号
    rest = (ev.text or "").replace(url, "", 1)
    credential = await resolve_or_reply(
        bot, ev, extract_uid(rest), extract_index(rest)
    )
    if credential is None:
        return

    payload = await fetch_heybox_export(url)
    if isinstance(payload, str):
        return await _send_text(bot, ev, payload)
    records = await convert_heybox_records(payload, credential.uid)
    if isinstance(records, str):
        return await _send_text(bot, ev, records)

    read_count = len(records)
    async with await _uid_sync_lock(credential.uid):
        local = await load_local_logs(credential.uid)
        local_count = len(merge_records(local, [], truncate=False))
        all_records = merge_records(local, records, truncate=False)
        added = max(0, len(all_records) - local_count)
        evicted = max(0, len(all_records) - gacha_service._KEEP_LIMIT)
        merged = all_records[:gacha_service._KEEP_LIMIT]
        saved = await save_local_logs(credential.uid, merged)
    if not saved:
        return await _send_text(
            bot, ev, "本地记录保存失败，本次导入未落盘，请稍后重试。"
        )

    # 小黑盒历史较老时官方增量可能拉不到对应时段：失败不影响已导入数据
    sync_note = "官方增量同步完成。"
    final_records = merged
    try:
        synced_records, warning = await sync_gacha_logs(credential)
        if synced_records:
            final_records = synced_records
        if warning:
            sync_note = f"官方增量同步未完全成功：{warning}"
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·抽卡] 小黑盒导入后官方同步异常: {exc.__class__.__name__}"
        )
        sync_note = "官方增量同步失败（小黑盒记录已保存，可稍后 ark抽卡记录 重试同步）。"
    if has_uncertain_cross_source_duplicates(final_records):
        sync_note += "\n" + _CROSS_SOURCE_WARNING
    await _send_text(
        bot,
        ev,
        f"已读取 {read_count} 条小黑盒记录，实际新增 {added} 条"
        f"（重复 {max(0, read_count - added)} 条已合并），"
        f"{f'因保存上限淘汰 {evicted} 条较旧记录，' if evicted else ''}"
        f"合并后共 {len(final_records)} 条。\n{sync_note}",
    )


async def _auto_pity(credential, pool_name: str = "") -> int:
    """限定规划只累计目标池的本地垫刀；不同限定池不继承。"""
    if not pool_name:
        return 0
    try:
        all_records = await load_local_logs(credential.uid)
        if not all_records:
            return 0
        legacy_one_based = gacha_service._legacy_one_based(all_records)
        pool_records = [r for r in all_records if r.get("pool_name") == pool_name]
        if not pool_records:
            return 0
        ordered = sorted(
            pool_records,
            key=lambda r: (
                gacha_service._safe_ts(r),
                gacha_service._safe_ts_ms(r),
                gacha_service._safe_pos(r),
            ),
        )
        pity = 0
        for record in ordered:
            if gacha_service._normalize_rarity(record, legacy_one_based) >= 6:
                pity = 0
            else:
                pity += 1
        return pity
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
    matches = [pool for pool in pools if keyword == pool.title]
    if not matches:
        matches = [pool for pool in pools if keyword in pool.title]
    if len(matches) == 1:
        return to_dict(matches[0]), ""
    if matches:
        return None, "卡池关键词不唯一，请补充完整名称：" + "、".join(p.title for p in matches)
    names = "、".join(p.title for p in pools[:5])
    return None, f"未找到包含「{keyword}」的进行中/即将开启卡池。当前：{names}"


@gacha_sv.on_command(
    (
        "攒抽规划",
        "抽卡规划",
        "攒抽",
        "zc",
    ),
    block=True,
)
async def plan_gacha_draws(bot: Bot, ev: Event) -> None:
    args = parse_plan_args(ev.text)
    if args.errors:
        return await _send_text(bot, ev, _PLAN_USAGE + "\n" + "；".join(args.errors))

    # 参数 token（key:value）与 1-4 位独立数字（=目标抽数）不参与
    # UID/序号提取，否则 `玉:60000` 的数字会被误认成 UID。
    # 5 位以上纯数字是 UID（extract_uid 口径）必须保留（审计 #23）：
    # 旧版 `not t.isdigit()` 会把裸 UID 删掉，`ark攒抽 149809765`
    # 静默落到默认账号、垫刀数据查错人
    positional = " ".join(
        t
        for t in ev.text.split()
        if ":" not in t
        and "：" not in t
        and not (t.isascii() and t.isdigit() and len(t) <= 4)
    )
    uid = extract_uid(positional)
    index = extract_index(positional)
    # 单次解析凭据：读本地垫刀 + 展示身份共用
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid, index)
    if isinstance(credential, str):
        pity, name, uid_masked = 0, "", ""
    else:
        pity = 0
        name = credential.nickname
        uid_masked = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    if args.pity is not None:
        pity = args.pity

    pool, pool_err = await _pick_pool(args.pool_keyword)
    if pool_err:
        return await _send_text(bot, ev, pool_err)
    if args.pity is None and not isinstance(credential, str) and pool:
        pity = await _auto_pity(credential, str(pool["title"]))

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

    from ArknightsUID.utils.hypergryph.gacha import (
        _legacy_one_based,
        _normalize_rarity,
        _pity_key_resolver,
        _safe_pos,
        _safe_ts,
        _safe_ts_ms,
    )

    _legacy = _legacy_one_based(records)

    def _year_of(ts: int) -> str:
        if not ts:
            return ""
        try:
            # 脏数据时间戳可能超范围（毫秒误当秒等），fromtimestamp
            # 会抛 Overflow/ValueError——按未知年份处理（审计 #39）
            return beijing_datetime(ts).strftime("%Y")
        except (OverflowError, OSError, ValueError):
            return ""

    ordered = sorted(records, key=lambda r: (_safe_ts(r), _safe_ts_ms(r), _safe_pos(r)))
    year_records = [r for r in ordered if _year_of(_safe_ts(r)) == year]
    if not year_records:
        return None

    total = len(year_records)
    sixes = [r for r in year_records if _normalize_rarity(r, _legacy) >= 6]
    six_count = len(sixes)
    new_chars = [r for r in sixes if r.get("is_new")]

    # 垫刀 = 同保底桶内上一个六星后的抽数；跨年界沿用同规则
    pity_key_of = _pity_key_resolver(ordered)
    pity_by_cate: dict[str, int] = {}
    worst_pity = 0
    worst_record: dict | None = None
    for record in ordered:
        record_year = _year_of(_safe_ts(record))
        if not record_year or record_year > year:
            continue
        cate = pity_key_of(record)
        pity_by_cate[cate] = pity_by_cate.get(cate, 0) + 1
        if _normalize_rarity(record, _legacy) >= 6:
            pity = pity_by_cate[cate]
            if record_year == year and pity > worst_pity:
                worst_pity, worst_record = pity, record
            pity_by_cate[cate] = 0

    # year_records 已按 _year_of 过滤（可解析该年），这里统一走 _safe_ts
    months = Counter(beijing_datetime(_safe_ts(r)).month for r in year_records)
    busiest = max(months.items(), key=lambda kv: kv[1])[0]

    lines = [
        f"「明日方舟」{year} 年寻访年度报告",
        f"总抽数：{total}（六星 {six_count}，六星率 {six_count * 100 / total:.2f}%）",
        f"平均 {total / six_count:.1f} 抽一个六星" if six_count else "全年无六星……节哀",
        f"六星新干员：{len(new_chars)} 位"
        + (
            f"（{'、'.join(str(r.get('name') or r.get('char_id') or '?') for r in new_chars[:8])}）"
            if new_chars
            else ""
        ),
    ]
    if worst_record is not None:
        # 计数含出货那一抽（"第 N 抽"口径）；文案写"前垫"须减 1，
        # 否则首个六星会显示"前垫了 1 抽"
        lines.append(f"最非时刻：{worst_record['name']} 第 {worst_pity} 抽才出货")
    lines.append(f"最肝月份：{busiest} 月（{months[busiest]} 抽）")
    lines.append("（基于本地已同步记录；ark抽卡记录 先同步最新数据）")
    return "\n".join(lines)


@gacha_sv.on_command(("年度报告", "抽卡年度", "年度"), block=True)
async def send_annual_report(bot: Bot, ev: Event) -> None:

    args = (ev.text or "").strip().split()
    year = (
        args[0]
        if args and args[0].isdigit() and len(args[0]) == 4
        else str(beijing_datetime().year)
    )
    uid = extract_uid(ev.text or "")
    credential = await resolve_or_reply(bot, ev, uid, 0)
    if credential is None:
        return

    records = await load_local_logs(credential.uid)
    if not records:
        return await _send_text(
            bot,
            ev,
            f"本地没有抽卡记录（{year} 年）。先用 ark抽卡记录 同步，再来看年度报告。",
        )
    report = _annual_report(records, year)
    if report is None:
        return await _send_text(
            bot, ev, f"{year} 年没有本地记录（可用年份见 ark抽卡分析）。"
        )
    await _send_text(bot, ev, report)


# ---------------------------------------------------------------------------
# 寻访模拟（ark十连 / ark单抽）：基础概率 + 新池首十抽五星保底
# ---------------------------------------------------------------------------


def _simulate_context(results: list[dict], summary: dict, count: int) -> dict[str, Any]:
    """模拟结果 → 卡片上下文（profession 代码转中文名）。"""
    from ArknightsUID.utils.player.parse import PROFESSION_NAMES

    rows = []
    for row in results:
        rows.append(
            {
                "rarity": row["rarity"],
                "name": row["name"],
                "char_id": row.get("char_id") or "",
                "profession": PROFESSION_NAMES.get(row.get("profession") or "", ""),
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
    from ArknightsUID.utils.gamedata.gacha_sim import format_pulls_text

    lines = ["「明日方舟」新池寻访模拟（基础概率，仅供娱乐）"]
    lines.append(
        f"六星 {summary.get(6, 0)} | 五星 {summary.get(5, 0)} | "
        f"四星 {summary.get(4, 0)} | 三星 {summary.get(3, 0)}"
    )
    lines.append(format_pulls_text(results))
    lines.append("每次十连视为新池首十抽，至少五星；干员范围不对应真实卡池，不累计真实垫刀。")
    return "\n".join(lines)


async def _run_gacha_simulation(bot: Bot, ev: Event, count: int) -> None:
    import random as _random

    from ArknightsUID.utils.gamedata.gacha_sim import (
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

    # 逐抽头像（hycdn 初始头像，并发受限，失败空串降级）
    try:
        from ArknightsUID.utils.image import gather_b64

        avatars = await gather_b64(
            char_avatar_b64,
            [str(row.get("char_id") or "") for row in context["results"]],
        )
        for row, avatar in zip(context["results"], avatars):
            row["avatar"] = avatar
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


def build_export_payload(uid: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """本地记录 → 自描述 JSON 存档（时间旧→新，字段名明确，含原始量纲）。"""
    from datetime import datetime

    from ArknightsUID.utils.hypergryph.gacha import (
        _safe_pos,
        _safe_ts,
        _safe_ts_ms,
    )

    def _safe_int(raw: Any) -> int:
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    ordered = sorted(records, key=lambda r: (_safe_ts(r), _safe_ts_ms(r), _safe_pos(r)))
    export_records = []
    for record in ordered:
        ts = _safe_ts(record)
        try:
            time_text = (
                datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds")
                if ts
                else ""
            )
        except (OverflowError, OSError, ValueError):
            time_text = ""
        export_records.append(
            {
                "time": time_text,
                "ts": ts,
                "name": str(record.get("name") or ""),
                "char_id": str(record.get("char_id") or ""),
                "rarity": _safe_int(record.get("rarity")),
                "is_new": bool(record.get("is_new")),
                "pool": str(record.get("pool_name") or ""),
                "pool_id": str(record.get("pool_id") or ""),
                "category": str(record.get("category") or ""),
                "source": str(record.get("source") or "official"),
                "basis": str(record.get("basis") or ""),
                "pos": _safe_pos(record),
            }
        )
    return {
        "info": {
            "app": "ArknightsUID",
            "game": "arknights",
            "uid": uid,
            "exported_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "count": len(export_records),
            # rarity 为官方 inquiry 接口原始量纲（0 基或 1 基并存，
            # 见 utils/hypergryph/gacha._normalize_rarity 的说明）；
            # 逐条量纲与来源见 records[].basis / records[].source
            "rarity_basis": "official-inquiry-raw",
        },
        "records": export_records,
    }


@gacha_sv.on_command(("删除抽卡记录", "清空抽卡记录"), block=True)
async def delete_gacha_records(bot: Bot, ev: Event) -> None:
    """隐私清理：删除该账号的本地寻访记录存档（官方服务器数据不受影响）。"""
    uid = extract_uid(ev.text)
    index = extract_index(ev.text)
    credential = await resolve_or_reply(bot, ev, uid, index)
    if credential is None:
        return

    shown = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    async with await _uid_sync_lock(credential.uid):
        records = await load_local_logs(credential.uid)
        saved = await save_local_logs(credential.uid, []) if records else True
    if not records:
        return await _send_text(bot, ev, f"UID {shown} 本地没有抽卡记录。")
    if not saved:
        return await _send_text(bot, ev, "本地抽卡记录删除失败，请稍后重试。")
    await _send_text(
        bot,
        ev,
        f"已删除 UID {shown} 的 {len(records)} 条本地抽卡记录。\n"
        "（仅清本地存档；如需重新统计，ark抽卡记录 会从官方接口重新拉取。）",
    )


@gacha_sv.on_command(("导出抽卡", "抽卡导出", "导出寻访记录"), block=True)
async def export_gacha_records(bot: Bot, ev: Event) -> None:
    # 导出文件含完整 UID 与全量抽卡历史（时间/卡池/干员），发到群里等于
    # 把账号标识和消费记录公开给全体群成员——仅允许私聊使用
    if ev.group_id:
        return await _send_text(
            bot,
            ev,
            "抽卡存档含完整 UID 与全部寻访历史，请在私聊中使用 ark导出抽卡。",
        )
    uid = extract_uid(ev.text)
    index = extract_index(ev.text)
    credential = await resolve_or_reply(bot, ev, uid, index)
    if credential is None:
        return

    records = await load_local_logs(credential.uid)
    if not records:
        return await _send_text(
            bot,
            ev,
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
            bot,
            ev,
            f"文件发送失败（{exc.__class__.__name__}），改发存档路径：\n{summary}",
        )
