from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

from ArknightsUID.utils.time_utils import BEIJING_TZ, beijing_datetime
from ArknightsUID.version import ArknightsUID_version

from ArknightsUID.utils.prts.client import (
    EnemyInfo,
    OperatorInfo,
    StageInfo,
    WikiPageSummary,
    level_resistances,
    mastery_icon_b64,
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

from ArknightsUID.utils.gamedata.itemicon import (
    enemy_icon_b64,
    item_icon_b64,
    split_material_text,
)
from ArknightsUID.utils.player.context import char_avatar_b64
from ArknightsUID.utils.gamedata.recruit import (
    build_tag_universe,
    load_recruit_data,
    match_recruit,
    normalize_recruit_tags,
    recommend_combos,
)
from ArknightsUID.utils.recruit_ocr import extract_recruit_tags
from ArknightsUID.utils.skillfx import skill_current_final

# 保留包级名称，兼容现有调用方；纯展示逻辑集中在 presentation。
from .presentation import (
    _clip as _clip,
    _append_source as _append_source,
    _format_remaining as _format_remaining,
    _final_level as _final_level,
    _owner_level as _owner_level,
    _format_level_line as _format_level_line,
    _format_page as _format_page,
    _format_search as _format_search,
    _format_enemy as _format_enemy,
    _format_stage as _format_stage,
    _format_operator as _format_operator,
    _format_mastery as _format_mastery,
    _format_op_panel as _format_op_panel,
    _format_relic as _format_relic,
    _ability_blocks as _ability_blocks,
    _enemy_secondary_stats as _enemy_secondary_stats,
    _stage_condition_blocks as _stage_condition_blocks,
)


wiki_sv = SV("ArknightsUID资料", priority=5)

# 文本回复公共实现在 utils.card.send_text；保留模块级别名供调用点/测试 patch
from ArknightsUID.utils.card import send_text as _send_text  # noqa: E402


def _clean_query(text: str) -> str:
    return text.strip().strip("：:").strip()


# 卫戍协议·盟约关卡代号（AC-1 ~ AC-9，大小写不敏感；页面见 send_stage_page）
_AC_STAGE_PATTERN = re.compile(r"(?i)ac-\d{1,2}")


async def _owner_player_data(ev: Event):
    """已绑定账号的 (credential, 玩家数据)；拿不到返回 None。

    干员查询/面板模拟是资料查询，账号只是增强信息：未绑定/接口失败一律
    静默降级，不向用户报账号侧错误。
    """
    try:
        from ArknightsUID.utils.enduid.adapter import resolve_credential
        from ArknightsUID.utils.player import (
            fetch_cached_player_info,
            parse_player_info,
        )

        credential = await resolve_credential(ev.user_id, ev.bot_id)
        if isinstance(credential, str):
            return None
        response = await fetch_cached_player_info(credential)
        if not isinstance(response, dict):
            return None
        data = parse_player_info(response)
        return None if data is None else (credential, data)
    except Exception:
        logger.debug("[ArknightsUID] 玩家数据获取失败，走通用展示", exc_info=True)
        return None


async def _build_owner_context(op: OperatorInfo, ev: Event) -> dict | None:
    """已绑定账号对该干员的练度状态（技能等级 + 在训剩余）；拿不到返回 None。

    干员查询是资料查询，账号只是增强信息：未绑定/未持有/接口失败一律静默
    降级为通用 PRTS 展示，不向用户报账号侧错误。
    """
    try:
        from ArknightsUID.utils.enduid.adapter import hide_uid_for
        from ArknightsUID.utils.gamedata.charinfo import (
            load_char_index,
            load_skill_names,
            resolve_char,
        )
        from ArknightsUID.utils.player import (
            parse_building,
            training_remaining_secs,
            training_status_text,
        )

        loaded = await _owner_player_data(ev)
        if loaded is None:
            return None
        credential, data = loaded
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
            training_remain = training_status_text(training_remaining_secs(building))
        return {
            "uid": await hide_uid_for(credential.uid, ev.user_id, ev.bot_id),
            "main_skill_lvl": entry.main_skill_lvl,
            "skills": skills,
            "training_remain": training_remain,
        }
    except Exception:
        logger.debug("[ArknightsUID] 玩家练度增强获取失败，走通用展示", exc_info=True)
        return None


async def _render_or_text(
    bot: Bot, ev: Event, template_name: str, context: dict, text: str
) -> None:
    """出图/回退文本的公共实现在 utils.card.send_card_or_text（含 wiki 在内
    全部业务模块共用）；此处保留同名薄壳，旧调用点与测试 patch 兼容。"""
    from ArknightsUID.utils.card import send_card_or_text

    await send_card_or_text(bot, ev, template_name, context, text)


async def _build_page_context(page: WikiPageSummary) -> dict:
    return {
        "title": page.title,
        "summary": _clip(page.summary),
        "fields": [{"key": k, "value": v} for k, v in page.fields.items() if v],
        "source_url": page.source_url,
        "version": ArknightsUID_version,
    }


async def _skill_icon_b64(skill_name: str, char_name: str = "") -> str:
    """技能名 → 干员归属 skillId / iconId（skill_table）→ hycdn 图标 base64。"""
    if not skill_name:
        return ""
    try:
        from ArknightsUID.utils.gamedata._table_cache import (
            ensure_char_table,
            ensure_skill_table,
        )
        from ArknightsUID.utils.gamedata.charinfo import (
            load_char_index,
            load_skill_names,
            resolve_char,
        )
        from ArknightsUID.utils.path import SKILL_CACHE_PATH
        from ArknightsUID.utils.player.context import skill_icon_url
        from ArknightsUID.utils.render import get_image_b64_with_cache

        names = await load_skill_names()
        skill_id = ""
        if char_name:
            index = await load_char_index()
            hit = resolve_char(char_name, index) if index else None
            if hit:
                char_table = await ensure_char_table()
                char_entry = (
                    (char_table or {}).get(hit[0])
                    if isinstance(char_table, dict)
                    else None
                )
                for sk in (
                    (char_entry or {}).get("skills") or []
                    if isinstance(char_entry, dict)
                    else []
                ):
                    if isinstance(sk, dict):
                        sid = str(sk.get("skillId") or "")
                        if sid and names.get(sid) == skill_name:
                            skill_id = sid
                            break
        if not skill_id:
            skill_id = next((sid for sid, n in names.items() if n == skill_name), "")
        if not skill_id:
            return ""
        icon_id = skill_id
        skill_table = await ensure_skill_table()
        if isinstance(skill_table, dict):
            skill_entry = skill_table.get(skill_id)
            if isinstance(skill_entry, dict) and skill_entry.get("iconId"):
                icon_id = str(skill_entry["iconId"])
        return await get_image_b64_with_cache(
            skill_icon_url(icon_id), SKILL_CACHE_PATH
        )
    except Exception as exc:
        logger.debug(
            f"[ArknightsUID·wiki] 技能图标获取失败 {skill_name}: "
            f"{exc.__class__.__name__}"
        )
        return ""


async def _operator_portrait_b64(name: str) -> str:
    """干员名 → hycdn 立绘 base64（gamedata 索引反查 charId，精英档位回退）。

    立绘路径 char_skin/portrait/{charId}#N.png（N=3 E2 / 2 E1 / 1 初始），
    逐档回退逻辑在共享助手 char_portrait_b64；索引缺失/下载失败返回空串降级。
    """
    try:
        from ArknightsUID.utils.gamedata.charinfo import (
            load_char_index,
            resolve_char,
        )
        from ArknightsUID.utils.player.context import char_portrait_b64

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
    skills.levels 全量保留（1-7 + 专1-3 正好 10 级，不裁剪）；current/final
    详情走共享层 utils.skillfx（描述 + 初动/消耗/持续 meta，与练度卡同口径）。
    """
    rarity = str(int(op.rarity) + 1) if op.rarity.isdigit() else op.rarity
    owner_skills = owner.get("skills") if owner else None
    main_lvl = (owner or {}).get("main_skill_lvl")
    op_name = op.name or op.title
    skills_ctx: list[dict] = []
    for s in op.skills:
        current, final = skill_current_final(
            s,
            owner_skills.get(s.name) if owner_skills else None,
            main_lvl,
        )
        skills_ctx.append(
            {
                "name": s.name,
                "skill_type": s.skill_type,
                "range": s.range_,
                "icon": await _skill_icon_b64(s.name, op_name),
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
                "current": current,
                "final": final,
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
                    {"condition": st.condition, "effect": st.effect} for st in t.stages
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
                # 具体效果摘要（特性追加 + 首条天赋更新；详情走 ark模组）
                "trait": _clip(m.trait.replace("\n", " "), 160),
                "talent_brief": _clip(
                    (next(iter(m.talent_updates.values()), "") or "").replace(
                        "\n", " "
                    ),
                    160,
                ),
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


async def build_enemy_context(enemy: EnemyInfo) -> dict:
    """敌方情报卡渲染上下文（rogue 详情同用——跨模块公开缝）。

    原私有名 _build_enemy_context 保留为别名，供既有调用点/测试不动。
    """
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


_build_enemy_context = build_enemy_context


async def _gather_b64(fetch, keys: list[str]) -> list[str]:
    """并发受限批量取图 b64（共享实现在 utils.image.gather_b64）。

    串行逐个 await 在冷缓存下是命令级卡顿（公招推荐最重 6×24=144 次，
    见审计 #39）；单飞/负缓存在各 fetch 内部，这里只管并发上限与
    失败降级（空串）。结果顺序与 keys 对齐。
    """
    from ArknightsUID.utils.image import gather_b64

    return await gather_b64(fetch, keys, limit=_ENEMY_FETCH_CONCURRENCY)


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


_MAP_MISS_TTL = 7 * 86400  # 无图关卡负缓存：地图文件名固定，7 天复核一次


async def _get_stage_map_b64(code: str, name: str, stage_id: str = "") -> str:
    """关卡地图 → base64（磁盘缓存）。来源优先级：

    1. PRTS 上传文件 md5 直链（map_file_url，零 API；主线/教学关有，
       活动关从无此命名文件），标准 + 辅助两种命名
    2. imageinfo 兜底（覆盖极少数改名文件）
    3. torappu level_id 预览（stage_map_preview_url；活动关地图的实际
       来源，当期活动开服首日即可拉，主线亦全覆盖）

    仍无则空串——卡片降级无图，map_url 仍给 PRTS.Map 链接。无图关卡
    （ST 剧情关/资源未传）写 7 天负缓存标记，避免每次查询都吃满
    2×404 + 1 次 API + 1 次 torappu。
    """
    if not code or not name:
        return ""
    import hashlib as _hashlib

    from ArknightsUID.utils.prts.client import (
        map_candidate_titles,
        map_file_url,
        stage_map_preview_url,
    )
    from ArknightsUID.utils.render import get_image_b64_with_cache
    from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

    cache = CACHE_PATH / "wiki" / "map"
    miss = cache / ("nomap_" + _hashlib.md5(f"{code}_{name}_{stage_id}".encode()).hexdigest()[:12])
    if miss.exists() and time.time() - miss.stat().st_mtime < _MAP_MISS_TTL:
        return ""

    def _url_miss(url: str) -> Path:
        from ArknightsUID.utils.gamedata.path_safety import safe_url_basename

        # 与 bulk_download 的地图 miss marker 同名（安全 basename）
        return cache / (safe_url_basename(url) + ".miss")

    async def _cached_map_b64(url: str) -> str:
        """瞬时下载失败时只读已有图片，坏缓存留给下一轮下载修复。"""
        import base64
        from io import BytesIO

        from PIL import Image

        from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url

        def _read() -> str:
            for filename in dict.fromkeys(cache_names_for_url(url)):
                if not filename:
                    continue
                local = cache / filename
                for candidate in dict.fromkeys((local.with_suffix(".webp"), local)):
                    try:
                        data = candidate.read_bytes()
                        with Image.open(BytesIO(data)) as image:
                            image.load()
                            image_format = (image.format or "png").lower()
                        return f"data:image/{image_format};base64,{base64.b64encode(data).decode()}"
                    except (OSError, ValueError):
                        continue
            return ""

        return await asyncio.to_thread(_read)

    async def _fetch_map_b64(url: str) -> str:
        """下载地图 → b64；只把**确认的 404** 当"地图不存在"写负缓存。

        审计 #18：此前任何失败（CDN 超时/网络抖动/图片损坏/fallback API
        挂掉）都写 7 天负缓存——一次瞬时故障让本来存在的地图 7 天不出图。
        网络类失败不写标记，下轮查询即自愈。
        """
        from ArknightsUID.utils.http import download_bytes_status

        # quiet_404：404 = 关卡确无地图文件（写负缓存的既定语义）
        status, content = await download_bytes_status(
            url, timeout=25.0, quiet_404=True
        )
        if status == 404:
            try:
                cache.mkdir(parents=True, exist_ok=True)
                _url_miss(url).touch()
            except OSError:
                pass
            return ""
        if status != 200 or not content:
            return await _cached_map_b64(url)  # 网络失败/5xx：复用旧图，不写负缓存
        # 200：落盘复用 bulk 的校验规则（非图片内容拒绝，防污染），
        # 随后走缓存读取路径出 b64（含烘焙/格式分支，零重复下载）
        from ArknightsUID.utils.gamedata.path_safety import cache_names_for_url
        from ArknightsUID.utils.resource.bulk_download import _save_image_sync

        url_last = cache_names_for_url(url)[0]
        if not url_last:
            return ""
        try:
            import asyncio as _asyncio

            cache.mkdir(parents=True, exist_ok=True)
            await _asyncio.to_thread(_save_image_sync, cache, url_last, content)
        except (OSError, ValueError):
            return await _cached_map_b64(url)  # 坏内容/写失败：保留可读旧图
        return await get_image_b64_with_cache(url, cache)

    for title in map_candidate_titles(code, name):
        url = map_file_url(title)
        marker = _url_miss(url)
        if marker.exists() and time.time() - marker.stat().st_mtime < _MAP_MISS_TTL:
            continue
        b64 = await _fetch_map_b64(url)
        if b64:
            return b64
    fallback_url = await resolve_map_file_url(code, name)
    if fallback_url:
        b64 = await _fetch_map_b64(fallback_url)
        if b64:
            return b64
    # torappu level_id 预览：活动关地图的主来源（wiki 从不上传活动地图文件）
    preview_url = stage_map_preview_url(stage_id)
    if preview_url:
        marker = _url_miss(preview_url)
        if not (
            marker.exists() and time.time() - marker.stat().st_mtime < _MAP_MISS_TTL
        ):
            b64 = await _fetch_map_b64(preview_url)
            if b64:
                return b64

    # 关卡级 nomap 标记：仅在所有来源明确无该文件（候选 URL 均 404）
    # 时才写——fallback 解析失败（网络）不写，下轮重试。
    # 判定输入必须校验标记新鲜度（审计 #39）：只查 exists() 会把
    # "已过期待重查 + 本轮重查遇网络失败（非 404，标记未刷新）"的
    # 候选也算"确认缺失"，一次网络抖动放大成关卡级 7 天无图。
    def _confirmed_missing(url: str) -> bool:
        marker = _url_miss(url)
        try:
            return marker.exists() and (
                time.time() - marker.stat().st_mtime < _MAP_MISS_TTL
            )
        except OSError:
            return False

    candidate_urls = [
        url
        for url in (map_file_url(title) for title in map_candidate_titles(code, name))
        if url
    ]
    if preview_url:
        candidate_urls.append(preview_url)
    if candidate_urls and all(_confirmed_missing(url) for url in candidate_urls):
        try:
            cache.mkdir(parents=True, exist_ok=True)
            miss.touch()
        except OSError:
            pass
    return ""


async def _build_stage_context(stage: StageInfo) -> dict:
    title = stage.code or stage.title
    # 地图：md5 直链优先（固定资源，命中磁盘缓存后零网络零 API）
    try:
        map_b64 = await _get_stage_map_b64(stage.code, stage.name, stage.stage_id)
    except Exception:
        logger.debug("[ArknightsUID] 关卡地图下载失败，卡片降级无图", exc_info=True)
        map_b64 = ""

    async def _drop_rows(names: list[str], limit: int = 8) -> list[dict]:
        """掉落物名 → {name, icon}（PRTS 带框道具图标；并发受限，
        失败空串降级）。"""
        picked = names[:limit]
        icons = await _gather_b64(item_icon_b64, picked)
        return [
            {"name": name, "icon": icon}
            for name, icon in zip(picked, icons, strict=True)
        ]

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


from ArknightsUID.utils.gamedata.panel_args import parse_panel_query as _parse_panel_query


from ArknightsUID.utils.player.parse import PROFESSION_NAMES as _PROFESSION_NAMES


async def _owner_panel_config(ev: Event, char_id: str) -> dict | None:
    """已绑定账号持有该干员时 → 当前面板配置（等级/潜能/好感/技能/模组）。"""
    loaded = await _owner_player_data(ev)
    if loaded is None:
        return None
    _, data = loaded
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


async def _build_op_panel_context(query: str, ev: Event) -> dict | str:
    """ark面板：任意 精英/等级/潜能/好感/模组 配置下的干员面板模拟。

    数值来自游戏解包表（character_table 插值 + 潜能/好感/模组累加），
    技能逐级文本来自 PRTS（skill_table 描述是未填充模板变量，不可用）。
    默认配置 = 用户自己的干员（未绑定/未持有 → 精2满级 潜0 技能7级 无模组）。
    """
    from ArknightsUID.utils.gamedata import oppanel
    from ArknightsUID.utils.gamedata.charinfo import (
        AMIYA_FORM_IDS,
        load_char_index,
        match_entries,
        resolve_char,
    )

    config = _parse_panel_query(query)
    name = config["name"]
    if not name:
        return "用法：ark面板 干员名 [精2] [Lv45/满级] [潜6/满潜] [技能2 专3] [X模/Y模/无模] [模3] [满好]"
    if config["mastery"] is not None and not 1 <= config["mastery"] <= 3:
        return "专精等级仅支持 专1 ~ 专3。"
    try:
        bot_id, user_id = ev.bot_id, ev.user_id
    except Exception:
        bot_id = user_id = ""
    if bot_id and user_id:
        try:
            from ArknightsUID.utils.user_alias import apply_user_alias

            name = await apply_user_alias(bot_id, user_id, name)
        except Exception:
            pass
    amiya_id = AMIYA_FORM_IDS.get(name)
    if amiya_id:
        char_id = amiya_id
    else:
        name = await _resolve_alias(name, bot_id, user_id)
        index = await load_char_index()
        candidates = match_entries(name, index) if index else []
        if len(candidates) > 1:
            shown = "、".join(str(c.get("name") or "") for c in candidates[:6])
            return f"「{name}」匹配到多位干员：{shown}，请写全名。"
        hit = resolve_char(name, index) if index else None
        if not hit:
            return f"未找到干员：{name}"
        char_id = hit[0]
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
    level = (
        config["level"]
        if config["level"] is not None
        else ((owner or {}).get("level") or max_level)
    )
    if level == -1:
        level = max_level
    level = max(1, min(level, max_level))
    max_potential = int(char_entry.get("maxPotentialLevel") or 0)
    if config["potential"] is not None:
        # 「潜N」是 UI 口径（1=本体无加成 … 6=满潜），而 potentialRanks/
        # 玩家数据 potentialRank 是 0 基档位：生效档 = N-1（此前直接把 N
        # 当档位用，潜1-潜5 全部多吃一档潜能加成）
        potential = (
            max_potential if config["potential"] == -1 else config["potential"] - 1
        )
    else:
        potential = (owner or {}).get("potential", 0)
    potential = max(0, min(potential, max_potential))
    favor = (
        config["favor"]
        if config["favor"] is not None
        else ((owner or {}).get("favor", 0))
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
        if config["skill_index"] is not None and not 1 <= config["skill_index"] <= len(
            op.skills
        ):
            return (
                f"{char_entry.get('name') or name} 没有第 {config['skill_index']} 技能"
                f"（共 {len(op.skills)} 个技能）。"
            )
        skill_index = config["skill_index"] or (owner or {}).get("skill_index", 1)
        skill_index = max(1, min(skill_index, len(op.skills)))
        skill = op.skills[skill_index - 1]
        mastery = config["mastery"]
        if mastery is None and not config["skill_level"]:
            mastery = (owner or {}).get("specialize", 0) or None
        main_lvl = config["skill_level"] or (owner or {}).get("main_skill_lvl") or 7
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
        extra = sum(seg.get(k, 0.0) for k in ("potential", "favor", "module"))
        if extra:
            source_bits = []
            if seg.get("potential"):
                source_bits.append(f"潜能+{oppanel._fmt(seg['potential'])}")
            if seg.get("favor"):
                source_bits.append(f"好感+{oppanel._fmt(seg['favor'])}")
            if seg.get("module"):
                source_bits.append(f"模组+{oppanel._fmt(seg['module'])}")
            breakdown.append(
                f"{label}：基础 {oppanel._fmt(seg['base'])} → " + "、".join(source_bits)
            )

    profession = _PROFESSION_NAMES.get(str(char_entry.get("profession") or ""), "")
    # character_table 的稀有度是 TIER_x（=x 星）
    rarity_text = str(char_entry.get("rarity") or "")
    try:
        rarity = int(rarity_text.rsplit("_", 1)[-1])
    except ValueError:
        rarity = 0
    from ArknightsUID.utils.player.context import char_avatar_b64

    config_labels = [
        f"精{phase} Lv{level}",
        f"潜能{potential}",
        f"好感{favor}%",
    ]
    if module:
        config_labels.append(f"{module['type']}模组·{module['name']} Lv{module_level}")
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
        "module_choices": [{"type": c["type"], "name": c["name"]} for c in choices],
        "source_url": op.source_url
        if op
        else page_url_title(char_entry.get("name") or name),
        "source_note": "数值 ArknightsGameData · 技能文本 PRTS Wiki",
        "version": ArknightsUID_version,
    }


def page_url_title(title: str) -> str:
    from ArknightsUID.utils.prts.client import page_url

    return page_url(title)


@wiki_sv.on_command(("面板", "干员面板", "oppanel", "mb"), block=True)
async def send_op_panel(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(
            bot,
            ev,
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


async def _resolve_alias(query: str, bot_id: str = "", user_id: str = "") -> str:
    """用户自定义别名 → 内建别名/英文代号/唯一前缀 → 标准干员名。

    统一管线见 charinfo.resolve_query（amiya_forms=False：词条搜索需要
    名称级结果，不走形态 charId 捷径）；索引不可用或匹配不唯一时原样
    返回，交由 PRTS 搜索兜底。
    """
    try:
        from ArknightsUID.utils.gamedata.charinfo import resolve_query

        res = await resolve_query(
            query, bot_id=bot_id, user_id=user_id, amiya_forms=False
        )
        if res.unique and res.name:
            return res.name
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


# ---------------------------------------------------------------------------
# ark专精：技能专精效果 + 材料合并卡
# 命令形态：正序 ark专精 塔露拉 二技能 专三 / 倒序 ark塔露拉 专精
# （倒序走全串锚定正则，只匹配以「专精」结尾的查询，不吃消息中段的专精）
# ---------------------------------------------------------------------------

# 优先级与全部命令服务一致（5）：core 排序"优先级先于关键词长度"，
# 高于此会抢走以专精开头的自定义别名（ark专精表 → 裸"专精"命令）
mastery_sv = SV("ArknightsUID专精", priority=5)

# 倒序（ark塔露拉 专精）是"兜底"形态：正则 .*?专精$ 恒长于任何命令词，
# 同优先级下排序必胜——必须放到比全部命令服务（5）更低的优先级，
# 否则会抢走参数恰好以专精结尾的既有命令（ark帮助搜索 专精、
# ark百科 技能专精、ark指令别名 zj=专精）
mastery_suffix_sv = SV("ArknightsUID专精倒序", priority=6)

_MASTERY_USAGE = (
    "用法：ark专精 干员名 [技能2|二技能] [专1|专精2|专三]\n"
    "示例：ark专精 塔露拉｜ark塔露拉 专精｜ark专精 塔露拉 二技能 专三\n"
    "（默认展示全部技能的专三效果与三档材料；材料清单用 ark专精材料 干员名）"
)


def _parse_mastery_query(text: str) -> dict:
    """专精查询参数：只取 name / skill_index / mastery（复用面板解析正则，
    面板其余参数不参与，避免等级/潜能等 token 混入）。"""
    config = _parse_panel_query(text)
    return {
        "name": config["name"],
        "skill_index": config["skill_index"],
        "mastery": config["mastery"],
    }


async def _build_mastery_context(query: str, ev: Event) -> dict | str:
    """专精卡共享上下文：技能图标 + 专精效果（skillfx 同口径）+ 材料图标。

    展示口径：不写技能 = 全部有专精数据的技能；不写专精档 = 默认专三效果
    + 三档材料；指定档位只展示该档效果与材料。
    """
    from ArknightsUID.utils.skillfx import skill_level_context

    parsed = _parse_mastery_query(query)
    if not parsed["name"]:
        return _MASTERY_USAGE
    name = await _resolve_alias(parsed["name"], ev.bot_id, ev.user_id)
    op = await query_operator(name)
    if not op or not op.skills or not any(s.mastery_costs for s in op.skills):
        return f"未找到干员专精数据：{name}"
    mastery_skills = [s for s in op.skills if s.mastery_costs]
    if parsed["skill_index"] is not None:
        # 序号按原始技能列表（与 ark面板 同口径）：PRTS 个别页面缺某技能
        # 专精数据时，过滤后列表的序号会整体前移，"二技能"会被解析到
        # 三技能；这里按原列表取，选中后再校验专精数据
        wanted = parsed["skill_index"]
        if not 1 <= wanted <= len(op.skills):
            return (
                f"{name} 没有第 {wanted} 技能"
                f"（共 {len(op.skills)} 个技能："
                f"{'、'.join(s.name for s in op.skills)}）"
            )
        chosen = op.skills[wanted - 1]
        if not chosen.mastery_costs:
            return f"{name} 的第 {wanted} 技能（{chosen.name}）暂无专精数据"
        mastery_skills = [chosen]
    mastery_wanted = parsed["mastery"]
    if mastery_wanted is not None and not 1 <= mastery_wanted <= 3:
        return "专精等级仅支持 专1 ~ 专3。"

    rarity = str(int(op.rarity) + 1) if op.rarity.isdigit() else op.rarity
    # 材料/技能图标先收集再并发受限拉取（3 技能 × 3 专精 × 若干材料串行
    # 下载冷缓存会卡数十秒）
    item_names: list[str] = []
    for s in mastery_skills:
        for mc in s.mastery_costs:
            for text in mc.materials:
                item_name, _count = split_material_text(text)
                if item_name:
                    item_names.append(item_name)
    item_icons = dict(zip(item_names, await _gather_b64(item_icon_b64, item_names)))
    op_name = op.name or op.title
    skill_icons = dict(
        zip(
            [s.name for s in mastery_skills],
            await _gather_b64(
                lambda n: _skill_icon_b64(n, op_name),
                [s.name for s in mastery_skills],
            ),
        )
    )
    # 专精档位官方图标（专精_N.png）：涉及的档位并发受限拉取，失败空串
    tier_values = sorted(
        {
            int(label[1:])
            for s in mastery_skills
            for label in (
                [f"专{mastery_wanted or 3}"] + [mc.mastery for mc in s.mastery_costs]
            )
            if label.startswith("专") and label[1:].isdigit()
        }
    )
    tier_icons = dict(
        zip(tier_values, await _gather_b64(mastery_icon_b64, tier_values))
    )
    skills_ctx = []
    # 技能序号按原始技能列表：过滤/筛选后的列表位置会前移，模板标签
    # （SKILL N）用真实序号，"二技能"卡片不能标成 SKILL 1
    skill_no = {id(s): i + 1 for i, s in enumerate(op.skills)}
    for s in mastery_skills:
        tier = mastery_wanted or 3  # 缺省展示专三效果
        costs = [
            mc for mc in s.mastery_costs
            if mastery_wanted is None or mc.mastery == f"专{mastery_wanted}"
        ] or s.mastery_costs
        costs_ctx = []
        for mc in costs:
            mats = []
            for text in mc.materials:
                mat_name, count = split_material_text(text)
                mats.append(
                    {"name": mat_name, "count": count, "icon": item_icons.get(mat_name, "")}
                )
            mc_tier = int(mc.mastery[1:]) if mc.mastery[1:].isdigit() else 0
            costs_ctx.append(
                {
                    "mastery": mc.mastery,
                    "tier_icon": tier_icons.get(mc_tier, ""),
                    "materials": mats,
                    "active": mc.mastery == f"专{tier}",
                }
            )
        effect = skill_level_context(select_owner_level(s.levels, tier, 7))
        skills_ctx.append(
            {
                "name": s.name,
                "index": skill_no[id(s)],
                "icon": skill_icons.get(s.name, ""),
                "effect_label": f"专{tier}",
                "effect_icon": tier_icons.get(tier, ""),
                "effect": effect,
                "mastery_costs": costs_ctx,
            }
        )
    return {
        "name": op.name or op.title,
        "rarity": rarity,
        "profession": op.profession,
        "skills": skills_ctx,
        "portrait": await _operator_portrait_b64(op.name or op.title),
        "source_url": op.source_url,
        "version": ArknightsUID_version,
    }


async def _send_mastery_card(bot: Bot, ev: Event, query: str) -> None:
    """正序/倒序两个入口的共用出卡路径。"""
    context = await _build_mastery_context(query, ev)
    if isinstance(context, str):
        return await _send_text(bot, ev, context)
    await _render_or_text(
        bot, ev, "ark_wiki_mastery.html", context, _format_mastery(context)
    )


# 注册顺序守恒：长命令（专精材料）必须先于短命令（专精）注册，否则
# command startswith 分发下 ark专精材料 会被 ark专精 抢走（触发器顺序
# 不变量测试会拦截）
@mastery_sv.on_command(("专精材料", "技能材料", "mastery"), block=True)
async def send_mastery_materials(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark专精材料 干员名")
    await _send_mastery_card(bot, ev, query)


@mastery_sv.on_command("专精", block=True)
async def send_mastery_query(bot: Bot, ev: Event) -> None:
    # ev.text 是去掉命令词后的 干员名 + 参数
    await _send_mastery_card(bot, ev, _clean_query(ev.text))


@mastery_suffix_sv.on_regex(
    r"^(?P<mastery_query>.*?)\s*专精\s*$", block=True
)
async def send_mastery_suffix_query(bot: Bot, ev: Event) -> None:
    # 倒序形态（ark塔露拉 专精）：正则触发器的 ev.text 已被改写，
    # 只消费命名组 mastery_query，不重新解析 raw_text
    query = str(ev.regex_dict.get("mastery_query") or "").strip()
    # core 只从 raw_text 剥 command_start，正则分支的组捕获来自 ev.text
    # （未剥）："/ark塔露拉 专精" 会查成 "/塔露拉"，这里补剥
    from gsuid_core.config import core_config

    command_starts = [s for s in (core_config.get_config("command_start") or []) if s]
    for start in command_starts:
        if query.startswith(start):
            query = query[len(start) :]
            break
    cleaned = _clean_query(query)
    # 若管理员开启了 allow_empty_prefix，未带显式 ark/方舟 前缀的普通句末
    # 「…专精」也会落进此正则：无显式前缀时仅在确能解析出干员时响应，静默忽略闲聊。
    raw_text = str(getattr(ev, "raw_text", "") or "").strip()
    for start in command_starts:
        if raw_text.startswith(start):
            raw_text = raw_text[len(start) :].lstrip()
            break
    plugin = getattr(mastery_suffix_sv, "plugins", None)
    non_empty_prefixes = [
        p
        for p in (
            list(getattr(plugin, "prefix", []) or [])
            + list(getattr(plugin, "force_prefix", []) or [])
        )
        if p
    ]
    has_explicit_prefix = (
        not raw_text
        or not non_empty_prefixes
        or any(raw_text.startswith(p) for p in non_empty_prefixes)
    )
    if not has_explicit_prefix:
        parsed = _parse_mastery_query(cleaned)
        if not parsed["name"] or len(parsed["name"]) > 16:
            return
        context = await _build_mastery_context(cleaned, ev)
        if isinstance(context, str):
            return
        await _render_or_text(
            bot, ev, "ark_wiki_mastery.html", context, _format_mastery(context)
        )
        return
    await _send_mastery_card(bot, ev, cleaned)


@wiki_sv.on_command(("材料", "素材", "物品", "item"), block=True)
async def send_material_page(bot: Bot, ev: Event) -> None:
    from ArknightsUID.arknightsuid_push import show_material_overview

    await show_material_overview(bot, ev)


@wiki_sv.on_command(("敌人", "敌方", "enemy"), block=True)
async def send_enemy_page(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark敌人 敌人名")
    enemy = await query_enemy(query)
    if not enemy:
        return await _send_text(
            bot,
            ev,
            f"未找到敌人词条：{query}"
            "（若为肉鸽主题敌人，试试 ark肉鸽敌人 " + query + "）",
        )
    context = await build_enemy_context(enemy)
    await _render_or_text(bot, ev, "ark_wiki_enemy.html", context, _format_enemy(enemy))


async def _build_relic_context(name: str, groups: list[dict]) -> dict:
    """藏品卡：同名跨期按效果去重，每组带主题与图标（tomimi 资源）。"""
    rows = []
    for g in groups:
        icon = ""
        if g.get("icon_id"):
            try:
                from ArknightsUID.utils.gamedata.rogue_relic import relic_icon_b64

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


@wiki_sv.on_command(("藏品",), block=True)
async def send_relic_query(bot: Bot, ev: Event) -> None:
    query = _clean_query(ev.text)
    if not query:
        return await _send_text(bot, ev, "用法：ark藏品 名字（如 ark藏品 钝爪-典训）")
    try:
        from ArknightsUID.utils.gamedata.rogue_relic import (
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
        return await _send_text(
            bot, ev, f"找到 {len(matched)} 个候选：{shown}（请用完整名）"
        )

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
    # 卫戍协议关卡页是「卫戍协议·盟约 AC-N」全名（无裸「AC-N」页）：
    # 前置归一化避免直查必 404 → 每次都走搜索回退的多余请求
    if _AC_STAGE_PATTERN.fullmatch(query):
        query = f"卫戍协议·盟约 {query.upper()}"
    stage = await query_stage(query)
    if not stage:
        return await _send_text(bot, ev, f"未找到关卡：{query}")
    context = await _build_stage_context(stage)
    await _render_or_text(
        bot,
        ev,
        "ark_wiki_stage.html",
        context,
        _format_stage(stage, context.get("enemies")),
    )


# 注册顺序约束：公招推荐 必须先于「公招」注册（core 的 command 匹配是
# startswith + 注册顺序优先，短词先注册会抢走「ark公招推荐/公招查询」）。

# 推荐卡每组合头像上限：干员名全量列出，头像只取池内前若干个（本地
# 缓存命中后为纯读盘，首查未缓存时控制下载量与渲染体量）
_RECOMMEND_AVATAR_LIMIT = 24


@wiki_sv.on_command(("公招推荐", "标签推荐", "公招最优", "gztj"), block=True)
async def send_recruit_recommend(bot: Bot, ev: Event) -> None:
    await _send_recruit(bot, ev, mode="recommend")


async def _send_recruit(bot: Bot, ev: Event, *, mode: str) -> None:
    raw = _clean_query(ev.text)
    image = getattr(ev, "image", None)
    sources = [
        s for s in (getattr(ev, "image_list", None) or [image])
        if isinstance(s, str) and s.strip()
    ]
    if not raw and (not sources or mode == "exact"):
        return await _send_text(
            bot, ev, "用法：ark公招 高资 术师（可输入界面标签，或附公招截图）"
        )
    data = await load_recruit_data()
    if not data:
        return await _send_text(
            bot, ev, "公招数据加载失败（首次需下载约 15MB 游戏数据），请稍后重试"
        )
    universe = build_tag_universe(data)
    via_ocr = not raw
    if via_ocr:
        ocr = await extract_recruit_tags(sources, universe)
        if ocr.error:
            return await _send_text(bot, ev, ocr.error)
        if not ocr.tags:
            shown = "、".join(ocr.raw_texts[:6]) or "无"
            return await _send_text(
                bot, ev, f"截图里没识别到公招标签（读到的文字：{shown}）。"
                "请截取标签区域，或文字输入 ark公招 标签…",
            )
        tags = ocr.tags
    else:
        tags = normalize_recruit_tags(raw)
    unknown = [tag for tag in tags if tag not in universe]
    if unknown:
        return await _send_text(
            bot, ev, f"未识别的公招标签：{'、'.join(unknown)}。请核对后重试。"
        )
    if not tags:
        return await _send_text(bot, ev, "请至少输入一个公招标签。")
    if mode == "exact" and len(tags) > 3:
        return await _send_text(bot, ev, "一次最多勾选 3 个标签；全部标签请用 ark公招推荐。")
    if mode == "recommend" or via_ocr or len(tags) > 3:
        return await _render_recruit_recommend(bot, ev, tags, data, via_ocr=via_ocr)
    matched = match_recruit(tags, data)
    if matched:
        return await _render_recruit_matches(bot, ev, tags, matched)
    note = f"原组合「{' + '.join(tags)}」无匹配干员"
    if mode == "auto" and len(tags) > 1:
        return await _render_recruit_recommend(bot, ev, tags, data, note=note + "，以下是可用子集。")
    await _send_text(bot, ev, note)


async def _render_recruit_recommend(
    bot: Bot, ev: Event, tags: list[str], data: list[dict], *,
    via_ocr: bool = False, note: str = "",
) -> None:
    result = recommend_combos(tags, data)
    combos = result["combos"]
    if not combos:
        return await _send_text(
            bot, ev, (note + "\n" if note else "") + "这些标签组不出任何可公招组合。",
        )

    # 头像仅取池内前 24 个控制渲染体量——池可到几十人，逐个头像会让
    # 首次查询的下载量爆炸；并发受限拉取（串行时冷缓存最多 144 次逐个
    # await，分钟级卡顿，审计 #39）
    avatar_ids = [
        str(entry.get("id", ""))
        for combo in combos
        for entry in combo["matches"][:_RECOMMEND_AVATAR_LIMIT]
    ]
    avatar_map = dict(
        zip(avatar_ids, await _gather_b64(char_avatar_b64, avatar_ids), strict=True)
    )
    rows = []
    for combo in combos:
        names = combo["match_names"]
        avatars = [
            avatar_map[str(entry.get("id", ""))]
            for entry in combo["matches"][:_RECOMMEND_AVATAR_LIMIT]
        ]
        rows.append(
            {
                "tags": " + ".join(combo["tags"]),
                "duration": combo["duration"],
                "tier": combo["tier"],
                "tier_name": combo["tier_name"],
                "min_rarity": combo["min_rarity"],
                "max_rarity": combo["max_rarity"],
                "pool_count": len(names),
                "names": names,
                "avatars": avatars,
            }
        )

    context = {
        "version": ArknightsUID_version,
        "input_tags": " / ".join(tags) + ("（截图识别）" if via_ocr else ""),
        "note": note,
        "rows": rows,
        "best": rows[0]["tags"] if rows else "",
    }
    source_label = "截图识别" if via_ocr else "输入"
    lines = [f"「明日方舟」公招组合推荐（{source_label}：{' / '.join(tags)}）"]
    if note:
        lines.append(note)
    if via_ocr:
        lines.append("📸 标签来自截图识别，有误可文字输入 ark公招推荐 标签… 修正")
    for row in rows:
        names = "、".join(row["names"])
        lines.append(
            f"[{row['tier_name']}] {row['tags']}（招募 {row['duration']}，保底{row['min_rarity']}★，"
            f"池{row['pool_count']}人）：{names}"
        )
    lines.append(
        "勾选高资/资深标签并拉满 9 小时：标签不划、必得 6★/5★；"
        "其他组合按 9 小时，支援机械按 3:50，新手按 7:30；非稀有标签仍可能划掉。"
    )
    await _render_or_text(bot, ev, "ark_recruit_plan.html", context, "\n".join(lines))


@wiki_sv.on_command(("公招查询", "recruit"), block=True)
async def send_recruit_exact(bot: Bot, ev: Event) -> None:
    await _send_recruit(bot, ev, mode="exact")


@wiki_sv.on_command(("公招", "gz"), block=True)
async def send_recruit_query(bot: Bot, ev: Event) -> None:
    await _send_recruit(bot, ev, mode="auto")


async def _render_recruit_matches(
    bot: Bot, ev: Event, tags: list[str], matched: list[dict]
) -> None:
    lines = [f"「明日方舟」公招：{' + '.join(tags)}（{len(matched)} 名）"]
    for entry in matched[:80]:
        tag_str = "/".join(entry["tags"][:3]) if entry["tags"] else ""
        suffix = f" {tag_str}" if tag_str else ""
        lines.append(
            f"  {entry['rarity']}★ {entry['name']} [{entry['profession']}]{suffix}"
        )
    if len(matched) > 80:
        lines.append(f"  ...共 {len(matched)} 名，已显示前 80")
    _append_source(lines, "https://prts.wiki/w/公开招募")

    top_entries = matched[:12]
    top_avatars = await _gather_b64(
        char_avatar_b64, [str(entry.get("id", "")) for entry in top_entries]
    )
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
                "avatar": avatar,
            }
            for entry, avatar in zip(top_entries, top_avatars, strict=True)
        ],
        # 第 13 名起的完整名单（大池不再截断丢人头：资深 55/输出 56 人全展示）
        "rest": [
            {"rarity": int(entry["rarity"]), "name": entry["name"]}
            for entry in matched[12:]
        ],
        "truncated": len(matched) > 12,
    }
    await _render_or_text(bot, ev, "ark_recruit.html", context, "\n".join(lines))


@wiki_sv.on_command(("日历", "calendar", "rl"), block=True)
async def send_calendar(bot: Bot, ev: Event) -> None:
    """活动日历：当前版本/维护 + 进行中活动/卡池（SMW 与 PRTS 首页同源）。"""
    now = beijing_datetime()
    now_ts = now.timestamp()
    now_str = now.strftime("%Y-%m-%d %H:%M")

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
        start_ts = (
            datetime.strptime(maint_start, fmt).replace(tzinfo=BEIJING_TZ).timestamp()
            if maint_start
            else 0.0
        )
        end_ts = (
            datetime.strptime(maint_end, fmt).replace(tzinfo=BEIJING_TZ).timestamp()
            if maint_end
            else 0.0
        )
        if start_ts > now_ts:
            maint_text = (
                f"下次维护：{maint_start}（{_format_remaining(start_ts, now_ts)}）"
            )
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

    # 活动标题图 + 卡池横幅（与 ark活动 同源：PRTS 标题图 / SMW 文件页，
    # 下载缓存后 b64 内嵌；失败空串降级为纯文字行）
    banner_urls: dict[str, str] = {}
    try:
        from ArknightsUID.utils.prts.client import activity_banner_urls

        banner_urls = await activity_banner_urls([a.title for a in activities])
    except Exception:
        logger.debug("[ArknightsUID] 日历活动标题图拉取失败", exc_info=True)

    async def _calendar_banner_b64(url: str) -> str:
        if not url:
            return ""
        try:
            from ArknightsUID.utils.render import get_image_b64_with_cache
            from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH

            return await get_image_b64_with_cache(
                url, CACHE_PATH / "activity", quality=82
            )
        except Exception:
            logger.debug("[ArknightsUID] 日历横幅下载失败", exc_info=True)
            return ""

    # 横幅并发受限拉取（活动 15 + 卡池 10 张串行下载冷缓存会卡住，
    # 审计 #39）
    activity_images = await _gather_b64(
        _calendar_banner_b64,
        [banner_urls.get(act.title, "") for act in activities],
    )
    pool_images = await _gather_b64(
        _calendar_banner_b64, [pool.image_url for pool in pools]
    )
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
                "image": image,
            }
            for act, image in zip(activities, activity_images, strict=True)
        ],
        "pools": [
            {
                "title": pool.title,
                "remaining": _format_remaining(pool.end_ts, now_ts),
                "image": image,
            }
            for pool, image in zip(pools, pool_images, strict=True)
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
    from ArknightsUID.utils.gamedata.modules_info import (
        build_module_context,
        format_module_context,
    )

    bot_id = str(getattr(ev, "bot_id", "") or "")
    user_id = str(getattr(ev, "user_id", "") or "")
    context = (
        await build_module_context(query, bot_id=bot_id, user_id=user_id)
        if bot_id and user_id
        else await build_module_context(query)
    )
    if isinstance(context, str):
        return await _send_text(bot, ev, context)
    # 模组图标（PRTS torappu 资产，按 uniequip id 直取；失败空串降级）。
    # 材料图标已由 build_module_context 统一挂好（三档全量）
    try:
        from ArknightsUID.utils.path import EQUIP_CACHE_PATH
        from ArknightsUID.utils.player.context import equip_icon_url
        from ArknightsUID.utils.render import get_image_b64_with_cache

        for module in context.get("modules") or []:
            module["icon"] = await get_image_b64_with_cache(
                equip_icon_url(str(module.get("id") or "")), EQUIP_CACHE_PATH
            )
    except Exception:
        logger.debug("[ArknightsUID] 模组图标获取失败", exc_info=True)
    context["version"] = ArknightsUID_version
    await _render_or_text(
        bot, ev, "ark_module.html", context, format_module_context(context)
    )


@wiki_sv.on_command(("危机合约", "合约"), block=True)
async def send_crisis_info(bot: Bot, ev: Event) -> None:
    # 词条文本数据源暂不可达（PRTS SMW 词条分类仅图标文件，
    # GameData crisis 表不含词条文本），当前仅提供赛季时间线
    # 无参数：赛季时间线（旧+新，最近 8 个）
    from ArknightsUID.utils.gamedata.crisis import load_crisis_seasons

    seasons = await load_crisis_seasons()
    if not seasons:
        return await _send_text(
            bot, ev, "危机合约赛季数据不可用（gamedata 拉取失败），请稍后再试。"
        )
    lines = ["「明日方舟」危机合约赛季（近 8 期）："]
    context_rows = []
    for season in seasons[:8]:
        mark = "（新制式）" if season["version"] == "危机合约V2" else ""
        lines.append(f"- {season['name']}{mark}｜{season['start']} ~ {season['end']}")
        context_rows.append(
            {
                "name": season["name"],
                "v2": season["version"] == "危机合约V2",
                "start": season["start"],
                "end": season["end"],
            }
        )
    # 词条检索数据源暂不可达（见函数头注释），不再提示不存在的功能
    return await _render_or_text(
        bot,
        ev,
        "ark_crisis.html",
        {"seasons": context_rows, "version": ArknightsUID_version},
        "\n".join(lines),
    )


@wiki_sv.on_command(("语音", "台词"), block=True)
async def send_voice_lines(bot: Bot, ev: Event) -> None:
    """ark语音 干员名 [关键词] → 中文台词（PRTS 语音记录子页）。"""
    query = _clean_query(ev.text)
    parts = query.split()
    if not parts:
        return await _send_text(
            bot,
            ev,
            "用法：ark语音 干员名 [关键词]（如 ark语音 银灰 / ark语音 银灰 合约）",
        )
    name = parts[0]
    keyword = parts[1] if len(parts) > 1 else ""
    name = await _resolve_alias(name, ev.bot_id, ev.user_id)

    from ArknightsUID.utils.prts.client import query_voice_lines

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
            line
            for line in lines
            if keyword in line["title"] or keyword in line["text"]
        ]
        if not hits:
            return await _send_text(
                bot, ev, f"「{name}」的台词里没有提到「{keyword}」。"
            )
        shown = hits[:8]
        header = (
            f"「{name}」台词 · 「{keyword}」（{len(hits)} 条，显示前 {len(shown)} 条）"
        )
    else:
        shown = lines[:10]
        header = f"「{name}」语音记录（共 {len(lines)} 条，显示前 {len(shown)} 条）"
    body = [header]
    body.extend(f"[{s['title']}] {s['text']}" for s in shown)
    body.append("数据：PRTS（语音记录）")
    await _send_text(bot, ev, "\n".join(body))
