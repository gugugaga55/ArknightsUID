from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.prts.client import (
        EnemyInfo,
        StageInfo,
        WikiPageSummary,
        query_enemy,
        query_page,
        query_stage,
        search_pages,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.prts.client import (
        EnemyInfo,
        StageInfo,
        WikiPageSummary,
        query_enemy,
        query_page,
        query_stage,
        search_pages,
    )

wiki_sv = SV("ArknightsUID资料", priority=5)


def _clean_query(text: str) -> str:
    return text.strip().strip("：:").strip()


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


def _clip(value: str, limit: int = 700) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _append_source(lines: list[str], source_url: str) -> None:
    if source_url:
        lines.append(f"来源：{source_url}")


def _format_page(kind: str, page: WikiPageSummary) -> str:
    lines = [f"「明日方舟」{kind}：{page.title}"]
    if page.summary:
        lines.append(_clip(page.summary))

    if page.fields.get("干员名"):
        shown_keys = (
            "干员名",
            "稀有度",
            "职业",
            "分支",
            "位置",
            "标签",
            "所属势力",
            "获得方式",
        )
    else:
        shown_keys = (
            "名称",
            "稀有度",
            "职业",
            "分支",
            "阵营",
            "用途",
            "获取方式",
            "获得方式",
            "描述",
        )
    for key in shown_keys:
        value = page.fields.get(key, "")
        if value and f"{key}：" not in "\n".join(lines):
            lines.append(f"{key}：{_clip(value, 160)}")
        if len(lines) >= 8:
            break
    if len(lines) == 1:
        lines.append("未能解析到摘要字段，可打开来源页面查看完整内容。")
    _append_source(lines, page.source_url)
    return "\n".join(lines)


def _format_search(query: str, results: list) -> str:
    lines = [f"「明日方舟」PRTS 搜索：{query}"]
    if not results:
        lines.append("未找到相关词条。")
        return "\n".join(lines)
    for index, item in enumerate(results[:5], start=1):
        snippet = f" - {item.snippet}" if item.snippet else ""
        lines.append(f"{index}. {item.title}{snippet}")
    lines.append("可使用 arkwiki 词条名 查看详情。")
    return "\n".join(lines)


def _format_enemy(enemy: EnemyInfo) -> str:
    name = enemy.name or enemy.title
    lines = [f"「明日方舟」敌人：{name}"]
    if enemy.index:
        lines.append(f"编号：{enemy.index}")
    basics = []
    for label, value in (
        ("级别", enemy.rank),
        ("伤害", enemy.damage_type),
        ("攻击", enemy.attack_type),
        ("移动", enemy.move_type),
        ("种类", enemy.kind),
    ):
        if value:
            basics.append(f"{label}：{value}")
    if basics:
        lines.append(" | ".join(basics))
    if enemy.description:
        lines.append(_clip(enemy.description, 240))

    if enemy.levels:
        level = enemy.levels[0]
        stats = []
        for label, value in (
            ("生命", level.max_hp),
            ("攻击", level.atk),
            ("防御", level.defense),
            ("法抗", level.res),
            ("重量", level.weight),
        ):
            if value:
                stats.append(f"{label} {value}")
        if stats:
            lines.append("属性：" + "，".join(stats))
        if level.abilities:
            lines.append("能力：" + _clip("；".join(level.abilities), 280))

    _append_source(lines, enemy.source_url)
    return "\n".join(lines)


def _format_stage(stage: StageInfo) -> str:
    title = stage.code or stage.title
    if stage.name and stage.name not in title:
        title = f"{title} {stage.name}"
    lines = [f"「明日方舟」关卡：{title}"]
    details = []
    for label, value in (
        ("类型", stage.stage_type),
        ("难度", stage.difficulty),
        ("推荐", stage.recommended_level),
        ("理智", stage.ap_cost),
        ("敌数", stage.enemy_count),
    ):
        if value:
            details.append(f"{label}：{value}")
    if details:
        lines.append(" | ".join(details))
    if stage.description:
        lines.append(_clip(stage.description, 220))
    if stage.drops:
        lines.append("常规掉落：" + "、".join(stage.drops[:8]))
    if stage.extra_drops:
        lines.append("额外物资：" + "、".join(stage.extra_drops[:8]))
    if stage.enemies:
        enemies = []
        for enemy in stage.enemies[:10]:
            suffix = f"x{enemy.count}" if enemy.count else ""
            enemies.append(f"{enemy.name}{suffix}")
        lines.append("敌人：" + "、".join(enemies))
    if stage.map_url:
        lines.append(f"地图：{stage.map_url}")
    _append_source(lines, stage.source_url)
    return "\n".join(lines)


@wiki_sv.on_command(("wiki", "百科", "词条"), block=True)
async def send_wiki_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：arkwiki 词条名")

    page = await query_page(query)
    if page:
        return await _send_text(bot, ev, _format_page("词条", page))

    results = await search_pages(query, limit=5)
    await _send_text(bot, ev, _format_search(query, results))


@wiki_sv.on_command(("干员", "operator", "char"), block=True)
async def send_operator_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark干员 干员名")
    page = await query_page(query)
    if not page:
        return await _send_text(bot, ev, f"未找到干员词条：{query}")
    await _send_text(bot, ev, _format_page("干员", page))


@wiki_sv.on_command(("材料", "素材", "物品", "item"), block=True)
async def send_material_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark材料 材料名")
    page = await query_page(query)
    if not page:
        return await _send_text(bot, ev, f"未找到材料词条：{query}")
    await _send_text(bot, ev, _format_page("材料", page))


@wiki_sv.on_command(("敌人", "敌方", "enemy"), block=True)
async def send_enemy_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark敌人 敌人名")
    enemy = await query_enemy(query)
    if not enemy:
        return await _send_text(bot, ev, f"未找到敌人词条：{query}")
    await _send_text(bot, ev, _format_enemy(enemy))


@wiki_sv.on_command(("关卡", "stage", "地图"), block=True)
async def send_stage_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark关卡 1-7")
    stage = await query_stage(query)
    if not stage:
        return await _send_text(bot, ev, f"未找到关卡：{query}")
    await _send_text(bot, ev, _format_stage(stage))
