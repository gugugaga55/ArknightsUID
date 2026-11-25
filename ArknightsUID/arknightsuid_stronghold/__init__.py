"""卫戍协议（stronghold / auto_chess）战绩与攻略。

- ark卫戍 / ark卫戍战绩：森空岛「卫戍协议数据」同源战绩卡
  （GET api/v1/game/arknights/auto-chess，统计每日更新一次）
- ark卫戍攻略：PRTS 卫戍协议页模式总览卡（自渲染，见 utils/prts/stronghold）

接口与 18 支小队元数据的口径来源见 utils/gamedata/stronghold.py 模块注释。
"""

from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ArknightsUID.utils.arg_utils import split_account
from ArknightsUID.utils.card import (
    resolve_or_reply,
    send_card_or_text,
    send_text as _send_text,
)
from ArknightsUID.utils.enduid.adapter import (
    fetch_stronghold_info,
    hide_uid_for,
)
from ArknightsUID.utils.gamedata.stronghold import (
    KIND_LABELS,
    KIND_ORDER,
    STAT_FIELDS,
    StrongholdStats,
    band_icon,
    parse_stronghold_info,
)
from ArknightsUID.utils.gamedata.stronghold_guide import (
    GarrisonBond,
    GarrisonChar,
    GarrisonGuideEntry,
    GarrisonResource,
    GarrisonStrategy,
    fetch_guides,
    fetch_resource,
    parse_combo_tokens,
    resolve_combo,
    resolve_query,
    strip_query_prefix,
)
from ArknightsUID.utils.prts.stronghold import (
    StrongholdGuide,
    fetch_stronghold_guide,
)
from ArknightsUID.utils.reply_safety import safe_external_message
from ArknightsUID.version import ArknightsUID_version

# 注册顺序约束：卫戍攻略 SV 必须先于卫戍 SV 创建——core 按 SV 注册序
# startswith 分发，否则「卫戍攻略」会被「卫戍/卫戍战绩」抢走
# （与 arknightsuid_rogue 的肉鸽别名/肉鸽同款约束）。
stronghold_guide_sv = SV("ArknightsUID卫戍攻略", priority=5)
stronghold_sv = SV("ArknightsUID卫戍", priority=5)

# 战绩端点（arknights/auto-chess）门控：已通过真号 stronghold_dump.json 实测校对
_STRONGHOLD_RECORDS_ENABLED = True
_RECORDS_GATED_HINT = (
    "卫戍战绩接口待实装校对，当前可使用 ark卫戍攻略 <盟约|策略|干员|道具> 查询图鉴与社区攻略。"
)

# 每协议展示的小队条数（按通关次数降序；全 18 支上卡不可读）
_TEAM_TOP_N = 3
# 每支小队展示的击杀排行条数
_DEFEAT_TOP_N = 2

_DAILY_NOTE = "统计非实时，每日更新一次"
_SOURCE_NOTE = "数据源 森空岛 · 攻略口径 PRTS Wiki"

# 查询卡：盟约成员/攻略列表的展示上限
_MEMBER_LIMIT = 12
_GUIDE_LIMIT = 5
_IDEA_MAX_LEN = 140
_QUERY_HINT = (
    "可查：盟约名（炎/不屈/乌萨斯）、策略名或发起干员（阿米娅）、本期干员名、卫戍道具；"
    "支持「核心盟约/附加盟约/策略/干员/道具」前缀限定；多词条用 + 组合（炎+不屈+阿米娅）"
)


def _kind_order(stats: StrongholdStats) -> list[str]:
    """展示协议顺序：已知 KIND_ORDER 在前，上游新增的未知 kind 追加。"""
    known = [k for k in KIND_ORDER if k in stats.teams]
    extra = [k for k in stats.raw_kinds if k not in KIND_ORDER and k in stats.teams]
    return known + extra


def _kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def _format_value(count: int, unit: str) -> str:
    return f"{count}{unit}" if unit else str(count)


def _build_stat_rows(stats: StrongholdStats) -> list[dict]:
    rows: list[dict] = []
    for key, label, unit, badge in STAT_FIELDS:
        entry = stats.stats.get(key) or {}
        rows.append(
            {
                "label": label,
                "value": _format_value(int(entry.get("count", 0)), unit),
                "is_most": bool(entry.get("isMost")),
                "badge": badge,
            }
        )
    return rows


def _build_team_rows(teams: list) -> list[dict]:
    ranked = sorted(
        teams,
        key=lambda t: (-t.pass_count, t.shortest_round or 10**9, t.name),
    )
    rows: list[dict] = []
    for team in ranked[:_TEAM_TOP_N]:
        active_defeats = sorted(
            (d for d in team.defeats if int(d.get("defeat", 0)) > 0),
            key=lambda d: -int(d.get("defeat", 0)),
        )
        rows.append(
            {
                "name": team.name,
                "band_id": team.id,
                "pass_count": team.pass_count,
                "shortest_round": team.shortest_round,
                "shortest_text": (
                    f"{team.shortest_round}回合" if team.shortest_round else "—"
                ),
                "is_most_pass": team.is_most_pass,
                "is_fastest": team.is_fastest,
                "defeats": active_defeats[:_DEFEAT_TOP_N],
            }
        )
    return rows


async def _build_records_context(
    stats: StrongholdStats,
    shown_uid: str,
    strategies: dict | None = None,
) -> dict:
    """战绩卡模板 context（含小队图标/头像 b64，失败回空不影响排版）。"""
    from ArknightsUID.utils.image import gather_b64
    from ArknightsUID.utils.render import get_image_b64_with_cache
    from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

    icon_dir = CACHE_PATH / "stronghold"
    icon_dir.mkdir(parents=True, exist_ok=True)

    async def _icon(band_id: str) -> str:
        url = band_icon(band_id, strategies)
        if not url:
            return ""
        return await get_image_b64_with_cache(url, icon_dir)

    kind_blocks = []
    band_ids: list[str] = []
    for kind in _kind_order(stats):
        team_rows = _build_team_rows(stats.teams[kind])
        if not team_rows:
            continue
        band_ids.extend(row["band_id"] for row in team_rows)
        kind_blocks.append(
            {"label": _kind_label(kind), "kind": kind, "teams": team_rows}
        )

    icons = dict(zip(band_ids, await gather_b64(_icon, band_ids), strict=True))
    for block in kind_blocks:
        for row in block["teams"]:
            row["icon"] = icons.get(row["band_id"], "")

    avatar = ""
    if stats.avatar_url:
        avatar = await get_image_b64_with_cache(stats.avatar_url, icon_dir)

    return {
        "version": ArknightsUID_version,
        "name_main": stats.player_name_main or "未知博士",
        "name_tag": stats.player_name_tag,
        "level": stats.player_level,
        "server_label": "官服" if stats.is_official else "B服",
        "uid_shown": shown_uid,
        "avatar": avatar,
        "badge": stats.badge,
        "stat_rows": _build_stat_rows(stats),
        "kind_blocks": kind_blocks,
        "char_upgrade": stats.char_upgrade[:3],
        "daily_note": _DAILY_NOTE,
        "source_note": _SOURCE_NOTE,
    }


def _format_records(stats: StrongholdStats, shown_uid: str) -> str:
    """战绩卡纯文本回退（模板缺失/渲染失败时发送）。"""
    lines = [
        f"【卫戍协议战绩】Dr.{stats.player_name_main}"
        f"{f'#{stats.player_name_tag}' if stats.player_name_tag else ''}"
        f" Lv.{stats.player_level}"
        f" {'官服' if stats.is_official else 'B服'} {shown_uid}".rstrip()
    ]
    if stats.badge:
        lines.append(f"头衔：{stats.badge}")
    for key, label, unit, _badge in STAT_FIELDS:
        entry = stats.stats.get(key) or {}
        count = int(entry.get("count", 0))
        mark = "（全服之最）" if entry.get("isMost") else ""
        lines.append(f"{label}：{_format_value(count, unit)}{mark}")
    for kind in _kind_order(stats):
        rows = _build_team_rows(stats.teams[kind])
        if not rows:
            continue
        lines.append(f"◆ {_kind_label(kind)}")
        for row in rows:
            tags = []
            if row["is_most_pass"]:
                tags.append("最常用")
            if row["is_fastest"]:
                tags.append("最速")
            tag_text = f"（{'/'.join(tags)}）" if tags else ""
            lines.append(
                f"  {row['name']}：通关 {row['pass_count']} 次"
                f"，最快 {row['shortest_text']}{tag_text}"
            )
    if stats.char_upgrade:
        ups = "、".join(
            f"{item['name']}×{item['count']}" for item in stats.char_upgrade[:3]
        )
        lines.append(f"升金干员：{ups}")
    lines.append(_DAILY_NOTE)
    return "\n".join(lines)


@stronghold_sv.on_command(("卫戍战绩", "卫戎战绩", "自走棋战绩"), block=True)
async def send_stronghold_records(bot: Bot, ev: Event) -> None:
    """ark卫戍战绩 [序号]：卫戍协议战绩卡（受 _STRONGHOLD_RECORDS_ENABLED 门控）。"""
    if not _STRONGHOLD_RECORDS_ENABLED:
        return await _send_text(bot, ev, _RECORDS_GATED_HINT)
    try:
        _, uid, index = split_account(ev.text or "")
    except ValueError as exc:
        return await _send_text(bot, ev, str(exc))
    await _send_stronghold_records(bot, ev, uid, index)


@stronghold_sv.on_command(("卫戍", "卫戎", "自走棋"), block=True)
async def send_stronghold_query(bot: Bot, ev: Event) -> None:
    try:
        query, uid, index = split_account(ev.text or "")
    except ValueError as exc:
        return await _send_text(bot, ev, str(exc))
    if query:
        return await _send_stronghold_guide(bot, ev, query)
    if not _STRONGHOLD_RECORDS_ENABLED:
        return await _send_stronghold_guide(bot, ev, "")
    await _send_stronghold_records(bot, ev, uid, index)


async def _send_stronghold_records(bot: Bot, ev: Event, uid: str, index: int) -> None:
    credential = await resolve_or_reply(bot, ev, uid=uid, index=index)
    if credential is None:
        return
    response = await fetch_stronghold_info(credential)
    if isinstance(response, str):
        return await _send_text(bot, ev, response)
    code = response.get("code")
    if code != 0:
        message = safe_external_message(
            response.get("message") or response.get("msg") or ""
        )[:120]
        return await _send_text(
            bot, ev, f"卫戍战绩查询失败：code={code}，{message or '无详情'}"
        )
    resource = await fetch_resource()
    strategies = resource.strategies if resource is not None else None
    stats = parse_stronghold_info(response.get("data"), strategies=strategies)
    if stats is None:
        return await _send_text(
            bot, ev, "卫戍数据为空或结构未识别（可能未开放或未游玩该模式）。"
        )

    shown_uid = await hide_uid_for(credential.uid, ev.user_id, ev.bot_id)
    context = await _build_records_context(stats, shown_uid, strategies=strategies)
    text = _format_records(stats, shown_uid)
    await send_card_or_text(bot, ev, "ark_stronghold.html", context, text)


# 物资调配各小节的英文眉标（卡片 sect-en；缺省 SUPPLY）
_SUPPLY_SECTION_EN = {
    "干员": "OPERATORS",
    "盟约": "BANDS",
    "特质": "TRAITS",
    "道具": "ITEMS",
}


def _entry_dict(entry) -> dict:
    # GuideEntry 是 slots dataclass（无 __dict__），显式展开保模板契约
    return {
        "name": entry.name,
        "tagline": entry.tagline,
        "bullets": entry.bullets,
    }


def _build_guide_context(guide: StrongholdGuide) -> dict:
    return {
        "version": ArknightsUID_version,
        "intro": guide.intro,
        "supply_sections": [
            {
                "name": name,
                "en": _SUPPLY_SECTION_EN.get(name, "SUPPLY"),
                "prose": prose,
                "bullets": bullets,
            }
            for name, prose, bullets in guide.supply_sections
        ],
        "flow_bullets": guide.flow_bullets,
        "coop_modes": [_entry_dict(entry) for entry in guide.coop_modes],
        "difficulty_modes": [
            _entry_dict(entry) for entry in guide.difficulty_modes
        ],
        "source_url": guide.source_url,
        "source_note": _SOURCE_NOTE,
        "daily_note": _DAILY_NOTE,
    }


def _format_guide(guide: StrongholdGuide) -> str:
    """攻略卡纯文本回退。"""
    lines = ["【卫戍协议 · 模式总览】"]
    if guide.intro:
        lines.append(guide.intro)
    for name, prose, bullets in guide.supply_sections:
        lines.append(f"◆ {name}")
        if prose:
            lines.append(f"  {prose}")
        for bullet in bullets:
            lines.append(f"  · {bullet}")
    if guide.flow_bullets:
        lines.append("◆ 模拟流程")
        lines.extend(f"  · {bullet}" for bullet in guide.flow_bullets)
    if guide.coop_modes:
        lines.append("◆ 联机方式")
        for entry in guide.coop_modes:
            lines.append(f"  · {entry.name}：{entry.tagline}")
    if guide.difficulty_modes:
        lines.append("◆ 难度模式")
        for entry in guide.difficulty_modes:
            lines.append(f"  · {entry.name}：{entry.tagline}")
    lines.append("关卡详情可用「ark关卡 AC-4」查询")
    lines.append(guide.source_url)
    return "\n".join(lines)


def _guide_row(entry: GarrisonGuideEntry, resource: GarrisonResource) -> dict:
    """攻略列表行（策略/盟约/阵容/装备 id → 展示名，运营思路压平截断）。"""
    strategy_names = [
        resource.strategies[s].name
        for s in entry.strategy_ids
        if s in resource.strategies
    ]
    bond_names = [
        resource.bonds[b].name
        for b in entry.season_bond_ids + entry.regular_bond_ids
        if b in resource.bonds
    ]
    rec_chars: list[str] = []
    for cid, merge_lv in entry.rec_char_ids:
        char = resource.chars.get(cid)
        label = char.name if char and char.name else cid
        if label:
            rec_chars.append(f"{label}★" if merge_lv else label)
        if len(rec_chars) >= 6:
            break
    rec_items: list[str] = []
    for iid in entry.rec_item_ids:
        info = resource.items.get(iid)
        label = str((info or {}).get("name") or "")
        if label:
            rec_items.append(label)
        if len(rec_items) >= 4:
            break
    compact_idea = " ".join((entry.idea or "").split())
    if len(compact_idea) > _IDEA_MAX_LEN:
        compact_idea = compact_idea[:_IDEA_MAX_LEN] + "…"
    return {
        "title": entry.title,
        "poster": entry.poster,
        "liked": entry.liked,
        "scope": entry.scope,
        "tags": (strategy_names + bond_names)[:5],
        "rec_chars": rec_chars,
        "rec_items": rec_items,
        "idea": compact_idea,
        "url": entry.url,
    }


async def _fill_images(context: dict) -> None:
    """查询卡图标/头像批量 b64 内联（失败回空，不影响排版）。"""
    from ArknightsUID.utils.image import gather_b64
    from ArknightsUID.utils.render import get_image_b64_with_cache
    from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

    icon_dir = CACHE_PATH / "stronghold_guide"
    icon_dir.mkdir(parents=True, exist_ok=True)

    async def _icon(url: str) -> str:
        return await get_image_b64_with_cache(url, icon_dir) if url else ""

    keys: list[str] = []
    if context.get("icon"):
        keys.append(context["icon"])
    for member in context.get("members", []):
        if member.get("avatar_url"):
            keys.append(member["avatar_url"])
    if context.get("avatar_url"):
        keys.append(context["avatar_url"])
    values = await gather_b64(_icon, keys)
    for key, value in zip(keys, values, strict=True):
        if context.get("icon") == key:
            context["icon_b64"] = value
        for member in context.get("members", []):
            if member.get("avatar_url") == key:
                member["avatar"] = value
        if context.get("avatar_url") == key:
            context["avatar"] = value


async def _build_combo_context(
    resource: GarrisonResource, combo: dict
) -> dict:
    """组合筛选卡 context（条件 chips + AND 命中的社区攻略）。"""
    guides = await fetch_guides(
        resource,
        season_bond_id=combo["season_bond"].bond_id
        if combo["season_bond"]
        else "",
        regular_bond_ids=[b.bond_id for b in combo["regular_bonds"]] or None,
        strategy_id=combo["strategy"].strategy_id if combo["strategy"] else "",
        limit=_GUIDE_LIMIT + 1,
    )
    conditions = [
        {"label": f"{c['label']}·{c['value']}"}
        for c in combo["labels"]
    ]
    return {
        "kind": "combo",
        "version": ArknightsUID_version,
        "season": f"{resource.season_name} {resource.phase_name}".strip(),
        "conditions": conditions,
        "condition_text": " + ".join(c["label"] for c in conditions),
        "guides": [_guide_row(g, resource) for g in guides[:_GUIDE_LIMIT]],
        "has_guides": bool(guides),
        "source_note": "数据源 森空岛卫戍攻略",
        "daily_note": "",
    }


def _format_combo(context: dict) -> str:
    """组合筛选卡文本回退。"""
    lines = [f"【卫戍攻略·组合筛选】{context['condition_text']}"]
    guides = context.get("guides") or []
    if guides:
        for g in guides:
            tags = "/".join(g["tags"][:3])
            suffix = f"（{tags}）" if tags else ""
            lines.append(
                f"  · {g['title']} — {g['poster']} 赞{g['liked']}{suffix}"
            )
            if g.get("rec_chars"):
                lines.append(f"    推荐阵容：{' · '.join(g['rec_chars'])}")
            if g.get("rec_items"):
                lines.append(f"    推荐装备：{' · '.join(g['rec_items'])}")
            if g.get("idea"):
                lines.append(f"    思路：{g['idea']}")
    else:
        lines.append("暂无同时满足全部条件的攻略，试试放宽条件（去掉一两个词条）。")
    lines.append("攻略详情见森空岛卫戍攻略页 · 查询 ark卫戍攻略 <关键词>")
    return "\n".join(lines)


async def _build_query_context(
    resource: GarrisonResource, result: dict
) -> dict:
    """三种查询形态的统一模板 context。"""
    context: dict = {
        "version": ArknightsUID_version,
        "season": f"{resource.season_name} {resource.phase_name}".strip(),
        "source_note": "数据源 森空岛卫戍攻略",
        "daily_note": "",
        "guides": [],
    }
    kind = result["kind"]

    if kind == "bond":
        bond: GarrisonBond = result["bond"]
        context.update(
            {
                "kind": "bond",
                "name": bond.name,
                "type_label": bond.type_label,
                "icon": bond.icon,
                "active_count": bond.active_count,
                "effect": bond.effect,
                "members": [
                    {"name": m.name or m.char_id, "avatar_url": m.avatar}
                    for m in bond.chars[:_MEMBER_LIMIT]
                ],
                "member_total": len(bond.chars),
            }
        )
        guides = await fetch_guides(
            resource,
            season_bond_id=bond.bond_id
            if bond.bond_type == 1
            else "",
            regular_bond_ids=[bond.bond_id] if bond.bond_type == 2 else None,
            limit=_GUIDE_LIMIT,
        )
        context["guides"] = [_guide_row(g, resource) for g in guides]

    elif kind == "strategy":
        strategy: GarrisonStrategy = result["strategy"]
        context.update(
            {
                "kind": "strategy",
                "name": strategy.name,
                "initiator": strategy.initiator,
                "icon": strategy.icon,
                "hp": strategy.hp,
                "effect": strategy.effect,
            }
        )
        guides = await fetch_guides(
            resource, strategy_id=strategy.strategy_id, limit=_GUIDE_LIMIT
        )
        context["guides"] = [_guide_row(g, resource) for g in guides]

    elif kind == "char":
        char: GarrisonChar = result["char"]
        from ArknightsUID.utils.player.parse import PROFESSION_NAMES

        context.update(
            {
                "kind": "char",
                "name": char.name,
                "avatar_url": char.avatar,
                "profession": PROFESSION_NAMES.get(char.profession, char.profession),
                "sub_profession": char.sub_profession,
                "traits": [
                    {
                        "type": t.trait_type,
                        "desc": t.desc,
                        "level": t.level,
                        "merge": t.merge_level,
                    }
                    for t in char.traits
                ],
                "skills": [{"name": s.name} for s in char.skills],
                "modules": [{"name": m.name} for m in char.modules],
                "bonds": [
                    {
                        "name": b.name,
                        "type_label": b.type_label,
                        "effect": b.effect,
                    }
                    for b in char.bonds
                ],
                "related_strategies": [
                    {"name": s.name, "initiator": s.initiator}
                    for s in result.get("related_strategies") or []
                ],
            }
        )
        # 干员维度无服务端过滤：标题关键词命中后本地再过滤一次
        guides = await fetch_guides(
            resource, title_keyword=char.name, limit=10
        )
        context["guides"] = [
            _guide_row(g, resource)
            for g in guides
            if char.name in g.title
        ][:_GUIDE_LIMIT]

    elif kind == "item":
        item_info: dict = result["item"]
        context.update(
            {
                "kind": "item",
                "name": str(item_info.get("name") or ""),
                "icon": str(item_info.get("icon") or ""),
                "effect": str(item_info.get("effect") or ""),
            }
        )

    await _fill_images(context)
    return context


def _format_query(context: dict) -> str:
    """查询卡文本回退。"""
    lines: list[str] = []
    kind = context.get("kind")
    if kind == "bond":
        lines.append(
            f"【卫戍盟约】{context['name']}（{context['type_label']}，"
            f"激活人数 {context['active_count']}）"
        )
        lines.append(context["effect"])
        members = "、".join(m["name"] for m in context["members"])
        lines.append(f"成员干员：{members}")
    elif kind == "strategy":
        hp = f"，初始生命 {context['hp']}" if context.get("hp") else ""
        lines.append(
            f"【卫戍策略】{context['name']}（发起：{context['initiator']}{hp}）"
        )
        lines.append(context["effect"])
    elif kind == "item":
        lines.append(f"【卫戍道具】{context['name']}")
        lines.append(context["effect"])
    elif kind == "char":
        lines.append(
            f"【卫戍干员】{context['name']}（{context['profession']}）"
        )
        for bond in context["bonds"]:
            lines.append(f"◆ {bond['name']}（{bond['type_label']}）")
            lines.append(f"  {bond['effect'].splitlines()[0] if bond['effect'] else ''}")
        for trait in context["traits"]:
            tag = "精锐" if trait["merge"] else "初始"
            lines.append(f"特质[{tag}]：{trait['desc']}")
        rel = context.get("related_strategies") or []
        if rel:
            names = "、".join(s["name"] for s in rel)
            lines.append(
                f"关联初始策略：{names}（可查：ark卫戍攻略 策略 {context['name']}）"
            )
    guides = context.get("guides") or []
    if guides:
        lines.append("◆ 社区攻略")
        for g in guides:
            tags = "/".join(g["tags"][:3])
            suffix = f"（{tags}）" if tags else ""
            lines.append(f"  · {g['title']} — {g['poster']} 赞{g['liked']}{suffix}")
            if g.get("rec_chars"):
                lines.append(f"    推荐阵容：{' · '.join(g['rec_chars'])}")
            if g.get("rec_items"):
                lines.append(f"    推荐装备：{' · '.join(g['rec_items'])}")
            if g.get("idea"):
                lines.append(f"    思路：{g['idea']}")
    lines.append("攻略详情见森空岛卫戍攻略页 · 查询 ark卫戍攻略 <关键词>")
    return "\n".join(line for line in lines if line.strip())


async def _resolve_operator_keyword(ev: Event, keyword: str) -> str:
    """将用户自定义干员别名 / 内置外号 / 已缓存干员索引代号归一为标准中文名。"""
    from ArknightsUID.utils.gamedata.charinfo import (
        CHARINDEX_PATH,
        CURATED_ALIASES,
        match_entries,
    )
    from ArknightsUID.utils.disk_cache import read_json
    from ArknightsUID.utils.user_alias import apply_user_alias

    mapped = await apply_user_alias(
        getattr(ev, "bot_id", ""), getattr(ev, "user_id", ""), keyword
    )
    if mapped != keyword:
        return mapped
    curated = CURATED_ALIASES.get(keyword) or CURATED_ALIASES.get(keyword.lower())
    if curated:
        return curated
    if CHARINDEX_PATH.exists():
        cached = await read_json(CHARINDEX_PATH)
        if isinstance(cached, dict) and cached:
            hits = match_entries(keyword, cached)
            if len(hits) == 1 and hits[0].get("name"):
                return str(hits[0]["name"])
    return keyword


@stronghold_guide_sv.on_command(("卫戍攻略", "卫戎攻略", "自走棋攻略"), block=True)
async def send_stronghold_guide(bot: Bot, ev: Event) -> None:
    """ark卫戍攻略 [关键词]：总览卡 / 结构化查询卡 / 组合筛选卡。

    无参 → PRTS 模式总览；单关键词 → 盟约/策略/干员实体卡；
    多关键词（+、、｜或多个类型前缀分隔）→ 组合筛选卡，条件间 AND
    （服务端 seasonBondId/regularBondIds/strategyId 同传实测 AND 语义）：
    「炎+不屈+阿米娅」「核心盟约 炎 附加盟约 不屈」。
    """
    await _send_stronghold_guide(bot, ev, ev.text.strip())


async def _send_stronghold_guide(bot: Bot, ev: Event, query: str) -> None:
    query = query.strip()
    if query == "攻略":
        query = ""
    elif query.startswith("攻略") and (query[2:3].isspace() or query[2:3] in ":："):
        query = query[2:].lstrip(" \t\r\n\u3000:：")
    if not query:
        guide = await fetch_stronghold_guide()
        if guide is None:
            return await _send_text(
                bot, ev, "卫戍协议攻略页获取失败，请稍后再试（PRTS Wiki）。"
            )
        context = _build_guide_context(guide)
        text = _format_guide(guide)
        return await send_card_or_text(
            bot, ev, "ark_stronghold_guide.html", context, text
        )

    resource = await fetch_resource()
    if resource is None:
        return await _send_text(
            bot, ev, "卫戍攻略数据获取失败，请稍后再试（森空岛）。"
        )

    # 组合路径：多词条（分隔符或多前缀）
    tokens = parse_combo_tokens(query)
    if tokens and len(tokens) > 1:
        combo = resolve_combo(resource, tokens)
        if not combo.get("ok"):
            return await _send_text(
                bot, ev, f"组合条件有误：{combo.get('error')}\n{_QUERY_HINT}"
            )
        context = await _build_combo_context(resource, combo)
        text = _format_combo(context)
        return await send_card_or_text(
            bot, ev, "ark_stronghold_query.html", context, text
        )

    prefix, keyword = strip_query_prefix(query)
    result = resolve_query(resource, keyword, prefix)
    if result["kind"] == "none" and prefix in ("", "干员"):
        aliased = await _resolve_operator_keyword(ev, keyword)
        if aliased != keyword:
            result = resolve_query(resource, aliased, prefix or "干员")
    kind = result["kind"]
    if kind == "ambiguous":
        candidates = "、".join(result["candidates"][:8])
        return await _send_text(
            bot, ev, f"「{keyword}」有多个候选：{candidates}\n请用更精确的名称"
            "或加「核心盟约/附加盟约/策略/干员/道具」前缀限定。"
        )
    if kind == "none":
        return await _send_text(bot, ev, f"未找到「{keyword}」。\n{_QUERY_HINT}")

    context = await _build_query_context(resource, result)
    text = _format_query(context)
    await send_card_or_text(
        bot, ev, "ark_stronghold_query.html", context, text
    )
