from __future__ import annotations

import re

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ArknightsUID.utils.arg_utils import (
    extract_index,
    extract_uid,
)
from ArknightsUID.utils.dict_utils import path
from ArknightsUID.utils.enduid.adapter import (
    fetch_player_info,
    fetch_rogue_records,
    hide_uid_for,
)
from ArknightsUID.utils.gamedata.rogue_enemy import (
    STAT_LABELS,
    THEME_ALIASES,
    THEME_DIRS,
    resolve_theme_dir,
)
from ArknightsUID.utils.reply_safety import safe_external_message
from ArknightsUID.utils.rogue import (
    flatten_rogue,
    parse_rogue,
)
from ArknightsUID.utils.rogue.records import (
    RogueTopicStats,
    format_run_ts,
    parse_rogue_records,
    topic_id_for,
)
from ArknightsUID.version import ArknightsUID_version

# 注册顺序约束：rogue_alias_sv 必须先于 rogue_sv 创建——core 按 SV 注册序
# startswith 分发，否则「肉鸽别名/肉鸽别名删除」会被「肉鸽」命令抢走
# （别名命令的 handler 定义在文件末尾，SV 创建序即分发序）。
# 凭据解析 + 失败即回复的统一入口（原各处四行样板）
from ArknightsUID.utils.card import resolve_or_reply  # noqa: E402

rogue_alias_sv = SV("ArknightsUID肉鸽别名", priority=5)
rogue_sv = SV("ArknightsUID肉鸽", priority=5)

# 集成战略主题名（玩家数据 rogue_id → 官方主题名）收敛至
# gamedata.rogue_registry（唯一事实源）。推位规律（roguelike_topic_table
# 5 对名字全吻合验证）：主题表 rogue_N 从傀影起编，玩家数据从灰蕈迷境
# 起编——玩家 rogue_6 = 表 rogue_5 = 岁的界园志异。
from ..utils.gamedata.rogue_registry import ROGUE_THEME_NAMES


# 文本回复公共实现在 utils.card.send_text；保留模块级别名供调用点/测试 patch
from ArknightsUID.utils.card import send_text as _send_text  # noqa: E402


def _rogue_sort_key(rogue_id: str) -> int:
    digits = "".join(ch for ch in rogue_id if ch.isdigit())
    return int(digits) if digits else 0


def _sorted_records(records: list) -> list:
    """最新期在前（rogue_N 数字倒序）。"""
    return sorted(records, key=lambda r: -_rogue_sort_key(r.rogue_id))


def _format_rogue(response: object) -> str:
    """把 player_info 信封里的 rogue 字段格式化为纯文本。

    records 精确展示（rogueId/通关/等级/收藏/蜡烛/奖章，来自真实 dump）；
    无 records 时用 flatten_rogue 通用遍历兜底。
    """
    if isinstance(response, str):
        # adapter 失败时返回中文错误串
        return response
    rogue = path(response, "data", "rogue")
    if not isinstance(rogue, dict) or not rogue:
        return "未识别到肉鸽（集成战略）详细字段（等待数据补全）。"
    info = parse_rogue(rogue)
    lines = ["「明日方舟」集成战略（肉鸽）"]
    if info and info.records:
        # 最新期在前 + 合计小结（与卡片一致）
        records = _sorted_records(info.records)
        for r in records:
            theme = ROGUE_THEME_NAMES.get(r.rogue_id, r.rogue_id)
            lines.append(
                f"{theme}：通关 {r.clear_time} | 声望 {r.bp_level} | "
                f"收藏 {r.relic_cnt} | 蜡烛 {r.bank.current}/{r.bank.record} | "
                f"奖章 {r.medal.current}/{r.medal.total}"
            )
        lines.append(
            f"合计通关 {sum(r.clear_time for r in records)} 次（{len(records)} 期主题）"
        )
        return "\n".join(lines)
    # 无 records 但有 rogue dict：通用遍历兜底
    flat = flatten_rogue(rogue)
    if not flat:
        lines.append("已获取肉鸽数据，但当前版本未识别可展示字段。")
    else:
        for label, val in flat[:25]:
            lines.append(f"{label}：{val}")
    return "\n".join(lines)


def _build_rogue_context(response: object, shown_uid: str = "") -> dict | None:
    """把 player_info 信封组装成 ark_rogue.html 的渲染上下文。

    shown_uid 必须由调用方传 hide_uid_for 的结果（卡片公开展示，UID 明文
    会同时绕过全局 HideUid 与用户级设置）。无 records 时返回 None（调用方
    回退纯文本）。
    """
    rogue = path(response, "data", "rogue")
    if not isinstance(rogue, dict) or not rogue:
        return None
    info = parse_rogue(rogue)
    if not info or not info.records:
        return None
    # 真实接口字段是 status.name（parse.py 同款口径）；nickName 仅为旧字段兜底
    name = (
        path(response, "data", "status", "name")
        or path(response, "data", "status", "nickName")
        or "博士"
    )
    records = _sorted_records(info.records)
    rows = [
        {
            "rogue_id": r.rogue_id,
            "name": ROGUE_THEME_NAMES.get(r.rogue_id, r.rogue_id),
            "clear_time": r.clear_time,
            "bp_level": r.bp_level,
            "relic_cnt": r.relic_cnt,
            "bank_current": r.bank.current,
            "bank_record": r.bank.record,
            "medal_current": r.medal.current,
            "medal_total": r.medal.total,
            "latest": False,
        }
        for r in records
    ]
    if rows:
        rows[0]["latest"] = True
    return {
        "name": name,
        "uid": shown_uid,
        "records": rows,
        "total_clears": sum(r.clear_time for r in records),
        "total_relics": sum(r.relic_cnt for r in records),
        "version": ArknightsUID_version,
    }


async def _fill_rogue_theme_images(context: dict) -> None:
    """每期肉鸽配主题 KV 图：主题名 → PRTS 主题页信息框标题图（与活动横幅
    同源，activity_banner_urls 批量取），下载缓存后 b64 内嵌；失败按无图降级。
    """
    records = context.get("records") or []
    names = [str(r.get("name") or "") for r in records]
    if not names:
        return
    try:
        from ArknightsUID.utils.prts.client import activity_banner_urls
        from ArknightsUID.utils.render import get_image_b64_with_cache
        from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

        banner_urls = await activity_banner_urls(names)
        for r in records:
            url = banner_urls.get(str(r.get("name") or ""), "")
            r["image"] = (
                await get_image_b64_with_cache(
                    url, CACHE_PATH / "rogue_theme", quality=82
                )
                if url
                else ""
            )
    except Exception:
        from gsuid_core.logger import logger

        logger.debug("[ArknightsUID] 肉鸽主题图获取失败", exc_info=True)


# ---------------------------------------------------------------------------
# 肉鸽战绩（ark肉鸽战绩）：森空岛 App「战绩查询」页独立端点（与 ark肉鸽 的
# player_info 进度卡不同源——战绩页有难度/得分/结算阵容/结局明细）。
# ---------------------------------------------------------------------------

# 卡片单期展示上限：最近战绩/收藏战绩各自条数、每条战绩的阵容头像数
_RECORD_CARD_LIMIT = 8
_RECORD_CHARS_LIMIT = 8
_FAV_CHARS_LIMIT = 6


def _resolve_record_theme(
    text: str, extra_aliases: dict[str, str]
) -> tuple[str, str] | str:
    """战绩命令的期名解析 → (topic_id, 期显示名)；空输入=服务端默认期。

    主题名是单个 token（别名/显示名/目录名均不含空格）；多 token 或不可
    解析时返回错误文案（列出可选期名）。
    """
    theme_text = (text or "").strip(" ：:")
    if not theme_text:
        return "", ""
    tokens = theme_text.split()
    theme_dir = None
    if len(tokens) == 1:
        theme_dir = extra_aliases.get(tokens[0].lower()) or resolve_theme_dir(tokens[0])
    if theme_dir is None:
        names = "、".join(THEME_DIRS.values())
        return (
            f"无法识别的期名「{theme_text}」。可用：{names}"
            "（也可用 ark肉鸽别名 自定义期名）。"
        )
    return topic_id_for(theme_dir), THEME_DIRS.get(theme_dir, theme_dir)


def _run_duration(start_ts: str, end_ts: str) -> str:
    """起止时间戳（秒字符串）→ '用时 xx 分钟'；异常/倒挂返回空串。"""
    if not (start_ts.isdigit() and end_ts.isdigit()):
        return ""
    minutes = (int(end_ts) - int(start_ts)) // 60
    if minutes <= 0 or minutes > 60 * 24:
        return ""
    return f"用时 {minutes} 分钟"


def _run_row(run, index: int) -> dict:
    """单条战绩 → 卡片行（结局文本优先，失败显示止步关卡）。"""
    battles = []
    if run.battle_normal:
        battles.append(f"普通 {run.battle_normal}")
    if run.battle_elite:
        battles.append(f"精英 {run.battle_elite}")
    if run.battle_boss:
        battles.append(f"领袖 {run.battle_boss}")
    return {
        "index": index,
        "success": run.success,
        "mode": run.mode,
        "mode_grade": run.mode_grade,
        "score": run.score,
        "band": run.band,
        "ending": run.ending_text,
        "last_stage": run.last_stage,
        "time": format_run_ts(run.end_ts),
        "duration": _run_duration(run.start_ts, run.end_ts),
        "zones": run.zones,
        "battles": " · ".join(battles),
        "relic_count": run.relic_count,
        "recruit_count": run.recruit_count,
        "tags": run.tags,
        "chars": [
            {"name": c.name, "char_id": c.char_id, "avatar": ""}
            for c in run.chars[:_RECORD_CHARS_LIMIT]
        ],
    }


async def _fill_record_avatars(context: dict) -> None:
    """常用干员 + 全服干员 + 各战绩阵容头像 b64 内嵌（并发受限，失败空串降级）。"""
    from ArknightsUID.utils.player.context import char_avatar_b64

    # (char dict) 打平成任务清单，统一回填 avatar 键
    char_slots: list[dict] = list(context.get("fav_chars") or [])
    for slot in (context.get("all_data") or {}).get("chars") or []:
        char_slots.append(slot["char"])
    for row in (context.get("records") or []) + (context.get("favour_records") or []):
        char_slots.extend(row.get("chars") or [])
    if not char_slots:
        return
    from ArknightsUID.utils.image import gather_b64

    avatars = await gather_b64(
        char_avatar_b64,
        [str(c.get("char_id") or "") for c in char_slots],
    )
    for slot, avatar in zip(char_slots, avatars):
        slot["avatar"] = avatar if isinstance(avatar, str) else ""


async def _skland_record_assets(topic_id: str) -> dict[str, str]:
    """战绩卡的官方装饰图（App「战绩查询」页同源，bbs.hycdn.cn CDN）。

    profileBg 期横幅（topics[].pic 缺失时的兜底）、historyCardTitleSuccess/Fail
    每条战绩的通关/失败标题横幅、careerBg 生涯/全服页底图（App 两个 Tab
    共用同一张竖版原画）、profileTitle 生涯页官方标题字。全部失败返回
    空 dict（模板按无图降级）。
    """
    if not topic_id:
        return {}
    from ArknightsUID.utils.rogue.skland_assets import skland_asset_b64

    from ArknightsUID.utils.image import gather_b64

    keys = (
        "profileBg",
        "historyCardTitleSuccess",
        "historyCardTitleFail",
        "careerBg",
        "profileTitle",
    )
    values = await gather_b64(
        lambda pair: skland_asset_b64(pair[0], pair[1]),  # type: ignore[arg-type,return-value]
        [(topic_id, key) for key in keys],
    )
    return {
        key: value if isinstance(value, str) else ""
        for key, value in zip(keys, values)
    }


def _profession_label(code: str) -> str:
    """职业代码 → 中文（未知代码原样返回）。"""
    from ArknightsUID.utils.player.parse import PROFESSION_NAMES

    return PROFESSION_NAMES.get(code, code)


def _all_data_rows(stats: RogueTopicStats) -> dict:
    """全服数据 → 战绩卡展示行（未开放/无数据返回空 dict，模板整体跳过）。

    只取各期公共契约（结局比例/战术道具/收藏品/分职业干员）；期专属趣味
    计数（骰点/烛火/佣兵收入等）结构逐期不同，不进稳定展示。
    """
    if not stats.all_open:
        return {}
    rows: dict = {
        # 比例读数统一一位小数（52.31 → 52.3），卡片与文本回退同口径
        "endings": [
            {"count": count, "pct": round(pct, 1), "width": min(round(pct, 1), 100.0)}
            for count, pct in stats.all_endings[:5]
        ],
        "tools": stats.all_tools,
        "relics": stats.all_relics,
        "chars": [
            {
                "label": _profession_label(c.profession) if c.profession else "",
                "char": {
                    "name": c.name,
                    "char_id": c.char_id,
                    "avatar": "",
                },
            }
            for c in stats.all_chars
        ],
    }
    if not any((rows["endings"], rows["tools"], rows["relics"], rows["chars"])):
        return {}
    return rows


async def _build_rogue_records_context(
    stats: RogueTopicStats, theme_display: str, shown_uid: str
) -> dict:
    """战绩解析结果 → ark_rogue_record.html 渲染上下文。"""
    theme_name = stats.topic_name or theme_display or "集成战略"
    assets = await _skland_record_assets(stats.topic_id)
    from ArknightsUID.utils.rogue.skland_assets import skland_topic_accent
    banner = ""
    try:
        from ArknightsUID.utils.prts.client import activity_banner_urls
        from ArknightsUID.utils.render import get_image_b64_with_cache
        from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

        # 横幅优先复用森空岛自绘图：topics[].pic（期切换页图）→ 官方生涯页
        # profileBg（assets 里已是下载缓存后的 b64，不再二次下载）；两者都
        # 缺失/失败再退 PRTS 主题页标题图
        if stats.topic_pic.startswith(("http://", "https://")):
            try:
                banner = await get_image_b64_with_cache(
                    stats.topic_pic, CACHE_PATH / "rogue_theme", quality=82
                )
            except Exception:
                banner = ""
        if not banner:
            banner = assets.get("profileBg", "")
        if not banner:
            url = (await activity_banner_urls([theme_name])).get(theme_name, "")
            if url:
                banner = await get_image_b64_with_cache(
                    url, CACHE_PATH / "rogue_theme", quality=82
                )
    except Exception:
        from gsuid_core.logger import logger

        logger.debug("[ArknightsUID] 肉鸽战绩主题图获取失败", exc_info=True)

    win_banner = assets.get("historyCardTitleSuccess", "")
    fail_banner = assets.get("historyCardTitleFail", "")

    def _run_row_wrapped(run, index: int) -> dict:
        row = _run_row(run, index)
        # 官方通关/失败标题横幅（App 每条战绩卡头图同源；缺图时模板回退字条）
        row["title_banner"] = win_banner if run.success else fail_banner
        return row

    return {
        "name": stats.player_name or "博士",
        "level": stats.player_level,
        "uid": shown_uid,
        "theme_name": theme_name,
        "topics": stats.topics,
        "banner": banner,
        "mode": stats.mode,
        "mode_grade": stats.mode_grade,
        "score": stats.score,
        "bp_level": stats.bp_level,
        "medal_current": stats.medal_current,
        "medal_total": stats.medal_total,
        "medal_pct": (
            round(stats.medal_current / stats.medal_total * 100, 1)
            if stats.medal_total > 0
            else 0
        ),
        "games": stats.games,
        "nodes": stats.nodes,
        "relics": stats.relics,
        "invest": stats.invest,
        "endings_total": stats.endings_total,
        "fav_chars": [
            {"name": c.name, "char_id": c.char_id, "avatar": ""}
            for c in stats.fav_chars[:_FAV_CHARS_LIMIT]
        ],
        "records": [
            _run_row_wrapped(r, i + 1)
            for i, r in enumerate(stats.records[:_RECORD_CARD_LIMIT])
        ],
        "favour_records": [
            _run_row_wrapped(r, i + 1)
            for i, r in enumerate(stats.favour_records[:_RECORD_CARD_LIMIT])
        ],
        "record_total": len(stats.records),
        "all_data": _all_data_rows(stats),
        # 官方生涯/全服页底图与标题字（App 两 Tab 共用 careerBg；缺图时
        # 模板回退浅色终端版式）+ 官方主题强调色（深色带内数字/进度条）
        "career_band": assets.get("careerBg", ""),
        "career_title": assets.get("profileTitle", ""),
        "theme_accent": skland_topic_accent(stats.topic_id),
        "version": ArknightsUID_version,
    }


def _format_rogue_records(
    stats: RogueTopicStats, theme_display: str, shown_uid: str
) -> str:
    """战绩纯文本回退（卡片渲染失败时）。"""
    theme_name = stats.topic_name or theme_display or "集成战略"

    def _line(r) -> str:
        result = "通关" if r.success else "失败"
        where = r.ending_text or (f"止步 {r.last_stage}" if r.last_stage else "")
        difficulty = f"{r.mode}·N{r.mode_grade}" if r.mode else f"N{r.mode_grade}"
        parts = [difficulty, f"得分 {r.score}"]
        tail_parts = [p for p in (where,) if p]
        if r.zones:
            tail_parts.append(f"探索 {r.zones} 层")
        battles = r.battle_normal + r.battle_elite + r.battle_boss
        if battles:
            tail_parts.append(
                f"战斗 {r.battle_normal}/{r.battle_elite}/{r.battle_boss}"
            )
        if r.relic_count:
            tail_parts.append(f"藏品 {r.relic_count}")
        tail_parts.extend(
            p
            for p in (_run_duration(r.start_ts, r.end_ts), format_run_ts(r.end_ts))
            if p
        )
        line = f"[{result}] {' | '.join(parts)}"
        if r.band:
            line += f" | {r.band}"
        return line + (f"（{' | '.join(tail_parts)}）" if tail_parts else "")

    lines = [f"「明日方舟」肉鸽战绩 · {theme_name}"]
    name = stats.player_name or "博士"
    uid_part = f"（UID {shown_uid}）" if shown_uid else ""
    lines.append(f"{name}{uid_part} Lv.{stats.player_level}")
    lines.append(
        f"最高达成：{stats.mode} N{stats.mode_grade} | 声望 {stats.bp_level} | "
        f"最高分 {stats.score} | 奖章 {stats.medal_current}/{stats.medal_total}"
    )
    career = [
        f"探索 {stats.games} 次" if stats.games else "",
        f"经过节点 {stats.nodes}" if stats.nodes else "",
        f"获得藏品 {stats.relics}" if stats.relics else "",
        f"结局达成 {stats.endings_total}" if stats.endings_total else "",
    ]
    if any(career):
        lines.append("生涯：" + " | ".join(c for c in career if c))
    if stats.fav_chars:
        lines.append(
            "常用干员：" + "、".join(c.name for c in stats.fav_chars[:10] if c.name)
        )
    if stats.records:
        lines.append("—— 最近战绩 ——")
        for r in stats.records[:10]:
            lines.append(_line(r))
    else:
        lines.append("该期暂无战绩记录（还没开过这一期？）。")
    if stats.favour_records:
        lines.append("—— 收藏战绩 ——")
        for r in stats.favour_records[:5]:
            lines.append(_line(r))
    if stats.all_open:
        lines.append("—— 全服数据 ——")
        if stats.all_endings:
            # 读数一位小数（与卡片同口径）
            ratio = " / ".join(
                f"{count}个 {round(pct, 1):g}%" for count, pct in stats.all_endings[:5]
            )
            lines.append(f"通关结局比例：{ratio}")
        if stats.all_tools:
            lines.append("最常用战术道具：" + "、".join(stats.all_tools))
        if stats.all_relics:
            lines.append("最常用收藏品：" + "、".join(stats.all_relics))
        if stats.all_chars:
            picks = "、".join(
                f"{c.name}（{_profession_label(c.profession)}）"
                for c in stats.all_chars
                if c.name
            )
            if picks:
                lines.append("出场率最高干员：" + picks)
    other = [
        str(t.get("short") or t.get("name"))
        for t in stats.topics
        if (t.get("short") or t.get("name")) and not t.get("selected")
    ]
    if other:
        lines.append(f"切换主题：ark肉鸽战绩 {' / '.join(other)}")
    else:
        lines.append("（数据：森空岛战绩查询；其他期：ark肉鸽战绩 期名）")
    return "\n".join(lines)


def _parse_rogue_query(
    text: str,
    extra_aliases: dict[str, str] | None = None,
) -> tuple[str, str | None, int | None, bool]:
    """`名字 [期] [难度] [紧急]` → (敌人名/关卡名, 期目录, 难度, 紧急)。

    难度取 N7 / 难度7 / 难度 7（大小写不敏感，N+数字 无空格也可）；
    期按整词匹配（extra_aliases 用户别名优先于 THEME_ALIASES 内建，
    萨卡兹/水月/黑流…）——不能用子串替换：「萨卡兹魔剑士」这类敌人名
    本身含期名，子串会拆坏名字；「紧急」为保留词（紧急作战：关卡自带
    buff + 额外出怪）。
    """
    remaining = (text or "").strip()
    difficulty: int | None = None
    elite = "紧急" in remaining.split()
    if elite:
        remaining = " ".join(t for t in remaining.split() if t != "紧急")

    # 难度整 token 匹配：N 前须是串首/空白/非拉丁字母数字（允许「歌利亚N12」
    # 紧贴中文名，但不会命中「sin7」这类拉丁词中段），数字须取到 token 末尾
    # （「N123」整体消化，不会只截两位残留「3」）。边界字符保留在名字里。
    diff_match = re.search(
        r"(^|\s|[^A-Za-z0-9])(?:[Nn]\s*(\d{1,3})|难度\s*(\d{1,3}))(?=\s|$)",
        remaining,
    )
    if diff_match:
        difficulty = int(diff_match.group(2) or diff_match.group(3))
        remaining = (
            remaining[: diff_match.start()]
            + diff_match.group(1)
            + remaining[diff_match.end() :]
        ).strip()

    theme_dir: str | None = None
    kept: list[str] = []
    for token in remaining.split():
        if theme_dir is None:
            hit = (extra_aliases or {}).get(token.lower())
            if hit is None:
                hit = THEME_ALIASES.get(token) or THEME_ALIASES.get(token.strip("· "))
            if hit:
                theme_dir = hit
                continue
        kept.append(token)
    return " ".join(kept), theme_dir, difficulty, elite


def _format_rogue_enemy(context: dict) -> str:
    lines = [f"「明日方舟」肉鸽敌人：{context['name']}"]
    tags = " ".join(context["tags"])
    if tags:
        lines.append(f"类别：{tags}")
    if context["resistances"]:
        lines.append("抗性：" + "、".join(context["resistances"]))
    for form in context["forms"]:
        extra = f"（{'、'.join(form['extras'])}）" if form["extras"] else ""
        lines.append(
            f"{form['title']}：HP {form['hp']} 攻 {form['atk']} "
            f"防 {form['def']} 抗 {form['res']} 重量 {form['weight']}"
            f"{extra}（{form['attack']}）"
        )
        for line in form.get("mechanics") or []:
            lines.append(f"  · {line}")
    for group in context.get("stage_groups") or []:
        more = (
            f"…等 {group['total']} 关" if group["total"] > len(group["stages"]) else ""
        )
        lines.append(f"出没·{group['theme_name']}：{'、'.join(group['stages'])}{more}")
    # 口径区分：本面板是肉鸽解包数据，主线/活动出没与通用数值走 PRTS
    lines.append(
        f"（出没为集成战略口径；主线/活动出没与通用数值见 ark敌人 {context['name']}）"
    )
    focus = context.get("focus")
    if focus:
        where = context["theme_name"]
        if context.get("stage_label"):
            where += f"·{context['stage_label']}"
        lines.append(
            f"指定面板（{where}·{focus['label']}）："
            f"HP {focus['stats']['hp']} 攻 {focus['stats']['atk']} "
            f"防 {focus['stats']['def']} 抗 {focus['stats']['res']}"
        )
        if focus["buffs"]:
            lines.append("  生效：" + "；".join(focus["buffs"]))
    elif context["theme_name"]:
        lines.append(f"难度面板（{context['theme_name']}，累积生效）：")
        for row in context["panels"]:
            stat = " ".join(f"{STAT_LABELS[k]}{v}" for k, v in row["stats"].items())
            note = f"｜{'、'.join(row['notes'])}" if row["notes"] else ""
            lines.append(f"  {row['label']}：{stat}{note}")
        lines.append(
            "  注：数值已按第1层折算每层增益（每深一层按复合追加），完整交互模拟见 tomimi.dev"
        )
    else:
        lines.append(
            "难度面板：未识别所属期（仅基础面板；可用「ark肉鸽敌人 名字 期名 难度」指定）"
        )
    return "\n".join(lines)


async def _build_rogue_enemy_context(
    query: str,
    extra_aliases: dict[str, str] | None = None,
) -> dict | str:
    """肉鸽敌人 buff 面板查询：基础形态面板 + 机制 + 条件精确面板。

    查询语法：`名字 [期名] [N难度] [关名] [紧急]`——不指定期时自动判定
    （显式点名 > 类型标签），带关名时以关卡指定的形态块为基底（期由关卡
    隐含），「紧急」叠加该关紧急作战效果。敌人名与关卡名零重名，按
    token 前缀拆分：前段敌名 + 后段关名。
    """
    from ArknightsUID.utils.gamedata.rogue_enemy import (
        THEME_DIRS,
        build_diff_panels,
        build_panel_at,
        build_stage_index,
        class_labels,
        detect_theme,
        enemy_icon_b64,
        find_stage,
        form_context,
        form_mechanisms,
        load_difficulty_mods,
        load_enemy_db,
        load_skill_dict,
        load_stage_data,
        match_rogue_enemies,
        max_difficulty,
        stages_for_enemy,
        status_labels,
    )

    name, theme_hint, difficulty, elite = _parse_rogue_query(query, extra_aliases)
    db = await load_enemy_db()
    if not db:
        return "敌人库不可用（tomimi 数据拉取失败），请稍后再试。"
    if not name:
        # 期名 token 与敌人名撞名（IS5 BOSS「岁」即期简称）：首个 token 在
        # 敌人库精确/唯一命中时按敌人名处理（难度/紧急等其余 token 已由
        # 上面的解析得到），不再只回用法提示
        raw_tokens = (query or "").strip().split()
        first = raw_tokens[0].strip("：:") if raw_tokens else ""
        if first and not re.fullmatch(r"(?:[Nn]\d{1,3}|难度\s*\d{1,3}|紧急)", first):
            matched = match_rogue_enemies(first, db)
            if len(matched) == 1:
                name = first
        if not name:
            return "用法：ark肉鸽敌人 名字 [期名] [N难度] [关名] [紧急]（如 ark肉鸽敌人 歌利亚 萨卡兹 N12）"

    # token 前缀拆分：前段敌名（唯一命中）+ 后段关名（精确命中）。
    # 敌人名/关卡名零重名且均不含空格，从长前缀往短试保证「敌名 关名」
    # 不被短敌名前缀劫持。
    tokens = name.split()
    entry = None
    stage_hit = None
    for i in range(len(tokens), 0, -1):
        head = " ".join(tokens[:i])
        matched = match_rogue_enemies(head, db)
        if len(matched) > 1:
            shown = "、".join(db[k].get("name_zh", k) for k in matched)
            return f"找到 {len(matched)} 个候选：{shown}（请用完整名）"
        if len(matched) != 1:
            continue
        stage_hit = None
        if i < len(tokens):
            stage_hit = await find_stage(" ".join(tokens[i:]))
            if stage_hit is None:
                continue  # 后段不是关名 → 该拆分无效
        entry = db[matched[0]]
        break

    if entry is None:
        # 没有敌人命中 → 交给关卡视图（敌人命令兜底渲染关卡卡）
        return await _build_rogue_stage_context(query)

    enemy_key = str(entry.get("key") or "")
    stage_data = None
    if stage_hit is not None:
        stage_data = await load_stage_data(stage_hit["level_id"])

    diff_data = await load_difficulty_mods()
    theme_dir = (
        stage_hit["theme"] if stage_hit is not None else theme_hint
    ) or detect_theme(enemy_key, list(entry.get("type") or []), diff_data)
    grades = diff_data.get(theme_dir or "") or []
    if difficulty is not None and not grades:
        if theme_dir:
            return (
                f"{THEME_DIRS.get(theme_dir, theme_dir)}的难度数据拉取失败，"
                "暂时只能看基础面板（稍后再试），或去掉 N难度 直接查询。"
            )
        known = "、".join(THEME_DIRS.values())
        return f"需指定期才能算难度面板（该敌人未识别所属期），如：ark肉鸽敌人 {name} 萨卡兹 N12。期名：{known}"

    focus = None
    base_block = None
    extra_effects = None
    stage_label = ""
    if stage_hit is not None:
        # 关卡出场形态块 + 紧急作战效果
        stage_enemy = next(
            (
                e
                for e in (stage_data.get("enemies") or [])
                if isinstance(e, dict) and str(e.get("id") or "") == enemy_key
            ),
            None,
        )
        blocks = entry.get("stats") or []
        if stage_enemy is not None and blocks:
            base_block = blocks[
                min(int(stage_enemy.get("level") or 0), len(blocks) - 1)
            ]
            stage_label = (
                f"{stage_hit['name']}·形态{int(stage_enemy.get('level') or 0) + 1}"
            )
        if elite and stage_data.get("elite_mods"):
            extra_effects = stage_data.get("elite_mods")
    if difficulty is not None:
        difficulty = max(0, min(difficulty, max_difficulty(grades)))
    # 每层增益折算层号：关卡命中时用该关最深层，否则按 tomimi 惯例第 1 层
    focus_floor = 1
    if stage_hit is not None:
        stage_floors = sorted(
            {int(f) for f in (stage_data.get("floors") or []) if str(f).isdigit()}
        )
        focus_floor = stage_floors[-1] if stage_floors else 1
    if stage_hit is not None or difficulty is not None:
        focus = build_panel_at(
            entry,
            grades,
            difficulty,
            base_block=base_block,
            extra_effects=extra_effects,
            floor=focus_floor,
        )
    panels = (
        []
        if focus is not None
        else build_diff_panels(entry, grades, floor=1)
        if theme_dir
        else []
    )
    icon = await enemy_icon_b64(enemy_key)
    skill_dict = await load_skill_dict()
    mechanics = form_mechanisms(entry, skill_dict)
    forms = form_context(entry)
    for i, form in enumerate(forms):
        form["mechanics"] = mechanics[i] if i < len(mechanics) else []
    # 出没关卡（全期索引，构建失败静默降级为不展示）
    stage_index = await build_stage_index()
    stage_groups = stages_for_enemy(stage_index, enemy_key)
    # atk_type 值域为英文（melee/ranged/…），且线上数据含 null 项（140 个
    # 敌人），须过滤后再按英文键映射——此前拿中文去 join 英文串是永假死代码，
    # 且 null 直接 join 会抛 TypeError 打断整条命令
    atk_types = {
        tt
        for f in (entry.get("forms") or [])
        for tt in ((f.get("normal_attack") or {}).get("atk_type") or [])
        if isinstance(tt, str)
    }
    tags = class_labels(entry) + [
        cn for en, cn in (("melee", "近战"), ("ranged", "远程")) if en in atk_types
    ]
    return {
        "name": str(entry.get("name_zh") or enemy_key),
        "enemy_key": enemy_key,
        "tags": tags,
        "resistances": status_labels(entry),
        "icon": icon,
        "forms": forms,
        "stage_groups": stage_groups,
        "theme_name": THEME_DIRS.get(theme_dir or "", ""),
        "panels": panels,
        "focus": focus,
        "stage_label": stage_label,
        "elite": elite,
        "template": "ark_rogue_enemy.html",
        "source_note": "数据 tomimi.dev（游戏解包）",
        "version": ArknightsUID_version,
    }


def _format_rogue_stage(context: dict) -> str:
    lines = [f"「明日方舟」肉鸽关卡：{context['name']}（{context['code']}）"]
    tags = [
        context["theme_name"],
        context["difficulty_label"],
        "紧急" if context["elite"] else "",
        f"第{context['floors']}" if context["floors"] else "",
    ]
    lines.append(" · ".join(t for t in tags if t))
    if context["elite_descs"]:
        lines.append("紧急作战：" + "；".join(context["elite_descs"]))
    if context.get("variant_note"):
        lines.append(context["variant_note"])
    for row in context["rows"]:
        mark = f"·形态{row['level'] + 1}" if row["level"] else ""
        elite_mark = f"·{row['elite_note']}" if row["elite_note"] else ""
        s = row["stats"]
        lines.append(
            f"  {row['name']}×{row['count']}{mark}{elite_mark}："
            f"HP {s['hp']} 攻 {s['atk']} 防 {s['def']} 抗 {s['res']}"
        )
    if context["floor_note"]:
        lines.append(f"注：{context['floor_note']}")
    if context["rows"]:
        lines.append(
            f"查具体敌人：ark肉鸽敌人 <敌名> {context['name']}"
            + (
                f" N{context['difficulty']}"
                if context["difficulty"] is not None
                else ""
            )
            + (" 紧急" if context["elite"] else "")
        )
    return "\n".join(lines)


async def _build_rogue_stage_context(
    query: str,
    extra_aliases: dict[str, str] | None = None,
) -> dict | str:
    """关卡视图：出怪列表（含难度/紧急折算面板）+ 紧急作战描述。

    语法：`关名 [期名] [N难度] [紧急]`——四期关卡名零重复，关名即唯一定位；
    期名可省（关卡自带期归属）。
    """
    from ArknightsUID.utils.gamedata.rogue_enemy import (
        THEME_DIRS,
        _floor_buff_note,
        _mod_note,
        enemy_icon_b64,
        find_stage,
        load_difficulty_mods,
        load_enemy_db,
        load_stage_data,
        max_difficulty,
        search_stages,
        stage_panel_rows,
    )

    name, _theme_hint, difficulty, elite = _parse_rogue_query(query, extra_aliases)
    if not name:
        return "用法：ark肉鸽关卡 关名 [N难度] [紧急]（如 ark肉鸽关卡 拆东补西 N15）"

    stage = await find_stage(name)
    if stage is None:
        candidates = await search_stages(name)
        if len(candidates) == 1:
            stage = candidates[0]
        elif candidates:
            shown = "、".join(
                f"{c['name']}（{THEME_DIRS.get(c['theme'], c['theme'])}）"
                for c in candidates
            )
            return f"找到 {len(candidates)} 个候选关卡：{shown}（请用完整关名）"
        else:
            return f"未找到关卡：{name}（六期关卡名均无匹配，请检查关名）"

    stage_data = await load_stage_data(stage["level_id"])
    db = await load_enemy_db()
    if not db:
        return "敌人库不可用（tomimi 数据拉取失败），请稍后再试。"
    diff_data = await load_difficulty_mods()
    grades = diff_data.get(stage["theme"]) or []
    if difficulty is not None:
        if grades:
            difficulty = max(0, min(difficulty, max_difficulty(grades)))
        else:
            # 该期难度表拉取失败：不钳到 0（否则 N15 被静默折算成
            # 难度0 的新手 HP×0.8），保留原难度仅作展示
            difficulty = max(0, difficulty)

    # 每层增益按该关所在层折算（跨层变体取最深层 = 玩家遇到时的最坏值）
    floor_nums = sorted(
        {int(f) for f in (stage_data.get("floors") or []) if str(f).isdigit()}
    )
    max_floor = floor_nums[-1] if floor_nums else 0
    rows = stage_panel_rows(stage_data, db, grades, difficulty, elite, floor=max_floor)
    # 头像（并发受限，失败降级空串）
    from ArknightsUID.utils.image import gather_b64

    icons = await gather_b64(enemy_icon_b64, [row["key"] for row in rows])
    for row, icon in zip(rows, icons):
        row["icon"] = icon

    elite_mods = stage_data.get("elite_mods") or []
    elite_chips: list[str] = []
    for effect in elite_mods:
        if not isinstance(effect, dict):
            continue
        for mod in effect.get("mods") or []:
            note = _mod_note(mod)
            if note not in elite_chips:
                elite_chips.append(note)

    floor_note = ""
    if difficulty is not None:
        floor_note = _floor_buff_note(grades, difficulty, applied_floor=max_floor)
        if floor_note and len(floor_nums) > 1:
            span = "/".join(str(f) for f in floor_nums)
            floor_note += f"（{span} 层变体按最深层）"
    difficulty_label = (
        f"难度{difficulty}" if difficulty is not None else "未指定难度（基础面板）"
    )
    if difficulty is not None and not grades:
        difficulty_label += "（该期难度数据暂不可用，按基础数值）"

    # 关卡地图：优先 wiki 上传文件（多变体），无则回退 torappu 预览（按
    # level_id 系统性生成，覆盖所有层关；PRTS 肉鸽关卡页地图即此来源）
    maps: list[str] = []
    try:
        from ArknightsUID.utils.prts.client import (
            rogue_map_file_urls,
            rogue_map_preview_urls,
        )
        from ArknightsUID.utils.render import get_image_b64_with_cache
        from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

        map_urls = await rogue_map_file_urls(stage["code"], stage["name"])
        if not map_urls:
            map_urls = rogue_map_preview_urls(stage["level_id"])
        for url in map_urls:
            b64 = await get_image_b64_with_cache(
                url, CACHE_PATH / "wiki" / "map_rogue", quality=85
            )
            if b64:
                maps.append(b64)
    except Exception as e:
        from gsuid_core.logger import logger

        logger.debug(f"[ArknightsUID] 肉鸽地图获取失败: {e}")

    return {
        "name": stage["name"],
        "code": stage["code"],
        "level_id": stage["level_id"],
        "theme_name": THEME_DIRS.get(stage["theme"], stage["theme"]),
        "difficulty": difficulty,
        "difficulty_label": difficulty_label,
        "variant_note": (
            f"同名关卡有 {stage.get('variants') or 1} 个分支变体，当前显示第一个"
            if (stage.get("variants") or 1) > 1
            else ""
        ),
        "elite": elite,
        "floors": _floors_display(stage_data.get("floors")),
        "elite_descs": [str(d) for d in (stage_data.get("eliteDesc_zh") or []) if d],
        "elite_chips": elite_chips,
        "rows": rows,
        "row_count": len(rows),
        "skipped": len(stage_data.get("enemies") or []) - len(rows),
        "floor_note": floor_note,
        "maps": maps,
        "template": "ark_rogue_stage.html",
        "source_note": "数据 tomimi.dev（游戏解包）",
        "version": ArknightsUID_version,
    }


def _floors_display(floors: list) -> str:
    """层数 [1,2] → '1-2层'；[2] → '2层'。"""
    nums = sorted({int(f) for f in (floors or []) if str(f).isdigit()})
    if not nums:
        return ""
    return f"{nums[0]}-{nums[-1]}层" if len(nums) > 1 else f"{nums[0]}层"


async def _build_rogue_ending_context(
    query: str,
    extra_aliases: dict[str, str] | None = None,
) -> dict | str:
    """肉鸽结局前置条件卡：`[期名] | 结局名 | 关键藏品名` → 该期结局清单。

    结局名单/描述来自主题表（权威），前置链来自内置攻略数据（PRTS 口径，
    见 utils/gamedata/rogue_ending.py 头注释）；结局名/藏品名命中时聚焦
    展示对应结局。关键藏品图标走 tomimi（失败降级无图），期 KV 图与
    ark肉鸽 进度卡同源。
    """
    from ArknightsUID.utils.gamedata.rogue_ending import (
        GUIDE_SOURCE_NOTE,
        GUIDE_UPDATED,
        load_theme_endings,
        resolve_query,
    )

    resolved = await resolve_query(query, extra_aliases)
    if isinstance(resolved, str):
        return resolved
    theme_dir, focus_names, via = resolved
    data = await load_theme_endings(theme_dir)
    if not data or not data.get("rows"):
        return "结局数据不可用（主题表拉取失败），请稍后再试。"

    all_rows: list[dict] = list(data["rows"])
    selected = set(focus_names)
    focus_note = ""
    if selected:
        if via == "relic":
            focus_note = f"按关键藏品「{query.strip()}」反查"
        else:
            focus_note = f"按结局「{'、'.join(focus_names)}」聚焦"
    shown = 0
    endings: list[dict] = []
    for i, row in enumerate(all_rows):
        if selected and row.get("name") not in selected:
            continue
        shown += 1
        endings.append({"seq": f"{i + 1:02d}", **row})

    # 关键藏品图标（并发受限，失败降级空串）
    from ArknightsUID.utils.gamedata.rogue_relic import relic_icon_b64

    from ArknightsUID.utils.image import gather_b64

    icon_keys = [c["item_id"] for e in endings for c in e["key_relics"]]
    icons = await gather_b64(relic_icon_b64, icon_keys)
    it = iter(icons)
    for e in endings:
        for chip in e["key_relics"]:
            icon = next(it)
            chip["icon"] = icon if isinstance(icon, str) else ""

    # 期主题 KV 图（与 _fill_rogue_theme_images 同源，失败降级无图）
    banner = ""
    try:
        from ArknightsUID.utils.prts.client import activity_banner_urls
        from ArknightsUID.utils.render import get_image_b64_with_cache
        from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

        url = (await activity_banner_urls([data["theme_name"]])).get(
            str(data["theme_name"]), ""
        )
        if url:
            banner = await get_image_b64_with_cache(
                url, CACHE_PATH / "rogue_theme", quality=82
            )
    except Exception:
        from gsuid_core.logger import logger

        logger.debug("[ArknightsUID] 肉鸽结局主题图获取失败", exc_info=True)

    return {
        "theme_name": data["theme_name"],
        "count": len(all_rows),
        "shown_count": shown,
        "focus_note": focus_note,
        "guide_updated": GUIDE_UPDATED,
        "banner": banner,
        "endings": endings,
        "notes": data.get("notes") or [],
        "template": "ark_rogue_ending.html",
        "source_note": GUIDE_SOURCE_NOTE,
        "version": ArknightsUID_version,
    }


def _format_rogue_ending(context: dict) -> str:
    lines = [
        f"「明日方舟」肉鸽结局：{context['theme_name']}（共 {context['count']} 个）"
    ]
    if context.get("focus_note"):
        lines.append(context["focus_note"])
    for e in context["endings"]:
        label = f"（{e['label']}）" if e.get("label") else ""
        lines.append(f"{e['seq']} {e['name']}{label}")
        if not e.get("has_guide"):
            desc = e.get("desc") or ""
            lines.append("  攻略整理中" + (f"：{desc}" if desc else ""))
            continue
        if e.get("unlock"):
            lines.append(f"  前置：{e['unlock']}")
        for step in e.get("steps") or []:
            lines.append(f"  · {step}")
        relics = "、".join(c["name"] for c in e.get("key_relics") or [])
        if relics:
            lines.append(f"  关键藏品：{relics}")
        if e.get("final"):
            lines.append(f"  最终：{e['final']}")
    for note in context.get("notes") or []:
        lines.append(f"※ {note}")
    lines.append(f"（{context['source_note']}，攻略核对 {context['guide_updated']}）")
    return "\n".join(lines)


@rogue_sv.on_command(("肉鸽关卡", "is关卡"), block=True)
async def send_rogue_stage(bot: Bot, ev: Event) -> None:
    query = ev.text.strip().strip("：:").strip()
    if not query:
        return await _send_text(
            bot,
            ev,
            "用法：ark肉鸽关卡 关名 [N难度] [紧急]（如 ark肉鸽关卡 拆东补西 N15）",
        )
    aliases = await _load_user_theme_aliases(ev.bot_id, ev.user_id)
    result = await _build_rogue_stage_context(query, aliases)
    if isinstance(result, str):
        return await _send_text(bot, ev, result)

    img = None
    try:
        from ArknightsUID.utils.render import (
            ark_templates,
            render_html,
        )

        img = await render_html(ark_templates, "ark_rogue_stage.html", result)
    except Exception:
        img = None
    if img:
        return await bot.send(img, at_sender=bool(ev.group_id))
    await _send_text(bot, ev, _format_rogue_stage(result))


async def _render_enemy_skill_card(name: str) -> str | None:
    """第二张图：PRTS 敌人详情卡（技能效果数值 + SP 初始/消耗/持续）。

    tomimi 简化数据只有技能键 + 词典文字，完整数值走 PRTS 敌人页
    （与主线 ark敌人 同一管线，24h 页面缓存）。查不到页/名字对不上/
    无机制数据 → None（只发第一张）。
    """
    try:
        from ArknightsUID.utils.prts.client import query_enemy

        enemy = await query_enemy(name)
        if enemy is None:
            return None
        if (enemy.name or enemy.title) != name:
            return None  # 搜索命中近似页，避免张冠李戴
        from ArknightsUID.arknightsuid_wiki import build_enemy_context
        from ArknightsUID.utils.render import (
            ark_templates,
            render_html,
        )

        context = await build_enemy_context(enemy)
        if not context.get("abilities"):
            return None
        return await render_html(ark_templates, "ark_wiki_enemy.html", context)
    except Exception:
        return None


@rogue_sv.on_command(("肉鸽敌人", "is敌人"), block=True)
async def send_rogue_enemy(bot: Bot, ev: Event) -> None:
    query = ev.text.strip().strip("：:").strip()
    if not query:
        return await _send_text(
            bot,
            ev,
            "用法：ark肉鸽敌人 名字 [期名] [N难度] [关名] [紧急]（如 ark肉鸽敌人 歌利亚 萨卡兹 N12）",
        )
    aliases = await _load_user_theme_aliases(ev.bot_id, ev.user_id)
    result = await _build_rogue_enemy_context(query, aliases)
    if isinstance(result, str):
        return await _send_text(bot, ev, result)

    template = result.get("template", "ark_rogue_enemy.html")
    img = None
    try:
        from ArknightsUID.utils.render import (
            ark_templates,
            render_html,
        )

        img = await render_html(ark_templates, template, result)
    except Exception:
        img = None
    if img:
        await bot.send(img, at_sender=bool(ev.group_id))
        # 第二张：敌人技能详情卡（PRTS 完整数值；仅敌人视图且有机制时）
        if template == "ark_rogue_enemy.html":
            skill_card = await _render_enemy_skill_card(str(result.get("name") or ""))
            if skill_card:
                await bot.send(skill_card)
        return
    if template == "ark_rogue_stage.html":
        return await _send_text(bot, ev, _format_rogue_stage(result))
    await _send_text(bot, ev, _format_rogue_enemy(result))


# 注册顺序约束：肉鸽结局 必须先于 肉鸽（startswith 分发，否则被「肉鸽」抢走，
# 与 肉鸽关卡/肉鸽敌人 同理）
@rogue_sv.on_command(("肉鸽结局", "is结局"), block=True)
async def send_rogue_ending(bot: Bot, ev: Event) -> None:
    from ArknightsUID.utils.gamedata.rogue_ending import theme_usage

    query = ev.text.strip().strip("：:").strip()
    if not query:
        return await _send_text(bot, ev, theme_usage())
    aliases = await _load_user_theme_aliases(ev.bot_id, ev.user_id)
    result = await _build_rogue_ending_context(query, aliases)
    if isinstance(result, str):
        return await _send_text(bot, ev, result)

    img = None
    try:
        from ArknightsUID.utils.render import (
            ark_templates,
            render_html,
        )

        img = await render_html(ark_templates, "ark_rogue_ending.html", result)
    except Exception:
        img = None
    if img:
        return await bot.send(img, at_sender=bool(ev.group_id))
    await _send_text(bot, ev, _format_rogue_ending(result))


# ---------------------------------------------------------------------------
# 肉鸽单局详情（ark肉鸽详情）：战绩页接口的单局战况（阵容/藏品/结局回顾）。
# 期名缺省=服务端默认期；序号缺省=最近第 1 局；「收藏」前缀查收藏战绩。
# ---------------------------------------------------------------------------

_DETAIL_CHARS_LIMIT = 12
_DETAIL_RELICS_LIMIT = 24


def _parse_detail_args(
    text: str, extra_aliases: dict[str, str]
) -> tuple[str, str, int, bool] | str:
    """ark肉鸽详情 的参数解析 → (topic_id, 期显示名, 局号, 是否收藏)。

    期名解析与战绩命令同源；序号缺省 1（最近一局）；「收藏」前缀查收藏战绩。
    """
    tokens = (text or "").split()
    favour = False
    run_no = 0
    theme_tokens: list[str] = []
    for tok in tokens:
        if tok in ("收藏", "fav"):
            favour = True
        elif tok.isdigit() and run_no == 0:
            run_no = int(tok)
        else:
            theme_tokens.append(tok)
    if run_no <= 0:
        run_no = 1
    theme_text = " ".join(theme_tokens)
    resolved = _resolve_record_theme(theme_text, extra_aliases)
    if isinstance(resolved, str):
        return resolved
    topic_id, theme_display = resolved
    return topic_id, theme_display, run_no, favour


async def _build_run_detail_context(
    stats: RogueTopicStats,
    theme_display: str,
    shown_uid: str,
    run_index: int,
    favour: bool,
) -> dict | str:
    """战绩详情卡上下文。run_index 为 1-based（最近/收藏列表内序号）。"""
    pool = stats.favour_records if favour else stats.records
    if not pool:
        kind = "收藏战绩" if favour else "最近战绩"
        return f"该期暂无{kind}记录。"
    if run_index < 1 or run_index > len(pool):
        kind = "收藏" if favour else "最近"
        return f"第 {run_index} 局超出范围（{kind}战绩共 {len(pool)} 条）。"
    run = pool[run_index - 1]

    # 主题 KV（与战绩卡同源：topics[].pic → 官方 profileBg → PRTS）
    theme_name = stats.topic_name or theme_display or "集成战略"
    banner = ""
    try:
        from ArknightsUID.utils.prts.client import activity_banner_urls
        from ArknightsUID.utils.render import get_image_b64_with_cache
        from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

        if stats.topic_pic.startswith(("http://", "https://")):
            try:
                banner = await get_image_b64_with_cache(
                    stats.topic_pic, CACHE_PATH / "rogue_theme", quality=82
                )
            except Exception:
                banner = ""
        if not banner:
            from ArknightsUID.utils.rogue.skland_assets import skland_asset_b64

            banner = await skland_asset_b64(stats.topic_id, "profileBg")
        if not banner:
            url = (await activity_banner_urls([theme_name])).get(theme_name, "")
            if url:
                banner = await get_image_b64_with_cache(
                    url, CACHE_PATH / "rogue_theme", quality=82
                )
    except Exception:
        from gsuid_core.logger import logger

        logger.debug("[ArknightsUID] 肉鸽详情主题图获取失败", exc_info=True)

    # 阵容头像（初始 + 结算，并发受限）
    from ArknightsUID.utils.player.context import char_avatar_b64

    from ArknightsUID.utils.image import gather_b64

    async def _fill(char_rows: list) -> None:
        avatars = await gather_b64(
            char_avatar_b64,
            [str(c.get("char_id") or "") for c in char_rows],
        )
        for c, av in zip(char_rows, avatars):
            c["avatar"] = av

    init_rows = [
        {"name": c.name, "char_id": c.char_id, "avatar": ""}
        for c in run.init_chars[:_DETAIL_CHARS_LIMIT]
    ]
    final_rows = [
        {"name": c.name, "char_id": c.char_id, "avatar": ""}
        for c in run.chars[:_DETAIL_CHARS_LIMIT]
    ]
    await _fill(init_rows)
    await _fill(final_rows)

    # 藏品（id → 名称 + 图标）
    relic_rows = [
        {"id": rid, "name": stats.item_names.get(rid, rid), "icon": ""}
        for rid in run.relic_ids[:_DETAIL_RELICS_LIMIT]
    ]
    from ArknightsUID.utils.gamedata.rogue_relic import relic_icon_b64

    relic_icons = await gather_b64(relic_icon_b64, [r["id"] for r in relic_rows])
    for r, ic in zip(relic_rows, relic_icons):
        r["icon"] = ic

    return {
        "name": stats.player_name or "博士",
        "uid": shown_uid,
        "theme_name": theme_name,
        "banner": banner,
        "run_index": run_index,
        "run_total": len(pool),
        "favour": favour,
        "success": run.success,
        "mode": run.mode,
        "mode_grade": run.mode_grade,
        "score": run.score,
        "band": run.band,
        "ending": run.ending_text,
        "ending_segments": run.ending_segments,
        "last_stage": run.last_stage,
        "time": format_run_ts(run.end_ts),
        "duration": _run_duration(run.start_ts, run.end_ts),
        "zones": run.zones,
        "nodes": run.arrived_nodes,
        "battle_normal": run.battle_normal,
        "battle_elite": run.battle_elite,
        "battle_boss": run.battle_boss,
        "relic_count": run.relic_count,
        "recruit_count": run.recruit_count,
        "seed": run.seed,
        "tags": run.tags,
        "init_chars": init_rows,
        "final_chars": final_rows,
        "relics": relic_rows,
        "version": ArknightsUID_version,
    }


def _format_run_detail(
    stats: RogueTopicStats,
    theme_display: str,
    shown_uid: str,
    run_index: int,
    favour: bool,
) -> str:
    """战绩详情纯文本回退（卡片渲染失败时）。"""
    pool = stats.favour_records if favour else stats.records
    run = pool[run_index - 1]
    theme_name = stats.topic_name or theme_display or "集成战略"
    kind = "收藏" if favour else "最近"
    result_text = run.ending_text or (
        f"止步 {run.last_stage}" if run.last_stage else ""
    )
    lines = [
        f"「明日方舟」肉鸽战绩 · {theme_name}",
        f"{stats.player_name or '博士'}（UID {shown_uid}）{kind}第 {run_index}/{len(pool)} 局",
        f"{'通关' if run.success else '失败'} · {run.mode}·N{run.mode_grade} · 得分 {run.score}",
    ]
    if run.band:
        lines.append(f"分队：{run.band}")
    if result_text:
        lines.append(f"结局：{result_text}")
    meta_parts = []
    if run.zones:
        meta_parts.append(f"探索 {run.zones} 层")
    if run.arrived_nodes:
        meta_parts.append(f"节点 {run.arrived_nodes}")
    battles = run.battle_normal + run.battle_elite + run.battle_boss
    if battles:
        meta_parts.append(
            f"战斗 {run.battle_normal}/{run.battle_elite}/{run.battle_boss}"
        )
    if run.relic_count:
        meta_parts.append(f"藏品 {run.relic_count}")
    if run.recruit_count:
        meta_parts.append(f"招募 {run.recruit_count}")
    duration = _run_duration(run.start_ts, run.end_ts)
    if duration:
        meta_parts.append(duration)
    time_text = format_run_ts(run.end_ts)
    if time_text:
        meta_parts.append(time_text)
    if meta_parts:
        lines.append(" · ".join(meta_parts))
    if run.tags:
        lines.append("标签：" + "、".join(run.tags))
    if run.seed:
        lines.append(f"种子：{run.seed}")
    if run.init_chars:
        lines.append("开局：" + "、".join(c.name for c in run.init_chars[:8] if c.name))
    if run.chars:
        lines.append("结算：" + "、".join(c.name for c in run.chars[:8] if c.name))
    if run.relic_ids:
        names = [stats.item_names.get(rid, rid) for rid in run.relic_ids[:20]]
        lines.append("藏品：" + "、".join(names))
    return "\n".join(lines)


# 注册顺序约束：肉鸽详情 与 肉鸽战绩 互不前缀冲突，但都须先于 肉鸽
# （长关键字优先分发已保证，此处仅维持文件内约定）
@rogue_sv.on_command(("肉鸽详情", "is详情"), block=True)
async def send_rogue_detail(bot: Bot, ev: Event) -> None:
    """ark肉鸽详情 [期名] [序号] [收藏]：单局战况（阵容/藏品/结局回顾）。

    序号是局号（战绩卡上的 #N），不是账号序号；多账号用 UID 指定。
    """
    raw = ev.text.strip()
    uid = extract_uid(raw)
    # 期名 = 剔除 UID 后的剩余词（局号/收藏在 _parse_detail_args 内消化）
    text_no_uid = raw.replace(uid, " ", 1) if uid else raw
    aliases = await _load_user_theme_aliases(ev.bot_id, ev.user_id)
    parsed = _parse_detail_args(text_no_uid, aliases)
    if isinstance(parsed, str):
        return await _send_text(bot, ev, parsed)
    topic_id, theme_display, run_no, favour = parsed

    credential = await resolve_or_reply(bot, ev, uid=uid)
    if credential is None:
        return
    response = await fetch_rogue_records(credential, topic_id)
    if isinstance(response, str):
        return await _send_text(bot, ev, response)
    code = response.get("code")
    if code != 0:
        message = safe_external_message(
            response.get("message") or response.get("msg") or ""
        )[:120]
        return await _send_text(
            bot, ev, f"肉鸽战绩查询失败：code={code}，{message or '无详情'}"
        )
    stats = parse_rogue_records(response.get("data"))
    if stats is None:
        return await _send_text(
            bot, ev, "战绩数据为空或结构未识别（该期可能未开放战绩页）。"
        )

    shown_uid = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    context = await _build_run_detail_context(
        stats, theme_display, shown_uid, run_no, favour
    )
    if isinstance(context, str):
        return await _send_text(bot, ev, context)

    img = None
    try:
        from ArknightsUID.utils.render import (
            ark_templates,
            render_html,
        )

        img = await render_html(ark_templates, "ark_rogue_run.html", context)
    except Exception:
        img = None
    if img:
        return await bot.send(img, at_sender=bool(ev.group_id))
    await _send_text(
        bot,
        ev,
        _format_run_detail(stats, theme_display, shown_uid, run_no, favour),
    )


# 注册顺序约束：肉鸽战绩 必须先于 肉鸽（长关键字优先分发已保证，此处仅
# 维持与 肉鸽关卡/肉鸽敌人/肉鸽结局 一致的文件内约定）
@rogue_sv.on_command(("肉鸽战绩", "is战绩"), block=True)
async def send_rogue_records(bot: Bot, ev: Event) -> None:
    """ark肉鸽战绩 [期名] [序号]：单期战绩明细（难度/得分/阵容/结局）。

    与 ark肉鸽（player_info 进度卡）不同源：走森空岛 App 战绩查询端点，
    期名缺省时由服务端返回默认期（响应 topics[].isSelected 标记）。
    """
    raw = ev.text.strip()
    uid = extract_uid(raw)
    index = extract_index(raw)
    # 期名 = 原文剔除 UID/序号后的剩余词（序号只在词尾，与 extract_index 同口径）
    theme_text = raw
    if uid:
        theme_text = theme_text.replace(uid, " ", 1)
    if index:
        theme_text = re.sub(r"第|号", "", theme_text)
        theme_text = re.sub(r"(?:^|\s|#)\d{1,2}\s*$", "", theme_text)

    aliases = await _load_user_theme_aliases(ev.bot_id, ev.user_id)
    resolved = _resolve_record_theme(theme_text, aliases)
    if isinstance(resolved, str):
        return await _send_text(bot, ev, resolved)
    topic_id, theme_display = resolved

    credential = await resolve_or_reply(bot, ev, uid=uid, index=index)
    if credential is None:
        return
    response = await fetch_rogue_records(credential, topic_id)
    if isinstance(response, str):
        return await _send_text(bot, ev, response)
    code = response.get("code")
    if code != 0:
        # 上游 message 可能回显请求参数，脱敏后再进回复
        message = safe_external_message(
            response.get("message") or response.get("msg") or ""
        )[:120]
        return await _send_text(
            bot, ev, f"肉鸽战绩查询失败：code={code}，{message or '无详情'}"
        )
    stats = parse_rogue_records(response.get("data"))
    if stats is None:
        return await _send_text(
            bot, ev, "战绩数据为空或结构未识别（该期可能未开放战绩页）。"
        )

    shown_uid = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    context = await _build_rogue_records_context(stats, theme_display, shown_uid)
    await _fill_record_avatars(context)
    text = _format_rogue_records(stats, theme_display, shown_uid)

    img = None
    try:
        from ArknightsUID.utils.render import (
            ark_templates,
            render_html,
        )

        img = await render_html(ark_templates, "ark_rogue_record.html", context)
    except Exception:
        img = None
    if img:
        return await bot.send(img, at_sender=bool(ev.group_id))
    await _send_text(bot, ev, text)


@rogue_sv.on_command(("肉鸽", "集成战略", "rg"), block=True)
async def send_rogue_info(bot: Bot, ev: Event) -> None:
    uid = extract_uid(ev.text)
    index = extract_index(ev.text)
    credential = await resolve_or_reply(bot, ev, uid=uid, index=index)
    if credential is None:
        return
    response = await fetch_player_info(credential)

    # 前端卡片路径：模板缺失/渲染失败时回退纯文本（与 wiki 模块同模式）。
    # 卡片带 UID 展示：走用户级覆盖脱敏（hide_uid_for），不再用接口原始 uid
    context = _build_rogue_context(
        response,
        await hide_uid_for(credential.uid, ev.user_id, ev.bot_id),
    )
    if context is not None:
        await _fill_rogue_theme_images(context)
        img = None
        try:
            from ArknightsUID.utils.render import (
                ark_templates,
                render_html,
            )

            img = await render_html(ark_templates, "ark_rogue.html", context)
        except Exception:
            img = None
        if img:
            return await bot.send(img, at_sender=bool(ev.group_id))

    await _send_text(bot, ev, _format_rogue(response))


# ---------------------------------------------------------------------------
# 用户自定义肉鸽期名（ark肉鸽别名）：对 肉鸽敌人/肉鸽关卡/肉鸽结局 的期名
# 解析生效，优先级高于内建期别名（THEME_ALIASES）。存储复用 ArkAlias 表，
# target 以 "rogue:" 前缀与干员别名区分（同一别名后写覆盖先写，与干员别名同键）。
# rogue_alias_sv 的创建见文件顶部（必须先于 rogue_sv，否则被「肉鸽」遮蔽）。
# ---------------------------------------------------------------------------
_ROGUE_ALIAS_PREFIX = "rogue:"


async def _load_user_theme_aliases(bot_id: str, user_id: str) -> dict[str, str]:
    """该用户的自定义期别名（{alias: theme_dir}；DB 异常静默为空，不阻断查询）。"""
    try:
        from ArknightsUID.utils.database.models import ArkAlias

        rows = await ArkAlias.list_aliases(bot_id=bot_id, user_id=user_id)
        return {
            row.alias: row.target[len(_ROGUE_ALIAS_PREFIX) :]
            for row in rows or []
            if str(row.target or "").startswith(_ROGUE_ALIAS_PREFIX)
        }
    except Exception:
        return {}


def _parse_alias_pair(text: str) -> tuple[str, str] | None:
    """「别名 目标」「别名=目标」「别名 → 目标」→ (alias, target)。"""
    raw = (text or "").strip()
    if not raw:
        return None
    for sep in ("=", "＝", "→", "->"):
        if sep in raw:
            left, _, right = raw.partition(sep)
            alias, target = left.strip(), right.strip()
            if alias and target:
                return alias, target
    parts = raw.split()
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return None


@rogue_alias_sv.on_fullmatch(("肉鸽别名列表", "我的肉鸽别名"), block=True)
async def list_rogue_theme_alias(bot: Bot, ev: Event) -> None:
    aliases = await _load_user_theme_aliases(ev.bot_id, ev.user_id)
    if not aliases:
        return await _send_text(
            bot,
            ev,
            "你还没有设置肉鸽期名。用法：ark肉鸽别名 树海=沉沦者的黑流树海",
        )
    from ArknightsUID.utils.gamedata.rogue_enemy import THEME_DIRS

    lines = ["「明日方舟」你的肉鸽期名："]
    lines.extend(
        f"- {alias} → {THEME_DIRS.get(theme_dir, theme_dir)}"
        for alias, theme_dir in aliases.items()
    )
    await _send_text(bot, ev, "\n".join(lines))


# 注册顺序约束：列表/删除必须先于「肉鸽别名」（core startswith 匹配，
# 否则「ark肉鸽别名列表」会被短词「肉鸽别名」抢走）
@rogue_alias_sv.on_command(("肉鸽别名删除", "删除肉鸽别名"), block=True)
async def delete_rogue_theme_alias(bot: Bot, ev: Event) -> None:
    alias = (ev.text or "").strip().lower()
    if not alias:
        return await _send_text(bot, ev, "用法：ark肉鸽别名删除 树海")
    from ArknightsUID.utils.database.models import ArkAlias

    target = await ArkAlias.resolve_alias(
        bot_id=ev.bot_id, user_id=ev.user_id, alias=alias
    )
    if target and not str(target).startswith(_ROGUE_ALIAS_PREFIX):
        return await _send_text(bot, ev, f"「{alias}」是干员别名，请用 ark别名删除。")
    if await ArkAlias.delete_alias(bot_id=ev.bot_id, user_id=ev.user_id, alias=alias):
        await _send_text(bot, ev, f"已删除肉鸽期名：{alias}")
    else:
        await _send_text(bot, ev, f"没有这个肉鸽期名：{alias}")


@rogue_alias_sv.on_command(("肉鸽别名",), block=True)
async def set_rogue_theme_alias(bot: Bot, ev: Event) -> None:
    parsed = _parse_alias_pair(ev.text)
    if not parsed:
        return await _send_text(
            bot,
            ev,
            "用法：ark肉鸽别名 <别名>=<期名>（如 ark肉鸽别名 树海=沉沦者的黑流树海）；"
            "ark肉鸽别名列表 查看；ark肉鸽别名删除 <别名> 删除。",
        )
    alias, target_text = parsed

    from ArknightsUID.utils.gamedata.rogue_enemy import (
        THEME_ALIASES,
        THEME_DIRS,
        resolve_theme_dir,
    )

    if alias in THEME_ALIASES:
        return await _send_text(bot, ev, f"「{alias}」已是内建期名，无需设置。")
    theme_dir = resolve_theme_dir(target_text)
    if not theme_dir:
        names = "、".join(THEME_DIRS.values())
        return await _send_text(
            bot,
            ev,
            f"无法识别的期名「{target_text}」。可用：{names}（或 black/sui 等目录名）。",
        )

    from ArknightsUID.utils.database.models import ArkAlias

    await ArkAlias.set_alias(
        bot_id=ev.bot_id,
        user_id=ev.user_id,
        alias=alias.lower(),
        target=f"{_ROGUE_ALIAS_PREFIX}{theme_dir}",
    )
    await _send_text(
        bot,
        ev,
        f"已设置肉鸽期名：{alias} → {THEME_DIRS.get(theme_dir, theme_dir)}"
        "（ark肉鸽敌人 / ark肉鸽关卡 / ark肉鸽结局 的期名参数生效）",
    )
