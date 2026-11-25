"""卫戍协议（stronghold / auto_chess）静态元数据与战绩解析。

官方口径来源：森空岛 App 2.3.0「卫戍协议数据」H5
（game.skland.com/arknights/stronghold，webpack chunk 4086）逆向——

- 端点：GET api/v1/game/arknights/auto-chess?uid=…（见 skland/client.py）
- 小队（盟约）18 支的中文名与图标是 H5 **静态内置**的（不在接口响应里），
  本模块 BANDS 即其转录；图标 CDN 为 bbs.hycdn.cn 公开地址
- 五项玩家统计各带 isMost 全服最值标记，H5 title getter 依此取头衔徽章；
  小队侧的 isMostPass / isFastest 同理（最常用 / 最速）

统计非实时：官方页提示「以下统计非实时数据，将于每日更新一次」。

响应 schema 是从 H5 渲染代码逐字段还原的（首版未经真实凭据联调），
字段口径以 tools/real_smoke.py 的 stronghold_dump.json 校对为准；
解析全部防御式 .get()，上游加字段/改嵌套层级时降级为 0/空而非抛错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_ICON_BASE = "https://bbs.hycdn.cn/public/skland-game/image/arknights/stronghold"


@dataclass(frozen=True, slots=True)
class StrongholdBand:
    """一支小队（盟约）的展示元数据。"""

    id: str  # band_*（接口 gameData.*.teams[].id 同口径）
    name: str  # 中文名（H5 静态内置）
    icon: str  # 图标 CDN（H5 静态内置；character 立绘未转录，卡片用不到）


# 18 支小队，按 H5 内置顺序转录（无业务序，仅保序展示）
BANDS: tuple[StrongholdBand, ...] = (
    StrongholdBand(
        "band_kalts", "天衣无缝",
        f"{_ICON_BASE}/team_icon/38dda202e264326eadfa0d3f388e4b96.png",
    ),
    StrongholdBand(
        "band_kirara", "游戏高手",
        f"{_ICON_BASE}/team_icon/2f2045e02b69a8687e4c74fe03165e0e.png",
    ),
    StrongholdBand(
        "band_humus", "回收利用",
        f"{_ICON_BASE}/team_icon/6f11561333c0101101445435978f1db0.png",
    ),
    StrongholdBand(
        "band_heyak", "内部情报",
        f"{_ICON_BASE}/team_icon/ac0cc74b218d2f0746c35be4468fbaf1.png",
    ),
    StrongholdBand(
        "band_acast", "法术优势",
        f"{_ICON_BASE}/team_icon/48c1886e79597e5ec3b55496bbe10bb0.png",
    ),
    StrongholdBand(
        "band_doberm", "教官团队",
        f"{_ICON_BASE}/team_icon/e2b4169407880a031a5938cf449df101.png",
    ),
    StrongholdBand(
        "band_aguard", "快刀乱麻",
        f"{_ICON_BASE}/team_icon/2942215a617ff905f5a20011dfc89622.png",
    ),
    StrongholdBand(
        "band_logos", "精研技艺",
        f"{_ICON_BASE}/team_icon/de99ff6d7eeeccbb14ef4058547e51e2.png",
    ),
    StrongholdBand(
        "band_fartth", "远程狙击",
        f"{_ICON_BASE}/team_icon/682dda1b4dcb939074220541bbdd9ddb.png",
    ),
    StrongholdBand(
        "band_asnipe", "暴风骤雨",
        f"{_ICON_BASE}/team_icon/50062edd8834599bec61d2047dd0cffd.png",
    ),
    StrongholdBand(
        "band_amiya", "众志成城",
        f"{_ICON_BASE}/team_icon/2f68ca15fcf95da3088d30cc9eef48a2.png",
    ),
    StrongholdBand(
        "band_acfend", "精良装备",
        f"{_ICON_BASE}/team_icon/e87449707e8fbd04cbd73e1989d9d5cb.png",
    ),
    StrongholdBand(
        "band_nearl2", "骑士精神",
        f"{_ICON_BASE}/team_icon/b1b3a73ba949713e605a01eb708b6974.png",
    ),
    StrongholdBand(
        "band_sophia", "名人效应",
        f"{_ICON_BASE}/team_icon/d9140929efaa1531872c600fed79749c.png",
    ),
    StrongholdBand(
        "band_glady", "海底洪流",
        f"{_ICON_BASE}/team_icon/6b02190be7bfc432b3a1d878d00e7ddf.png",
    ),
    StrongholdBand(
        "band_ulpia", "深洋之力",
        f"{_ICON_BASE}/team_icon/61f338714cb4144c5ac7a3ae1eb948f3.png",
    ),
    StrongholdBand(
        "band_svrash", "企业运作",
        f"{_ICON_BASE}/team_icon/b3992a4748eef350f74e18bc71384087.png",
    ),
    StrongholdBand(
        "band_gnosis", "天寒地冻",
        f"{_ICON_BASE}/team_icon/c41820511caa55f24f7820f476e3e522.png",
    ),
)

BAND_INDEX: dict[str, StrongholdBand] = {b.id: b for b in BANDS}


def band_name(band_id: str, strategies: dict[str, Any] | None = None) -> str:
    """小队 id → 中文名；优先从动态 strategies 表读取，其次静态 18 支表，最后回退原 id。"""
    if strategies and band_id in strategies:
        dyn_name = getattr(strategies[band_id], "name", "")
        if dyn_name:
            return str(dyn_name)
    band = BAND_INDEX.get(band_id)
    return band.name if band else band_id


def band_icon(band_id: str, strategies: dict[str, Any] | None = None) -> str:
    """小队 id → 图标 URL；优先从动态 strategies 表读取，未知 id 返回空串。"""
    if strategies and band_id in strategies:
        dyn_icon = getattr(strategies[band_id], "icon", "")
        if dyn_icon:
            return str(dyn_icon)
    band = BAND_INDEX.get(band_id)
    return band.icon if band else ""


# 玩家五项统计：(接口字段, 中文名, 单位, isMost 头衔徽章)。
# 徽章名转录自 H5；BADGE_ORDER 即 H5 title getter 的判定顺序（后者覆盖前者）。
STAT_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("pass", "总通关次数", "次", "功勋显赫"),
    ("shortestRound", "最快通关", "回合", "速通专家"),
    ("upgrade", "升金次数", "次", "升金圣手"),
    ("defeat", "击杀次数", "次", "战果累累"),
    ("gold", "累计消费", "", "挥金如土"),
)
_BADGE_BY_KEY: dict[str, str] = {k: badge for k, _, _, badge in STAT_FIELDS}


def stat_badge(key: str) -> str:
    return _BADGE_BY_KEY.get(key, "")


def badge_title(stats: dict[str, Any]) -> str:
    """按 H5 title getter 口径取头衔：isMost 命中的徽章里取最后一个。"""
    title = ""
    for key, _, _, _badge in STAT_FIELDS:
        entry = stats.get(key)
        if isinstance(entry, dict) and entry.get("isMost"):
            title = _badge
    return title


# 两种协议（gameData 的 key）。官方中文叫法未出现在 H5 字符串里
# （tab 是样式渲染），初版按玩法语义暂拟，待烟测 dump/游戏内口径校对。
KIND_LABELS: dict[str, str] = {
    "start": "开场协议",
    "normal": "常规协议",
}
# 展示顺序（未知 key 追加在尾部队列，避免上游新增模式被吞）
KIND_ORDER: tuple[str, ...] = ("start", "normal")


@dataclass(slots=True)
class StrongholdTeam:
    """一支小队在某协议下的战绩。"""

    id: str = ""  # band_*（BANDS 同口径）
    name: str = ""  # 中文名（元数据解析后回填）
    pass_count: int = 0
    shortest_round: int = 0
    is_most_pass: bool = False
    is_fastest: bool = False
    defeats: list[dict[str, Any]] = field(default_factory=list)
    # [{name, defeat}] 击杀排行（H5 逐条渲染 名次/击杀数）


@dataclass(slots=True)
class StrongholdStats:
    """auto-chess 端点 data 字段的解析结果。"""

    # 玩家信息
    player_name: str = ""  # 原始 "名字#编号"
    player_name_main: str = ""  # # 前主名
    player_name_tag: str = ""  # # 后编号
    player_level: int = 0
    is_official: bool = True  # 官服 True / B服 False
    avatar_url: str = ""  # 森空岛头像（失败时卡片回退无头像）
    # 五项统计 {key: {count, isMost}}（STAT_FIELDS 顺序消费）
    stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    badge: str = ""  # 头衔徽章（badge_title 结果；无 isMost 时为空）
    # 各协议小队战绩 {kind: [StrongholdTeam]}（KIND_ORDER 顺序消费）
    teams: dict[str, list[StrongholdTeam]] = field(default_factory=dict)
    # 升金干员 Top3 [{name, count}]
    char_upgrade: list[dict[str, Any]] = field(default_factory=list)
    # 原始 kinds（含未知 key，供调试/后续新协议兼容）
    raw_kinds: list[str] = field(default_factory=list)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _parse_team(
    raw: Any, strategies: dict[str, Any] | None = None
) -> StrongholdTeam | None:
    if not isinstance(raw, dict):
        return None
    team_id = _to_str(raw.get("id"))
    if not team_id:
        return None
    defeats: list[dict[str, Any]] = []
    for item in raw.get("defeats") or []:
        if not isinstance(item, dict):
            continue
        defeats.append(
            {
                "name": _to_str(item.get("name")),
                "defeat": _to_int(item.get("defeat")),
            }
        )
    return StrongholdTeam(
        id=team_id,
        name=band_name(team_id, strategies),
        pass_count=_to_int(raw.get("passCount")),
        shortest_round=_to_int(raw.get("shortestRound")),
        is_most_pass=bool(raw.get("isMostPass")),
        is_fastest=bool(raw.get("isFastest")),
        defeats=defeats,
    )


def parse_stronghold_info(
    data: Any, strategies: dict[str, Any] | None = None
) -> StrongholdStats | None:
    """解析 auto-chess 端点 data 字段；非 dict / 缺 userInfo 返回 None。"""
    if not isinstance(data, dict):
        return None
    user = data.get("userInfo")
    if not isinstance(user, dict):
        return None

    stats = StrongholdStats()
    raw_name = _to_str(user.get("name"))
    main_name, _, tag = raw_name.partition("#")
    stats.player_name = raw_name
    stats.player_name_main = main_name
    stats.player_name_tag = tag
    stats.player_level = _to_int(user.get("level"))
    stats.is_official = bool(user.get("isOfficial", True))
    avatar = user.get("avatar")
    if isinstance(avatar, dict):
        stats.avatar_url = _to_str(avatar.get("url"))

    for key, _, _, _badge in STAT_FIELDS:
        entry = user.get(key)
        if isinstance(entry, dict):
            stats.stats[key] = {
                "count": _to_int(entry.get("count")),
                "isMost": bool(entry.get("isMost")),
            }
    stats.badge = badge_title(stats.stats)

    game_data = data.get("gameData")
    if isinstance(game_data, dict):
        kinds = list(KIND_ORDER)
        for extra in game_data:
            if extra not in kinds:
                kinds.append(extra)
        stats.raw_kinds = list(game_data)
        for kind in kinds:
            block = game_data.get(kind)
            if not isinstance(block, dict):
                continue
            teams = [
                t
                for t in (
                    _parse_team(item, strategies) for item in block.get("teams") or []
                )
                if t is not None
            ]
            if teams:
                stats.teams[kind] = teams

    for item in data.get("charUpgrade") or []:
        if not isinstance(item, dict):
            continue
        name = _to_str(item.get("name"))
        if name:
            stats.char_upgrade.append(
                {"name": name, "count": _to_int(item.get("count"))}
            )

    return stats
