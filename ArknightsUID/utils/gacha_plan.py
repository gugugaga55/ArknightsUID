"""抽卡规划（攒抽计算器）核心计算，无网络 IO。

口径来源（ark.yituliu.cn/tools/gachaCalc 前端逻辑逆向确认，
bundle main-DPBXoxQ6.js 的预计算概率表 lEe/sEe 与 gacha-calc-test chunk
的资源换算函数 x()）：

1. 资源换算：抽数 = 合成玉/600 + 源石×0.3 + 单抽券 + 十连券×10
   （源石 1 颗换 180 玉，180/600 = 0.3 抽）
2. 六星概率：基础 2%/抽；50 抽后每抽线性 +2.5%（官方公示，封顶 100%）
3. UP 占比：单 UP 限定池六星里限定占 70%；双 UP 池各 35%
   （lEe 表首抽 0.007 = 2%×35% 逆向印证）
4. 非 UP 六星同样重置递增垫刀 → 概率用严格状态 DP，非朴素连乘
5. 300 抽井：定向兑换保底，累计表在 300 处置 1（与一图流一致）

本模块的 build_up_table 与一图流 lEe（双UP单限定）/sEe（双UP双限定）
两张 301 项表逐点校验（tests/test_gacha_plan.py）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# === 官方公示常量 ===
SIX_BASE = 0.02  # 六星基础概率
SIX_INC = 0.025  # 50 抽后每抽递增
PITY_START = 50  # 递增起始抽数
WELL_PULLS = 300  # 井（定向兑换）抽数
ORUNDUM_PER_DRAW = 600  # 600 合成玉 = 1 抽
DRAWS_PER_ORIGINIUM = 0.3  # 1 源石 = 180 玉 = 0.3 抽（一图流口径）

# UP 占比（六星内限定占比）
UP_SHARE_SINGLE = 0.70  # 单 UP 限定池
UP_SHARE_DOUBLE = 0.35  # 双 UP 限定池（指定其中一个）


def six_rate(pity: int) -> float:
    """第 pity+1 抽（当前垫刀为 pity）出六星的概率。"""
    n = pity + 1
    if n <= PITY_START:
        return SIX_BASE
    return min(1.0, SIX_BASE + SIX_INC * (n - PITY_START))


def resources_to_draws(
    orundum: float = 0.0,
    originium: float = 0.0,
    single_tickets: float = 0.0,
    ten_tickets: float = 0.0,
) -> float:
    """四种资源折算成抽数（一图流 gacha-calc 口径）。"""
    return (
        orundum / ORUNDUM_PER_DRAW
        + originium * DRAWS_PER_ORIGINIUM
        + single_tickets
        + ten_tickets * 10
    )


def build_up_table(
    up_share: float,
    *,
    max_pulls: int = WELL_PULLS,
    targets: int = 1,
    start_pity: int = 0,
    want_off: int = 0,
    off_share: float | None = None,
) -> list[float]:
    """累计达成目标的概率表（严格状态 DP）。

    build_up_table(up_share) —— 拿到指定 UP 限定（一图流 limited300 口径）
    want_off=1 —— 额外要求至少 1 个常驻陪跑六星（all300「限定+陪跑」口径，
    陪跑 share = 六星里非 UP 部分，双 UP 池为 30%，由 sEe 表逆向验证）。

    table[n] = 从 start_pity 垫刀出发再抽 n 抽达成目标的概率；
    table[0]=0，table[max_pulls]=1（井保底，与一图流表尾置 1 一致）。

    状态机：dp[u][o][k] = 已拿到 u 个限定 / o 个陪跑、连续 k 抽未出六星的
    概率。每抽以 six_rate(k) 出六星，按 share 拆成 UP / 陪跑 / 池内其它
    六星（其它同样重置垫刀，不影响目标计数）；未出六星则 k+1。
    """
    if targets < 1 or want_off < 0 or start_pity < 0:
        raise ValueError("invalid args")
    if off_share is None:
        off_share = max(0.0, 1.0 - up_share) if want_off else 0.0
        # 双 UP 池（up_share=0.35 指定其一）陪跑=常驻 30%：另一 UP 35% 不算陪跑
        if want_off and abs(up_share - UP_SHARE_DOUBLE) < 1e-9:
            off_share = 1.0 - 2 * UP_SHARE_DOUBLE

    size = max_pulls + 2
    # dp[(u, o)][k]：已拿到 u 个限定（0..targets）、o 个陪跑（0..want_off）、
    # 连续 k 抽未出六星；u/o 任一未达标即活跃（双达标即吸收进 done）
    dp: dict[tuple[int, int], list[float]] = {
        (u, o): [0.0] * size
        for u in range(targets + 1)
        for o in range(want_off + 1)
        if u < targets or o < want_off
    }
    dp[(0, 0)][min(start_pity, size - 1)] = 1.0
    other_share = max(0.0, 1.0 - up_share - off_share)

    def place(u: int, o: int, k: int, mass: float) -> None:
        if u >= targets and o >= want_off:
            nonlocal done
            done += mass
        else:
            nxt[(u, o)][k] += mass

    table: list[float] = [0.0]
    done = 0.0
    for _ in range(max_pulls):
        nxt = {key: [0.0] * size for key in dp}
        for (u, o), row in dp.items():
            for k, mass in enumerate(row):
                if mass <= 0.0:
                    continue
                p6 = six_rate(k)
                place(min(u + 1, targets), o, 0, mass * p6 * up_share)
                place(u, min(o + 1, want_off), 0, mass * p6 * off_share)
                # 出池内其它六星：重置垫刀，计数不变
                nxt[(u, o)][0] += mass * p6 * other_share
                # 未出六星
                kk = min(k + 1, size - 1)
                nxt[(u, o)][kk] += mass * (1.0 - p6)
        dp = nxt
        table.append(done)
    table[-1] = 1.0
    return table


def chance_within(
    draws: int,
    *,
    up_share: float = UP_SHARE_SINGLE,
    start_pity: int = 0,
) -> float:
    """从 start_pity 垫刀出发，draws 抽内拿到指定 UP 限定的概率（0-1）。"""
    if draws <= 0:
        return 0.0
    table = build_up_table(up_share, start_pity=start_pity)
    n = min(draws, WELL_PULLS)
    return table[n]


# === 命令参数解析 ===


@dataclass(slots=True)
class PlanArgs:
    """ark攒抽 的用户参数（全部可选，未提供走自动值）。"""

    orundum: float = 0.0
    originium: float = 0.0
    single_tickets: float = 0.0
    ten_tickets: float = 0.0
    pity: int | None = None  # None = 自动读本地记录
    pool_keyword: str = ""  # 空 = 自动取下一个结束的卡池
    up_mode: str = "单UP"  # 单UP(70%) / 双UP(各35%)
    days: int | None = None  # None = 按卡池结束时间算
    target_draws: int | None = None  # 目标抽数（缺省 300 井）
    errors: list[str] = field(default_factory=list)


_ALIASES: dict[str, str] = {
    "玉": "orundum",
    "合成玉": "orundum",
    "orundum": "orundum",
    "石": "originium",
    "源石": "originium",
    "石头": "originium",
    "originium": "originium",
    "单": "single_tickets",
    "单抽": "single_tickets",
    "十": "ten_tickets",
    "十连": "ten_tickets",
    "刀": "pity",
    "垫刀": "pity",
    "pity": "pity",
    "池": "pool_keyword",
    "卡池": "pool_keyword",
    "pool": "pool_keyword",
    "类型": "up_mode",
    "up": "up_mode",
    "天": "days",
    "天数": "days",
    "目标": "target_draws",
}

# 各数值参数合理上限（防天文数字让概率 DP 空转）
_PLAN_ARG_LIMITS = {
    "pity": 1000,
    "days": 3650,
    "target_draws": 10**6,
    "orundum": 10**9,
    "originium": 10**9,
    "single_tickets": 10**7,
    "ten_tickets": 10**7,
}


def parse_plan_args(text: str) -> PlanArgs:
    """解析 `key:value` 参数串（空格分隔，key 支持别名）。

    独立纯数字视为目标抽数（`ark攒抽 300`）。5 位以上的纯数字 token
    是 UID（命令层 extract_uid 的口径）——不当抽数也不当池关键词，
    留给账号解析（审计 #23：`ark攒抽 玉:60000 149809765` 不得把 UID
    吃成 1.5 亿抽目标）。
    """
    args = PlanArgs()
    for token in (text or "").split():
        if ":" not in token and "：" not in token:
            # isascii 守卫：`①`/`²` 等 isdigit() 为 True 但 int() 抛 ValueError
            if token.isascii() and token.isdigit():
                if len(token) <= 4:
                    args.target_draws = int(token)
                # 5+ 位纯数字 = UID 候选，跳过
            else:
                # 无分隔且非数字：当卡池关键词（ark攒抽 深池）
                args.pool_keyword = token
            continue
        key, _, value = token.replace("：", ":", 1).partition(":")
        name = _ALIASES.get(key.strip().lower()) or _ALIASES.get(key.strip())
        if not name:
            args.errors.append(f"未知参数 {token}")
            continue
        value = value.strip()
        if name == "pool_keyword":
            args.pool_keyword = value
        elif name == "up_mode":
            mode = "双UP" if "双" in value else "单UP"
            args.up_mode = mode
        else:
            try:
                number = float(value)
            except ValueError:
                args.errors.append(f"{key} 需要数字：{value}")
                continue
            # 审计 #28：float() 接受 nan/inf/1e309——不拦会在 round()/int()
            # 抛异常或让概率 DP 拿天文数字空转
            if not math.isfinite(number) or number < 0:
                args.errors.append(f"{key} 需要非负有限数字：{value}")
                continue
            limit = _PLAN_ARG_LIMITS.get(name, 10**12)
            if number > limit:
                args.errors.append(f"{key} 超出合理范围（≤{limit}）：{value}")
                continue
            if name == "pity":
                args.pity = max(0, int(number))
            elif name == "days":
                args.days = max(0, int(number))
            elif name == "target_draws":
                args.target_draws = max(0, int(number))
            else:
                setattr(args, name, max(0.0, number))
    return args


def up_share_for(mode: str) -> float:
    return UP_SHARE_DOUBLE if "双" in (mode or "") else UP_SHARE_SINGLE


@dataclass(slots=True)
class PlanSummary:
    """规划结果（文本与模板渲染共用）。"""

    pool_name: str = ""
    pool_end: str = ""
    days_left: float = 0.0
    pity: int = 0
    pity_source: str = "默认"  # 默认 / 本地记录 / 手动指定
    current_draws: float = 0.0  # 库存资源折算抽数
    orundum: float = 0.0
    originium: float = 0.0
    single_tickets: float = 0.0
    ten_tickets: float = 0.0
    up_mode: str = "单UP"
    up_share: float = UP_SHARE_SINGLE
    target_draws: int = WELL_PULLS
    # 概率视角（现有库存抽数下 / 再补档位抽数）
    chance_now: float = 0.0
    chance_50: float = 0.0
    chance_100: float = 0.0
    chance_150: float = 0.0
    # 井缺口（按本池抽数计，不含垫刀——井计数不跨池继承）
    well_gap: int = WELL_PULLS
    daily_needed: float = 0.0  # 到 300 井每天需攒抽数（0=已达/无期限）
    notes: list[str] = field(default_factory=list)


def build_summary(
    args: PlanArgs, pool: dict | None, pity: int, *, now_ts: float | None = None
) -> PlanSummary:
    """组装规划摘要。pool 需含 title/end/end_ts（PRTS SMW 口径）。

    概率表按 up_mode 的 UP 占比从当前垫刀出发；每天需攒 = 井缺口 /
    剩余天数（剩余 0 天或未取到卡池时置 0，由展示层提示）。
    now_ts 钉基准时钟（预览/测试用，保证 days_left 可回归），默认真实时间。
    """
    import time as _time

    now = _time.time() if now_ts is None else float(now_ts)
    summary = PlanSummary(
        pity=pity if pity >= 0 else 0,
        pity_source=(
            "手动指定"
            if args.pity is not None
            else ("本地记录" if pity > 0 else "默认")
        ),
        current_draws=resources_to_draws(
            args.orundum, args.originium, args.single_tickets, args.ten_tickets
        ),
        orundum=args.orundum,
        originium=args.originium,
        single_tickets=args.single_tickets,
        ten_tickets=args.ten_tickets,
        up_mode=args.up_mode,
        up_share=up_share_for(args.up_mode),
        target_draws=args.target_draws or WELL_PULLS,
    )
    if args.pity is not None:
        summary.pity_source = "手动指定"

    if pool:
        summary.pool_name = str(pool.get("title") or "")
        summary.pool_end = str(pool.get("end") or "")
        end_ts = float(pool.get("end_ts") or 0)
        if end_ts > 0:
            summary.days_left = max(0.0, (end_ts - now) / 86400)
    if args.days is not None:
        # 手动指定规划天数（覆盖卡池剩余时间）
        summary.days_left = float(args.days)

    # 井进度：井按本池内抽数累计（不继承垫刀），库存抽数计入本池可抽
    well_gap = summary.target_draws - int(summary.current_draws)
    summary.well_gap = max(0, well_gap)
    if summary.days_left > 0 and summary.well_gap > 0:
        summary.daily_needed = summary.well_gap / summary.days_left

    table = build_up_table(summary.up_share, start_pity=summary.pity)
    have = int(summary.current_draws)
    summary.chance_now = table[min(have, WELL_PULLS)]
    summary.chance_50 = table[min(have + 50, WELL_PULLS)]
    summary.chance_100 = table[min(have + 100, WELL_PULLS)]
    summary.chance_150 = table[min(have + 150, WELL_PULLS)]

    if have <= 0:
        summary.notes.append("未提供库存资源，按 0 抽计算；可用 玉:石:单:十: 参数补充")
    summary.notes.append(
        f"井计数按本池抽数累计（不继承垫刀），目标 {summary.target_draws} 抽"
    )
    summary.notes.append(
        f"UP 占比按{summary.up_mode}口径"
        f"（{'各 35%' if summary.up_share == UP_SHARE_DOUBLE else '70%'}），"
        "出货概率含陪跑六星重置垫刀的严格计算"
    )
    return summary


# === 展示层（文本与模板共用的 context/文案） ===


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}"


def _fmt_res(value: float, unit: str) -> str:
    text = f"{value:g}"
    return f"{text}{unit}" if value else ""


def build_plan_context(
    summary: PlanSummary, name: str, uid_masked: str, version: str = ""
) -> dict:
    """PlanSummary → 模板上下文（与 format_plan_text 同源）。"""
    res_parts = [
        p
        for p in (
            _fmt_res(summary.orundum, " 玉"),
            _fmt_res(summary.originium, " 石"),
            _fmt_res(summary.single_tickets, " 单"),
            _fmt_res(summary.ten_tickets, " 十"),
        )
        if p
    ] or ["无"]
    return {
        "name": name or "博士",
        "uid": uid_masked,
        "pool_name": summary.pool_name or "未获取到进行中卡池",
        "pool_end": summary.pool_end,
        "days_left": f"{summary.days_left:.1f}",
        "pity": summary.pity,
        "pity_source": summary.pity_source,
        "current_draws": int(summary.current_draws),
        "res_text": " + ".join(res_parts),
        "up_mode": summary.up_mode,
        "up_share_pct": f"{summary.up_share * 100:g}",
        "target_draws": summary.target_draws,
        "chance_now": _fmt_pct(summary.chance_now),
        "chance_50": _fmt_pct(summary.chance_50),
        "chance_100": _fmt_pct(summary.chance_100),
        "chance_150": _fmt_pct(summary.chance_150),
        "well_gap": summary.well_gap,
        "well_pct": min(
            100, int(summary.current_draws * 100 / max(1, summary.target_draws))
        ),
        "daily_needed": f"{summary.daily_needed:.1f}",
        "notes": summary.notes,
        "version": version,
    }


def format_plan_text(summary: PlanSummary, name: str, uid_masked: str) -> str:
    ctx = build_plan_context(summary, name, uid_masked)
    lines = ["「明日方舟」攒抽规划"]
    if summary.pool_name:
        end_info = f"，{summary.pool_end} 结束" if summary.pool_end else ""
        lines.append(f"卡池：{summary.pool_name}（剩 {ctx['days_left']} 天{end_info}）")
    lines.append(f"垫刀：{summary.pity}（{summary.pity_source}）")
    lines.append(f"库存：{ctx['res_text']} ≈ {ctx['current_draws']} 抽")
    lines.append(f"→ 现有抽数内拿到限定概率：{ctx['chance_now']}%")
    lines.append(
        f"→ 再补 50 抽 {ctx['chance_50']}% ／ 100 抽 {ctx['chance_100']}%"
        f" ／ 150 抽 {ctx['chance_150']}%"
    )
    if summary.well_gap > 0:
        daily = (
            f"，每天需攒 {ctx['daily_needed']} 抽" if summary.daily_needed > 0 else ""
        )
        lines.append(
            f"井缺口：{summary.target_draws} - {ctx['current_draws']}"
            f" = {summary.well_gap} 抽{daily}"
        )
    else:
        lines.append(f"已攒够 {summary.target_draws} 抽井线 ✅")
    lines.append(f"（{summary.up_mode}口径，UP 占六星 {ctx['up_share_pct']}%）")
    return "\n".join(lines)
