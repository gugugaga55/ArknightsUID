"""Wiki 展示格式：只转换已取得的数据，不执行 I/O 或注册命令。

文本回退与卡片区块共用确定性的截断、等级选择和内容整理；查询、玩家
数据读取、图片获取和发送由包入口负责。本模块不依赖 Bot/Event 或渲染器。
"""

from __future__ import annotations

import re

from ArknightsUID.utils.prts.client import (
    EnemyInfo,
    EnemyLevel,
    OperatorInfo,
    SkillLevelDetail,
    StageInfo,
    WikiPageSummary,
    final_level,
    level_resistances,
    select_owner_level,
)
from ArknightsUID.utils.skillfx import skill_meta

# 机制条目尾部的 SP 参数括号（解析层合并格式：初始X·消耗Y·持续Z）
_RE_ABILITY_PARAMS = re.compile(r"（([^（）]*(?:初始|消耗|持续)[^（）]*)）\s*$")


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


def _format_level_line(lv: SkillLevelDetail) -> str:
    """单级详情行：`专3：描述（初动10·消耗24·持续30秒）`，长描述截断。"""
    meta = skill_meta(lv)
    suffix = f"（{meta}）" if meta else ""
    return f"{lv.level}：{_clip(lv.description, 140)}{suffix}"


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
                lines.append(f"等级{i}机制：" + _clip("\n".join(lv.abilities), 300))

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
                for label, key in (
                    ("HP", "hp"),
                    ("攻", "atk"),
                    ("防", "defense"),
                    ("抗", "res"),
                )
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
            head = (
                f"  {t.slot}·{t.name}" if t.slot and t.name else f"  {t.name or t.slot}"
            )
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
            if s.range_:
                head += f"｜范围 {s.range_}"
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
                    meta = skill_meta(s.levels[idx])
                    shown.append(
                        f"{s.levels[idx].level}({meta})"
                        if meta
                        else s.levels[idx].level
                    )
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
            # 具体效果摘要：特性追加 + 天赋更新（详情见 ark模组）
            if m.trait:
                lines.append(
                    f"    特性追加：{_clip(m.trait.replace(chr(10), ' '), 100)}"
                )
            for lvl in ("2", "3"):
                update = m.talent_updates.get(lvl)
                if update:
                    lines.append(
                        f"    Lv{lvl}：{_clip(update.replace(chr(10), ' '), 100)}"
                    )
    if op.potentials:
        lines.append("【潜能】" + "，".join(op.potentials))
    if op.paradox:
        reward = f" 报酬：{'/'.join(op.paradox.rewards)}" if op.paradox.rewards else ""
        lines.append(f"【悖论模拟】{op.paradox.name}（精英{op.paradox.elite}）{reward}")
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
        sp = f"（初动{skill['sp_init']}·消耗{skill['sp_cost']}"
        sp += f"·持续{skill['duration']}秒" if skill.get("duration") else ""
        sp += "）"
        lines.append(f"{skill['title']}：{_clip(skill['description'], 200)}{sp}")
    if context.get("potentials"):
        lines.append("潜能：" + "、".join(context["potentials"]))
    lines.append(f"来源：{context['source_url']}")
    return "\n".join(lines)


def _format_relic(name: str, groups: list[dict]) -> str:
    lines = [f"「明日方舟」肉鸽藏品：{name}"]
    for g in groups:
        head = f"  [{g.get('rarity_name') or g.get('rarity') or '-'}]"
        lines.append(
            f"{head} {'、'.join(t for t in g['themes'] if t)}\n    {_clip(g['usage'] or '（无效果说明）', 200)}"
        )
    desc = next((g["desc"] for g in groups if g.get("desc")), "")
    if desc:
        lines.append(f"  趣文：{_clip(desc, 120)}")
    return "\n".join(lines)


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
            {
                "label": "突袭条件",
                "en": "ASSAULT",
                "tone": "red",
                "note": "",
                "rows": items,
            }
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
