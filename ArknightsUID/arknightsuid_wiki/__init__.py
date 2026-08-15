from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from jinja2.exceptions import TemplateNotFound

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.version import ArknightsUID_version

try:
    from ArknightsUID.ArknightsUID.utils.prts.client import (
        EnemyInfo,
        OperatorInfo,
        StageInfo,
        WikiPageSummary,
        query_activities,
        query_enemy,
        query_gacha_pools,
        query_game_info,
        query_operator,
        query_page,
        query_stage,
        search_pages,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.prts.client import (
        ActivityInfo,
        EnemyInfo,
        OperatorInfo,
        StageInfo,
        WikiPageSummary,
        query_activities,
        query_enemy,
        query_gacha_pools,
        query_game_info,
        query_operator,
        query_page,
        query_stage,
        search_pages,
    )

try:
    from ArknightsUID.ArknightsUID.utils.gamedata.itemicon import (
        item_icon_b64,
        split_material_text,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.gamedata.itemicon import (
        item_icon_b64,
        split_material_text,
    )

try:
    from ArknightsUID.ArknightsUID.utils.gamedata.recruit import (
        load_recruit_data,
        match_recruit,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.gamedata.recruit import (
        load_recruit_data,
        match_recruit,
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


def _format_remaining(target_ts: float, now_ts: float) -> str:
    """倒计时人读格式：剩3天14小时 / 剩5小时20分 / 剩40分钟 / 已结束。"""
    delta = target_ts - now_ts
    if delta <= 0:
        return "已结束"
    minutes = int(delta // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"剩{days}天{hours}小时"
    if hours:
        return f"剩{hours}小时{mins}分"
    return f"剩{mins}分钟"


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
    if stage.assault_diffs:
        lines.append("突袭差异：" + "；".join(stage.assault_diffs))
    if stage.map_url:
        lines.append(f"地图：{stage.map_url}")
    _append_source(lines, stage.source_url)
    return "\n".join(lines)


def _format_operator(op: OperatorInfo) -> str:
    rarity = f"{int(op.rarity) + 1}星" if op.rarity.isdigit() else op.rarity
    lines = [f"「明日方舟」干员：{op.name or op.title}"]
    basics = []
    for label, value in (
        ("稀有度", rarity),
        ("职业", op.profession),
        ("分支", op.branch),
        ("位置", op.position),
        ("势力", op.faction),
        ("标签", op.tags),
        ("获得", op.obtain),
    ):
        if value:
            basics.append(f"{label}：{value}")
    if basics:
        lines.append(" | ".join(basics))
    if op.trait:
        lines.append(f"特性：{_clip(op.trait, 160)}")

    if op.talents:
        lines.append("【天赋】")
        for t in op.talents:
            head = f"  {t.slot}·{t.name}" if t.slot and t.name else f"  {t.name or t.slot}"
            lines.append(head)
            if t.description:
                lines.append(f"    {_clip(t.description, 200)}")

    if op.skills:
        lines.append("【技能】")
        for s in op.skills:
            head = f"  {s.name}"
            if s.skill_type:
                head += f"（{s.skill_type}）"
            lines.append(head)
            # 关键级 L1/L4/L7/专1/专3 展示成长曲线（全量交给模板）
            shown = []
            for idx in (0, 3, 6, 7, 9):
                if idx < len(s.levels):
                    lv = s.levels[idx]
                    parts = []
                    if lv.sp_cost:
                        parts.append(f"{lv.sp_cost}耗")
                    if lv.duration:
                        parts.append(f"{lv.duration}持")
                    shown.append(f"{lv.level}({'/'.join(parts)})" if parts else lv.level)
            if shown:
                lines.append(f"    逐级：{' | '.join(shown)}")
            for mc in s.mastery_costs:
                lines.append(f"    {mc.mastery}材料：{'、'.join(mc.materials)}")

    if op.panel:
        panels = op.panel.panels
        lines.append("【属性】")
        # 低星干员无精英2数据，按可精英化最高阶段取（精2→精1→精0）
        for tier_label, prefix in (
            ("精二满级", "精英2_满级"),
            ("精一满级", "精英1_满级"),
            ("满级", "精英0_满级"),
        ):
            hp = panels.get(f"{prefix}_生命上限", "")
            if not hp:
                continue
            lines.append(
                f"  {tier_label}：HP{hp} "
                f"攻{panels.get(f'{prefix}_攻击', '')} "
                f"防{panels.get(f'{prefix}_防御', '')} "
                f"抗{panels.get(f'{prefix}_法术抗性', '')}"
            )
            break
        lines.append(
            f"  再部署{op.panel.redeploy} 费用{op.panel.deploy_cost} "
            f"阻挡{op.panel.block} 攻速{op.panel.attack_speed}"
        )
    if op.modules:
        lines.append("【模组】")
        for m in op.modules:
            tag = f"[{m.type_}]" if m.type_ else ""
            lines.append(f"  {tag}{m.name}：{_clip(m.description, 80)}")
    if op.potentials:
        lines.append("【潜能】" + "，".join(op.potentials))
    if op.paradox:
        reward = f" 报酬：{'/'.join(op.paradox.rewards)}" if op.paradox.rewards else ""
        lines.append(
            f"【悖论模拟】{op.paradox.name}（精英{op.paradox.elite}）{reward}"
        )
    if op.base_skills:
        lines.append("【后勤技能】")
        for b in op.base_skills:
            head = f"  {b.name}（{b.unlock}）"
            if b.room:
                head += f"[{b.room}]"
            lines.append(head)
            if b.description:
                lines.append(f"    {_clip(b.description, 120)}")

    _append_source(lines, op.source_url)
    return "\n".join(lines)


def _format_mastery(op: OperatorInfo) -> str:
    """专精材料清单纯文本（聚焦材料，按技能分组列专1/专2/专3）。"""
    lines = [f"「明日方舟」{op.name or op.title} 专精材料"]
    has_any = False
    for s in op.skills:
        if not s.mastery_costs:
            continue
        has_any = True
        lines.append(f"\n【{s.name}】")
        for mc in s.mastery_costs:
            lines.append(f"  {mc.mastery}：{'、'.join(mc.materials)}")
    if not has_any:
        lines.append("该干员无专精材料数据。")
    _append_source(lines, op.source_url)
    return "\n".join(lines)


async def _render_or_text(
    bot: Bot, ev: Event, template_name: str, context: dict, text: str
) -> None:
    """有模板则渲染出图，模板缺失/报错则回退纯文本。

    render 模块（含 fastapi/playwright）惰性导入，避免纯文本路径付出重依赖成本，
    也让单元测试在无需渲染引擎时仍可走回退分支。
    出图路径直接 bot.send(bytes)，不加 at_sender（图片无需 @，与文本路径不同，非 bug）。
    """
    img = None
    try:
        from ..utils.render import ark_templates, render_html

        img = await render_html(ark_templates, template_name, context)
    except TemplateNotFound:
        # 模板尚未由前端提供（kimi），属预期状态，降级 debug 避免日志刷屏
        logger.debug(f"[ArknightsUID] 模板未就绪，回退纯文本：{template_name}")
        img = None
    except Exception:
        logger.exception(f"[ArknightsUID] 卡片渲染失败，回退纯文本：{template_name}")
        img = None
    if img:
        await bot.send(img, at_sender=bool(ev.group_id))
    else:
        await _send_text(bot, ev, text)


async def _build_page_context(page: WikiPageSummary) -> dict:
    return {
        "title": page.title,
        "summary": _clip(page.summary),
        "fields": [{"key": k, "value": v} for k, v in page.fields.items() if v],
        "source_url": page.source_url,
        "version": ArknightsUID_version,
    }


async def _build_operator_context(op: OperatorInfo) -> dict:
    """干员富信息卡片上下文（技能全量逐级 + 天赋 + 专精材料）。

    rarity 在 PRTS 存 0-5，展示时 +1 转为 1-6 星人读值。
    skills.levels 全量保留（1-7 + 专1-3 正好 10 级，不裁剪）。
    """
    rarity = str(int(op.rarity) + 1) if op.rarity.isdigit() else op.rarity
    return {
        "title": op.title,
        "name": op.name or op.title,
        "rarity": rarity,
        "profession": op.profession,
        "branch": op.branch,
        "position": op.position,
        "faction": op.faction,
        "tags": op.tags,
        "obtain": op.obtain,
        "trait": op.trait,
        "summary": _clip(op.summary),
        "skills": [
            {
                "name": s.name,
                "skill_type": s.skill_type,
                "range": s.range_,
                "levels": [
                    {
                        "level": lv.level,
                        "description": lv.description,
                        "sp_init": lv.sp_init,
                        "sp_cost": lv.sp_cost,
                        "duration": lv.duration,
                    }
                    for lv in s.levels
                ],
                "mastery_costs": [
                    {"mastery": mc.mastery, "materials": list(mc.materials)}
                    for mc in s.mastery_costs
                ],
            }
            for s in op.skills
        ],
        "talents": [
            {
                "slot": t.slot,
                "name": t.name,
                "unlock": t.unlock,
                "description": t.description,
            }
            for t in op.talents
        ],
        "panel": (
            {
                "redeploy": op.panel.redeploy,
                "deploy_cost": op.panel.deploy_cost,
                "block": op.panel.block,
                "attack_speed": op.panel.attack_speed,
                "panels": dict(op.panel.panels),
            }
            if op.panel
            else None
        ),
        "modules": [
            {
                "name": m.name,
                "type": m.type_,
                "description": _clip(m.description, 200),
            }
            for m in op.modules
        ],
        "potentials": list(op.potentials),
        "paradox": (
            {
                "name": op.paradox.name,
                "elite": op.paradox.elite,
                "rewards": list(op.paradox.rewards),
                "description": _clip(op.paradox.description, 200),
            }
            if op.paradox
            else None
        ),
        "base_skills": [
            {
                "name": b.name,
                "unlock": b.unlock,
                "room": b.room,
                "description": _clip(b.description, 160),
            }
            for b in op.base_skills
        ],
        "source_url": op.source_url,
        "version": ArknightsUID_version,
    }


async def _build_enemy_context(enemy: EnemyInfo) -> dict:
    level = enemy.levels[0] if enemy.levels else None
    return {
        "title": enemy.title,
        "name": enemy.name or enemy.title,
        "index": enemy.index,
        "rank": enemy.rank,
        "description": _clip(enemy.description, 240),
        "damage_type": enemy.damage_type,
        "attack_type": enemy.attack_type,
        "move_type": enemy.move_type,
        "kind": enemy.kind,
        "has_stats": bool(level),
        "max_hp": level.max_hp if level else "",
        "atk": level.atk if level else "",
        "defense": level.defense if level else "",
        "res": level.res if level else "",
        "weight": level.weight if level else "",
        "abilities": list(level.abilities) if level else [],
        "source_url": enemy.source_url,
        "version": ArknightsUID_version,
    }


async def _build_stage_context(stage: StageInfo) -> dict:
    title = stage.code or stage.title
    map_b64 = ""
    if stage.map_url:
        try:
            from ..utils.render import get_image_b64_with_cache
            from ..utils.resource.RESOURCE_PATH import CACHE_PATH

            map_b64 = await get_image_b64_with_cache(
                stage.map_url, CACHE_PATH / "wiki" / "map"
            )
        except Exception:
            logger.exception("[ArknightsUID] 关卡地图转 base64 失败，模板降级")
            map_b64 = ""
    return {
        "title": title,
        "name": stage.name,
        "stage_id": stage.stage_id,
        "stage_type": stage.stage_type,
        "difficulty": stage.difficulty,
        "recommended_level": stage.recommended_level,
        "ap_cost": stage.ap_cost,
        "enemy_count": stage.enemy_count,
        "description": _clip(stage.description, 220),
        "drops": list(stage.drops[:8]),
        "extra_drops": list(stage.extra_drops[:8]),
        "enemies": [{"name": e.name, "count": e.count} for e in stage.enemies[:10]],
        "assault_diffs": list(stage.assault_diffs),
        "map_image": map_b64,
        "map_url": stage.map_url,
        "source_url": stage.source_url,
        "version": ArknightsUID_version,
    }


@wiki_sv.on_command(("wiki", "百科", "词条"), block=True)
async def send_wiki_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：arkwiki 词条名")

    page = await query_page(query)
    if page:
        context = await _build_page_context(page)
        return await _render_or_text(
            bot, ev, "ark_wiki_page.html", context, _format_page("词条", page)
        )

    # 搜索回退：列表型结果保持纯文本，不套卡片
    results = await search_pages(query, limit=5)
    await _send_text(bot, ev, _format_search(query, results))


async def _resolve_alias(query: str) -> str:
    """别名/英文代号/唯一前缀 → 标准干员名（gamedata 索引）。

    索引不可用或匹配不唯一时原样返回，交由 PRTS 搜索兜底。
    """
    try:
        from ..utils.gamedata.charinfo import load_char_index, resolve_char

        index = await load_char_index()
        if index:
            hit = resolve_char(query, index)
            if hit:
                return hit[1]
    except Exception:
        logger.debug("[ArknightsUID] 别名解析失败，使用原始查询词")
    return query


@wiki_sv.on_command(("干员", "operator", "char"), block=True)
async def send_operator_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark干员 干员名")
    query = await _resolve_alias(query)
    op = await query_operator(query)
    if op and (op.skills or op.talents):
        # 富解析成功（有技能/天赋）→ OperatorInfo 卡片
        context = await _build_operator_context(op)
        return await _render_or_text(
            bot, ev, "ark_wiki_operator.html", context, _format_operator(op)
        )
    # 回退：非干员页 或 富解析拿不到技能/天赋 → 通用 WikiPageSummary 格式
    page = await query_page(query)
    if not page:
        return await _send_text(bot, ev, f"未找到干员词条：{query}")
    context = await _build_page_context(page)
    await _render_or_text(
        bot, ev, "ark_wiki_page.html", context, _format_page("干员", page)
    )


@wiki_sv.on_command(("专精材料", "技能材料", "mastery"), block=True)
async def send_mastery_materials(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark专精材料 干员名")
    query = await _resolve_alias(query)
    op = await query_operator(query)
    if not op or not op.skills or not any(s.mastery_costs for s in op.skills):
        return await _send_text(bot, ev, f"未找到干员专精材料：{query}")
    rarity = str(int(op.rarity) + 1) if op.rarity.isdigit() else op.rarity
    skills_ctx = []
    for s in op.skills:
        if not s.mastery_costs:
            continue
        costs_ctx = []
        for mc in s.mastery_costs:
            mats = []
            for text in mc.materials:
                name, count = split_material_text(text)
                mats.append(
                    {"name": name, "count": count, "icon": await item_icon_b64(name)}
                )
            costs_ctx.append({"mastery": mc.mastery, "materials": mats})
        skills_ctx.append({"name": s.name, "mastery_costs": costs_ctx})
    context = {
        "name": op.name or op.title,
        "rarity": rarity,
        "profession": op.profession,
        "skills": skills_ctx,
        "source_url": op.source_url,
        "version": ArknightsUID_version,
    }
    await _render_or_text(
        bot, ev, "ark_wiki_mastery.html", context, _format_mastery(op)
    )


@wiki_sv.on_command(("材料", "素材", "物品", "item"), block=True)
async def send_material_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark材料 材料名")
    page = await query_page(query)
    if not page:
        return await _send_text(bot, ev, f"未找到材料词条：{query}")
    context = await _build_page_context(page)
    await _render_or_text(
        bot, ev, "ark_wiki_page.html", context, _format_page("材料", page)
    )


@wiki_sv.on_command(("敌人", "敌方", "enemy"), block=True)
async def send_enemy_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark敌人 敌人名")
    enemy = await query_enemy(query)
    if not enemy:
        return await _send_text(bot, ev, f"未找到敌人词条：{query}")
    context = await _build_enemy_context(enemy)
    await _render_or_text(
        bot, ev, "ark_wiki_enemy.html", context, _format_enemy(enemy)
    )


@wiki_sv.on_command(("关卡", "stage", "地图"), block=True)
async def send_stage_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark关卡 1-7")
    stage = await query_stage(query)
    if not stage:
        return await _send_text(bot, ev, f"未找到关卡：{query}")
    context = await _build_stage_context(stage)
    await _render_or_text(
        bot, ev, "ark_wiki_stage.html", context, _format_stage(stage)
    )


@wiki_sv.on_command(("公招", "公招查询", "recruit"), block=True)
async def send_recruit_query(bot: Bot, ev: Event) -> None:
    raw = _clean_query(ev.text)
    if not raw:
        return await _send_text(
            bot, ev, "用法：ark公招 标签1 标签2（空格或逗号分隔，如 ark公招 高级资深干员 术师）"
        )
    tags = [t.strip() for t in re.split(r"[\s,，、]+", raw) if t.strip()]
    data = await load_recruit_data()
    if not data:
        return await _send_text(
            bot, ev, "公招数据加载失败（首次需下载约 15MB 游戏数据），请稍后重试"
        )
    matched = match_recruit(tags, data)
    if not matched:
        return await _send_text(bot, ev, f"标签 {' + '.join(tags)} 无匹配的可公招干员")
    lines = [f"「明日方舟」公招：{' + '.join(tags)}（{len(matched)} 名）"]
    for entry in matched[:30]:
        tag_str = "/".join(entry["tags"][:3]) if entry["tags"] else ""
        suffix = f" {tag_str}" if tag_str else ""
        lines.append(
            f"  {entry['rarity']}★ {entry['name']} [{entry['profession']}]{suffix}"
        )
    if len(matched) > 30:
        lines.append(f"  ...共 {len(matched)} 名，已显示前 30")
    _append_source(lines, "https://prts.wiki/w/公开招募")

    context = {
        "version": ArknightsUID_version,
        "tags": " + ".join(tags),
        "total": len(matched),
        "rows": [
            {
                "rarity": int(entry["rarity"]),
                "name": entry["name"],
                "profession": entry["profession"],
                "tags": "/".join(entry["tags"][:3]) if entry["tags"] else "",
            }
            for entry in matched[:20]
        ],
        "truncated": len(matched) > 20,
    }
    await _render_or_text(bot, ev, "ark_recruit.html", context, "\n".join(lines))


@wiki_sv.on_command(("日历", "活动日历", "calendar"), block=True)
async def send_calendar(bot: Bot, ev: Event) -> None:
    """活动日历：当前版本/维护 + 进行中活动/卡池（SMW 与 PRTS 首页同源）。"""
    beijing = timezone(timedelta(hours=8))
    now_ts = datetime.now(beijing).timestamp()
    now_str = datetime.now(beijing).strftime("%Y-%m-%d %H:%M")

    activities = await query_activities()
    pools = await query_gacha_pools()
    gameinfo = await query_game_info()
    if not activities and not pools and not gameinfo:
        return await _send_text(bot, ev, "日历数据获取失败，请稍后重试")

    lines = [f"「明日方舟」日历 {now_str}（北京时间）"]
    if gameinfo.get("cnver"):
        updated = f"（更新 {gameinfo['cndate']}）" if gameinfo.get("cndate") else ""
        lines.append(f"当前版本：{gameinfo['cnver']}{updated}")

    # 维护状态（数据页手动维护，可能滞后，措辞保守）
    maint_start = gameinfo.get("cnmaintstart", "")
    maint_end = gameinfo.get("cnmaintend", "")
    maint_text = ""
    try:
        fmt = "%Y-%m-%d %H:%M"
        start_ts = datetime.strptime(maint_start, fmt).replace(
            tzinfo=beijing
        ).timestamp() if maint_start else 0.0
        end_ts = datetime.strptime(maint_end, fmt).replace(
            tzinfo=beijing
        ).timestamp() if maint_end else 0.0
        if start_ts > now_ts:
            maint_text = f"下次维护：{maint_start}（{_format_remaining(start_ts, now_ts)}）"
        elif start_ts and end_ts and start_ts <= now_ts <= end_ts:
            maint_text = f"维护中，预计 {maint_end} 结束"
    except ValueError:
        pass
    if maint_text:
        lines.append(maint_text)

    if activities:
        lines.append("【限时活动】")
        for act in activities:
            lines.append(f"  {act.title}：{_format_remaining(act.end_ts, now_ts)}")
            lines.append(f"    {act.start} ~ {act.end}")
    if pools:
        lines.append("【寻访卡池】")
        for pool in pools:
            lines.append(f"  {pool.title}：{_format_remaining(pool.end_ts, now_ts)}")

    _append_source(lines, "https://prts.wiki/w/首页")

    context = {
        "version": ArknightsUID_version,
        "now_str": now_str,
        "game_version": gameinfo.get("cnver", ""),
        "game_updated": gameinfo.get("cndate", ""),
        "maint_text": maint_text,
        "activities": [
            {
                "title": act.title,
                "remaining": _format_remaining(act.end_ts, now_ts),
                "start": act.start,
                "end": act.end,
            }
            for act in activities
        ],
        "pools": [
            {"title": pool.title, "remaining": _format_remaining(pool.end_ts, now_ts)}
            for pool in pools
        ],
    }
    await _render_or_text(bot, ev, "ark_calendar.html", context, "\n".join(lines))
