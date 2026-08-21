"""抽卡记录 / 礼包码兑换 / 攒抽规划命令（P3，鹰角 OAuth 链）。

命令（前缀 ark/方舟/明日方舟）：
- 抽卡记录 / 抽卡分析 [序号|UID]：增量拉取官方寻访记录并出统计卡
- 兑换码 <code>：官网礼包码兑换（纯文本回复）
- 攒抽 / 抽卡规划 [参数]：攒抽规划卡（自动垫刀 + 自动当前卡池，
  参数 玉: 石: 单: 十: 垫刀: 池: 类型:单UP|双UP 天数: 目标抽数）
"""
from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.ArknightsUID.utils.card import send_card_or_text
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        hide_uid,
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
    context = build_gacha_context(
        nickname, hide_uid(credential.uid), records, ArknightsUID_version
    )
    text = format_gacha_text(
        nickname, hide_uid(credential.uid), records, warning
    )
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
        "攒抽",
        "抽卡规划",
        "攒抽规划",
    ),
    block=True,
)
async def plan_gacha_draws(bot: Bot, ev: Event) -> None:
    args = parse_plan_args(ev.text)
    if args.errors:
        return await _send_text(
            bot, ev, _PLAN_USAGE + "\n" + "；".join(args.errors)
        )

    # 参数 token（key:value）与独立数字（=目标抽数）不参与 UID/序号提取，
    # 否则 `玉:60000` 的数字会被误认成 UID，绑定用户被当成未绑定
    positional = " ".join(
        t for t in ev.text.split()
        if ":" not in t and "：" not in t and not t.isdigit()
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
        uid_masked = hide_uid(credential.uid)
    if args.pity is not None:
        pity = args.pity

    pool, pool_err = await _pick_pool(args.pool_keyword)
    if pool_err:
        return await _send_text(bot, ev, pool_err)

    summary = build_summary(args, pool, pity=pity)

    context = build_plan_context(summary, name, uid_masked, ArknightsUID_version)
    text = format_plan_text(summary, name, uid_masked)
    if not uid_masked:
        # 未绑定：垫刀必然 0，提示可绑定后自动读垫刀
        context["notes"] = list(context["notes"]) + [
            "未绑定账号：绑定后自动读取本地抽卡记录的当前垫刀"
        ]
        text += "\n提示：绑定账号后可自动读取当前垫刀（ark绑定 / ark扫码登录）"
    await send_card_or_text(bot, ev, "ark_gacha_plan.html", context, text)
