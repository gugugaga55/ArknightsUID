"""肉鸽战绩（森空岛 App「战绩查询」页）解析器。

响应结构参考社区 nonebot-plugin-skland 的 RogueData schema（生产实测）：
data.topics     主题列表（id=rogue_1..rogue_6，isSelected 标记当前期，
                allOpenTs 全服数据开放时间戳）
data.history    当期战绩（mode/modeGrade 最高达成、score 最高分、bpLevel 声望、
                medal 奖章、chars 常用干员、records 最近战绩、favourRecords 收藏）
data.career     个人生涯累计（game/node/relic/invest + clearInfo 最高达成）
data.gameUserInfo  玩家名/等级/头像
data.all        全服数据（App「全服数据」页，rogue_2 起开放；开放判定 =
                currentTs >= 选中主题 allOpenTs 且 all 非空）：
                endings {通关结局数: 玩家比例%}（按值顺序，App 端 Ky 组件
                逐值渲染「通关 N 个结局」比例条）
                activeTool [{id,name}] 最常用战术道具 top3
                relic [{id,name}]     最常用收藏品 top5
                character {职业: {id,name,rarity,profession}} 分职业出场率最高干员
                （另有 persuade/dice/candle 等每期专属趣味计数，结构逐期不同，
                不纳入稳定契约）
data.currentTs  服务器当前时间戳（秒；allOpenTs 判定用）
单条战绩（Record）：mode/modeGrade 难度、success 通关、score 得分、band 分队、
lastStage 止步关卡、endingText 结局、startTs/endTs 起止时间戳（秒，字符串）、
lastChars 结算阵容。

字段口径以社区 schema 为准（端点经 tools/real_smoke.py 的 rogue probe
真实凭据验证 code=0；字段级 dump 校对未做），解析全程防御性降级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import _to_int

__all__ = [
    "TOPIC_IDS",
    "TOPIC_SHORT_NAMES",
    "RogueRunChar",
    "RogueRun",
    "RogueTopicStats",
    "topic_id_for",
    "parse_rogue_records",
    "format_run_ts",
]

# 期目录 → 战绩端点 topicId / 主题表键 → 芯片短名：收敛至
# gamedata.rogue_registry（唯一事实源）。主题表从傀影起编
# （rogue_1=傀影 … rogue_6=黑流树海），与社区 nonebot-plugin-skland
# Topics._MAPPING 一致；灰蕈迷境不在战绩页口径内。
from ..gamedata.rogue_registry import (
    THEME_TABLE_IDS,
    TOPIC_SHORT_NAMES,
)

# 语义别名：战绩端点 topicId 即主题表键
TOPIC_IDS = THEME_TABLE_IDS


def topic_id_for(theme_dir: str) -> str:
    """期目录 → topicId（未知目录回退原样串，由端点自行报错）。"""
    return TOPIC_IDS.get(theme_dir, theme_dir)


def _to_str(value: Any) -> str:
    return "" if value is None else str(value)


def format_run_ts(value: Any) -> str:
    """战绩时间戳 → 'MM-DD HH:MM'（秒级 unix 字符串；非数字原样截断返回）。"""
    text = _to_str(value).strip()
    if not text:
        return ""
    if text.isdigit():
        from datetime import datetime

        try:
            return datetime.fromtimestamp(int(text)).strftime("%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            return ""
    return text[:16]


@dataclass(slots=True)
class RogueRunChar:
    char_id: str = ""
    name: str = ""
    rarity: int = 0  # 0-5（6★=5）
    profession: str = ""
    evolve_phase: int = 0
    level: int = 0
    is_candle: bool = False  # 烛火招募（界园/树海机制）


@dataclass(slots=True)
class RogueRun:
    run_id: str = ""
    mode: str = ""  # 难度模式名（如 标准/畅游，以接口为准）
    mode_grade: int = 0  # 难度等级数字
    success: bool = False
    score: int = 0
    band: str = ""  # 分队名
    last_stage: str = ""  # 止步关卡（失败时语义为倒在了哪）
    ending_text: str = ""  # 结局文本（通关时非空）
    start_ts: str = ""
    end_ts: str = ""
    is_collect: bool = False  # 玩家收藏标记
    zones: int = 0  # 抵达层数（cntCrossedZone）
    battle_normal: int = 0  # 普通作战场次
    battle_elite: int = 0  # 精英/紧急作战场次
    battle_boss: int = 0  # 领袖作战场次
    relic_count: int = 0  # 本局获得藏品数（cntGainRelicItem）
    recruit_count: int = 0  # 招募/进阶次数（cntRecruitUpgrade）
    arrived_nodes: int = 0  # 经过节点数（cntArrivedNode）
    seed: str = ""  # 种子号（界园起，用于复盘/分享）
    tags: list[str] = field(default_factory=list)  # 局内标签名（月度/深入调查等）
    chars: list[RogueRunChar] = field(default_factory=list)  # 结算阵容
    init_chars: list[RogueRunChar] = field(default_factory=list)  # 开局干员
    troop_chars: list[RogueRunChar] = field(default_factory=list)  # 招募干员（全程）
    relic_ids: list[str] = field(default_factory=list)  # 获得藏品 id（gainRelicList）
    ending_segments: list[dict] = field(
        default_factory=list
    )  # 结局回顾分段 [{text,color}]


@dataclass(slots=True)
class RogueTopicStats:
    """单期战绩（data 选定主题的全部展示数据）。"""

    topic_id: str = ""  # 选中期 rogue_N
    topic_name: str = ""  # 选中期官方名（topics[].name，权威）
    topic_pic: str = ""  # 选中期图标 URL（topics[].pic）
    topics: list[dict] = field(default_factory=list)  # 全部主题（id/name/selected）
    player_name: str = ""
    player_level: int = 0
    mode: str = ""  # 历史最高达成难度模式
    mode_grade: int = 0
    score: int = 0  # 历史最高得分
    bp_level: int = 0  # 声望等级
    medal_current: int = 0
    medal_total: int = 0
    # 生涯累计（career 块；字段名按社区 schema，口径以联调 dump 校对为准）
    games: int = 0  # 探索次数
    nodes: int = 0  # 经过节点
    relics: int = 0  # 获得藏品
    invest: int = 0  # 投资系统累计
    endings_total: int = 0  # 已达成结局数（clearInfo.endings 长度）
    fav_chars: list[RogueRunChar] = field(default_factory=list)
    records: list[RogueRun] = field(default_factory=list)
    favour_records: list[RogueRun] = field(default_factory=list)
    item_names: dict[str, str] = field(default_factory=dict)  # itemInfo id → 藏品名
    # 全服数据（App「全服数据」页；rogue_2 起开放，all_open=False 时全空）
    all_open: bool = False  # currentTs >= allOpenTs 且 all 非空
    # (通关结局数, 玩家比例%) 按接口值顺序（App 端逐值渲染比例条）
    all_endings: list[tuple[int, float]] = field(default_factory=list)
    all_tools: list[str] = field(default_factory=list)  # 最常用战术道具 top3
    all_relics: list[str] = field(default_factory=list)  # 最常用收藏品 top5
    all_chars: list[RogueRunChar] = field(
        default_factory=list
    )  # 分职业出场率最高干员


def _parse_char(item: Any) -> RogueRunChar | None:
    if not isinstance(item, dict):
        return None
    char_id = _to_str(item.get("id"))
    name = _to_str(item.get("name"))
    if not char_id and not name:
        return None
    return RogueRunChar(
        char_id=char_id,
        name=name,
        rarity=_to_int(item.get("rarity")),
        profession=_to_str(item.get("profession")),
        evolve_phase=_to_int(item.get("evolvePhase")),
        level=_to_int(item.get("level")),
        is_candle=bool(item.get("isCandle")),
    )


def _parse_ending(item: Any) -> tuple[str, list[dict]]:
    """endingText 可能是 JSON 分段 [{content,color}]（结算回顾），也可能是纯文本。

    返回 (纯文本, 分段列表)；分段仅保留带文字的 {"text","color"}。
    """
    raw = _to_str(item.get("endingText")).strip()
    if not raw:
        return "", []
    if raw.startswith("["):
        segments_out: list[dict] = []
        try:
            import json

            segments = json.loads(raw)
            if isinstance(segments, list):
                for seg in segments:
                    if isinstance(seg, dict) and seg.get("content"):
                        segments_out.append(
                            {
                                "text": str(seg.get("content")),
                                "color": str(seg.get("color") or ""),
                            }
                        )
                if segments_out:
                    return "".join(s["text"] for s in segments_out), segments_out
        except (TypeError, ValueError):
            pass
    return raw, []


def _parse_run(item: Any) -> RogueRun | None:
    if not isinstance(item, dict):
        return None
    band = item.get("band")
    chars = [c for c in (_parse_char(x) for x in item.get("lastChars") or []) if c]
    init_chars = [c for c in (_parse_char(x) for x in item.get("initChars") or []) if c]
    troop_chars = [
        c for c in (_parse_char(x) for x in item.get("troopChars") or []) if c
    ]
    # gainRelicList 项兼容两种形态：{"id": ...} 或裸字符串 id
    relic_ids: list[str] = []
    for x in item.get("gainRelicList") or []:
        if isinstance(x, dict) and x.get("id"):
            relic_ids.append(str(x["id"]))
        elif isinstance(x, str) and x:
            relic_ids.append(x)
    tags = [
        str(t.get("name"))
        for t in item.get("tagList") or []
        if isinstance(t, dict) and t.get("name")
    ]
    ending_text, ending_segments = _parse_ending(item)
    return RogueRun(
        run_id=_to_str(item.get("id")),
        mode=_to_str(item.get("mode")),
        mode_grade=_to_int(item.get("modeGrade")),
        success=bool(_to_int(item.get("success"))),
        score=_to_int(item.get("score")),
        band=_to_str(band.get("name")) if isinstance(band, dict) else "",
        last_stage=_to_str(item.get("lastStage")),
        ending_text=ending_text,
        start_ts=_to_str(item.get("startTs")),
        end_ts=_to_str(item.get("endTs")),
        is_collect=bool(item.get("isCollect")),
        zones=_to_int(item.get("cntCrossedZone")),
        battle_normal=_to_int(item.get("cntBattleNormal")),
        battle_elite=_to_int(item.get("cntBattleElite")),
        battle_boss=_to_int(item.get("cntBattleBoss")),
        relic_count=_to_int(item.get("cntGainRelicItem")),
        recruit_count=_to_int(item.get("cntRecruitUpgrade")),
        arrived_nodes=_to_int(item.get("cntArrivedNode")),
        seed=_to_str(item.get("seed")),
        tags=tags,
        chars=chars,
        init_chars=init_chars,
        troop_chars=troop_chars,
        relic_ids=relic_ids,
        ending_segments=ending_segments,
    )


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_all_block(data: dict, topic_all_open_ts: int) -> dict:
    """解析全服数据块（data.all + 开放判定）→ RogueTopicStats 的 all_* 字段值。

    开放判定对齐 H5 端（record model getBrief）：currentTs >= 选中主题
    allOpenTs 且 all 非空，否则视为未开放（App 端 Tab 不可点）。
    """
    all_block = data.get("all")
    if not isinstance(all_block, dict) or not all_block:
        return {}
    current_ts = _to_int(data.get("currentTs"))
    if topic_all_open_ts and current_ts and current_ts < topic_all_open_ts:
        return {}

    # 通关结局比例：按值顺序逐条（index+1 = 通关结局数），对齐 App Ky 组件
    endings: list[tuple[int, float]] = []
    raw_endings = all_block.get("endings")
    if isinstance(raw_endings, dict):
        for i, value in enumerate(raw_endings.values()):
            pct = _to_float(value)
            if pct > 0:
                endings.append((i + 1, pct))

    def _names(key: str, limit: int) -> list[str]:
        out: list[str] = []
        for item in all_block.get(key) or []:
            if isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
            elif isinstance(item, str) and item:
                out.append(item)
            if len(out) >= limit:
                break
        return out

    # 分职业出场率最高干员：{职业代码: 干员}，保持接口顺序
    # （character 键存在但非 dict 时整体跳过——get 默认值只兜缺键，
    # 拦不住显式 null，先 isinstance 再取 values）
    raw_chars = all_block.get("character")
    chars = [
        c
        for c in (_parse_char(value) for value in raw_chars.values())
        if c
    ] if isinstance(raw_chars, dict) else []
    return {
        "all_open": True,
        "all_endings": endings,
        "all_tools": _names("activeTool", 3),
        "all_relics": _names("relic", 5),
        "all_chars": chars,
    }


def parse_rogue_records(data: Any) -> RogueTopicStats | None:
    """解析战绩端点 data 字段；结构完全缺失（非 dict/无 history）返回 None。"""
    if not isinstance(data, dict) or not data:
        return None
    history = data.get("history")
    if not isinstance(history, dict):
        return None

    stats = RogueTopicStats()
    topics = data.get("topics") or []
    topic_all_open_ts = 0
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        topic_id = _to_str(topic.get("id"))
        stats.topics.append(
            {
                "id": topic_id,
                "name": _to_str(topic.get("name")),
                "short": TOPIC_SHORT_NAMES.get(topic_id, _to_str(topic.get("name"))),
                "selected": bool(topic.get("isSelected")),
            }
        )
        if topic.get("isSelected"):
            stats.topic_id = topic_id
            stats.topic_name = _to_str(topic.get("name"))
            stats.topic_pic = _to_str(topic.get("pic"))
            topic_all_open_ts = _to_int(topic.get("allOpenTs"))

    user = data.get("gameUserInfo")
    if isinstance(user, dict):
        stats.player_name = _to_str(user.get("name"))
        stats.player_level = _to_int(user.get("level"))

    career = data.get("career")
    if isinstance(career, dict):
        stats.games = _to_int(career.get("game"))
        stats.nodes = _to_int(career.get("node"))
        stats.relics = _to_int(career.get("relic"))
        stats.invest = _to_int(career.get("invest"))
        clear_info = career.get("clearInfo")
        if isinstance(clear_info, dict):
            endings = clear_info.get("endings")
            if isinstance(endings, list):
                stats.endings_total = len(endings)

    stats.mode = _to_str(history.get("mode"))
    stats.mode_grade = _to_int(history.get("modeGrade"))
    stats.score = _to_int(history.get("score"))
    stats.bp_level = _to_int(history.get("bpLevel"))
    medal = history.get("medal")
    if isinstance(medal, dict):
        # 与 player_info 的 medal{total,current} 不同名：count=总数
        stats.medal_total = _to_int(medal.get("count"))
        stats.medal_current = _to_int(medal.get("current"))

    stats.fav_chars = [
        c for c in (_parse_char(x) for x in history.get("chars") or []) if c
    ]
    stats.records = [
        r for r in (_parse_run(x) for x in history.get("records") or []) if r
    ]
    stats.favour_records = [
        r for r in (_parse_run(x) for x in history.get("favourRecords") or []) if r
    ]
    item_info = data.get("itemInfo")
    if isinstance(item_info, dict):
        stats.item_names = {
            str(k): str(v.get("name"))
            for k, v in item_info.items()
            if isinstance(v, dict) and v.get("name")
        }
    for field_name, value in _parse_all_block(data, topic_all_open_ts).items():
        setattr(stats, field_name, value)
    return stats
