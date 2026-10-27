from __future__ import annotations

import asyncio
import re
import time
import unicodedata
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


async def _skill_icon_b64(skill_name: str) -> str:
    """技能名 → skill_id（skill_table 反查）→ hycdn 图标 base64（失败空串）。"""
    if not skill_name:
        return ""
    try:
        from ArknightsUID.utils.gamedata.charinfo import load_skill_names
        from ArknightsUID.utils.path import SKILL_CACHE_PATH
        from ArknightsUID.utils.player.context import skill_icon_url
        from ArknightsUID.utils.render import get_image_b64_with_cache

        names = await load_skill_names()
        skill_id = next((sid for sid, n in names.items() if n == skill_name), "")
        if not skill_id:
            return ""
        return await get_image_b64_with_cache(
            skill_icon_url(skill_id), SKILL_CACHE_PATH
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


# ark面板 条件解析的正则（模块级预编译）
_RE_PHASE = re.compile(r"^精([012])$")
_RE_LV = re.compile(r"^(?:[Ll][Vv]?(\d{1,3})|(\d{1,3})级|(满级))$")
_RE_POT = re.compile(r"^(?:潜(\d)|满潜)$")
_RE_SKILL = re.compile(r"^(?:技能([123])|([一二三])技能)$")
_RE_MASTERY = re.compile(r"^专([123])$")
_RE_SKILL_LV = re.compile(r"^([1-7])级$")
_RE_MODULE_TYPE = re.compile(r"^([XYABDxyabdＸＹＡＢＤｘｙａｂｄ])模组?$")
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
        "name": "",
        "phase": None,
        "level": None,
        "potential": None,
        "favor": None,
        "skill_index": None,
        "skill_level": None,
        "mastery": None,
        "module_type": None,
        "no_module": False,
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
            # 全角 Ｘ/Ｙ 先 NFKC 归一半角再大写，否则 'Ｙ' != 'Y'
            # 永不命中模组类型，静默回退 X 模
            config["module_type"] = unicodedata.normalize("NFKC", m.group(1)).upper()
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
    "PIONEER": "先锋",
    "GUARD": "近卫",
    "DEFENDER": "重装",
    "SNIPER": "狙击",
    "CASTER": "术师",
    "MEDIC": "医疗",
    "SUPPORTER": "辅助",
    "SPECIALIST": "特种",
}


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
        load_char_index,
        resolve_char,
    )

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
    # 材料图标先收集再并发受限拉取（3 技能 × 3 专精 × 若干材料串行下载
    # 冷缓存会卡数十秒，审计 #39）
    mastery_skills = [s for s in op.skills if s.mastery_costs]
    item_names: list[str] = []
    for s in mastery_skills:
        for mc in s.mastery_costs:
            for text in mc.materials:
                name, _count = split_material_text(text)
                if name:
                    item_names.append(name)
    item_icons = dict(zip(item_names, await _gather_b64(item_icon_b64, item_names)))
    skill_icons = dict(
        zip(
            [s.name for s in mastery_skills],
            await _gather_b64(_skill_icon_b64, [s.name for s in mastery_skills]),
        )
    )
    skills_ctx = []
    for s in mastery_skills:
        costs_ctx = []
        for mc in s.mastery_costs:
            mats = []
            for text in mc.materials:
                name, count = split_material_text(text)
                mats.append(
                    {"name": name, "count": count, "icon": item_icons.get(name, "")}
                )
            costs_ctx.append({"mastery": mc.mastery, "materials": mats})
        skills_ctx.append(
            {
                "name": s.name,
                "icon": skill_icons.get(s.name, ""),
                "mastery_costs": costs_ctx,
            }
        )
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
    """ark公招推荐 标签…（把界面看到的标签全输入）→ 1-3 标签最优组合排行。

    也支持不带标签、直接发公招界面截图：本地 rapidocr（可选依赖）识别
    标签后走同一套推荐逻辑；带文字参数时以文字为准（忽略截图）。

    机制口径（与 utils/gamedata/recruit.py 的服务端数据一致）：
    资深干员=5★、高级资深干员=6★（9 小时招募标签不划、必得档）。
    """
    raw = _clean_query(ev.text)
    image_sources = [
        s
        for s in (ev.image_list or ([ev.image] if ev.image else []))
        if isinstance(s, str) and s.strip()
    ]
    if not raw and not image_sources:
        return await _send_text(
            bot,
            ev,
            "用法：ark公招推荐 标签1 标签2 …（把公招界面看到的标签全部输入，"
            "如 ark公招推荐 高级资深干员 术师 输出 远程位 生存），"
            "或直接发送公招界面截图自动识别标签",
        )
    data = await load_recruit_data()
    if not data:
        return await _send_text(
            bot, ev, "公招数据加载失败（首次需下载约 15MB 游戏数据），请稍后重试"
        )
    via_ocr = False
    if not raw and image_sources:
        ocr = await extract_recruit_tags(image_sources, build_tag_universe(data))
        if ocr.error:
            return await _send_text(bot, ev, ocr.error)
        if not ocr.tags:
            shown = "、".join(ocr.raw_texts[:6]) or "无"
            return await _send_text(
                bot,
                ev,
                f"截图里没识别到公招标签（读到的文字：{shown}）。"
                "请截取包含标签区域的公招界面，"
                "或文字输入 ark公招推荐 标签…",
            )
        tags = ocr.tags
        via_ocr = True
    else:
        tags = [t.strip() for t in re.split(r"[\s,，、+]+", raw) if t.strip()]
    try:
        from ArknightsUID.utils.gamedata.recruit import recommend_combos
    except ImportError:
        from ArknightsUID.utils.gamedata.recruit import recommend_combos

    result = recommend_combos(tags, data)
    combos = result["combos"]
    if not combos:
        hint = ""
        if result["unknown"]:
            hint = f"（未识别的标签：{'、'.join(result['unknown'])}）"
        return await _send_text(
            bot,
            ev,
            f"这些标签组不出任何可公招组合{hint}。检查标签写法，"
            "或用 ark公招 <标签> 查询单个组合。",
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
        "unknown_tags": "、".join(result["unknown"]),
        "rows": rows,
        "best": rows[0]["tags"] if rows else "",
    }
    source_label = "截图识别" if via_ocr else "输入"
    lines = [f"「明日方舟」公招组合推荐（{source_label}：{' / '.join(tags)}）"]
    if result["unknown"]:
        lines.append(f"⚠️ 未识别标签：{'、'.join(result['unknown'])}（已忽略）")
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


@wiki_sv.on_command(("公招查询", "recruit", "公招", "gz"), block=True)
async def send_recruit_query(bot: Bot, ev: Event) -> None:
    raw = _clean_query(ev.text)
    if not raw:
        return await _send_text(
            bot,
            ev,
            "用法：ark公招 标签1 标签2（空格或逗号分隔，如 ark公招 高级资深干员 术师）",
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

    context = await build_module_context(query)
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
