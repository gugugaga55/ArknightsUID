"""8 张业务卡的 context 组装层（P1）。

把 PlayerData 组装成 templates/ark_*.html 头部注释声明的 jinja2 上下文。
每个 builder 在关键字段缺失时返回 None，调用方回退纯文本（与 wiki/rogue 模块同模式）。

单干员卡（char card）涉及头像/技能图标下载，单独放 async builder。
"""

from __future__ import annotations

import time
import re
from typing import Any
from urllib.parse import quote

from gsuid_core.logger import logger

from ..dict_utils import path
from ..time_utils import beijing_datetime

from .building import (
    MORALE_LOW_THRESHOLD,
    MORALE_MAX_POINTS,
    TRADING_STRATEGY_NAMES,
    TRAINING_BASE_HOURS_BY_LEVEL,
    BuildingData,
    RoomMember,
    format_training_remaining,
    training_progress_pct,
    training_remaining_secs,
    training_status_text,
)
from .parse import (
    CAMPAIGN_STAGE_KILL_CAP,
    PROFESSION_NAMES,
    PROFESSION_ORDER,
    CharEntry,
    PlayerData,
    effective_ap,
    recruit_done_count,
    recruit_next_remaining,
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
    return beijing_datetime(ts).strftime("%m-%d %H:%M")


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


def _encode_skin_key(skin_id: str) -> str:
    """皮肤键 → hycdn 编码形态（审计 #21）。

    @N → #N；裸 charId（接口未给 skinId 时调用方传 char_xxx 兜底）末尾
    补 #1——hycdn 无后缀路径 404，基础皮肤的真实键名是 char_xxx#1。
    已含 # 的装扮键保持原样。
    完整 quote 而非手工替换 #/@：形态键含 +（如 char_002_amiya#1+）时
    裸 + 会被 CDN 拒（实测 %2B 200 / 裸 + 404，2026-09）；常规键的
    quote 输出与旧手工替换逐字一致，存量缓存名不变。
    """
    value = re.sub(r"@(\d+)$", r"#\1", skin_id)
    if "#" not in value and "@" not in value:
        value += "#1"
    return quote(value, safe="")


def skin_avatar_url(skin_id: str) -> str:
    if not skin_id:
        return ""
    # player_info 的基础皮肤写作 char_xxx@N（N=1 初始 / 2 精一 / 3 精二），
    # hycdn 按皮肤表键名 char_xxx#N 提供头像；装扮皮肤本身是 char_xxx@系列#N，
    # 已含 #，保持原样。经验证：@N 形态 404，#N 与 @系列#N 均 200。
    # 裸 charId（无后缀）由 _encode_skin_key 补 #1，否则该路径恒 404。
    return f"{_HYCDN}/char_skin/avatar/{_encode_skin_key(skin_id)}.png"


def skill_icon_url(skill_id: str) -> str:
    if not skill_id:
        return ""
    return f"{_HYCDN}/char_skill/{quote(skill_id, safe='')}.png"


def skin_portrait_url(skin_id: str) -> str:
    """皮肤立绘 URL（portrait 路径，编码规则同 skin_avatar_url）。"""
    if not skin_id:
        return ""
    return f"{_HYCDN}/char_skin/portrait/{_encode_skin_key(skin_id)}.png"


def equip_icon_url(equip_id: str) -> str:
    """模组图标 URL（PRTS torappu 资产，按 uniequip id 直取）。"""
    if not equip_id:
        return ""
    return (
        f"https://torappu.prts.wiki/assets/uniequip_img/{quote(equip_id, safe='')}.png"
    )


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


def _mask_uid(uid_masked: str, raw_uid: str) -> str:
    """展示上下文的 UID：调用方传入的脱敏值优先；空值时本地默认掩码。

    fail-closed：绝不回退原始 UID——调用方漏传 mask 也不把明文 UID 送进
    卡片（审计 P3-1）。默认掩码与 hide_uid 的规格一致（前3后3）。
    """
    if uid_masked:
        return uid_masked
    text = str(raw_uid or "")
    if len(text) <= 6:
        return "***"
    return f"{text[:3]}***{text[-3:]}"


def build_status_context(
    data: PlayerData, version: str, uid_masked: str = "", now: float | None = None
) -> dict[str, Any] | None:
    """ark_status.html（#2 便签 · 理智+日常合并卡）。需要昵称与理智。

    ark理智 / ark日常 同卡输出：理智为主线，日常/周常有任务数据才展示
    （`has_routine` 控制整块）。
    """
    if not data.name or data.ap_max <= 0:
        return None
    now = time.time() if now is None else now
    ap_current = effective_ap(data, now)
    full_at = data.ap_complete_recovery_time
    daily_done = min(data.daily_done, data.daily_total)
    weekly_done = min(data.weekly_done, data.weekly_total)
    registered = beijing_datetime(data.register_ts) if data.register_ts > 0 else None
    return {
        "name": data.name,
        "level": data.level,
        "uid": _mask_uid(uid_masked, data.uid),
        "ap_current": ap_current,
        "ap_max": data.ap_max,
        "ap_pct": round(ap_current * 100 / data.ap_max) if data.ap_max else 0,
        "ap_full_at": format_dt(full_at, now) or "已回满",
        "ap_recover_in": format_remaining(full_at, now) or "已回满",
        "reg_days": data.register_days(now),
        # 注册日期（2023年5月12日 形态；无注册时间戳为空串）
        "register_date": (
            f"{registered.year}年{registered.month}月{registered.day}日"
            if registered is not None
            else ""
        ),
        "chars_total": data.chars_total,
        "campaign_current": data.campaign_current,
        "campaign_total": data.campaign_total,
        # 日常/周常（ark日常 并入后的第二主线；无任务数据整块隐藏）
        "has_routine": data.daily_total > 0 or data.weekly_total > 0,
        "daily_done": daily_done,
        "daily_total": data.daily_total,
        "daily_pct": (
            round(daily_done * 100 / data.daily_total) if data.daily_total > 0 else 0
        ),
        "weekly_done": weekly_done,
        "weekly_total": data.weekly_total,
        "weekly_pct": (
            round(weekly_done * 100 / data.weekly_total)
            if data.weekly_total > 0
            else 0
        ),
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
        {
            "name": data.campaign_name(zone_key),
            "progress": kills,
            "total": CAMPAIGN_STAGE_KILL_CAP,
        }
        for zone_key, kills in sorted(
            data.campaign_records.items(), key=lambda kv: -kv[1]
        )[:6]
    ]
    return {
        "name": data.name,
        "uid": _mask_uid(uid_masked, data.uid),
        "reward_current": data.campaign_current,
        "reward_total": data.campaign_total,
        "reward_pct": pct,
        "stages": stages,
        "version": version,
    }


def _profession_rows(chars: list[CharEntry]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for prof in PROFESSION_ORDER:
        members = [
            c
            for c in chars
            if c.profession == prof
            or (not c.profession and prof == "CASTER" and c.char_id == "char_002_amiya")
        ]
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
        "uid": _mask_uid(uid_masked, data.uid),
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
        "uid": _mask_uid(uid_masked, data.uid),
        "total": total,
        "rarity_rows": rarity_rows,
        "elite_rows": elite_rows,
        "version": version,
    }


def equip_display_name(equip_id: str, metadata: dict | None = None) -> str:
    """优先使用游戏表中的模组名；缺少元数据时显示 ID，不猜类型或等级。"""
    return str(path(metadata, "uniEquipName") or equip_id or "模组")


def _mastery_target(
    data: PlayerData, building: BuildingData
) -> tuple[CharEntry | None, int, int]:
    """受训干员与专精目标档（接口 targetSkill=技能序号；目标档=当前专精+1）。

    返回 (char_entry|None, mastery_from, mastery_to)；推不出 → (char|None, 0, 0)。
    """
    char = next(
        (c for c in data.chars if c.char_id == building.training_trainee_id),
        None,
    )
    idx = building.training_target_skill
    if char is not None and 0 <= idx < len(char.skills):
        cur = char.skills[idx].specialize_level
        return char, cur, min(3, cur + 1)
    return char, 0, 0


async def build_building_context(
    data: PlayerData,
    building: BuildingData,
    version: str,
    uid_masked: str = "",
) -> dict[str, Any] | None:
    """ark_building.html（基建总览，深色房间网格版）。需要 building 解析结果可用。"""
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

    def _member_dicts(
        members: list[RoomMember], with_morale: bool = True
    ) -> list[dict[str, Any]]:
        """房间成员 → 模板成员 dict（头像/职业图标由 _mount_assets 后补）。"""
        return [
            {
                "name": m.name,
                "char_id": m.char_id,
                "avatar": "",
                "profession": m.profession,
                "prof_icon": "",
                # 训练位等无心情口径的成员传 None（模板不出心情条）
                "morale": m.morale if with_morale else None,
                "morale_pct": (
                    round(m.morale * 100 / MORALE_MAX_POINTS) if with_morale else 0
                ),
                "tired": with_morale and m.morale < MORALE_LOW_THRESHOLD,
            }
            for m in members
        ]

    # 训练位成员（受训/协助干员无 ap 字段，不出心情条）；职业经 charInfoMap 反查
    info_map = data.raw.get("charInfoMap")
    if not isinstance(info_map, dict):
        info_map = {}

    def _profession_of(char_id: str) -> str:
        info = info_map.get(char_id)
        return str(info.get("profession") or "") if isinstance(info, dict) else ""

    def _empty_slot(label: str, face: str = "") -> dict[str, Any]:
        return {"empty": True, "label": label, "face": face}

    remaining = training_remaining_secs(building)
    if not building.training_trainee_id or remaining < 0:
        training_state = "空闲"
    elif remaining == 0:
        training_state = "已完成"
    else:
        training_state = "受训中"
    training_members: list[dict[str, Any]] = []
    for char_id, name in (
        (building.training_trainee_id, building.training_trainee),
        (building.training_trainer_id, building.training_trainer),
    ):
        if not char_id:
            continue
        training_members.append(
            {
                "name": name,
                "char_id": char_id,
                "avatar": "",
                "profession": _profession_of(char_id),
                "prof_icon": "",
                "morale": None,
                "morale_pct": 0,
                "tired": False,
            }
        )
    # 训练位固定 2（受训 + 协助；游戏机制），空位带槽位名便于辨认缺谁
    training_slots: list[dict[str, Any]] = list(training_members)
    if not building.training_trainee_id:
        training_slots.append(_empty_slot("受训位"))
    if not building.training_trainer_id:
        training_slots.append(_empty_slot("协助位"))

    control = {
        "level": building.control_level,
        "members": _member_dicts(building.control_members),
        # 中枢进驻位 = 房间等级（游戏机制，上限 5）
        "empty": max(
            0, min(building.control_level, 5) - len(building.control_members)
        ),
    }
    control["slots"] = control["members"] + [
        _empty_slot("空闲") for _ in range(control["empty"])
    ]
    manufactures = []
    for i, room in enumerate(building.manufactures, start=1):
        members = _member_dicts(room.members)
        manufactures.append(
            {
                "label": "制造站" if i == 1 else f"制造站{i}",
                "level": room.level,
                "formula": (
                    f"{_item_name(room.item_id)}×{room.item_count}"
                    if room.item_id
                    else "未在生产"
                ),
                "speed_pct": round(room.speed * 100) if room.speed > 0 else 100,
                "members": members,
                "slots": members,
            }
        )
    tradings = []
    for i, room in enumerate(building.tradings, start=1):
        members = _member_dicts(room.members)
        tradings.append(
            {
                "label": "贸易站" if i == 1 else f"贸易站{i}",
                "level": room.level,
                "strategy": TRADING_STRATEGY_NAMES.get(
                    room.strategy, room.strategy or "未知"
                ),
                "stock": room.stock_count,
                "stock_limit": room.stock_limit,
                "stock_pct": (
                    min(100, round(room.stock_count * 100 / room.stock_limit))
                    if room.stock_limit > 0
                    else 0
                ),
                "members": members,
                "slots": members,
            }
        )
    powers = []
    for i, room in enumerate(building.powers, start=1):
        members = _member_dicts(room.members)
        powers.append(
            {
                "label": f"发电站{i}",
                "level": room.level,
                "members": members,
                # 发电站固定 1 个进驻位（游戏机制）
                "slots": members
                + [_empty_slot("空闲") for _ in range(max(0, 1 - len(members)))],
            }
        )
    dormitories = [
        {
            "label": f"宿舍{i}",
            "level": room.level,
            "comfort": room.comfort,
            "comfort_pct": min(100, round(room.comfort * 100 / 5000)),
            "members": _member_dicts(room.members),
            # 宿舍固定 5 个休息位（游戏机制，与等级无关）
            "empty": max(0, 5 - len(room.members)),
        }
        for i, room in enumerate(building.dormitories, start=1)
    ]
    for dorm in dormitories:
        dorm["slots"] = dorm["members"] + [
            _empty_slot("自动补位", face="AUTO") for _ in range(dorm["empty"])
        ]
    meeting = None
    if building.meeting_level > 0 or building.meeting_members:
        # 会客室固定 2 个进驻位（游戏机制）
        empty = max(0, 2 - len(building.meeting_members))
        meeting_members = _member_dicts(building.meeting_members)
        meeting = {
            "level": building.meeting_level,
            "members": meeting_members,
            "empty": empty,
            "slots": meeting_members + [_empty_slot("空闲") for _ in range(empty)],
        }
    hire = None
    if building.hire_level > 0 or building.hire_members:
        # 办公室固定 1 个进驻位（游戏机制）
        empty = max(0, 1 - len(building.hire_members))
        hire_members = _member_dicts(building.hire_members)
        hire = {
            "level": building.hire_level,
            "members": hire_members,
            "empty": empty,
            "slots": hire_members + [_empty_slot("空闲") for _ in range(empty)],
        }

    # 全房间成员头像 + 职业图标：受限并发批量挂载（磁盘缓存，失败空串降级）；
    # 资产层故障不拖垮整卡（降级无头像，与既有卡片同口径）
    try:
        from ..gamedata.itemicon import profession_icon_b64
        from ..image import gather_b64

        all_slots = (
            control["slots"]
            + training_slots
            + [
                s
                for room in (
                    manufactures + tradings + powers + dormitories
                )
                for s in room.get("slots", room["members"])
            ]
            + (meeting["slots"] if meeting else [])
            + (hire["slots"] if hire else [])
        )
        all_members = [s for s in all_slots if not s.get("empty") and s.get("char_id")]
        char_ids = list(dict.fromkeys(m["char_id"] for m in all_members))
        avatars = await gather_b64(char_avatar_b64, char_ids)
        avatar_map = dict(zip(char_ids, avatars))
        prof_names = list(
            dict.fromkeys(
                PROFESSION_NAMES[m["profession"]]
                for m in all_members
                if m["profession"] in PROFESSION_NAMES
            )
        )
        icons = await gather_b64(profession_icon_b64, prof_names)
        icon_by_name = dict(zip(prof_names, icons))
        icon_map = {
            code: icon_by_name[name]
            for code, name in PROFESSION_NAMES.items()
            if name in icon_by_name
        }
        for m in all_members:
            m["avatar"] = avatar_map.get(m["char_id"], "")
            m["prof_icon"] = icon_map.get(m["profession"], "")
    except Exception:
        logger.debug("[ArknightsUID·基建] 房间成员资产挂载失败（降级无头像）", exc_info=True)

    recruit_done = recruit_done_count(data.recruit_slots)
    recruit_next = recruit_next_remaining(data.recruit_slots)
    return {
        "name": data.name or "博士",
        "uid": _mask_uid(uid_masked, data.uid),
        "control": control,
        "labor_value": building.labor_value,
        "labor_max": building.labor_max,
        "labor_pct": (
            round(building.labor_value * 100 / building.labor_max)
            if building.labor_max > 0
            else 0
        ),
        "manufactures": manufactures,
        "tradings": tradings,
        "powers": powers,
        "dormitories": dormitories,
        "meeting": meeting,
        "hire": hire,
        "clue_own": building.clue_own,
        "clue_board": building.clue_board,
        "clue_daily_reward": building.clue_daily_reward,
        "training": {
            "state": training_state,
            "level": building.training_level,
            "members": training_members,
            "slots": training_slots,
            "remain": (
                training_status_text(remaining) if training_members else ""
            ),
            "pct": training_progress_pct(
                building,
                base_hours=TRAINING_BASE_HOURS_BY_LEVEL.get(
                    _mastery_target(data, building)[2] or 0
                ),
            ),
        },
        "recruit_total": len(data.recruit_slots),
        "recruit_done": recruit_done,
        # 最早一个未完成槽位的剩余文案（全部空闲/已完成为空串）
        "recruit_next": (
            format_training_remaining(recruit_next) if recruit_next else ""
        ),
        "tired_count": len(building.tired_chars),
        "version": version,
    }


async def build_training_context(
    data: PlayerData,
    building: BuildingData,
    version: str,
    uid_masked: str = "",
) -> dict[str, Any] | None:
    """ark_training.html（训练室专精进度卡）。building 不可用返回 None。

    目标档位链：接口 trainee.targetSkill（技能序号）+ 玩家 chars 该技能
    当前专精 +1 = 目标专精档，基准时长按档取 8/16/24h（游戏机制）；
    技能名经 load_skill_names 反查，任一环节缺失即留空降级（不臆造）。
    """
    if not building.available:
        return None
    trainee_id = building.training_trainee_id
    remaining = training_remaining_secs(building)
    char, mastery_from, mastery_to = _mastery_target(data, building)
    rarity = char.rarity_display if char is not None else 0
    target_idx = building.training_target_skill
    skill_label = f"{target_idx + 1}技能" if 0 <= target_idx <= 2 else ""
    skill_name = ""
    if char is not None and 0 <= target_idx < len(char.skills):
        try:
            from ..gamedata.charinfo import load_skill_names

            skill_name = (await load_skill_names()).get(
                char.skills[target_idx].skill_id, ""
            )
        except Exception:
            skill_name = ""
    base_hours = TRAINING_BASE_HOURS_BY_LEVEL.get(mastery_to or 0)
    portrait = await char_portrait_b64(trainee_id, rarity) if trainee_id else ""
    avatar = await char_avatar_b64(trainee_id) if trainee_id else ""
    trainer_avatar = (
        await char_avatar_b64(building.training_trainer_id)
        if building.training_trainer_id
        else ""
    )
    finish_at = ""
    if remaining > 0:
        finish_at = beijing_datetime(time.time() + remaining).strftime(
            "%m-%d %H:%M"
        )
    if not trainee_id or remaining < 0:
        # remaining=-1 是接口哨兵（空闲/未知）：trainee 只是残留指派，
        # 不能当"受训中"渲染（空闲期的常态查询此前全部误标）
        state2, state_en = "空闲", "IDLE"
    elif remaining == 0:
        state2, state_en = "已完成", "DONE"
    else:
        state2, state_en = "受训中", "TRAINING"
    return {
        "name": data.name or "博士",
        "uid": _mask_uid(uid_masked, data.uid),
        "trainee": building.training_trainee,
        "portrait": portrait,
        "avatar": avatar,
        "skill_name": skill_name,
        "skill_label": skill_label,
        "mastery_from": mastery_from,
        "mastery_to": mastery_to,
        "trainer": building.training_trainer,
        "trainer_avatar": trainer_avatar,
        "state": state2,
        "state_en": state_en,
        "remain_text": (
            format_training_remaining(remaining)
            if trainee_id and remaining >= 0
            else ""
        ),
        "finish_at": finish_at,
        "pct": (
            training_progress_pct(building, base_hours=base_hours)
            if trainee_id
            else None
        ),
        "base_exact": base_hours is not None,
        # speed 缺字段时为 0.0，与进度条口径（<=0 按 1.0x 折算）一致化，
        # 否则卡片显示"0.0x（无加成）"而进度按 1.0x 算，自相矛盾
        "speed": (building.training_speed if building.training_speed > 0 else 1.0),
        "version": version,
    }


async def build_char_card_context(
    entry: CharEntry, version: str, skill_effects: dict[str, dict] | None = None
) -> dict[str, Any]:
    """ark_char_card.html（#5 单干员卡）。立绘/头像/技能图标走 hycdn 下载缓存。

    skill_effects：技能名 → {current, final}，两者均为 utils.skillfx 的
    等级上下文 {level, description, sp_init, sp_cost, duration, meta}
    （PRTS 逐级描述 + 初动/消耗/持续，由调用方从 wiki 层取；缺省只展示
    专精点）。
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
        current = (effects.get(name) or {}).get("current") or {}
        final = (effects.get(name) or {}).get("final") or {}
        skills.append(
            {
                "icon": icon_by_id.get(skill.skill_id, ""),
                "name": name,
                "specialize": skill.specialize_level,
                "current": current.get("description", ""),
                "current_level": current.get("level", ""),
                "current_meta": current.get("meta", ""),
                "final": final.get("description", ""),
                "final_level": final.get("level", ""),
                "final_meta": final.get("meta", ""),
            }
        )
    equip_table = None
    if any(eq.equip_id for eq in entry.equips):
        from ..gamedata._table_cache import ensure_uniequip_table

        equip_table = await ensure_uniequip_table()
    equips = [
        {
            "name": equip_display_name(
                eq.equip_id, path(equip_table, "equipDict", eq.equip_id)
            ),
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


async def build_skin_context(
    data: PlayerData, version: str, uid_masked: str = "", max_rows: int = 12
) -> dict[str, Any] | None:
    """ark_skin.html（皮肤收集卡）。目录不可用返回 None（调用方回退文本）。

    - 目录：gamedata/skins.load_skin_index（skin_table × 干员索引）
    - 持有：player_info 的 owned_skin_ids
    - 最近获得：raw skins 条目按 ts 倒序前 8，皮肤头像走 hycdn
    """
    from ..gamedata.charinfo import load_char_index
    from ..gamedata.skins import load_skin_index, summarize_collection

    catalog = await load_skin_index()
    if not catalog:
        return None
    char_index = await load_char_index() or {}
    char_names = {cid: str(info.get("name") or cid) for cid, info in char_index.items()}
    summary = summarize_collection(
        catalog, data.owned_skin_ids, char_names, max_rows=max_rows
    )

    # 最近获得（ts 倒序前 8）：raw skins 的 {id, ts}
    recent: list[dict[str, Any]] = []
    raw_skins = data.raw.get("skins")
    if isinstance(raw_skins, list):
        entries = sorted(
            (
                {"id": str(s.get("id") or ""), "ts": int(s.get("ts") or 0)}
                for s in raw_skins
                if isinstance(s, dict) and s.get("id")
            ),
            key=lambda item: -item["ts"],
        )
        for entry in entries[:8]:
            info = catalog.get(entry["id"])
            recent.append(
                {
                    "skin_id": entry["id"],
                    "name": info["name"] if info else entry["id"],
                    "group": info["group"] if info else "",
                    "char": (
                        char_names.get(info["char_id"], info["char_id"]) if info else ""
                    ),
                    "ts": entry["ts"],
                    "avatar": await _skin_avatar_b64(entry["id"]),
                }
            )

    return {
        "name": data.name or "博士",
        "uid": _mask_uid(uid_masked, data.uid),
        "total": summary["total"],
        "owned": summary["owned"],
        "pct": summary["pct"],
        "missing_count": summary["missing_count"],
        "missing": summary["missing"],
        "extra_owned": summary["extra_owned"],
        "recent": recent,
        "version": version,
    }


async def _skin_avatar_b64(skin_id: str) -> str:
    """皮肤头像（hycdn char_skin/avatar，@→%40/#→%23）；失败空串。"""
    if not skin_id:
        return ""
    try:
        from ..render.render_utils import get_image_b64_with_cache
        from ..path import BAKE_PATH

        url = skin_avatar_url(skin_id)
        return await get_image_b64_with_cache(url, BAKE_PATH / "skin_avatars")
    except Exception:
        return ""
