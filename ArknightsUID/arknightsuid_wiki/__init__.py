from __future__ import annotations

import asyncio
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
        EnemyLevel,
        OperatorInfo,
        OperatorSkill,
        SkillLevelDetail,
        StageInfo,
        WikiPageSummary,
        final_level,
        level_resistances,
        query_activities,
        query_enemy,
        query_gacha_pools,
        query_game_info,
        query_operator,
        query_page,
        query_stage,
        resolve_map_file_url,
        search_pages,
        select_owner_level,
    )
except ModuleNotFoundError:
    from ArknightsUID.utils.prts.client import (
        ActivityInfo,
        EnemyInfo,
        EnemyLevel,
        OperatorInfo,
        OperatorSkill,
        SkillLevelDetail,
        StageInfo,
        WikiPageSummary,
        final_level,
        level_resistances,
        query_activities,
        query_enemy,
        query_gacha_pools,
        query_game_info,
        query_operator,
        query_page,
        query_stage,
        resolve_map_file_url,
        search_pages,
        select_owner_level,
    )

try:
    from ArknightsUID.ArknightsUID.utils.gamedata.itemicon import (
        enemy_icon_b64,
        item_icon_b64,
        split_material_text,
    )
    from ArknightsUID.ArknightsUID.utils.player.context import char_avatar_b64
except ModuleNotFoundError:
    from ArknightsUID.utils.gamedata.itemicon import (
        enemy_icon_b64,
        item_icon_b64,
        split_material_text,
    )
    from ArknightsUID.utils.player.context import char_avatar_b64

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


def _final_level(levels: list[SkillLevelDetail]) -> SkillLevelDetail | None:
    """满级展示级（公共实现见 prts.client.final_level，此处保留旧名供调用/测试）。"""
    return final_level(levels)


def _owner_level(
    levels: list[SkillLevelDetail], specialize: int, main_lvl: int
) -> SkillLevelDetail | None:
    """玩家当前等级选择（公共实现见 prts.client.select_owner_level）。"""
    return select_owner_level(levels, specialize, main_lvl)


def _skill_level_context(lv: SkillLevelDetail) -> dict:
    parts = []
    if lv.sp_cost:
        parts.append(f"{lv.sp_cost}耗")
    if lv.duration:
        parts.append(f"{lv.duration}s")
    return {
        "level": lv.level,
        "description": lv.description,
        "sp_init": lv.sp_init,
        "sp_cost": lv.sp_cost,
        "duration": lv.duration,
        "meta": "·".join(parts),
    }


def _format_level_line(lv: SkillLevelDetail) -> str:
    """单级详情行：`专3：描述（90耗·30持）`，长描述截断。"""
    parts = []
    if lv.sp_cost:
        parts.append(f"{lv.sp_cost}耗")
    if lv.duration:
        parts.append(f"{lv.duration}持")
    suffix = f"（{'·'.join(parts)}）" if parts else ""
    return f"{lv.level}：{_clip(lv.description, 140)}{suffix}"


async def _build_owner_context(op: OperatorInfo, ev: Event) -> dict | None:
    """已绑定账号对该干员的练度状态（技能等级 + 在训剩余）；拿不到返回 None。

    干员查询是资料查询，账号只是增强信息：未绑定/未持有/接口失败一律静默
    降级为通用 PRTS 展示，不向用户报账号侧错误。
    """
    try:
        from ..utils.enduid.adapter import hide_uid, resolve_credential
        from ..utils.gamedata.charinfo import (
            load_char_index,
            load_skill_names,
            resolve_char,
        )
        from ..utils.player import (
            fetch_cached_player_info,
            parse_building,
            parse_player_info,
            training_remaining_secs,
            training_status_text,
        )

        credential = await resolve_credential(ev.user_id, ev.bot_id)
        if isinstance(credential, str):
            return None
        response = await fetch_cached_player_info(credential)
        if not isinstance(response, dict):
            return None
        data = parse_player_info(response)
        if data is None:
            return None
        index = await load_char_index()
        hit = resolve_char(op.name or op.title, index) if index else None
        if not hit:
            return None
        char_id = hit[0]
        entry = next((c for c in data.chars if c.char_id == char_id), None)
        if entry is None:
            return None
        skill_names = await load_skill_names()
        skills: dict[str, int] = {}
        for skill in entry.skills:
            name = skill_names.get(skill.skill_id)
            if name:
                skills[name] = skill.specialize_level
        training_remain = ""
        building = parse_building(data)
        if building and building.training_trainee_id == char_id:
            training_remain = training_status_text(
                training_remaining_secs(building)
            )
        return {
            "uid": hide_uid(credential.uid),
            "main_skill_lvl": entry.main_skill_lvl,
            "skills": skills,
            "training_remain": training_remain,
        }
    except Exception:
        logger.debug("[ArknightsUID] 玩家练度增强获取失败，走通用展示", exc_info=True)
        return None


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
        ("登场", enemy.event),
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
        for row in _enemy_secondary_stats(level):
            stats.append(f"{row['label']} {row['value']}")
        for label, value in (
            ("生命", level.max_hp),
            ("攻击", level.atk),
            ("防御", level.defense),
            ("法抗", level.res),
        ):
            if value:
                stats.append(f"{label} {value}")
        if stats:
            lines.append("属性：" + "，".join(stats))
        resistances = level_resistances(level)
        if resistances:
            lines.append(
                "抗性："
                + "，".join(
                    f"{label}（{value}）" if value != "有" else label
                    for label, value in resistances
                )
            )
        if level.abilities:
            # 合并后的机制条目（技能·名：效果（初始/消耗）+ 天赋/能力），逐条成行
            lines.append("机制：" + _clip("\n".join(level.abilities), 600))
        # 等级 2+ 变体（弑君者等有 3 个等级；差异字段已在解析层继承补全）
        for i, lv in enumerate(enemy.levels[1:], start=2):
            stats2 = [
                f"{label} {value}"
                for label, value in (
                    ("生命", lv.max_hp),
                    ("攻击", lv.atk),
                    ("防御", lv.defense),
                    ("法抗", lv.res),
                )
                if value
            ]
            if stats2:
                lines.append(f"等级{i}：" + "，".join(stats2))
            if lv.abilities:
                lines.append(
                    f"等级{i}机制：" + _clip("\n".join(lv.abilities), 300)
                )

    _append_source(lines, enemy.source_url)
    return "\n".join(lines)


def _format_stage(stage: StageInfo, enemies_ctx: list[dict] | None = None) -> str:
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
    if stage.enemies or enemies_ctx:
        # enemies_ctx 来自 _build_stage_context（含基础数值）；缺失时回退纯名单
        rows = enemies_ctx or [
            {"name": e.name, "count": e.count} for e in stage.enemies[:10]
        ]
        enemies = []
        for row in rows:
            suffix = f"x{row['count']}" if row.get("count") else ""
            stats = [
                f"{label}{row[key]}"
                for label, key in (("HP", "hp"), ("攻", "atk"), ("防", "defense"), ("抗", "res"))
                if row.get(key)
            ]
            stat_text = f"（{'·'.join(stats)}）" if stats else ""
            entry = f"{row['name']}{suffix}{stat_text}"
            if row.get("mechanics"):
                entry += f"｜机制：{_clip(row['mechanics'], 50)}"
            enemies.append(entry)
        lines.append("敌人：" + "、".join(enemies))
    if stage.assault_info:
        lines.append("突袭情报：" + _clip(stage.assault_info, 120))
    if stage.assault_diffs:
        lines.append("突袭差异：" + "；".join(stage.assault_diffs))
    if stage.env_conditions:
        lines.append(
            "环境条件：" + "；".join(_clip(c, 80) for c in stage.env_conditions[:4])
        )
    if stage.holo_indicators:
        sections = {
            i.section for i in stage.holo_indicators if i.section.startswith("基础")
        }
        lines.append(
            f"矩阵指标：{len(stage.holo_indicators)} 条"
            f"（基础{'/'.join(sections) or '分组'}每组必选1，附加自愿叠加提供评分，详见图片）"
        )
    if stage.has_contracts:
        lines.append("危机合约关卡：词条随赛季轮换，以当期赛季合约详情页为准")
    if stage.map_url:
        lines.append(f"地图：{stage.map_url}")
    _append_source(lines, stage.source_url)
    return "\n".join(lines)


def _format_operator(op: OperatorInfo, owner: dict | None = None) -> str:
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
            # 逐阶段（精英1/精英2/模组级）各带具体数值，与卡片展示一致
            if t.stages:
                for st in t.stages:
                    label = f"{st.condition}：" if st.condition else ""
                    lines.append(f"    {label}{_clip(st.effect, 150)}")
            elif t.description:
                lines.append(f"    {_clip(t.description, 200)}")
            if t.potential:
                lines.append(f"    满潜：{_clip(t.potential.effect, 150)}")

    if owner:
        bits = [f"技能等级 {owner.get('main_skill_lvl') or '?'}/7"]
        if owner.get("training_remain"):
            bits.append(f"专精训练{owner['training_remain']}")
        lines.append(f"我的练度（UID {owner.get('uid', '')}）：{'，'.join(bits)}")

    if op.skills:
        lines.append("【技能】")
        owner_skills = owner.get("skills") if owner else None
        for s in op.skills:
            head = f"  {s.name}"
            if s.skill_type:
                head += f"（{s.skill_type}）"
            lines.append(head)
            # 玩家当前级（专N 或 Lv.main）+ 满级（专3/无专精回退 7 级）详情
            current_lv: SkillLevelDetail | None = None
            if owner_skills and s.name in owner_skills:
                current_lv = _owner_level(
                    s.levels,
                    int(owner_skills[s.name]),
                    int(owner.get("main_skill_lvl") or 0),
                )
            if current_lv is not None and current_lv.description:
                lines.append(f"    当前 {_format_level_line(current_lv)}")
            final_lv = _final_level(s.levels)
            if final_lv is not None and final_lv is not current_lv:
                lines.append(f"    {_format_level_line(final_lv)}")
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


async def _skill_icon_b64(skill_name: str) -> str:
    """技能名 → skill_id（skill_table 反查）→ hycdn 图标 base64（失败空串）。"""
    if not skill_name:
        return ""
    try:
        from ..utils.gamedata.charinfo import load_skill_names
        from ..utils.path import SKILL_CACHE_PATH
        from ..utils.player.context import skill_icon_url
        from ..utils.render import get_image_b64_with_cache

        names = await load_skill_names()
        skill_id = next((sid for sid, n in names.items() if n == skill_name), "")
        if not skill_id:
            return ""
        return await get_image_b64_with_cache(skill_icon_url(skill_id), SKILL_CACHE_PATH)
    except Exception:
        return ""


async def _operator_portrait_b64(name: str) -> str:
    """干员名 → hycdn 立绘 base64（gamedata 索引反查 charId，精英档位回退）。

    立绘路径 char_skin/portrait/{charId}#N.png（N=3 E2 / 2 E1 / 1 初始），
    逐档回退逻辑在共享助手 char_portrait_b64；索引缺失/下载失败返回空串降级。
    """
    try:
        from ..utils.gamedata.charinfo import load_char_index, resolve_char
        from ..utils.player.context import char_portrait_b64

        index = await load_char_index()
        if not index:
            return ""
        hit = resolve_char(name, index)
        if not hit:
            return ""
        char_id, _ = hit
        rarity = int(index.get(char_id, {}).get("rarity") or 0)
        return await char_portrait_b64(char_id, rarity)
    except Exception:
        logger.debug("[ArknightsUID] 干员立绘获取失败", exc_info=True)
        return ""


async def _build_operator_context(op: OperatorInfo, owner: dict | None = None) -> dict:
    """干员富信息卡片上下文（技能全量逐级 + 天赋 + 专精材料 + 立绘）。

    rarity 在 PRTS 存 0-5，展示时 +1 转为 1-6 星人读值。
    skills.levels 全量保留（1-7 + 专1-3 正好 10 级，不裁剪）；另给
    current（玩家当前级，需 owner）/ final（满级展示级）两条详情，模板
    直接渲染描述文本（伤害数值在描述里，此前两版渲染均未展示）。
    """
    rarity = str(int(op.rarity) + 1) if op.rarity.isdigit() else op.rarity
    owner_skills = owner.get("skills") if owner else None
    skills_ctx: list[dict] = []
    for s in op.skills:
        current_lv: SkillLevelDetail | None = None
        if owner_skills and s.name in owner_skills:
            current_lv = _owner_level(
                s.levels,
                int(owner_skills[s.name]),
                int((owner or {}).get("main_skill_lvl") or 0),
            )
        final_lv = _final_level(s.levels)
        skills_ctx.append(
            {
                "name": s.name,
                "skill_type": s.skill_type,
                "range": s.range_,
                "icon": await _skill_icon_b64(s.name),
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
                "current": (
                    _skill_level_context(current_lv)
                    if current_lv is not None and current_lv.description
                    else None
                ),
                "final": (
                    None
                    if (current_lv is not None and current_lv is final_lv)
                    else (
                        _skill_level_context(final_lv)
                        if final_lv is not None and final_lv.description
                        else None
                    )
                ),
                "mastery_costs": [
                    {"mastery": mc.mastery, "materials": list(mc.materials)}
                    for mc in s.mastery_costs
                ],
            }
        )
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
        "portrait": await _operator_portrait_b64(op.name or op.title),
        "owner": owner,
        "skills": skills_ctx,
        "talents": [
            {
                "slot": t.slot,
                "name": t.name,
                "unlock": t.unlock,
                "description": t.description,
                "stages": [
                    {"condition": st.condition, "effect": st.effect}
                    for st in t.stages
                ],
                "potential": (
                    {"condition": t.potential.condition, "effect": t.potential.effect}
                    if t.potential
                    else None
                ),
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


# 机制条目尾部的 SP 参数括号（解析层合并格式：初始X·消耗Y·持续Z）
_RE_ABILITY_PARAMS = re.compile(r"（([^（）]*(?:初始|消耗|持续)[^（）]*)）\s*$")


def _ability_blocks(entries: list[str]) -> list[dict]:
    """机制字符串 → 分块结构 [{kind, badge, name, tag, text, params}]。

    输入是解析层 _merge_level_abilities 的合并产物，这里只做展示拆分，
    供模板按类型分色（技能=信号黄 / 天赋=情报蓝 / 其他=墨色）：
    · 技能·名字[↵[姿态标签]]：效果（初始X·消耗Y） → 名字/标签/正文/SP 参数
    · 天赋：名字↵详情 → 首行为名，余下为正文
    · 其余「键：值」条目 badge=键名，无法识别的整行原样进 text
    """
    blocks: list[dict] = []
    for entry in entries:
        text = (entry or "").strip()
        if not text:
            continue
        params: list[str] = []
        match = _RE_ABILITY_PARAMS.search(text)
        if match:
            params = [p.strip() for p in match.group(1).split("·") if p.strip()]
            text = text[: match.start()].rstrip()
        if text.startswith("技能·"):
            name_part, _, body = text[3:].partition("：")
            name_lines = name_part.split("\n", 1)
            tag = ""
            if len(name_lines) == 2 and name_lines[1].strip().startswith("["):
                name, tag = name_lines[0].strip(), name_lines[1].strip()
            else:
                name = " ".join(part.strip() for part in name_lines).strip()
            blocks.append(
                {
                    "kind": "skill",
                    "badge": "技能",
                    "name": name,
                    "tag": tag,
                    "text": body.strip(),
                    "params": params,
                }
            )
            continue
        if text.startswith("天赋"):
            label, _, body = text.partition("：")
            first_line, _, rest = body.partition("\n")
            blocks.append(
                {
                    "kind": "talent",
                    "badge": label.strip() or "天赋",
                    "name": first_line.strip(),
                    "tag": "",
                    "text": rest.strip(),
                    "params": params,
                }
            )
            continue
        label, sep, body = text.partition("：")
        if sep and len(label) <= 12 and "\n" not in label:
            blocks.append(
                {
                    "kind": "other",
                    "badge": label.strip(),
                    "name": "",
                    "tag": "",
                    "text": body.strip(),
                    "params": params,
                }
            )
        else:
            blocks.append(
                {
                    "kind": "other",
                    "badge": "",
                    "name": "",
                    "tag": "",
                    "text": text,
                    "params": params,
                }
            )
    return blocks


def _enemy_secondary_stats(level: EnemyLevel | None) -> list[dict]:
    """次级属性行 [{label, value}]：非空且非 "0" 才有展示价值（PRTS 默认 0）。"""
    rows = []
    for label, value in (
        ("移动速度", level.move_speed),
        ("攻击间隔", level.attack_interval),
        ("攻击范围半径", level.attack_radius),
        ("重量", level.weight),
        ("生命恢复", level.hp_regen),
        ("SP恢复", level.sp_regen),
        ("元素抗性", level.ep_res),
        ("损伤抵抗", level.ep_damage_res),
        ("嘲讽等级", level.taunt),
    ):
        text = (value or "").strip()
        if text and text != "0":
            rows.append({"label": label, "value": text})
    return rows


async def _build_enemy_context(enemy: EnemyInfo) -> dict:
    level = enemy.levels[0] if enemy.levels else None
    # 等级 2+ 变体通用化（弑君者等有 3 个等级块；差异字段已在解析层继承补全）
    variants = [
        {
            "label": f"等级{i + 1}",
            "hp": lv.max_hp,
            "atk": lv.atk,
            "defense": lv.defense,
            "res": lv.res,
            # 差异块未写则与等级1一致，不重复展示
            "abilities": list(lv.abilities) if lv.abilities else [],
            "blocks": _ability_blocks(lv.abilities) if lv.abilities else [],
        }
        for i, lv in enumerate(enemy.levels[1:], start=1)
    ]
    resistances = (
        [{"label": label, "value": value} for label, value in level_resistances(level)]
        if level
        else []
    )
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
        "event": enemy.event,
        "enemy_id": enemy.enemy_id,
        "has_stats": bool(level),
        "max_hp": level.max_hp if level else "",
        "atk": level.atk if level else "",
        "defense": level.defense if level else "",
        "res": level.res if level else "",
        "secondary_stats": _enemy_secondary_stats(level) if level else [],
        "resistances": resistances,
        "has_resistances": bool(resistances),
        "variants": variants,
        "abilities": list(level.abilities) if level else [],
        "ability_blocks": _ability_blocks(level.abilities) if level else [],
        "icon": await enemy_icon_b64(enemy.name or enemy.title),
        "source_url": enemy.source_url,
        "version": ArknightsUID_version,
    }


# 关卡敌方情报批量取数值的并发上限（PRTS 页面请求，页面缓存 24h）
_ENEMY_FETCH_CONCURRENCY = 4


async def _enemy_basic_stats(name: str) -> dict:
    """敌人名 → 一级基础数值 {hp, atk, defense, res}；查不到返回空 dict。

    关卡敌方情报只给名字，基础数值需逐个查敌人页（parse_page 自带 24h
    缓存，重复查询走本地）。完整数值/技能效果归敌人详情卡（ark敌人）。
    """
    try:
        enemy = await query_enemy(name)
        if enemy and enemy.levels:
            lv = enemy.levels[0]
            return {
                "hp": lv.max_hp,
                "atk": lv.atk,
                "defense": lv.defense,
                "res": lv.res,
                # 机制（等级块的天赋/能力/技能字段；完整内容走 ark敌人 详情卡）
                "abilities": [a for a in (lv.abilities or []) if a],
            }
    except Exception:
        logger.debug(f"[ArknightsUID] 关卡敌人数值获取失败：{name}", exc_info=True)
    return {}


async def _enrich_stage_enemies(stage: StageInfo, limit: int = 10) -> list[dict]:
    """敌方情报条目 + 头像 + 基础数值（并发受限）；失败条目静默降级为仅名字。"""
    targets = stage.enemies[:limit]
    semaphore = asyncio.Semaphore(_ENEMY_FETCH_CONCURRENCY)

    async def _fetch(name: str) -> dict:
        async with semaphore:
            # 头像按名散列直链（与敌人详情卡同源，失败负缓存）；
            # 数值查敌人页（parse_page 24h 缓存）
            stats = await _enemy_basic_stats(name)
            stats["icon"] = await enemy_icon_b64(name)
            return stats

    try:
        stats_list = await asyncio.gather(
            *(_fetch(e.name) for e in targets), return_exceptions=True
        )
    except Exception:
        stats_list = [{} for _ in targets]
    rows: list[dict] = []
    for enemy, stats in zip(targets, stats_list):
        row = {"name": enemy.name, "count": enemy.count, "level": enemy.level}
        if isinstance(stats, dict):
            row.update(stats)
        if not row.get("icon"):
            row["icon"] = ""
        # 机制压缩为一行（多行条目展平；完整机制在敌人详情卡）
        abilities = row.pop("abilities", None) or []
        if abilities:
            flat = "；".join(str(a).replace("\n", "；") for a in abilities)
            row["mechanics"] = _clip(flat, 120)
        else:
            row["mechanics"] = ""
        rows.append(row)
    return rows


def _stage_condition_blocks(stage: StageInfo) -> list[dict]:
    """作战条件统一区块：突袭 / 环境条件（磨难等）/ 全息矩阵指标 / 合约词条（预留）。

    块结构 {label, en, tone, note, items: [{text, tags}]}：
    tone 语义色与敌人卡机制块同源——突袭=alert红 / 环境=墨 / 指标·基础=信号黄 /
    指标·附加=情报蓝 / 合约=ghost。CC 词条本身在赛季子页（关卡页 {{合约详情}}
    为动态模板无法静态解析），先以提示条占位。
    """
    blocks: list[dict] = []
    if stage.assault_info or stage.assault_diffs:
        items = []
        if stage.assault_info:
            items.append({"text": _clip(stage.assault_info, 160), "tags": []})
        items.extend({"text": _clip(d, 100), "tags": []} for d in stage.assault_diffs)
        blocks.append(
            {"label": "突袭条件", "en": "ASSAULT", "tone": "red", "note": "", "rows": items}
        )
    if stage.env_conditions:
        blocks.append(
            {
                "label": "环境条件",
                "en": "ENVIRONMENT",
                "tone": "ink",
                "note": "",
                "rows": [
                    {"text": _clip(c, 160), "tags": []} for c in stage.env_conditions
                ],
            }
        )
    if stage.holo_indicators:
        sections: list[str] = []
        for indicator in stage.holo_indicators:
            if indicator.section and indicator.section not in sections:
                sections.append(indicator.section)
        for section in sections:
            rows = [i for i in stage.holo_indicators if i.section == section]
            basic = section.startswith("基础")
            items = []
            for indicator in rows:
                tags = []
                if indicator.kind == "锁定":
                    tags.append("锁定")
                if indicator.score:
                    tags.append(f"分数{indicator.score}")
                if indicator.grouped and not basic:
                    tags.append("冲突")
                items.append({"text": _clip(indicator.text, 220), "tags": tags})
            blocks.append(
                {
                    "label": f"矩阵指标 · {section}",
                    "en": "INDICATORS",
                    "tone": "yellow" if basic else "blue",
                    "note": "每组必选其一" if basic else "自愿叠加，仅附加指标提供评分",
                    "rows": items,
                }
            )
    if stage.has_contracts:
        blocks.append(
            {
                "label": "合约词条",
                "en": "CONTRACTS",
                "tone": "ghost",
                "note": "随赛季轮换",
                "rows": [
                    {"text": "危机合约关卡：词条以当期赛季合约详情页为准", "tags": []}
                ],
            }
        )
    return blocks


async def _build_stage_context(stage: StageInfo) -> dict:
    title = stage.code or stage.title
    # 地图：PRTS 文件命名规律 {代号}_{关卡名}_地图.png（imageinfo 解析直链，
    # 24h 缓存）；活动关等无此文件时降级为无图，map_url 仍给 PRTS.Map 链接
    map_b64 = ""
    if stage.code and stage.name:
        try:
            image_url = await resolve_map_file_url(stage.code, stage.name)
            if image_url:
                from ..utils.render import get_image_b64_with_cache
                from ..utils.resource.RESOURCE_PATH import CACHE_PATH

                map_b64 = await get_image_b64_with_cache(
                    image_url, CACHE_PATH / "wiki" / "map"
                )
        except Exception:
            logger.debug("[ArknightsUID] 关卡地图下载失败，卡片降级无图", exc_info=True)
            map_b64 = ""

    async def _drop_rows(names: list[str], limit: int = 8) -> list[dict]:
        """掉落物名 → {name, icon}（PRTS 带框道具图标，失败空串降级）。"""
        rows = []
        for name in names[:limit]:
            rows.append({"name": name, "icon": await item_icon_b64(name)})
        return rows

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
        "drops": await _drop_rows(list(stage.drops)),
        "extra_drops": await _drop_rows(list(stage.extra_drops)),
        "enemies": await _enrich_stage_enemies(stage),
        "assault_diffs": list(stage.assault_diffs),
        "has_assault": bool(stage.assault_info or stage.assault_diffs),
        "condition_blocks": _stage_condition_blocks(stage),
        "map_image": map_b64,
        "map_url": stage.map_url,
        "source_url": stage.source_url,
        "version": ArknightsUID_version,
    }


# ark面板 条件解析的正则（模块级预编译）
_RE_PHASE = re.compile(r"^精([012])$")
_RE_LV = re.compile(r"^(?:[Ll][Vv]?(\d{1,3})|(\d{1,3})级|(满级))$")
_RE_POT = re.compile(r"^(?:潜(\d)|满潜)$")
_RE_SKILL = re.compile(r"^(?:技能([123])|([一二三])技能)$")
_RE_MASTERY = re.compile(r"^专([123])$")
_RE_SKILL_LV = re.compile(r"^([1-7])级$")
_RE_MODULE_TYPE = re.compile(r"^([XYxｙy])模组?$")
_RE_MODULE_LV = re.compile(r"^模组?([123])$")
_CN_NUM = {"一": 1, "二": 2, "三": 3}


def _parse_panel_query(text: str) -> dict:
    """`干员名 [精2] [Lv45/满级] [潜6/满潜] [技能2] [7级/专3] [X模/Y模/无模] [模3] [满好]`。

    「N级」1-7 视为技能等级（独立或在 技能K 后）；干员等级用 Lv45/
    45级（≥10）/满级。返回 {name, phase, level, potential, favor,
    skill_index, skill_level, mastery, module_type, no_module,
    module_level}，未指定的键为 None/False。
    """
    config: dict = {
        "name": "", "phase": None, "level": None, "potential": None,
        "favor": None, "skill_index": None, "skill_level": None,
        "mastery": None, "module_type": None, "no_module": False,
        "module_level": None,
    }
    tokens = (text or "").strip().split()
    if not tokens:
        return config
    kept: list[str] = []
    skill_context = False
    for token in tokens:
        if m := _RE_PHASE.match(token):
            config["phase"] = int(m.group(1))
        elif m := _RE_SKILL_LV.match(token):
            # 独立 N级（1-7）：视为技能等级——干员等级低于 10 实战无意义，
            # 指定低干员等级应写 Lv7，两位数以上（45级）在 _RE_LV 兜底
            config["skill_level"] = int(m.group(1))
        elif m := _RE_LV.match(token):
            if m.group(3) is not None:  # 满级
                config["level"] = -1
            else:
                lv = int(m.group(1) or m.group(2))
                if skill_context and lv <= 7:
                    config["skill_level"] = lv
                else:
                    config["level"] = lv
                    skill_context = False
        elif m := _RE_POT.match(token):
            config["potential"] = int(m.group(1)) if m.group(1) else -1  # -1=满潜
        elif m := _RE_SKILL.match(token):
            idx = m.group(1) or _CN_NUM.get(m.group(2))
            if idx:
                config["skill_index"] = int(idx)
                skill_context = True
        elif m := _RE_MASTERY.match(token):
            config["mastery"] = int(m.group(1))
            skill_context = False
        elif m := _RE_MODULE_TYPE.match(token):
            config["module_type"] = m.group(1).upper()
        elif _RE_MODULE_LV.match(token):
            config["module_level"] = int(_RE_MODULE_LV.match(token).group(1))
        elif token in ("无模", "无模组"):
            config["no_module"] = True
        elif token == "满好":
            config["favor"] = 200
        else:
            kept.append(token)
            skill_context = False
    config["name"] = " ".join(kept)
    return config


_PROFESSION_NAMES = {
    "PIONEER": "先锋", "GUARD": "近卫", "DEFENDER": "重装", "SNIPER": "狙击",
    "CASTER": "术师", "MEDIC": "医疗", "SUPPORTER": "辅助", "SPECIALIST": "特种",
}


def _format_op_panel(context: dict) -> str:
    lines = [f"「明日方舟」面板模拟：{context['name']}"]
    lines.append("配置：" + " · ".join(context["config_labels"]))
    attrs = context["attrs"]
    lines.append(
        f"面板：生命 {attrs['hp']} 攻击 {attrs['atk']} 防御 {attrs['def']} "
        f"法抗 {attrs['res']}｜费用 {attrs['cost']} 阻挡 {attrs['block']} "
        f"再部署 {attrs['respawn']}s 间隔 {attrs['interval']}s"
    )
    for line in context.get("breakdown") or []:
        lines.append(f"  {line}")
    for talent in context.get("talents") or []:
        lines.append(f"天赋·{talent['name']}：{_clip(talent['description'], 120)}")
    skill = context.get("skill")
    if skill:
        sp = f"（初始{skill['sp_init']}·消耗{skill['sp_cost']}"
        sp += f"·持续{skill['duration']}" if skill.get("duration") else ""
        sp += "）"
        lines.append(f"{skill['title']}：{_clip(skill['description'], 200)}{sp}")
    if context.get("potentials"):
        lines.append("潜能：" + "、".join(context["potentials"]))
    lines.append(f"来源：{context['source_url']}")
    return "\n".join(lines)


async def _owner_panel_config(ev: Event, char_id: str) -> dict | None:
    """已绑定账号持有该干员时 → 当前面板配置（等级/潜能/好感/技能/模组）。"""
    try:
        from ..utils.enduid.adapter import resolve_credential
        from ..utils.player import fetch_cached_player_info, parse_player_info

        credential = await resolve_credential(ev.user_id, ev.bot_id)
        if isinstance(credential, str):
            return None
        response = await fetch_cached_player_info(credential)
        if not isinstance(response, dict):
            return None
        data = parse_player_info(response)
        if data is None:
            return None
        entry = next((c for c in data.chars if c.char_id == char_id), None)
        if entry is None:
            return None
        # 默认技能：取专精等级最高的技能（配对取该技能自身的专精档，
        # 避免「技能2专3、技能3专1」时误按技能3×专3 折算），无专精则 1 技能
        skill_index, specialize = 1, 0
        for i, skill in enumerate(entry.skills or [], start=1):
            level = skill.specialize_level or 0
            if level > specialize:
                skill_index, specialize = i, level
        equip_level = 0
        if entry.default_equip_id:
            for equip in entry.equips or []:
                if equip.equip_id == entry.default_equip_id:
                    equip_level = equip.level
        return {
            "owned": True,
            "phase": entry.evolve_phase,
            "level": entry.level,
            "potential": entry.potential_rank,
            "favor": entry.favor_percent,
            "skill_index": skill_index,
            "main_skill_lvl": entry.main_skill_lvl,
            "specialize": specialize,
            "default_equip_id": entry.default_equip_id,
            "equip_level": equip_level,
        }
    except Exception:
        return None


async def _build_op_panel_context(query: str, ev: Event) -> dict | str:
    """ark面板：任意 精英/等级/潜能/好感/模组 配置下的干员面板模拟。

    数值来自游戏解包表（character_table 插值 + 潜能/好感/模组累加），
    技能逐级文本来自 PRTS（skill_table 描述是未填充模板变量，不可用）。
    默认配置 = 用户自己的干员（未绑定/未持有 → 精2满级 潜0 技能7级 无模组）。
    """
    from ..utils.gamedata import oppanel
    from ..utils.gamedata.charinfo import load_char_index, resolve_char

    config = _parse_panel_query(query)
    name = config["name"]
    if not name:
        return "用法：ark面板 干员名 [精2] [Lv45/满级] [潜6/满潜] [技能2 专3] [X模/Y模/无模] [模3] [满好]"
    try:
        bot_id, user_id = ev.bot_id, ev.user_id
    except Exception:
        bot_id = user_id = ""
    name = await _resolve_alias(name, bot_id, user_id)
    index = await load_char_index()
    hit = resolve_char(name, index) if index else None
    if not hit:
        return f"未找到干员：{name}"
    char_id, _ = hit[0], hit[1]
    char_entry = await oppanel.load_char_entry(char_id)
    if not char_entry:
        return "干员数据表不可用（gamedata 拉取失败），请稍后再试。"

    # 默认配置：用户自己 > PRTS 基准（精2满级/潜0/技能7级/无模组）
    owner = await _owner_panel_config(ev, char_id)
    phases_count = len(char_entry.get("phases") or [])
    base_phase = min((owner or {}).get("phase", phases_count - 1), phases_count - 1)
    phase = config["phase"] if config["phase"] is not None else base_phase
    phase = max(0, min(phase, phases_count - 1))
    max_level = oppanel.max_level_at(char_entry, phase)
    level = config["level"] if config["level"] is not None else (
        (owner or {}).get("level") or max_level
    )
    if level == -1:
        level = max_level
    level = max(1, min(level, max_level))
    max_potential = int(char_entry.get("maxPotentialLevel") or 0)
    potential = config["potential"] if config["potential"] is not None else (
        (owner or {}).get("potential", 0)
    )
    if potential == -1:
        potential = max_potential
    potential = max(0, min(potential, max_potential))
    favor = config["favor"] if config["favor"] is not None else (
        (owner or {}).get("favor", 0)
    )

    # 模组：显式 X/Y > 用户默认装备 > 无
    uniequip, battle_equip = await oppanel.load_module_tables()
    choices = oppanel.module_choices(char_id, uniequip)
    module = None
    module_level = config["module_level"]
    if config["no_module"]:
        module = None
    elif config["module_type"]:
        module = oppanel.find_module(choices, config["module_type"])
    elif owner and owner.get("default_equip_id"):
        module = next(
            (c for c in choices if c["id"] == owner["default_equip_id"]), None
        )
        if module_level is None:
            module_level = owner.get("equip_level") or 3
    if module and module_level is None:
        module_level = 3
    module_modifiers = (
        oppanel.module_stat_modifiers(battle_equip, module["id"], module_level)
        if module
        else {}
    )

    panel = oppanel.compute_panel(
        char_entry, phase, level, potential, favor, module_modifiers
    )
    talents = oppanel.talents_at(char_entry, phase, level, potential)
    potentials = oppanel.potential_texts(char_entry, potential)

    # 技能（PRTS 逐级文本；选中技能 + 等级/专精）
    skill_ctx = None
    skill_list = []
    op = await query_operator(char_entry.get("name") or name)
    if op and op.skills:
        skill_index = config["skill_index"] or (owner or {}).get("skill_index", 1)
        skill_index = max(1, min(skill_index, len(op.skills)))
        skill = op.skills[skill_index - 1]
        mastery = config["mastery"]
        if mastery is None and not config["skill_level"]:
            mastery = (owner or {}).get("specialize", 0) or None
        main_lvl = (
            config["skill_level"]
            or (owner or {}).get("main_skill_lvl")
            or 7
        )
        detail = select_owner_level(
            skill.levels, mastery if mastery else 0, int(main_lvl)
        )
        if detail:
            title = skill.name or f"技能{skill_index}"
            label = f"专{mastery}" if mastery else f"{main_lvl}级"
            skill_ctx = {
                "title": title,
                "label": label,
                "description": detail.description,
                "sp_init": detail.sp_init or "0",
                "sp_cost": detail.sp_cost or "0",
                "duration": detail.duration or "",
            }
        skill_list = [
            {"name": s.name or f"技能{i}", "type": s.skill_type}
            for i, s in enumerate(op.skills, start=1)
        ]

    # 修正来源分项（基础/潜能/好感/模组）
    breakdown = []
    parts = panel["parts"]
    for label, key in (("生命", "hp"), ("攻击", "atk"), ("防御", "def")):
        seg = parts.get(key) or {}
        extra = sum(
            seg.get(k, 0.0) for k in ("potential", "favor", "module")
        )
        if extra:
            source_bits = []
            if seg.get("potential"):
                source_bits.append(f"潜能+{oppanel._fmt(seg['potential'])}")
            if seg.get("favor"):
                source_bits.append(f"好感+{oppanel._fmt(seg['favor'])}")
            if seg.get("module"):
                source_bits.append(f"模组+{oppanel._fmt(seg['module'])}")
            breakdown.append(f"{label}：基础 {oppanel._fmt(seg['base'])} → " + "、".join(source_bits))

    profession = _PROFESSION_NAMES.get(
        str(char_entry.get("profession") or ""), ""
    )
    # character_table 的稀有度是 TIER_x（=x 星）
    rarity_text = str(char_entry.get("rarity") or "")
    try:
        rarity = int(rarity_text.rsplit("_", 1)[-1])
    except ValueError:
        rarity = 0
    from ..utils.player.context import char_avatar_b64

    config_labels = [
        f"精{phase} Lv{level}", f"潜能{potential}",
        f"好感{favor}%",
    ]
    if module:
        config_labels.append(
            f"{module['type']}模组·{module['name']} Lv{module_level}"
        )
    else:
        config_labels.append("无模组")
    if skill_ctx:
        config_labels.append(f"技能：{skill_ctx['title']}·{skill_ctx['label']}")
    return {
        "name": str(char_entry.get("name") or name),
        "char_id": char_id,
        "profession": profession,
        "rarity": rarity,
        "avatar": await char_avatar_b64(char_id),
        "config_labels": config_labels,
        "owned": bool(owner),
        "attrs": panel["attrs"],
        "breakdown": breakdown,
        "talents": talents,
        "skill": skill_ctx,
        "skills": skill_list,
        "potentials": potentials,
        "module_choices": [
            {"type": c["type"], "name": c["name"]} for c in choices
        ],
        "source_url": op.source_url if op else page_url_title(char_entry.get("name") or name),
        "source_note": "数值 ArknightsGameData · 技能文本 PRTS Wiki",
        "version": ArknightsUID_version,
    }


def page_url_title(title: str) -> str:
    from ..utils.prts.client import page_url

    return page_url(title)


@wiki_sv.on_command(("面板", "干员面板", "oppanel"), block=True)
async def send_op_panel(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(
            bot, ev,
            "用法：ark面板 干员名 [精2 Lv45 潜6 满好 X模 模3 技能2 专3]（默认取你自己的练度）",
        )
    context = await _build_op_panel_context(query, ev)
    if isinstance(context, str):
        return await _send_text(bot, ev, context)
    await _render_or_text(
        bot, ev, "ark_op_panel.html", context, _format_op_panel(context)
    )


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


async def _resolve_alias(
    query: str, bot_id: str = "", user_id: str = ""
) -> str:
    """用户自定义别名 → 内建别名/英文代号/唯一前缀 → 标准干员名。

    用户别名（ArkAlias 表，精确匹配）优先级最高；gamedata 索引不可用
    或匹配不唯一时原样返回，交由 PRTS 搜索兜底。
    """
    if bot_id and user_id:
        try:
            from ..utils.user_alias import apply_user_alias

            query = await apply_user_alias(bot_id, user_id, query)
        except Exception:
            logger.debug("[ArknightsUID] 用户别名解析失败，跳过")
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
    query = await _resolve_alias(query, ev.bot_id, ev.user_id)
    op = await query_operator(query)
    if op and (op.skills or op.talents):
        # 已绑定账号时叠加玩家练度（当前技能级 vs 满级对比、在训剩余）
        owner = await _build_owner_context(op, ev)
        context = await _build_operator_context(op, owner)
        return await _render_or_text(
            bot, ev, "ark_wiki_operator.html", context, _format_operator(op, owner)
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
    query = await _resolve_alias(query, ev.bot_id, ev.user_id)
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
        skills_ctx.append({
            "name": s.name,
            "icon": await _skill_icon_b64(s.name),
            "mastery_costs": costs_ctx,
        })
    context = {
        "name": op.name or op.title,
        "rarity": rarity,
        "profession": op.profession,
        "skills": skills_ctx,
        "portrait": await _operator_portrait_b64(op.name or op.title),
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


def _format_relic(name: str, groups: list[dict]) -> str:
    lines = [f"「明日方舟」肉鸽藏品：{name}"]
    for i, g in enumerate(groups):
        head = f"  [{g.get('rarity_name') or g.get('rarity') or '-'}]"
        lines.append(
            f"{head} {'、'.join(t for t in g['themes'] if t)}\n    {_clip(g['usage'] or '（无效果说明）', 200)}"
        )
    desc = next((g["desc"] for g in groups if g.get("desc")), "")
    if desc:
        lines.append(f"  趣文：{_clip(desc, 120)}")
    return "\n".join(lines)


async def _build_relic_context(name: str, groups: list[dict]) -> dict:
    """藏品卡：同名跨期按效果去重，每组带主题与图标（tomimi 资源）。"""
    rows = []
    for g in groups:
        icon = ""
        if g.get("icon_id"):
            try:
                from ..utils.gamedata.rogue_relic import relic_icon_b64

                icon = await relic_icon_b64(g["icon_id"])
            except Exception:
                logger.debug("[ArknightsUID] 藏品图标获取失败", exc_info=True)
        rows.append(
            {
                "usage": g.get("usage") or "",
                "rarity_name": g.get("rarity_name") or "",
                "themes": [t for t in g.get("themes", []) if t],
                "icon": icon,
            }
        )
    desc = next((g["desc"] for g in groups if g.get("desc")), "")
    return {
        "name": name,
        "rows": rows,
        "desc": _clip(desc, 160),
        "source_note": "数据 ArknightsGameData · 图标 tomimi.dev",
        "version": ArknightsUID_version,
    }


@wiki_sv.on_command(("藏品", "肉鸽藏品"), block=True)
async def send_relic_query(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark藏品 名字（如 ark藏品 钝爪-典训）")
    try:
        from ..utils.gamedata.rogue_relic import (
            group_relic_effects,
            load_relic_index,
            match_relics,
        )

        index = await load_relic_index()
    except Exception:
        logger.exception("[ArknightsUID] 藏品索引加载失败")
        index = {}
    if not index:
        return await _send_text(
            bot, ev, "藏品数据不可用（roguelike_topic_table 拉取失败），请稍后再试。"
        )

    matched = match_relics(query, index)
    if not matched:
        return await _send_text(bot, ev, f"未找到藏品：{query}")
    if len(matched) > 1:
        shown = "、".join(matched)
        return await _send_text(bot, ev, f"找到 {len(matched)} 个候选：{shown}（请用完整名）")

    name = matched[0]
    groups = group_relic_effects(index[name])
    context = await _build_relic_context(name, groups)
    return await _render_or_text(
        bot, ev, "ark_rogue_relic.html", context, _format_relic(name, groups)
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
        bot, ev, "ark_wiki_stage.html", context,
        _format_stage(stage, context.get("enemies")),
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
                "avatar": await char_avatar_b64(str(entry.get("id", ""))),
            }
            for entry in matched[:20]
        ],
        "truncated": len(matched) > 20,
    }
    await _render_or_text(bot, ev, "ark_recruit.html", context, "\n".join(lines))


@wiki_sv.on_command(("公招推荐", "标签推荐", "公招最优"), block=True)
async def send_recruit_recommend(bot: Bot, ev: Event) -> None:
    """ark公招推荐 标签…（把界面看到的标签全输入）→ 1-3 标签最优组合排行。

    机制口径（PRTS 公开招募页实证）：资深干员=4★+、高级资深干员=5★+
    （9 小时招募标签不划、必得 5★/6★ 档）；6★ 仅经高资标签追加，
    具体名单为服务端数据，本工具不列 6★ 名单。
    """
    raw = _clean_query(ev.text)
    if not raw:
        return await _send_text(
            bot, ev,
            "用法：ark公招推荐 标签1 标签2 …（把公招界面看到的标签全部输入，"
            "如 ark公招推荐 高级资深干员 术师 输出 远程位 生存）",
        )
    tags = [t.strip() for t in re.split(r"[\s,，、+]+", raw) if t.strip()]
    data = await load_recruit_data()
    if not data:
        return await _send_text(
            bot, ev, "公招数据加载失败（首次需下载约 15MB 游戏数据），请稍后重试"
        )
    try:
        from ..utils.gamedata.recruit import recommend_combos
    except ImportError:
        from ArknightsUID.utils.gamedata.recruit import recommend_combos

    result = recommend_combos(tags, data)
    combos = result["combos"]
    if not combos:
        hint = ""
        if result["unknown"]:
            hint = f"（未识别的标签：{'、'.join(result['unknown'])}）"
        return await _send_text(
            bot, ev,
            f"这些标签组不出任何可公招组合{hint}。检查标签写法，"
            "或用 ark公招 <标签> 查询单个组合。",
        )

    rows = []
    for combo in combos:
        names = combo["match_names"]
        shown = names[:5]
        more = len(names) - len(shown)
        avatars = []
        for entry in combo["matches"][:4]:
            avatars.append(await char_avatar_b64(str(entry.get("id", ""))))
        rows.append(
            {
                "tags": " + ".join(combo["tags"]),
                "tier": combo["tier"],
                "tier_name": combo["tier_name"],
                "min_rarity": combo["min_rarity"],
                "max_rarity": combo["max_rarity"],
                "pool_count": len(names),
                "names": shown,
                "more": more,
                "avatars": avatars,
            }
        )

    context = {
        "version": ArknightsUID_version,
        "input_tags": " / ".join(tags),
        "unknown_tags": "、".join(result["unknown"]),
        "rows": rows,
        "best": rows[0]["tags"] if rows else "",
    }
    lines = [f"「明日方舟」公招组合推荐（输入：{' / '.join(tags)}）"]
    if result["unknown"]:
        lines.append(f"⚠️ 未识别标签：{'、'.join(result['unknown'])}（已忽略）")
    for row in rows:
        names = "、".join(row["names"])
        more = f" 等{row['pool_count']}人" if row["more"] > 0 else ""
        lines.append(
            f"[{row['tier_name']}] {row['tags']}（保底{row['min_rarity']}★，"
            f"池{row['pool_count']}人）：{names}{more}"
        )
    lines.append(
        "勾选高资/资深标签并拉满 9 小时：标签不划、必得 5★/6★ 档；"
        "6★ 仅经高级资深干员追加（名单以游戏内为准）。"
    )
    await _render_or_text(
        bot, ev, "ark_recruit_plan.html", context, "\n".join(lines)
    )


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


# ---------------------------------------------------------------------------
# 模组详情 / 危机合约 / 干员语音
# ---------------------------------------------------------------------------


@wiki_sv.on_command(("模组", "module"), block=True)
async def send_module_info(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark模组 干员名（如 ark模组 银灰）")
    try:
        from ArknightsUID.utils.gamedata.modules_info import (
            build_module_context,
            format_module_context,
        )
    except ModuleNotFoundError:
        from ArknightsUID.ArknightsUID.utils.gamedata.modules_info import (
            build_module_context,
            format_module_context,
        )

    context = await build_module_context(query)
    if isinstance(context, str):
        return await _send_text(bot, ev, context)
    await _send_text(bot, ev, format_module_context(context))


@wiki_sv.on_command(("危机合约", "合约"), block=True)
async def send_crisis_info(bot: Bot, ev: Event) -> None:
    # 词条文本数据源暂不可达（PRTS SMW 词条分类仅图标文件，
    # GameData crisis 表不含词条文本），当前仅提供赛季时间线
    # 无参数：赛季时间线（旧+新，最近 8 个）
        try:
            from ArknightsUID.utils.gamedata.crisis import load_crisis_seasons
        except ModuleNotFoundError:
            from ArknightsUID.ArknightsUID.utils.gamedata.crisis import (
                load_crisis_seasons,
            )

        seasons = await load_crisis_seasons()
        if not seasons:
            return await _send_text(
                bot, ev, "危机合约赛季数据不可用（gamedata 拉取失败），请稍后再试。"
            )
        lines = ["「明日方舟」危机合约赛季（近 8 期）："]
        for season in seasons[:8]:
            mark = "（新制式）" if season["version"] == "危机合约V2" else ""
            lines.append(
                f"- {season['name']}{mark}｜{season['start']} ~ {season['end']}"
            )
        lines.append("词条检索：ark危机合约 <关键词>（如 ark危机合约 攻击速度）")
        return await _send_text(bot, ev, "\n".join(lines))

@wiki_sv.on_command(("语音", "台词"), block=True)
async def send_voice_lines(bot: Bot, ev: Event) -> None:
    """ark语音 干员名 [关键词] → 中文台词（PRTS 语音记录子页）。"""
    query = _clean_query(ev.text)
    parts = query.split()
    if not parts:
        return await _send_text(
            bot, ev, "用法：ark语音 干员名 [关键词]（如 ark语音 银灰 / ark语音 银灰 合约）"
        )
    name = parts[0]
    keyword = parts[1] if len(parts) > 1 else ""
    name = await _resolve_alias(name, ev.bot_id, ev.user_id)

    from ..utils.prts.client import query_voice_lines

    try:
        lines = await query_voice_lines(name)
    except Exception:
        logger.warning("[ArknightsUID] 语音页查询失败", exc_info=True)
        lines = None
    if not lines:
        return await _send_text(
            bot, ev, f"未收录「{name}」的语音记录（PRTS 语音记录子页缺失）。"
        )

    if keyword:
        hits = [
            l for l in lines if keyword in l["title"] or keyword in l["text"]
        ]
        if not hits:
            return await _send_text(bot, ev, f"「{name}」的台词里没有提到「{keyword}」。")
        shown = hits[:8]
        header = f"「{name}」台词 · 「{keyword}」（{len(hits)} 条，显示前 {len(shown)} 条）"
    else:
        shown = lines[:10]
        header = f"「{name}」语音记录（共 {len(lines)} 条，显示前 {len(shown)} 条）"
    body = [header]
    body.extend(f"[{l['title']}] {l['text']}" for l in shown)
    body.append("数据：PRTS（语音记录）")
    await _send_text(bot, ev, "\n".join(body))
