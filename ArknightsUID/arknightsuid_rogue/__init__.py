from __future__ import annotations

import asyncio
import re

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.ArknightsUID.utils.dict_utils import path
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import (
        fetch_player_info,
        resolve_credential,
    )
    from ArknightsUID.ArknightsUID.utils.gamedata.rogue_enemy import (
        STAT_LABELS,
        THEME_ALIASES,
    )
    from ArknightsUID.ArknightsUID.utils.rogue import flatten_rogue, parse_rogue
    from ArknightsUID.ArknightsUID.version import ArknightsUID_version
except ModuleNotFoundError:
    from ArknightsUID.utils.arg_utils import extract_index, extract_uid
    from ArknightsUID.utils.dict_utils import path
    from ArknightsUID.utils.enduid.adapter import (
        fetch_player_info,
        resolve_credential,
    )
    from ArknightsUID.utils.gamedata.rogue_enemy import (
        STAT_LABELS,
        THEME_ALIASES,
    )
    from ArknightsUID.utils.rogue import flatten_rogue, parse_rogue
    from ArknightsUID.version import ArknightsUID_version

rogue_sv = SV("ArknightsUID肉鸽", priority=5)

# 集成战略主题名（玩家数据 rogue_id → 官方主题名，客观游戏数据）。
# 推位规律（roguelike_topic_table 5 对名字全吻合验证）：主题表 rogue_N 从傀影
# 起编，玩家数据从灰蕈迷境起编——玩家 rogue_6 = 表 rogue_5 = 岁的界园志异。
ROGUE_THEME_NAMES = {
    "rogue_1": "刻俄柏的灰蕈迷境",
    "rogue_2": "傀影与猩红孤钻",
    "rogue_3": "水月与深蓝之树",
    "rogue_4": "探索者的银凇止境",
    "rogue_5": "萨卡兹的无终奇语",
    "rogue_6": "岁的界园志异",
    "rogue_7": "沉沦者的黑流树海",
}


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


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
            f"合计通关 {sum(r.clear_time for r in records)} 次"
            f"（{len(records)} 期主题）"
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


def _build_rogue_context(response: object) -> dict | None:
    """把 player_info 信封组装成 ark_rogue.html 的渲染上下文。

    无 records 时返回 None（调用方回退纯文本）。
    """
    rogue = path(response, "data", "rogue")
    if not isinstance(rogue, dict) or not rogue:
        return None
    info = parse_rogue(rogue)
    if not info or not info.records:
        return None
    name = path(response, "data", "status", "nickName") or "博士"
    uid = path(response, "data", "status", "uid") or ""
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
        "uid": uid,
        "records": rows,
        "total_clears": sum(r.clear_time for r in records),
        "total_relics": sum(r.relic_cnt for r in records),
        "version": ArknightsUID_version,
    }


def _parse_rogue_query(text: str) -> tuple[str, str | None, int | None, bool]:
    """`名字 [期] [难度] [紧急]` → (敌人名/关卡名, 期目录, 难度, 紧急)。

    难度取 N7 / 难度7 / 难度 7（大小写不敏感，N+数字 无空格也可）；
    期按整词匹配 THEME_ALIASES（萨卡兹/水月/黑流…，长名优先）——不能用
    子串替换：「萨卡兹魔剑士」这类敌人名本身含期名，子串会拆坏名字；
    「紧急」为保留词（紧急作战：关卡自带 buff + 额外出怪）。
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
        extra = (
            f"（{'、'.join(form['extras'])}）" if form["extras"] else ""
        )
        lines.append(
            f"{form['title']}：HP {form['hp']} 攻 {form['atk']} "
            f"防 {form['def']} 抗 {form['res']} 重量 {form['weight']}"
            f"{extra}（{form['attack']}）"
        )
        for line in form.get("mechanics") or []:
            lines.append(f"  · {line}")
    for group in context.get("stage_groups") or []:
        more = (
            f"…等 {group['total']} 关"
            if group["total"] > len(group["stages"])
            else ""
        )
        lines.append(
            f"出没·{group['theme_name']}：{'、'.join(group['stages'])}{more}"
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
            stat = " ".join(
                f"{STAT_LABELS[k]}{v}" for k, v in row["stats"].items()
            )
            note = f"｜{'、'.join(row['notes'])}" if row["notes"] else ""
            lines.append(f"  {row['label']}：{stat}{note}")
        lines.append("  注：层数增益（每层按 floorBuff 复合）未计入，完整模拟见 tomimi.dev")
    else:
        lines.append("难度面板：未识别所属期（仅基础面板；可用「ark肉鸽敌人 名字 期名 难度」指定）")
    return "\n".join(lines)


async def _build_rogue_enemy_context(query: str) -> dict | str:
    """肉鸽敌人 buff 面板查询：基础形态面板 + 机制 + 条件精确面板。

    查询语法：`名字 [期名] [N难度] [关名] [紧急]`——不指定期时自动判定
    （显式点名 > 类型标签），带关名时以关卡指定的形态块为基底（期由关卡
    隐含），「紧急」叠加该关紧急作战效果。敌人名与关卡名零重名，按
    token 前缀拆分：前段敌名 + 后段关名。
    """
    from ..utils.gamedata.rogue_enemy import (
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

    name, theme_hint, difficulty, elite = _parse_rogue_query(query)
    if not name:
        return "用法：ark肉鸽敌人 名字 [期名] [N难度] [关名] [紧急]（如 ark肉鸽敌人 歌利亚 萨卡兹 N12）"

    db = await load_enemy_db()
    if not db:
        return "敌人库不可用（tomimi 数据拉取失败），请稍后再试。"

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
    if stage_hit is not None or difficulty is not None:
        focus = build_panel_at(
            entry,
            grades,
            difficulty,
            base_block=base_block,
            extra_effects=extra_effects,
        )
    panels = (
        []
        if focus is not None
        else build_diff_panels(entry, grades) if theme_dir else []
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
    tags = class_labels(entry) + [
        t for t in ("近战", "远程") if t in "/".join(
            tt for f in (entry.get("forms") or [])
            for tt in ((f.get("normal_attack") or {}).get("atk_type") or [])
        )
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
    lines = [
        f"「明日方舟」肉鸽关卡：{context['name']}（{context['code']}）"
    ]
    tags = [
        context["theme_name"],
        context["difficulty_label"],
        "紧急" if context["elite"] else "",
        f"第{context['floors']}" if context["floors"] else "",
    ]
    lines.append(" · ".join(t for t in tags if t))
    if context["elite_descs"]:
        lines.append("紧急作战：" + "；".join(context["elite_descs"]))
    for row in context["rows"]:
        mark = "·形态%d" % (row["level"] + 1) if row["level"] else ""
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
            + (f" N{context['difficulty']}" if context["difficulty"] is not None else "")
            + (" 紧急" if context["elite"] else "")
        )
    return "\n".join(lines)


async def _build_rogue_stage_context(query: str) -> dict | str:
    """关卡视图：出怪列表（含难度/紧急折算面板）+ 紧急作战描述。

    语法：`关名 [期名] [N难度] [紧急]`——四期关卡名零重复，关名即唯一定位；
    期名可省（关卡自带期归属）。
    """
    from ..utils.gamedata.rogue_enemy import (
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

    name, _theme_hint, difficulty, elite = _parse_rogue_query(query)
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
            return f"未找到关卡：{name}（岁/黑流两期关卡数据 tomimi 暂未收录）"

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

    rows = stage_panel_rows(stage_data, db, grades, difficulty, elite)
    # 头像（并发受限，失败降级空串）
    semaphore = asyncio.Semaphore(4)

    async def _icon(key: str) -> str:
        async with semaphore:
            return await enemy_icon_b64(key)

    icons = await asyncio.gather(
        *(_icon(row["key"]) for row in rows), return_exceptions=True
    )
    for row, icon in zip(rows, icons):
        row["icon"] = icon if isinstance(icon, str) else ""

    elite_mods = stage_data.get("elite_mods") or []
    elite_chips: list[str] = []
    for effect in elite_mods:
        if not isinstance(effect, dict):
            continue
        for mod in effect.get("mods") or []:
            note = _mod_note(mod)
            if note not in elite_chips:
                elite_chips.append(note)

    floor_note = (
        _floor_buff_note(grades, difficulty) if difficulty is not None else ""
    )
    difficulty_label = (
        f"难度{difficulty}" if difficulty is not None else "未指定难度（基础面板）"
    )
    if difficulty is not None and not grades:
        difficulty_label += "（该期难度数据暂不可用，按基础数值）"

    # 关卡地图：优先 wiki 上传文件（多变体），无则回退 torappu 预览（按
    # level_id 系统性生成，覆盖所有层关；PRTS 肉鸽关卡页地图即此来源）
    maps: list[str] = []
    try:
        from ..utils.prts.client import rogue_map_file_urls, rogue_map_preview_urls
        from ..utils.render import get_image_b64_with_cache
        from ..utils.resource.RESOURCE_PATH import CACHE_PATH

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
        "elite": elite,
        "floors": _floors_display(stage_data.get("floors")),
        "elite_descs": [
            str(d) for d in (stage_data.get("eliteDesc_zh") or []) if d
        ],
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


@rogue_sv.on_command(("肉鸽关卡", "is关卡"), block=True)
async def send_rogue_stage(bot: Bot, ev: Event) -> None:
    query = ev.text.strip().strip("：:").strip()
    if not query:
        return await _send_text(bot, ev, "用法：ark肉鸽关卡 关名 [N难度] [紧急]（如 ark肉鸽关卡 拆东补西 N15）")
    result = await _build_rogue_stage_context(query)
    if isinstance(result, str):
        return await _send_text(bot, ev, result)

    img = None
    try:
        from ..utils.render import ark_templates, render_html

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
        from ..utils.prts.client import query_enemy

        enemy = await query_enemy(name)
        if enemy is None:
            return None
        if (enemy.name or enemy.title) != name:
            return None  # 搜索命中近似页，避免张冠李戴
        from ..arknightsuid_wiki import _build_enemy_context
        from ..utils.render import ark_templates, render_html

        context = await _build_enemy_context(enemy)
        if not context.get("abilities"):
            return None
        return await render_html(ark_templates, "ark_wiki_enemy.html", context)
    except Exception:
        return None


@rogue_sv.on_command(("肉鸽敌人", "is敌人"), block=True)
async def send_rogue_enemy(bot: Bot, ev: Event) -> None:
    query = ev.text.strip().strip("：:").strip()
    if not query:
        return await _send_text(bot, ev, "用法：ark肉鸽敌人 名字 [期名] [N难度] [关名] [紧急]（如 ark肉鸽敌人 歌利亚 萨卡兹 N12）")
    result = await _build_rogue_enemy_context(query)
    if isinstance(result, str):
        return await _send_text(bot, ev, result)

    template = result.get("template", "ark_rogue_enemy.html")
    img = None
    try:
        from ..utils.render import ark_templates, render_html

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


@rogue_sv.on_command(("肉鸽", "集成战略"), block=True)
async def send_rogue_info(bot: Bot, ev: Event) -> None:
    uid = extract_uid(ev.text)
    index = extract_index(ev.text)
    credential = await resolve_credential(ev.user_id, ev.bot_id, uid=uid, index=index)
    if isinstance(credential, str):
        return await _send_text(bot, ev, credential)
    response = await fetch_player_info(credential)

    # 前端卡片路径：模板缺失/渲染失败时回退纯文本（与 wiki 模块同模式）
    context = _build_rogue_context(response)
    if context is not None:
        img = None
        try:
            from ..utils.render import ark_templates, render_html

            img = await render_html(ark_templates, "ark_rogue.html", context)
        except Exception:
            img = None
        if img:
            return await bot.send(img, at_sender=bool(ev.group_id))

    await _send_text(bot, ev, _format_rogue(response))
