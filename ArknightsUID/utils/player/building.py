"""player_info 的 building（基建）解析层。

字段结构一手证据：ProbiusOfficial/Skland_API dump.json 的 data.building：
- labor: {value, maxValue, ...}            无人机
- control: {level, chars}                  控制中枢（level = 基建等级）
- manufactures: [{level, chars, formulaId, speed, ...}]      制造站
- tradings: [{level, chars, strategy, stock[], stockLimit}] 贸易站
- powers: [{level, chars}]                 发电站
- dormitories: [{level, chars, comfort}]   宿舍（comfort = 氛围值）
- meeting: {level, chars, clue: {own, board[], dailyReward}} 会客室（线索）
- training: {level, trainee, speed}        训练室
- manufactureFormulaInfoMap（data 顶层）: {formulaId → {itemId, count, ...}}

心情（morale）计量单位（dump 实测推断，双重证据）：
- chars[].ap 为细粒度内部值，满心情 = 8640000（宿舍干员全部为该哨兵值）
- 1 显示点 = 360000，显示上限 24 点；心情点 = ap / 360000
- 会客室/人力办公室的 ap=8640000 哨兵与宿舍一致（不消耗心情的房间满值）

展示口径（宁缺毋滥）：complete/remain/weight 等库存中间量语义未验证，不展示；
workTime（累计工时）同理。低心情干员从四个工作房间（制造/贸易/发电/控制）
聚合，宿舍（休息回复）不计入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..dict_utils import path

# 满心情哨兵/换算（见模块头注释）
MORALE_FULL_AP = 8640000
MORALE_PER_POINT = 360000
MORALE_MAX_POINTS = 24
# 低于该心情点视为待换班（游戏内半心情以下效率受损的通行口径）
MORALE_LOW_THRESHOLD = 12.0

# 贸易站订单策略（客观游戏事实；2026-08-15 真实凭据 实测出现 O_DIAMOND）
TRADING_STRATEGY_NAMES = {
    "O_GOLD": "龙门币",
    "O_ORUNDUM": "合成玉",
    "O_DIAMOND": "源石",
}

# 会客室线索派系代码 → 中文名（客观游戏数据；未知代码回退原文。
# 代码拼写以 2026-08-15 真实凭据 实测为准：URSUS 非 URASUS）
CLUE_FACTION_NAMES = {
    "RHODES": "罗德岛制药",
    "PENGUIN": "企鹅物流",
    "BLACKSTEEL": "黑钢国际",
    "URSUS": "乌萨斯学生自治团",
    "GLASGOW": "格拉斯哥",
    "KJERAG": "喀兰贸易",
    "RHINE": "莱茵生命",
    "SAMI": "萨米",
    "VICTORIA": "维多利亚",
    "LEITHANIA": "莱塔尼亚",
    "SIRACUSA": "叙拉古",
    "KAZIMIERZ": "卡西米尔",
    "IBERIA": "伊比利亚",
    "LATERO": "拉特兰",
}


def _as_int(data: Any, default: int = 0) -> int:
    if isinstance(data, bool):
        return default
    if isinstance(data, (int, float)):
        return int(data)
    return default


def _as_float(data: Any, default: float = 0.0) -> float:
    if isinstance(data, bool):
        return default
    if isinstance(data, (int, float)):
        return float(data)
    return default


def morale_points(ap: Any) -> float:
    """内部 ap 值 → 心情点（0-24，一位小数）。非法/负值按 0 处理。"""
    value = _as_float(ap, -1.0)
    if value < 0:
        return 0.0
    return round(min(value / MORALE_PER_POINT, MORALE_MAX_POINTS), 1)


@dataclass(slots=True)
class RoomMember:
    char_id: str
    name: str  # charInfoMap 解析名，缺失回退 char_id
    morale: float  # 心情点 0-24


@dataclass(slots=True)
class ManufactureRoom:
    slot_id: str
    level: int
    members: list[RoomMember] = field(default_factory=list)
    formula_id: str = ""
    item_id: str = ""  # 制造产物 itemId（item_table 反查名）
    item_count: int = 0  # 每批产量
    speed: float = 0.0  # 效率倍率（1.0 = 100%）


@dataclass(slots=True)
class TradingRoom:
    slot_id: str
    level: int
    members: list[RoomMember] = field(default_factory=list)
    strategy: str = ""  # O_GOLD / O_ORUNDUM
    stock_count: int = 0  # 待交付订单数
    stock_limit: int = 0


@dataclass(slots=True)
class PowerRoom:
    slot_id: str
    level: int
    members: list[RoomMember] = field(default_factory=list)


@dataclass(slots=True)
class DormitoryRoom:
    slot_id: str
    level: int
    comfort: int  # 氛围值（满值 5000）
    members: list[RoomMember] = field(default_factory=list)


@dataclass(slots=True)
class TiredChar:
    char_id: str
    name: str
    morale: float
    room: str  # 房间中文名（"制造站 2" 风格）


@dataclass(slots=True)
class BuildingData:
    control_level: int = 0
    labor_value: int = 0
    labor_max: int = 0
    manufactures: list[ManufactureRoom] = field(default_factory=list)
    tradings: list[TradingRoom] = field(default_factory=list)
    powers: list[PowerRoom] = field(default_factory=list)
    dormitories: list[DormitoryRoom] = field(default_factory=list)
    clue_own: int = 0
    clue_board: list[str] = field(default_factory=list)  # 派系中文名
    clue_daily_reward: bool = False
    training_speed: float = 0.0
    training_trainee: str = ""  # 训练中干员名（空 = 未在训练）
    tired_chars: list[TiredChar] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.control_level > 0 or bool(
            self.manufactures or self.tradings or self.powers
        )


def _parse_members(raw: Any, info_map: dict[str, Any]) -> list[RoomMember]:
    members: list[RoomMember] = []
    if not isinstance(raw, list):
        return members
    for item in raw:
        if not isinstance(item, dict):
            continue
        char_id = str(item.get("charId") or "")
        if not char_id:
            continue
        info = info_map.get(char_id)
        name = str(path(info, "name") or "") if isinstance(info, dict) else ""
        members.append(
            RoomMember(char_id=char_id, name=name or char_id, morale=morale_points(item.get("ap")))
        )
    return members


def _room_label(kind: str, seq: int) -> str:
    return f"{kind}{seq}" if seq > 1 else kind


def parse_building(data: "PlayerData") -> BuildingData | None:
    """从 PlayerData.raw 解析基建。building 缺失或完全无法识别返回 None。"""
    building = path(data.raw, "building")
    if not isinstance(building, dict):
        return None
    info_map = path(data.raw, "charInfoMap")
    if not isinstance(info_map, dict):
        info_map = {}
    formula_map = path(data.raw, "manufactureFormulaInfoMap")
    if not isinstance(formula_map, dict):
        formula_map = {}

    result = BuildingData(
        control_level=_as_int(path(building, "control", "level")),
        labor_value=_as_int(path(building, "labor", "value")),
        labor_max=_as_int(path(building, "labor", "maxValue")),
    )

    for index, room in enumerate(building.get("manufactures") or [], start=1):
        if not isinstance(room, dict):
            continue
        formula_id = str(room.get("formulaId") or "")
        formula = formula_map.get(formula_id)
        item_id = str(path(formula, "itemId") or "") if isinstance(formula, dict) else ""
        result.manufactures.append(
            ManufactureRoom(
                slot_id=str(room.get("slotId") or ""),
                level=_as_int(room.get("level")),
                members=_parse_members(room.get("chars"), info_map),
                formula_id=formula_id,
                item_id=item_id,
                item_count=_as_int(path(formula, "count")),
                speed=_as_float(room.get("speed")),
            )
        )

    for room in building.get("tradings") or []:
        if not isinstance(room, dict):
            continue
        stock = room.get("stock")
        result.tradings.append(
            TradingRoom(
                slot_id=str(room.get("slotId") or ""),
                level=_as_int(room.get("level")),
                members=_parse_members(room.get("chars"), info_map),
                strategy=str(room.get("strategy") or ""),
                stock_count=len(stock) if isinstance(stock, list) else 0,
                stock_limit=_as_int(room.get("stockLimit")),
            )
        )

    for room in building.get("powers") or []:
        if not isinstance(room, dict):
            continue
        result.powers.append(
            PowerRoom(
                slot_id=str(room.get("slotId") or ""),
                level=_as_int(room.get("level")),
                members=_parse_members(room.get("chars"), info_map),
            )
        )

    for room in building.get("dormitories") or []:
        if not isinstance(room, dict):
            continue
        result.dormitories.append(
            DormitoryRoom(
                slot_id=str(room.get("slotId") or ""),
                level=_as_int(room.get("level")),
                comfort=_as_int(room.get("comfort")),
                members=_parse_members(room.get("chars"), info_map),
            )
        )

    clue = path(building, "meeting", "clue")
    if isinstance(clue, dict):
        result.clue_own = _as_int(clue.get("own"))
        result.clue_daily_reward = bool(clue.get("dailyReward"))
        board: list[str] = []
        for code in clue.get("board") or []:
            board.append(CLUE_FACTION_NAMES.get(str(code), str(code)))
        result.clue_board = board

    training = building.get("training")
    if isinstance(training, dict):
        result.training_speed = _as_float(training.get("speed"))
        trainee = training.get("trainee")
        trainee_id = str(path(trainee, "charId") or "") if isinstance(trainee, dict) else ""
        if trainee_id:
            info = info_map.get(trainee_id)
            result.training_trainee = (
                str(path(info, "name") or "") if isinstance(info, dict) else ""
            ) or trainee_id

    # 低心情干员：从工作房间聚合（宿舍休息不计），按心情升序
    tired: list[TiredChar] = []
    for kind, rooms in (
        ("控制中枢", [(path(building, "control") or {})]),
        ("制造站", building.get("manufactures") or []),
        ("贸易站", building.get("tradings") or []),
        ("发电站", building.get("powers") or []),
    ):
        for index, room in enumerate(rooms, start=1):
            if not isinstance(room, dict):
                continue
            label = _room_label(kind, index) if kind != "控制中枢" else kind
            for member in _parse_members(room.get("chars"), info_map):
                if member.morale < MORALE_LOW_THRESHOLD:
                    tired.append(
                        TiredChar(
                            char_id=member.char_id,
                            name=member.name,
                            morale=member.morale,
                            room=label,
                        )
                    )
    tired.sort(key=lambda item: item.morale)
    result.tired_chars = tired
    return result if result.available else None
