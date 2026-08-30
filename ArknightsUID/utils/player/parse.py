"""player_info 富数据解析层（P1）。

把 `game/player/info` 的完整 dump（1w+ 行 JSON）解析成 dataclass，
供各卡片 context builder 消费。字段名以真实证据为准：
- status/chars 键名：ProbiusOfficial/Skland_API 仓库 dump.json（一手 dump）
- charInfoMap/routine/campaign 键名：gxy12345/arknights-plugin apps/{char_stat,profile}.js
  （工作实现；字段映射属客观接口事实，允许参考）

与早期文档记录的两处出入（以本文件为准）：
- 干员潜能是 `potentialRank`（非 potential）、信赖是 `favorPercent`（非 favor）
- 昵称是 `status.name`（rogue 模块旧代码用 nickName，解析时两者兼容，name 优先）

已知展示规则（客观游戏事实）：
- charInfoMap.rarity 取值 0-5，展示时 +1 转人读 1-6 星（rarity=6 是升变近卫阿米娅）
- 阿米娅三形态同名校验：charId 后缀区分职业，展示名追加职业后缀
- 当前理智 = ap.max - ceil((completeRecoveryTime - now) / 360)（每 6 分钟回 1 点）
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

from ..dict_utils import first_path, path, value

# 职业代码 → 中文（8 职业，客观游戏数据）
PROFESSION_NAMES = {
    "PIONEER": "先锋",
    "WARRIOR": "近卫",
    "SNIPER": "狙击",
    "TANK": "重装",
    "MEDIC": "医疗",
    "CASTER": "术师",
    "SUPPORT": "辅助",
    "SPECIAL": "特种",
}
# 练度统计卡的职业展示顺序（先锋→特种）
PROFESSION_ORDER = [
    "PIONEER",
    "WARRIOR",
    "SNIPER",
    "TANK",
    "MEDIC",
    "CASTER",
    "SUPPORT",
    "SPECIAL",
]

# 各稀有度 [E0, E1, E2] 等级上限（客观游戏数据；1-2 星无 E2）
_MAX_LEVEL_BY_RARITY = {
    6: (50, 80, 90),
    5: (50, 70, 80),
    4: (45, 60, 70),
    3: (40, 55, 60),
    2: (30, 30, 30),
    1: (30, 30, 30),
}

# 阿米娅三形态（同一 name，靠 charId 区分职业）
_AMIYA_PROFESSION = {
    "char_002_amiya": "CASTER",
    "char_1001_amiya2": "WARRIOR",
    "char_1037_amiya3": "MEDIC",
}

# 剿灭单图击杀上限（客观游戏规则，所有常驻剿灭图 400 击杀）
CAMPAIGN_STAGE_KILL_CAP = 400


def _as_int(data: Any, default: int = 0) -> int:
    if isinstance(data, bool):
        return default
    if isinstance(data, (int, float)):
        return int(data)
    return default


@dataclass(slots=True)
class SkillState:
    skill_id: str
    specialize_level: int  # 0-3（专精）


@dataclass(slots=True)
class EquipState:
    equip_id: str
    level: int  # 模组阶段 1-3


@dataclass(slots=True)
class CharEntry:
    char_id: str
    skin_id: str = ""
    level: int = 0
    evolve_phase: int = 0  # 0-2
    potential_rank: int = 0  # 0-6
    main_skill_lvl: int = 0  # 1-7
    favor_percent: int = 0  # 0-200
    default_equip_id: str = ""
    skills: list[SkillState] = field(default_factory=list)
    equips: list[EquipState] = field(default_factory=list)
    # 来自 charInfoMap 的元数据（缺失时留空）
    name: str = ""
    profession: str = ""  # 职业代码（PIONEER...）
    sub_prof: str = ""
    rarity: int = 0  # 0-5（原始值，展示时 +1）

    @property
    def profession_name(self) -> str:
        prof = self.profession or _AMIYA_PROFESSION.get(self.char_id, "")
        return PROFESSION_NAMES.get(prof, prof)

    @property
    def display_name(self) -> str:
        """展示名：阿米娅三形态追加职业后缀，其余用 charInfoMap 名。"""
        base = self.name or self.char_id
        prof = _AMIYA_PROFESSION.get(self.char_id)
        if prof:
            return f"{base}({PROFESSION_NAMES.get(prof, prof)})"
        return base

    @property
    def rarity_display(self) -> int:
        """人读星级 1-6（原始 0-5 加一）。"""
        return min(self.rarity + 1, 6)

    def max_level(self) -> int:
        """当前精英化阶段的等级上限。"""
        table = _MAX_LEVEL_BY_RARITY.get(self.rarity_display)
        if table is None:
            return 30
        return table[min(self.evolve_phase, len(table) - 1)]


@dataclass(slots=True)
class RecruitSlot:
    """公招标位（data.recruit，dump 一手证据：{startTs, finishTs, state}）。

    finishTs > 0 表示招募中/已完成（startTs/finishTs 均为 -1 是空槽）；
    state 原始码语义未验证（空槽样本为 1），不参与判定——完成判定只用
    finishTs 与当前时间比较。
    """

    start_ts: int = -1
    finish_ts: int = -1
    state: int = 0

    @property
    def recruiting(self) -> bool:
        return self.finish_ts > 0

    def remaining_secs(self, now: float | None = None) -> int:
        """剩余秒：-1 = 空槽/未知，0 = 已到点可领取，>0 = 招募中剩余。"""
        if not self.recruiting:
            return -1
        now = time.time() if now is None else now
        return max(0, int(self.finish_ts - now))


def recruit_done_count(slots: list[RecruitSlot], now: float | None = None) -> int:
    """已到点可领取的公招槽数。"""
    return sum(
        1 for slot in slots if slot.recruiting and slot.remaining_secs(now) == 0
    )


def recruit_next_remaining(
    slots: list[RecruitSlot], now: float | None = None
) -> int | None:
    """最早一个未完成槽位的剩余秒（全部空闲/已完成返回 None）。"""
    pendings = [
        slot.remaining_secs(now)
        for slot in slots
        if slot.recruiting and slot.remaining_secs(now) > 0
    ]
    return min(pendings) if pendings else None


@dataclass(slots=True)
class PlayerData:
    name: str = ""
    uid: str = ""
    level: int = 0
    main_stage_progress: str = ""
    register_ts: int = 0
    char_cnt: int = 0  # status.charCnt（0 表示缺失，回退 len(chars)）
    skin_cnt: int = 0
    # None = 接口未提供该字段（走恢复时间/回满回退）；0 = 真的 0 理智
    ap_current: int | None = None
    ap_max: int = 0
    ap_complete_recovery_time: int = 0
    daily_done: int = 0
    daily_total: int = 0
    weekly_done: int = 0
    weekly_total: int = 0
    campaign_current: int = 0
    campaign_total: int = 0
    campaign_records: dict[str, int] = field(default_factory=dict)
    chars: list[CharEntry] = field(default_factory=list)
    recruit_slots: list[RecruitSlot] = field(default_factory=list)
    owned_skin_ids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def chars_total(self) -> int:
        return self.char_cnt if self.char_cnt > 0 else len(self.chars)

    def register_days(self, now: float | None = None) -> int:
        if self.register_ts <= 0:
            return 0
        now = time.time() if now is None else now
        return max(1, int((now - self.register_ts) / 86400) + 1)


def compute_ap_current(ap_max: int, complete_recovery_time: int, now: float | None = None) -> int:
    """按恢复公式推算当前理智：每 360 秒回 1 点，回满时间戳倒推。"""
    if complete_recovery_time <= 0:
        return ap_max
    now = time.time() if now is None else now
    remain = math.ceil((complete_recovery_time - now) / 360)
    if remain <= 0:
        return ap_max
    return max(0, ap_max - remain)


def _parse_skills(raw: Any) -> list[SkillState]:
    skills: list[SkillState] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            skills.append(
                SkillState(
                    skill_id=str(item.get("id") or ""),
                    specialize_level=_as_int(item.get("specializeLevel")),
                )
            )
    return skills


def _parse_equips(raw: Any) -> list[EquipState]:
    equips: list[EquipState] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            equips.append(
                EquipState(
                    equip_id=str(item.get("id") or ""),
                    level=_as_int(item.get("level")),
                )
            )
    return equips


def _parse_char(raw: dict[str, Any], char_info: dict[str, Any] | None) -> CharEntry:
    info = char_info if isinstance(char_info, dict) else {}
    return CharEntry(
        char_id=str(raw.get("charId") or ""),
        skin_id=str(raw.get("skinId") or ""),
        level=_as_int(raw.get("level")),
        evolve_phase=_as_int(raw.get("evolvePhase")),
        potential_rank=_as_int(raw.get("potentialRank")),
        main_skill_lvl=_as_int(raw.get("mainSkillLvl")),
        favor_percent=_as_int(raw.get("favorPercent")),
        default_equip_id=str(raw.get("defaultEquipId") or ""),
        skills=_parse_skills(raw.get("skills")),
        equips=_parse_equips(raw.get("equip")),
        name=str(info.get("name") or ""),
        profession=str(info.get("profession") or ""),
        sub_prof=str(info.get("subProf") or ""),
        rarity=_as_int(info.get("rarity")),
    )


def _data_of(response: Any) -> dict[str, Any] | None:
    """接受完整信封 {code, data} 或裸 data dict；无效返回 None。"""
    if not isinstance(response, dict):
        return None
    data = value(response, "data")
    if isinstance(data, dict) and data:
        return data
    # 裸 data（无信封）直接就是玩家数据
    if isinstance(response.get("chars"), list) or isinstance(response.get("status"), dict):
        return response
    return None


def parse_player_info(response: Any) -> PlayerData | None:
    """解析 player_info 响应。信封 code != 0 或结构无法识别时返回 None。"""
    if not isinstance(response, dict):
        return None
    code = value(response, "code")
    if code not in (None, 0):
        return None
    data = _data_of(response)
    if data is None:
        return None

    # 结构守卫（审计 #26e）：异常信封里这些字段可能是列表/字符串，
    # truthy 非 dict 会让后续 .get() 抛异常拖垮命令
    status = path(data, "status")
    status = status if isinstance(status, dict) else {}
    routine = path(data, "routine")
    routine = routine if isinstance(routine, dict) else {}
    campaign = path(data, "campaign")
    campaign = campaign if isinstance(campaign, dict) else {}
    ap = status.get("ap")
    ap = ap if isinstance(ap, dict) else {}

    info_map = path(data, "charInfoMap") or {}
    chars: list[CharEntry] = []
    for raw_char in data.get("chars") or []:
        if isinstance(raw_char, dict) and raw_char.get("charId"):
            chars.append(_parse_char(raw_char, info_map.get(str(raw_char.get("charId")))))

    records = campaign.get("records")
    if not isinstance(records, dict):
        records = {}

    recruit_slots: list[RecruitSlot] = []
    for slot in data.get("recruit") or []:
        if isinstance(slot, dict):
            recruit_slots.append(
                RecruitSlot(
                    start_ts=_as_int(slot.get("startTs"), -1),
                    finish_ts=_as_int(slot.get("finishTs"), -1),
                    state=_as_int(slot.get("state")),
                )
            )

    owned_skin_ids: list[str] = []
    for skin in data.get("skins") or []:
        if not isinstance(skin, dict):
            continue
        # Skland dump 实测为 {id, ts}；部分响应/适配层用 skinId（审计 #25
        # 防御性兼容：两种键名都认，避免皮肤收集静默清零）
        skin_id = skin.get("id") or skin.get("skinId")
        if skin_id:
            owned_skin_ids.append(str(skin_id))

    return PlayerData(
        name=str(first_path(status, (("name",), ("nickName",))) or ""),
        uid=str(status.get("uid") or ""),
        level=_as_int(status.get("level")),
        main_stage_progress=str(status.get("mainStageProgress") or ""),
        register_ts=_as_int(status.get("registerTs")),
        char_cnt=_as_int(status.get("charCnt")),
        skin_cnt=_as_int(status.get("skinCnt")),
        ap_current=_as_int_or_none(first_path(ap, (("current",),))),
        ap_max=_as_int(ap.get("max")),
        ap_complete_recovery_time=_as_int(ap.get("completeRecoveryTime")),
        daily_done=_as_int(path(routine, "daily", "current")),
        daily_total=_as_int(path(routine, "daily", "total")),
        weekly_done=_as_int(path(routine, "weekly", "current")),
        weekly_total=_as_int(path(routine, "weekly", "total")),
        campaign_current=_as_int(path(campaign, "reward", "current")),
        campaign_total=_as_int(path(campaign, "reward", "total")),
        campaign_records={
            str(k): _as_int(v) for k, v in records.items() if v is not None
        },
        chars=chars,
        recruit_slots=recruit_slots,
        owned_skin_ids=owned_skin_ids,
        raw=data,
    )


def _as_int_or_none(raw) -> int | None:
    """_as_int 的缺失保留版：None/不可解析 → None（0 原样返回）。"""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def effective_ap(data: PlayerData, now: float | None = None) -> int:
    """当前理智：优先用实时公式，无恢复时间戳时退回 dump 里的 current。"""
    if data.ap_max <= 0:
        return 0
    if data.ap_complete_recovery_time > 0:
        return compute_ap_current(data.ap_max, data.ap_complete_recovery_time, now)
    if data.ap_current is not None:
        # 字段存在即信接口值——0 理智是真 0，不再当"缺失"回退成满理智（审计 #29）
        return min(max(0, data.ap_current), data.ap_max)
    return data.ap_max
