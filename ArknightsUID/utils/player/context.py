"""8 张业务卡的 context 组装层（P1）。

把 PlayerData 组装成 templates/ark_*.html 头部注释声明的 jinja2 上下文。
每个 builder 在关键字段缺失时返回 None，调用方回退纯文本（与 wiki/rogue 模块同模式）。

单干员卡（char card）涉及头像/技能图标下载，单独放 async builder。
"""
from __future__ import annotations

import time
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

from gsuid_core.logger import logger

from .building import (
    TRADING_STRATEGY_NAMES,
    BuildingData,
    training_remaining_secs,
    training_status_text,
)
from .parse import (
    CAMPAIGN_STAGE_KILL_CAP,
    PROFESSION_ORDER,
    CharEntry,
    PlayerData,
    effective_ap,
)

# BOX 分析稀有度分档（label, 最低星级, 颜色）——游戏客观稀有度配色，
# 仅用于堆叠条与图例菱形标（与 ark_base.css 的 --rarity-* token 一致）
_BOX_RARITY_ROWS = (
    ("六星", 6, "#ff8c00"),
    ("五星", 5, "#d9a34a"),
    ("四星", 4, "#8a73c4"),
    ("三星及以下", 3, "#4a90d9"),
)

# hycdn 资源 URL 规则（客观 CDN 事实）：
# - 皮肤头像 skinId 需把 @ → %40、# → %23，路径 char_skin/avatar
# - 技能图标直接拼 char_skill/{id}.png（id 可能含 []，需整体编码）
_HYCDN = "https://web.hycdn.cn/arknights/game/assets"


def format_dt(ts: int, now: float | None = None) -> str:
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def format_remaining(ts: int, now: float | None = None) -> str:
    """距回满的 "3h 12m" 文案。已回满返回 ""。"""
    if ts <= 0:
        return ""
    now = time.time() if now is None else now
    seconds = int(ts - now)
    if seconds <= 0:
        return ""
    hours, minutes = divmod(seconds, 3600)
    minutes //= 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def skin_avatar_url(skin_id: str) -> str:
    if not skin_id:
        return ""
    # player_info 的基础皮肤写作 char_xxx@N（N=1 初始 / 2 精一 / 3 精二），
    # hycdn 按皮肤表键名 char_xxx#N 提供头像；装扮皮肤本身是 char_xxx@系列#N，
    # 已含 #，保持原样。经验证：@N 形态 404，#N 与 @系列#N 均 200。
    skin_id = re.sub(r"@(\d+)$", r"#\1", skin_id)
    encoded = skin_id.replace("@", "%40").replace("#", "%23")
    return f"{_HYCDN}/char_skin/avatar/{encoded}.png"


def skill_icon_url(skill_id: str) -> str:
    if not skill_id:
        return ""
    return f"{_HYCDN}/char_skill/{quote(skill_id, safe='')}.png"


def skin_portrait_url(skin_id: str) -> str:
    """皮肤立绘 URL（portrait 路径，编码规则同 skin_avatar_url）。"""
    if not skin_id:
        return ""
    skin_id = re.sub(r"@(\d+)$", r"#\1", skin_id)
    encoded = skin_id.replace("@", "%40").replace("#", "%23")
    return f"{_HYCDN}/char_skin/portrait/{encoded}.png"


def equip_icon_url(equip_id: str) -> str:
    """模组图标 URL（PRTS torappu 资产，按 uniequip id 直取）。"""
    if not equip_id:
        return ""
    return f"https://torappu.prts.wiki/assets/uniequip_img/{quote(equip_id, safe='')}.png"


def base_avatar_url(char_id: str) -> str:
    """干员初始头像（avatar/{charId}#1，公招/寻访列表的小头像用）。"""
    if not char_id:
        return ""
    return f"{_HYCDN}/char_skin/avatar/{char_id}%231.png"


async def char_avatar_b64(char_id: str) -> str:
    """干员初始头像 base64（磁盘缓存，失败空串降级）。"""
    url = base_avatar_url(char_id)
    if not url:
        return ""
    try:
        from ..path import AVATAR_CACHE_PATH
        from ..render import get_image_b64_with_cache

        return await get_image_b64_with_cache(url, AVATAR_CACHE_PATH)
    except Exception:
        return ""


# 立绘 404 负缓存时效：避免每个缺档位每次查询都重打 CDN（404 日志刷屏）。
# 取 1h 而非更长：瞬时网络失败也会落标记，太长会误伤可恢复的缺图。
_PORTRAIT_MISS_TTL = 3600.0


async def char_portrait_b64(char_id: str, rarity: int = 0) -> str:
    """干员立绘 base64：portrait/{charId}#N，按稀有度 E2→E1→初始 逐档回退。

    4★ 及以上才有 E2 立绘（#3）；404/下载失败自动降档，全部失败返回空串。
    缺失档位写 .miss 标记（1h 负缓存），期间直接跳过不再请求。
    """
    if not char_id:
        return ""
    order = ("3", "2", "1") if rarity >= 4 else (("2", "1") if rarity == 3 else ("1",))
    try:
        from ..path import CHAR_CACHE_PATH
        from ..render import get_image_b64_with_cache

        CHAR_CACHE_PATH.mkdir(parents=True, exist_ok=True)
        for tier in order:
            miss_path = CHAR_CACHE_PATH / f"{char_id}%23{tier}.miss"
            if miss_path.exists():
                try:
                    if time.time() - miss_path.stat().st_mtime < _PORTRAIT_MISS_TTL:
                        continue
                    miss_path.unlink()
                except OSError:
                    continue
            url = f"{_HYCDN}/char_skin/portrait/{char_id}%23{tier}.png"
            b64 = await get_image_b64_with_cache(url, CHAR_CACHE_PATH)
            if b64:
                return b64
            miss_path.write_text("", encoding="utf-8")
    except Exception:
        logger.debug("[ArknightsUID] 干员立绘获取失败", exc_info=True)
    return ""


def build_status_context(
    data: PlayerData, version: str, uid_masked: str = "", now: float | None = None
) -> dict[str, Any] | None:
    """ark_status.html（#2 便签）。需要昵称与理智。"""
    if not data.name or data.ap_max <= 0:
        return None
    now = time.time() if now is None else now
    ap_current = effective_ap(data, now)
    full_at = data.ap_complete_recovery_time
    return {
        "name": data.name,
        "level": data.level,
        "uid": uid_masked or data.uid,
        "ap_current": ap_current,
        "ap_max": data.ap_max,
        "ap_pct": round(ap_current * 100 / data.ap_max) if data.ap_max else 0,
        "ap_full_at": format_dt(full_at, now) or "已回满",
        "ap_recover_in": format_remaining(full_at, now) or "已回满",
        "reg_days": data.register_days(now),
        "chars_total": data.chars_total,
        "campaign_current": data.campaign_current,
        "campaign_total": data.campaign_total,
        "version": version,
    }


def build_daily_context(
    data: PlayerData, version: str, uid_masked: str = ""
) -> dict[str, Any] | None:
    """ark_daily.html（#3 日常/周常）。需要 routine 任务数据。"""
    if data.daily_total <= 0 and data.weekly_total <= 0:
        return None
    return {
        "name": data.name,
        "uid": uid_masked or data.uid,
        "daily_done": min(data.daily_done, data.daily_total),
        "daily_total": data.daily_total,
        "weekly_done": min(data.weekly_done, data.weekly_total),
        "weekly_total": data.weekly_total,
        "campaign_current": data.campaign_current,
        "campaign_total": data.campaign_total,
        "version": version,
    }


def build_campaign_context(
    data: PlayerData, version: str, uid_masked: str = ""
) -> dict[str, Any] | None:
    """ark_campaign.html（#4 剿灭）。需要 campaign.reward。"""
    if data.campaign_total <= 0 and not data.campaign_records:
        return None
    pct = (
        round(data.campaign_current * 100 / data.campaign_total)
        if data.campaign_total > 0
        else 0
    )
    stages = [
        {"name": zone_key, "progress": kills, "total": CAMPAIGN_STAGE_KILL_CAP}
        for zone_key, kills in sorted(
            data.campaign_records.items(), key=lambda kv: -kv[1]
        )[:6]
    ]
    return {
        "name": data.name,
        "uid": uid_masked or data.uid,
        "reward_current": data.campaign_current,
        "reward_total": data.campaign_total,
        "reward_pct": pct,
        "stages": stages,
        "version": version,
    }


def _profession_rows(chars: list[CharEntry]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for prof in PROFESSION_ORDER:
        members = [c for c in chars if c.profession == prof or (
            not c.profession and prof == "CASTER" and c.char_id == "char_002_amiya"
        )]
        if not members:
            continue
        e2 = sum(1 for c in members if c.evolve_phase >= 2)
        maxlvl = sum(
            1 for c in members if c.evolve_phase >= 2 and c.level >= c.max_level()
        )
        rows.append(
            {
                "name": members[0].profession_name or prof,
                "en": prof,
                "count": len(members),
                "e2": e2,
                "maxlvl": maxlvl,
            }
        )
    return rows


def build_char_stat_context(
    data: PlayerData, version: str, uid_masked: str = ""
) -> dict[str, Any] | None:
    """ark_char_stat.html（#6 练度统计）。需要 chars + charInfoMap 元数据。"""
    if not data.chars:
        return None
    chars = data.chars
    e2_total = sum(1 for c in chars if c.evolve_phase >= 2)
    maxlvl_total = sum(
        1 for c in chars if c.evolve_phase >= 2 and c.level >= c.max_level()
    )
    return {
        "name": data.name,
        "uid": uid_masked or data.uid,
        "total": len(chars),
        "e2_total": e2_total,
        "maxlvl_total": maxlvl_total,
        "professions": _profession_rows(chars),
        "version": version,
    }


def build_box_context(
    data: PlayerData, version: str, uid_masked: str = ""
) -> dict[str, Any] | None:
    """ark_box_analysis.html（#7 BOX 分析）。需要 chars + charInfoMap 元数据。"""
    if not data.chars:
        return None
    chars = data.chars
    total = len(chars)
    rarity_rows: list[dict[str, Any]] = []
    for label, star, color in _BOX_RARITY_ROWS:
        if star > 3:
            count = sum(1 for c in chars if c.rarity_display == star)
        else:
            # 末档收口：三星及以下
            count = sum(1 for c in chars if c.rarity_display <= star)
        rarity_rows.append(
            {
                "label": label,
                "count": count,
                "pct": round(count * 100 / total) if total else 0,
                "color": color,
            }
        )
    elite_rows = [
        {"label": "精英化二", "count": sum(1 for c in chars if c.evolve_phase >= 2)},
        {
            "label": "精英化一",
            "count": sum(1 for c in chars if c.evolve_phase == 1),
        },
        {"label": "无精英化", "count": sum(1 for c in chars if c.evolve_phase == 0)},
    ]
    return {
        "name": data.name,
        "uid": uid_masked or data.uid,
        "total": total,
        "rarity_rows": rarity_rows,
        "elite_rows": elite_rows,
        "version": version,
    }


def equip_display_name(equip_id: str) -> str:
    """模组展示名：uniequip_002_silverash → "X2 模组" 风格退化展示。

    player_info 的 equipmentInfoMap 字段语义未完全确认（真实 dump 截断），
    先用 id 顺序位做 X/Y 猜测 + 原始 id 兜底，后续联调后替换为表内名称。
    """
    if not equip_id:
        return "模组"
    parts = equip_id.split("_")
    if len(parts) >= 3 and parts[1].isdigit():
        # 偶数序号 → X 型，奇数 → Y 型（干员模组通用规律）
        slot = int(parts[1])
        return f"{'X' if slot % 2 == 0 else 'Y'}{max(1, (slot + 1) // 2)} 模组"
    return equip_id


async def build_building_context(
    data: PlayerData,
    building: "BuildingData",
    version: str,
    uid_masked: str = "",
) -> dict[str, Any] | None:
    """ark_building.html（基建总览）。需要 building 解析结果可用。"""
    if not building.available:
        return None

    # 制造产物名：item_table 共享缓存，失败回退 "物品#<id>"
    item_names: dict[str, str] = {}
    if any(room.item_id for room in building.manufactures):
        try:
            from ..gamedata.charinfo import load_item_index

            item_names = await load_item_index() or {}
        except Exception:
            item_names = {}

    def _item_name(item_id: str) -> str:
        info = item_names.get(item_id)
        if isinstance(info, dict) and info.get("name"):
            return str(info["name"])
        return f"物品#{item_id}" if item_id else "未知配方"

    manufactures = [
        {
            "label": f"制造站" if i == 1 else f"制造站{i}",
            "level": room.level,
            "formula": (
                f"{_item_name(room.item_id)}×{room.item_count}"
                if room.item_id
                else "未在生产"
            ),
            "speed_pct": round(room.speed * 100) if room.speed > 0 else 100,
            "members": [
                {"name": m.name, "char_id": m.char_id} for m in room.members
            ],
        }
        for i, room in enumerate(building.manufactures, start=1)
    ]
    tradings = [
        {
            "label": f"贸易站" if i == 1 else f"贸易站{i}",
            "level": room.level,
            "strategy": TRADING_STRATEGY_NAMES.get(
                room.strategy, room.strategy or "未知"
            ),
            "stock": room.stock_count,
            "stock_limit": room.stock_limit,
            "members": [
                {"name": m.name, "char_id": m.char_id} for m in room.members
            ],
        }
        for i, room in enumerate(building.tradings, start=1)
    ]
    dormitories = [
        {
            "label": f"宿舍{i}",
            "comfort": room.comfort,
            "comfort_pct": min(100, round(room.comfort * 100 / 5000)),
        }
        for i, room in enumerate(building.dormitories, start=1)
    ]
    tired = [
        {"name": c.name, "room": c.room, "morale": c.morale}
        for c in building.tired_chars[:8]
    ]
    return {
        "name": data.name or "博士",
        "uid": uid_masked or data.uid,
        "control_level": building.control_level,
        "labor_value": building.labor_value,
        "labor_max": building.labor_max,
        "labor_pct": (
            round(building.labor_value * 100 / building.labor_max)
            if building.labor_max > 0
            else 0
        ),
        "manufactures": manufactures,
        "tradings": tradings,
        "power_count": len(building.powers),
        "power_levels": [room.level for room in building.powers],
        "dormitories": dormitories,
        "clue_own": building.clue_own,
        "clue_board": building.clue_board,
        "clue_daily_reward": building.clue_daily_reward,
        "training_trainee": building.training_trainee,
        "training_remain": training_status_text(
            training_remaining_secs(building)
        ),
        "tired_chars": tired,
        "tired_more": max(0, len(building.tired_chars) - 8),
        "version": version,
    }


async def build_char_card_context(
    entry: CharEntry, version: str, skill_effects: dict[str, dict] | None = None
) -> dict[str, Any]:
    """ark_char_card.html（#5 单干员卡）。立绘/头像/技能图标走 hycdn 下载缓存。

    skill_effects：技能名 → {current, current_level, final, final_level}
    （PRTS 逐级描述，由调用方从 wiki 层取；缺省只展示专精点）。
    """
    avatar = ""
    portrait = ""
    icon_by_id: dict[str, str] = {}
    equip_icon_by_id: dict[str, str] = {}
    try:
        from ..render import get_image_b64_with_cache
        from ..path import (
            AVATAR_CACHE_PATH,
            CHAR_CACHE_PATH,
            EQUIP_CACHE_PATH,
            SKILL_CACHE_PATH,
        )

        # 立绘优先（玩家当前穿戴皮肤），头像兜底
        portrait_url = skin_portrait_url(entry.skin_id or entry.char_id)
        if portrait_url:
            portrait = await get_image_b64_with_cache(portrait_url, CHAR_CACHE_PATH)
        if not portrait:
            avatar_url = skin_avatar_url(entry.skin_id or entry.char_id)
            if avatar_url:
                avatar = await get_image_b64_with_cache(avatar_url, AVATAR_CACHE_PATH)
        for skill in entry.skills:
            icon_url = skill_icon_url(skill.skill_id)
            if icon_url:
                icon_by_id[skill.skill_id] = await get_image_b64_with_cache(
                    icon_url, SKILL_CACHE_PATH
                )
        for eq in entry.equips:
            if eq.equip_id:
                equip_icon_by_id[eq.equip_id] = await get_image_b64_with_cache(
                    equip_icon_url(eq.equip_id), EQUIP_CACHE_PATH
                )
    except Exception:
        # 渲染层或网络不可用：无图也照出卡（模板对空 portrait/avatar/icon 有降级）
        portrait = ""
        avatar = ""
        icon_by_id = {}
        equip_icon_by_id = {}

    # 技能名来自 skill_table（gamedata，惰性加载；失败退回 skillId 原文）
    skill_names: dict[str, str] = {}
    try:
        from ..gamedata.charinfo import load_skill_names

        skill_names = await load_skill_names()
    except Exception:
        skill_names = {}

    effects = skill_effects or {}
    skills: list[dict[str, Any]] = []
    for skill in entry.skills:
        name = skill_names.get(skill.skill_id) or skill.skill_id
        effect = effects.get(name, {})
        skills.append(
            {
                "icon": icon_by_id.get(skill.skill_id, ""),
                "name": name,
                "specialize": skill.specialize_level,
                "current": effect.get("current", ""),
                "current_level": effect.get("current_level", ""),
                "final": effect.get("final", ""),
                "final_level": effect.get("final_level", ""),
            }
        )
    equips = [
        {
            "name": equip_display_name(eq.equip_id),
            "stage": eq.level,
            "icon": equip_icon_by_id.get(eq.equip_id, ""),
        }
        for eq in entry.equips
        if eq.equip_id
    ]
    return {
        "name": entry.display_name,
        "rarity": entry.rarity_display,
        "profession": f"{entry.profession_name} {entry.profession}".strip(),
        "avatar": avatar,
        "portrait": portrait,
        "level": entry.level,
        "max_level": entry.max_level(),
        "elite": entry.evolve_phase,
        "potential": entry.potential_rank,
        "favor": entry.favor_percent,
        "main_skill_lvl": entry.main_skill_lvl,
        "skills": skills,
        "equips": equips,
        "version": version,
    }
