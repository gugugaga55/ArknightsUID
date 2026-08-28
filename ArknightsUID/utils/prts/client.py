from __future__ import annotations

import re
import json
import time
import calendar
import html
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import field, dataclass
from typing import Any
from urllib.parse import quote

from gsuid_core.logger import logger

try:
    from ArknightsUID.ArknightsUID.utils.http import get_client
    from ArknightsUID.ArknightsUID.utils.disk_cache import (
        cache_read,
        cache_write,
        is_fresh,
    )
    from ArknightsUID.ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH
except ModuleNotFoundError:
    from ArknightsUID.utils.http import get_client
    from ArknightsUID.utils.disk_cache import (
        cache_read,
        cache_write,
        is_fresh,
    )
    from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH


PRTS_API_URL = "https://prts.wiki/api.php"
PRTS_PAGE_URL = "https://prts.wiki/w/"
# PRTS.Map 交互地图（ark-nights.com 家族，默认中文界面；mapen.* 为英文站）
PRTS_MAP_URL = "https://map.ark-nights.com/?map="
# PRTS 的 Tengine 反代实测（2026-08-14）：封杀浏览器特征 UA（Chrome 等一律 403），
# 放行"可联系的自定义 UA"——这恰是 MediaWiki 官方对 bot 的建议。切勿改成浏览器 UA，
# 否则 wiki 模块全部接口（query/parse/prop=text）会被打成 403。
USER_AGENT = "ArknightsUID/0.1 (+GsCore; PRTS MediaWiki API)"

PRTS_CACHE_PATH = CACHE_PATH / "prts"
PRTS_CACHE_PATH.mkdir(parents=True, exist_ok=True)

# 单关敌方情报模板最多解析的敌人条目数；超过此值的极少数关卡按上限截断。
_MAX_ENEMY_FIELDS = 80
# 缓存过期清理阈值与节流间隔。
_CACHE_MAX_AGE_SECONDS = 7 * 86400
_CACHE_CLEANUP_INTERVAL = 3600
_last_cleanup_at: float = 0.0

# 对 PRTS 的最小请求间隔（秒）：社区 wiki 的反爬阈值远比商业 API 敏感，
# 一条命令最多连打 9+ 个请求（直查 + 搜索回退 + Cargo 补全），不隔开
# 容易整段 IP 被 403。缓存命中不经过 _api 网络路径，不受此间隔影响。
_MIN_REQUEST_INTERVAL = 0.5
_last_request_at: float = 0.0
_request_gap_lock: "asyncio.Lock | None" = None


async def _throttle() -> None:
    """保证本模块发出的相邻两个 PRTS 请求之间至少隔 _MIN_REQUEST_INTERVAL。"""
    global _last_request_at, _request_gap_lock
    import asyncio

    if _request_gap_lock is None:
        _request_gap_lock = asyncio.Lock()
    async with _request_gap_lock:
        now = time.monotonic()
        wait = _MIN_REQUEST_INTERVAL - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


@dataclass(slots=True)
class SearchResult:
    title: str
    snippet: str = ""


@dataclass(slots=True)
class PageWikitext:
    title: str
    wikitext: str


@dataclass(slots=True)
class StageEnemy:
    name: str
    count: str = ""
    level: str = ""


@dataclass(slots=True)
class HoloIndicator:
    """全息作战矩阵指标（关卡页「矩阵指标一览」的 {{测试指标/全息}} 模板）。

    section: 所属分区标题（基础：<主题> / 附加条件，来自表格 group_title 行）
    token:   指标键（r_fixed_rune / r_enemy_speed_a 等）
    kind:    类型=锁定（FXD 唯一锁定指标）/ 指定 / 空
    score:   分数=N（仅附加指标提供评分）
    grouped: 在 {{测试指标/全息/分组}} 内（基础=每组必选1 / 附加=冲突组）
    """
    section: str = ""
    token: str = ""
    text: str = ""
    kind: str = ""
    score: str = ""
    grouped: bool = False


@dataclass(slots=True)
class StageInfo:
    title: str
    code: str = ""
    name: str = ""
    stage_id: str = ""
    stage_type: str = ""
    difficulty: str = ""
    unlock: str = ""
    recommended_level: str = ""
    zone: str = ""
    ap_cost: str = ""
    practice_cost: str = ""
    deploy_limit: str = ""
    initial_cost: str = ""
    cost_limit: str = ""
    life_point: str = ""
    enemy_count: str = ""
    map_size: str = ""
    min_time: str = ""
    description: str = ""
    first_drops: list[str] = field(default_factory=list)
    drops: list[str] = field(default_factory=list)
    extra_drops: list[str] = field(default_factory=list)
    enemies: list[StageEnemy] = field(default_factory=list)
    assault_info: str = ""
    # 突袭 vs 普通：突袭模板全字段 + 人读差异点（数字字段给差值，文本字段直接列）
    assault_fields: dict[str, str] = field(default_factory=dict)
    assault_diffs: list[str] = field(default_factory=list)
    # 关卡描述拆出的「环境条件」段（磨难难度/部分活动关的敌方机制修改描述）
    env_conditions: list[str] = field(default_factory=list)
    # 全息作战矩阵指标（非该模式关卡为空）
    holo_indicators: list[HoloIndicator] = field(default_factory=list)
    # 危机合约关卡标记（页内 {{合约详情}} 动态模板，词条本身在赛季子页）
    has_contracts: bool = False
    source_url: str = ""
    map_url: str = ""


@dataclass(slots=True)
class EnemyLevel:
    index: str = ""
    max_hp: str = ""
    atk: str = ""
    defense: str = ""
    res: str = ""
    move_speed: str = ""
    attack_interval: str = ""
    attack_radius: str = ""
    weight: str = ""
    hp_regen: str = ""       # 生命恢复速度
    sp_regen: str = ""       # sp恢复速度
    ep_res: str = ""         # 元素抗性
    ep_damage_res: str = ""  # 损伤抵抗
    taunt: str = ""          # 基础嘲讽等级
    stun_res: str = ""
    silence_res: str = ""
    sleep_res: str = ""
    freeze_res: str = ""
    levitate_res: str = ""
    tremor_res: str = ""     # 战栗抗性
    fear_res: str = ""       # 恐惧抗性
    paralysis_res: str = ""  # 麻痹抗性
    lure_res: str = ""       # 诱导抗性
    teleport_res: str = ""   # 传送抗性
    bind_res: str = ""       # 缚地抗性
    abilities: list[str] = field(default_factory=list)


# 全部异常抗性（attr, 中文标签）：取值 "有"/"无"（个别敌人可能是部分抗性描述）
RESISTANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("stun_res", "眩晕"), ("silence_res", "沉默"), ("sleep_res", "沉睡"),
    ("freeze_res", "冻结"), ("levitate_res", "浮空"), ("tremor_res", "战栗"),
    ("fear_res", "恐惧"), ("paralysis_res", "麻痹"), ("lure_res", "诱导"),
    ("teleport_res", "传送"), ("bind_res", "缚地"),
)


def level_resistances(level: EnemyLevel) -> list[tuple[str, str]]:
    """等级块的生效抗性 [(标签, 原值)]：空/无 视为无抗性，不进列表。"""
    pairs = []
    for attr, label in RESISTANCE_FIELDS:
        value = (getattr(level, attr) or "").strip()
        if value and value != "无":
            pairs.append((label, value))
    return pairs


@dataclass(slots=True)
class EnemyInfo:
    title: str
    name: str = ""
    enemy_id: str = ""
    index: str = ""
    rank: str = ""
    description: str = ""
    damage_type: str = ""
    attack_type: str = ""
    move_type: str = ""
    kind: str = ""
    event: str = ""
    levels: list[EnemyLevel] = field(default_factory=list)
    source_url: str = ""


@dataclass(slots=True)
class WikiPageSummary:
    title: str
    source_url: str
    summary: str = ""
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SkillLevelDetail:
    """技能单级数值（1-7 级或专精 1-3，来自 {{技能2}} 模板）。"""
    level: str = ""
    description: str = ""
    sp_init: str = ""
    sp_cost: str = ""
    duration: str = ""


@dataclass(slots=True)
class SkillMasteryCost:
    """技能专精材料（专1/专2/专3，来自 {{技能升级材料}} 的 一8/一9/一10 等键）。"""
    mastery: str = ""
    materials: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OperatorSkill:
    name: str = ""
    skill_type: str = ""        # 技能类型1 + 类型2，如 "自动回复 / 手动触发"
    range_: str = ""            # 技能范围（range 为内置名，故用 range_）
    levels: list[SkillLevelDetail] = field(default_factory=list)
    mastery_costs: list[SkillMasteryCost] = field(default_factory=list)
    remark: str = ""


@dataclass(slots=True)
class TalentStage:
    condition: str = ""  # 精英1 / 精英2 / 精英2 X模组3级 ...
    effect: str = ""     # 含具体数值


@dataclass(slots=True)
class OperatorTalent:
    slot: str = ""              # 第一天赋 / 第二天赋
    name: str = ""
    unlock: str = ""            # 精英1 / 精英2 X模组2级 ...
    description: str = ""
    stages: list[TalentStage] = field(default_factory=list)
    potential: TalentStage | None = None  # 满潜强化效果（最高档）


@dataclass(slots=True)
class BaseSkill:
    """后勤技能。名字/阶段来自 {{后勤技能}}，效果经 Cargo 表 building_skill2 补全。"""
    name: str = ""
    unlock: str = ""            # 精英0 / 精英2
    room: str = ""              # 贸易站 / 制造站 / ...
    description: str = ""       # 进驻效果（含数值）


@dataclass(slots=True)
class OperatorPanel:
    """属性面板（{{属性}}：再部署/费用/阻挡/攻速 + 精英各级满级数值）。"""
    redeploy: str = ""
    deploy_cost: str = ""
    block: str = ""
    attack_speed: str = ""
    panels: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class OperatorModule:
    """模组（{{模组}}）。"""
    name: str = ""
    branch: str = ""
    type_: str = ""             # 基础证章 / X模组 / Y模组
    description: str = ""       # 基础信息


@dataclass(slots=True)
class ParadoxSimulation:
    """悖论模拟（{{悖论模拟}}）。"""
    name: str = ""
    description: str = ""
    elite: str = ""
    level: str = ""
    rewards: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ActivityInfo:
    """限时活动/卡池（SMW 属性查询）。时间为北京时间。"""
    title: str
    start: str = ""       # 2026-08-01 04:00
    end: str = ""
    start_ts: float = 0.0
    end_ts: float = 0.0
    source_url: str = ""
    image_url: str = ""   # 官方标题图直链（activity_banner_urls 填充，可空）


@dataclass(slots=True)
class OperatorInfo:
    title: str
    name: str = ""
    rarity: str = ""            # PRTS 存 0-5（对应 1-6 星），展示时 +1
    profession: str = ""
    branch: str = ""
    position: str = ""
    faction: str = ""
    tags: str = ""
    obtain: str = ""
    trait: str = ""
    skills: list[OperatorSkill] = field(default_factory=list)
    talents: list[OperatorTalent] = field(default_factory=list)
    panel: OperatorPanel | None = None
    modules: list[OperatorModule] = field(default_factory=list)
    potentials: list[str] = field(default_factory=list)
    paradox: ParadoxSimulation | None = None
    base_skills: list[BaseSkill] = field(default_factory=list)
    summary: str = ""
    source_url: str = ""


def page_url(title: str) -> str:
    return f"{PRTS_PAGE_URL}{quote(title.replace(' ', '_'), safe='')}"


def map_url(stage_id: str) -> str:
    return f"{PRTS_MAP_URL}{quote(stage_id, safe='')}" if stage_id else ""


def map_file_title(code: str, name: str) -> str:
    """关卡地图文件名（PRTS 命名规律：`{代号}_{关卡名}_地图.png`）。

    实测覆盖主线（1-7_暴君_地图.png）与教学关（TR-10_小队防御_地图.png）；
    活动代号（CW- 等）无此命名文件，返回的标题查不到由调用方降级。
    """
    if not code or not name:
        return ""
    return f"{code}_{name}_地图.png"


def map_file_url(title: str) -> str:
    """地图文件名 → media 直链（MediaWiki md5 散列路径，与道具图标同规则）。

    文件路径由文件名 md5 决定且永久固定，无需 imageinfo API——已对照
    1-7/H5-1/JT8-3/5-10 实际下载地址逐一验证。文件不存在时该 URL 404，
    由调用方按无图降级。
    """
    title = title.strip()
    if not title:
        return ""
    digest = hashlib.md5(title.encode("utf-8")).hexdigest()
    return (
        f"https://media.prts.wiki/{digest[0]}/{digest[:2]}/"
        f"{quote(title)}"
    )


def map_candidate_titles(code: str, name: str) -> list[str]:
    """关卡地图候选文件名：标准命名 + 辅助命名（JT 插曲关只有后者）。"""
    if not code or not name:
        return []
    return [f"{code}_{name}_地图.png", f"{code}_{name}_辅助地图.png"]


async def resolve_map_file_url(code: str, name: str) -> str:
    """关卡地图文件直链（MediaWiki imageinfo，随 _api 缓存 24h）。

    依次尝试 `{代号}_{名}_地图.png` 与 `{代号}_{名}_辅助地图.png`
    （JT 插曲关等只有辅助地图命名，如 JT8-3_昂首，足践烈焰_辅助地图.png）；
    均不存在/接口失败返回空串——调用方降级为 PRTS.Map 链接。

    注意：常规路径优先走 map_file_url 的 md5 直链（零 API），
    本函数仅作改名/特殊路径文件的兜底查询。
    """
    if not code or not name:
        return ""
    for title in map_candidate_titles(code, name):
        url = await _imageinfo_url(title)
        if url:
            return url
    return ""


async def _imageinfo_url(title: str) -> str:
    """单文件 imageinfo 直链；文件不存在返回空串。"""
    data = await _api(
        {
            "action": "query",
            "titles": f"File:{title}",
            "prop": "imageinfo",
            "iiprop": "url",
        }
    )
    if not isinstance(data, dict):
        return ""
    query = data.get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, dict):
        return ""
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        info = page.get("imageinfo")
        if isinstance(info, list) and info and isinstance(info[0], dict):
            return str(info[0].get("url") or "")
    return ""


async def rogue_map_file_urls(code: str, name: str) -> list[str]:
    """肉鸽关卡地图直链·wiki 上传文件（多变体，最多 3 张，仅少数关卡有）。

    两种命名并存：标准 `{代号}_{名}_地图.png`（ISW-NO_乌萨斯方块_地图.png）
    与多变体 `{代号}_{名}_地图_{1..3}.png`（ISW-DF_不容拒绝_地图_1.png，
    页面 tabber 切换的紧急/难度变体）。均查不到返回空列表——调用方回退
    rogue_map_preview_urls（torappu 预览）。
    """
    if not code or not name:
        return []
    urls: list[str] = []
    for title in (
        f"{code}_{name}_地图.png",
        *(f"{code}_{name}_地图_{i}.png" for i in (1, 2, 3)),
    ):
        url = await _imageinfo_url(title)
        if url:
            urls.append(url)
    return urls[:3]


def rogue_map_preview_urls(level_id: str) -> list[str]:
    """tomimi level_id → torappu 地图预览直链（PRTS 肉鸽关卡页地图的实际来源）。

    规律（实测）：level_rogue4_2-2 → ro4_n_2_2.png（层关加 n 前缀=普通；
    紧急 e 版地图布局相同不重复嵌）；level_rogue4_b-8 → ro4_b_8.png（分歧关）；
    level_rogue5_b-9-c → ro5_b_9_c.png（带变体尾）。非肉鸽 id 返回空。
    """
    match = re.match(r"^level_rogue(\d+)_(.+)$", (level_id or "").strip())
    if not match:
        return []
    k, rest = match.groups()
    part = rest.replace("-", "_")
    if part[0].isdigit():
        part = f"n_{part}"
    return [f"https://torappu.prts.wiki/assets/map_preview/ro{k}_{part}.png"]


def _cache_file(params: dict[str, Any]) -> Path:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return PRTS_CACHE_PATH / f"{digest}.json"


async def _load_cache(path: Path, cache_seconds: int) -> dict[str, Any] | None:
    payload, saved_ts = await cache_read(path)
    if payload is not None and is_fresh(saved_ts, cache_seconds):
        return payload if isinstance(payload, dict) else None
    return None


async def _load_stale_cache(path: Path) -> dict[str, Any] | None:
    payload, _saved_ts = await cache_read(path)
    return payload if isinstance(payload, dict) else None


def _cleanup_stale_cache(max_age_seconds: int = _CACHE_MAX_AGE_SECONDS) -> None:
    # 删除 mtime 超过 max_age_seconds 的 .json 缓存条目。
    # 单文件失败仅记录 debug 日志，不影响其他条目或主流程。
    global _last_cleanup_at
    now = time.time()
    if now - _last_cleanup_at < _CACHE_CLEANUP_INTERVAL:
        return
    _last_cleanup_at = now
    try:
        threshold = now - max_age_seconds
        for entry in PRTS_CACHE_PATH.iterdir():
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                if entry.stat().st_mtime < threshold:
                    entry.unlink()
            except Exception as exc:
                logger.debug(
                    "[ArknightsUID] PRTS cache entry cleanup failed: "
                    f"{exc.__class__.__name__}: {exc}"
                )
    except Exception as exc:
        logger.debug(
            "[ArknightsUID] PRTS cache cleanup scan failed: "
            f"{exc.__class__.__name__}: {exc}"
        )


async def _api(
    params: dict[str, Any],
    *,
    cache_seconds: int = 86400,
) -> dict[str, Any] | None:
    cache_path = _cache_file(params)
    cached = await _load_cache(cache_path, cache_seconds)
    if cached is not None:
        _cleanup_stale_cache()
        return cached

    await _throttle()
    request_params = {"format": "json", **params}
    client = await get_client()
    try:
        response = await client.get(
            PRTS_API_URL,
            params=request_params,
            headers={"User-Agent": USER_AGENT},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()
        await cache_write(cache_path, payload)
        _cleanup_stale_cache()
        return payload
    except Exception as exc:
        # PRTS 层无脱敏上下文（异常 message 可能携带 srsearch 参数等
        # 任意文本），按安全约定只打类名
        logger.warning(
            "[ArknightsUID] PRTS API request failed: "
            f"{exc.__class__.__name__}"
        )
        return await _load_stale_cache(cache_path)


async def search_pages(query: str, limit: int = 5) -> list[SearchResult]:
    query = query.strip()
    if not query:
        return []
    data = await _api(
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
        },
        cache_seconds=86400,
    )
    if not data:
        return []
    results = []
    for item in (data.get("query") or {}).get("search", []):
        title = str(item.get("title") or "")
        if title:
            results.append(
                SearchResult(
                    title=title,
                    snippet=_clean_wiki_value(str(item.get("snippet") or "")),
                )
            )
    return results


def _redirect_target(wikitext: str) -> str:
    match = re.match(r"\s*#redirect\s*\[\[([^\]]+)\]\]", wikitext, flags=re.I)
    if not match:
        return ""
    return match.group(1).split("|", 1)[0].strip()


async def parse_page(
    title: str,
    *,
    follow_redirect: bool = True,
    deadline: float | None = None,
) -> PageWikitext | None:
    """解析页面 wikitext（跟随重定向，最多 3 跳）。

    deadline 为 time.monotonic() 时刻：查询链总预算耗尽时立即放弃
    （单次 _api 已有 20s 超时，deadline 防的是"直查+搜索回退 8 个候选页"
    这类多请求链路把命令卡住数分钟）。
    """
    current = title.strip()
    if not current:
        return None
    seen: set[str] = set()
    for _ in range(3):
        if deadline is not None and time.monotonic() > deadline:
            return None
        if current in seen:
            return None
        seen.add(current)
        data = await _api(
            {
                "action": "parse",
                "page": current,
                "prop": "wikitext",
            },
            cache_seconds=_STATIC_PAGE_CACHE_SECONDS,
        )
        if not data or "error" in data:
            return None
        parsed = data.get("parse") or {}
        wikitext_data = parsed.get("wikitext") or {}
        if "*" not in wikitext_data:
            return None
        wikitext = str(wikitext_data.get("*") or "")
        parsed_title = str(parsed.get("title") or current)
        redirect = _redirect_target(wikitext)
        if redirect and follow_redirect:
            current = redirect
            continue
        return PageWikitext(title=parsed_title, wikitext=wikitext)
    return None


def _template_lines(wikitext: str, names: tuple[str, ...]) -> list[list[str]]:
    results: list[list[str]] = []
    lines = wikitext.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not any(stripped.startswith(f"{{{{{name}") for name in names):
            continue
        block = [stripped]
        for next_line in lines[index + 1 :]:
            block.append(next_line.rstrip())
            if next_line.strip() == "}}":
                break
        results.append(block)
    return results


def _fields_from_template(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key = ""
    current_value: list[str] = []

    def flush() -> None:
        nonlocal current_key, current_value
        if current_key:
            fields[current_key] = _clean_wiki_value("\n".join(current_value))
        current_key = ""
        current_value = []

    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "}}":
            flush()
            break
        if line.startswith("|") and "=" in line:
            flush()
            key, value = line[1:].split("=", 1)
            current_key = key.strip()
            current_value = [value.strip()]
        elif current_key:
            current_value.append(line.strip())
    flush()
    return fields


# 变动数值 参数2是方向：-=下调 → 绿色下箭头，+=上调 → 红色上箭头
_ARROW_MARKS = {"-": "↓", "+": "↑"}
# 变动数值lite 参数1是字面符号（+/−）或方向图标（up/down）
_SIGN_MARKS = {"up": "↑", "down": "↓", "+": "+", "-": "-"}


def _collapse_inline_template(match: re.Match[str]) -> str:
    """内嵌模板 {{名|参数…}} → 纯文本（数值模板必须保数值，不能只取末段）。

    通用兜底取最后一个管道段（color 等模板显示段在末尾）；
    变动数值/异常效果等语义模板按显示规则展开：
    - {{变动数值|5%|-}} → 5%↓（参数1为值，参数2为相对上一级的方向）
    - {{变动数值lite|up|蓝|+20%}} → ↑+20%（参数1图标，参数3为值）
    - {{异常效果|眩晕|免疫}} → 眩晕免疫（参数拼接，跳过 color= 等命名参数）
    """
    parts = match.group(1).split("|")
    name = parts[0].strip()
    args = [p.strip() for p in parts[1:]]
    if name == "变动数值":
        value = args[0] if args else ""
        mark = args[1] if len(args) > 1 else ""
        return f"{value}{_ARROW_MARKS.get(mark, '')}"
    if name in ("变动数值lite", "变动数值Lite"):
        mark = _SIGN_MARKS.get(args[0], "") if args else ""
        value = args[2] if len(args) > 2 else (args[-1] if args else "")
        return f"{mark}{value}"
    if name in ("异常效果", "异常状态"):
        return "".join(a for a in args if a and "=" not in a)
    # 通用兜底：取最后一个非命名段（color 等模板显示段在末尾）；全是命名段
    # （{{Popup|内容=…|颜色=…}} 等）时取 内容/文本 显示参数，避免泄漏「颜色=#007DFA」
    unnamed = [a for a in args if a and "=" not in a]
    if unnamed:
        return unnamed[-1]
    for arg in args:
        match = re.fullmatch(r"(?:内容|文本|text|display)=(.+)", arg)
        if match:
            return match.group(1).strip()
    return (parts[-1] or "").strip()


def _clean_wiki_value(value: str) -> str:
    text = html.unescape(value or "")
    text = text.replace("\xa0", " ")
    text = text.replace("<br/>", "\n").replace("<br />", "\n").replace("<br>", "\n")
    text = re.sub(r"</?(?:span|div|small|big|b|i|u|ruby|rt|rb)[^>]*>", "", text)
    text = re.sub(r"<references\s*/>", "", text)
    text = re.sub(r"\[\[[^|\]]+\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"\[https?://[^\s\]]+\]", "", text)

    previous = None
    while "{{" in text and "}}" in text and text != previous:
        previous = text
        text = re.sub(
            r"\{\{([^{}]*)\}\}",
            _collapse_inline_template,
            text,
        )

    text = re.sub(r"'''?", "", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<[A-Za-z/][^>]*>", "", text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_list(value: str) -> list[str]:
    if not value:
        return []
    parts = re.split(r"[,，、]\s*", value)
    return [item.strip() for item in parts if item.strip()]


def _material_to_text(value: str) -> list[str]:
    """把材料字段值拆成人读列表，保留 {{材料消耗|名|数}} 结构。

    必须对**未经 _clean_wiki_value 的原始值**调用——后者会把
    {{材料消耗|龙门币|3w}} 归约成 "3w"（丢失材料名）。
    """
    if not value:
        return []
    items: list[str] = []
    for match in re.finditer(r"\{\{材料消耗\|([^|}]+)\|([^|}]+)\}\}", value):
        name = _clean_wiki_value(match.group(1))
        count = _clean_wiki_value(match.group(2))
        items.append(f"{name} ×{count}")
    if items:
        return items
    # 兜底：无材料消耗模板（已清洗过的纯文本）走通用拆分
    return _split_list(_clean_wiki_value(value))


def _fields_from_template_raw(lines: list[str]) -> dict[str, str]:
    """同 _fields_from_template 但不做 _clean_wiki_value，保留模板原样。

    材料模板（{{技能升级材料}}/{{精英化材料}}）字段必须用此函数提取，
    否则 _clean_wiki_value 会把 {{材料消耗|名|数}} 归约成只剩数量。
    """
    fields: dict[str, str] = {}
    current_key = ""
    current_value: list[str] = []

    def flush() -> None:
        nonlocal current_key, current_value
        if current_key:
            fields[current_key] = "\n".join(current_value).strip()
        current_key = ""
        current_value = []

    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "}}":
            flush()
            break
        if line.startswith("|") and "=" in line:
            flush()
            key, value = line[1:].split("=", 1)
            current_key = key.strip()
            current_value = [value.strip()]
        elif current_key:
            current_value.append(line.strip())
    flush()
    return fields


def _first_template_fields(wikitext: str, names: tuple[str, ...]) -> dict[str, str]:
    blocks = _template_lines(wikitext, names)
    if not blocks:
        return {}
    return _fields_from_template(blocks[0])


def _derive_stage_id(code: str) -> str:
    normalized = code.strip().upper()
    match = re.fullmatch(r"(\d+)-(\d+)", normalized)
    if match:
        chapter = int(match.group(1))
        stage = int(match.group(2))
        return f"main_{chapter:02d}-{stage:02d}"
    match = re.fullmatch(r"TR-(\d+)", normalized)
    if match:
        return f"tr_{int(match.group(1)):02d}"
    return ""


def _split_template_args(text: str) -> list[str]:
    """模板实参按顶层 | 切分（嵌套 {{color|…}} 内的管道不切断）。"""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == "|" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _parse_holo_indicator(
    line: str, section: str, grouped: bool
) -> HoloIndicator | None:
    """单行 {{测试指标/全息|…}} → HoloIndicator；非该模板行返回 None。

    实参形如 |选中=yes|类型=锁定|r_fixed_rune|描述…（命名参数在前，
    键/描述为位置实参；描述内嵌 {{color}} 等，须顶层切分后再清洗）。
    """
    stripped = line.strip()
    head = "{{测试指标/全息|"
    if not (stripped.startswith(head) and stripped.endswith("}}")):
        return None
    args = [a for a in _split_template_args(stripped[len("{{测试指标/全息"):-2]) if a]
    indicator = HoloIndicator(section=section, grouped=grouped)
    positional: list[str] = []
    for arg in args:
        match = re.fullmatch(r"(选中|类型|分数)=(.+)", arg)
        if match:
            key, value = match.groups()
            if key == "类型":
                indicator.kind = value.strip()
            elif key == "分数":
                indicator.score = value.strip()
            continue
        positional.append(arg)
    for arg in positional:
        if re.fullmatch(r"[A-Za-z0-9_]+", arg.strip()):
            indicator.token = arg.strip()
        else:
            indicator.text = _clean_wiki_value(arg).strip()
    if not indicator.text:
        return None
    return indicator


def parse_holo_indicators(wikitext: str) -> list[HoloIndicator]:
    """关卡页 → 全息矩阵指标列表（实测 全息H5-1 页结构）。

    布局：wikitable 的 group_title 行给出分区（{{mdi|…}} 基础：<主题> /
    附加条件）；分区内 {{测试指标/全息/分组}}（到单独 }} 行为止）包着
    分组指标，裸行模板为独立指标。锁定=分组标记不另存（指标自身带
    类型=锁定）。非全息关卡返回 []。
    """
    match = re.search(
        r"={2,4}\s*矩阵指标一览\s*={2,4}(.*?)(?=\n={2,4}|\Z)", wikitext, re.S
    )
    section_text = match.group(1) if match else ""
    if not section_text:
        return []
    indicators: list[HoloIndicator] = []
    current_section = ""
    in_group = False
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('!class="group_title"|'):
            raw_title = stripped.split("|", 1)[1] if "|" in stripped else ""
            title = _clean_wiki_value(
                re.sub(r"^\s*\{\{mdi[^{}]*\}\}", "", raw_title)
            ).strip(" ：:")
            if title:
                current_section = title
            in_group = False
            continue
        if stripped.startswith("{{测试指标/全息/分组"):
            in_group = True
            continue
        if stripped == "}}":
            in_group = False
            continue
        # 分组内首条指标带前导 |（作为分组模板首参分隔符），剥掉再识别
        content = stripped.lstrip("|").strip()
        if content.startswith("{{测试指标/全息|"):
            indicator = _parse_holo_indicator(content, current_section, in_group)
            if indicator:
                indicators.append(indicator)
    return indicators


def _extract_env_conditions(description: str) -> tuple[str, list[str]]:
    """关卡描述中的「环境条件：」段拆出（磨难难度/部分活动关）。

    实测格式：描述行…⏎环境条件：⏎条件行…（标签可独占一行或与首条同行）。
    返回 (剩余描述, 条件行)；无标签时原样返回 (description, [])。
    """
    text = description or ""
    match = re.search(r"环境条件：", text)
    if not match:
        return text, []
    head = text[: match.start()].rstrip()
    tail = text[match.end():].strip()
    conditions = [c.strip() for c in tail.split("\n") if c.strip()]
    return head, conditions


def _build_assault_diffs(
    normal: dict[str, str], assault: dict[str, str]
) -> list[str]:
    """生成突袭 vs 普通的人读差异点。

    数字字段→"{label}：{normal}→{assault}（±Δ）"；非数字/缺失字段→"突袭 {label}：{value}"。
    只列有差异的字段；情报/关卡描述已走 assault_info，不在此重复。
    """
    diffs: list[str] = []
    # 过滤失效/噪音字段（降低承诺：只展示有实战意义的突袭机制差异）：
    #   演习消耗——游戏已改为无限演习不消耗凭证，PRTS 该字段为历史遗留（失效数据）
    #   关卡难度/解锁条件/推荐等级——描述性标签，无突袭实战差异意义（噪音）
    #   首次掉落——掉落表随版本调整频繁，对比稳定性差
    #   情报/关卡描述——已走 assault_info 字段，不在此重复
    skip_keys = {
        "情报",
        "关卡描述",
        "演习消耗",
        "关卡难度",
        "解锁条件",
        "推荐等级",
        "首次掉落",
    }
    for key, assault_val in assault.items():
        if not assault_val or key in skip_keys:
            continue
        normal_val = normal.get(key, "")
        if normal_val == assault_val:
            continue
        try:
            delta = float(assault_val) - float(normal_val)
            sign = "+" if delta > 0 else ""
            diffs.append(f"{key}：{normal_val}→{assault_val}（{sign}{delta:g}）")
        except ValueError:
            # 非数字值可能自带「→」（如部署费用 18→20），措辞避免双箭头
            if normal_val:
                diffs.append(f"{key}：{normal_val}，突袭改为 {assault_val}")
            else:
                diffs.append(f"突袭 {key}：{assault_val}")
    return diffs


def parse_stage_wikitext(page: PageWikitext) -> StageInfo | None:
    normal = _first_template_fields(
        page.wikitext,
        ("普通关卡信息", "活动关卡信息", "剿灭关卡信息", "危机合约关卡信息"),
    )
    if not normal:
        return None

    assault = _first_template_fields(page.wikitext, ("突袭关卡信息",))
    enemy_fields = _first_template_fields(page.wikitext, ("敌方情报",))
    enemies: list[StageEnemy] = []
    for index in range(1, _MAX_ENEMY_FIELDS + 1):
        name = enemy_fields.get(f"敌人{index}", "")
        if not name:
            continue
        enemies.append(
            StageEnemy(
                name=name,
                count=enemy_fields.get(f"敌人{index}数量", ""),
                level=enemy_fields.get(f"敌人{index}级别", ""),
            )
        )

    code = normal.get("关卡代号", "")
    name = normal.get("关卡名", "")
    stage_id = normal.get("关卡id", "") or _derive_stage_id(code)
    assault_info = assault.get("情报") or assault.get("关卡描述") or ""
    assault_diffs = _build_assault_diffs(normal, assault)
    description, env_conditions = _extract_env_conditions(normal.get("关卡描述", ""))

    return StageInfo(
        title=page.title,
        code=code,
        name=name,
        stage_id=stage_id,
        stage_type=normal.get("关卡类型", ""),
        difficulty=normal.get("关卡难度", ""),
        unlock=normal.get("解锁条件", ""),
        recommended_level=normal.get("推荐等级", ""),
        zone=normal.get("所属区域", ""),
        ap_cost=normal.get("作战消耗", ""),
        practice_cost=normal.get("演习消耗", ""),
        deploy_limit=normal.get("部署上限", ""),
        initial_cost=normal.get("初始COST", ""),
        cost_limit=normal.get("COST上限", ""),
        life_point=normal.get("目标点耐久", ""),
        enemy_count=normal.get("敌人数量", ""),
        map_size=normal.get("地图大小", ""),
        min_time=normal.get("最短用时", ""),
        description=description,
        first_drops=_split_list(normal.get("首次掉落", "")),
        drops=_split_list(normal.get("常规掉落", "")),
        extra_drops=_split_list(normal.get("额外物资", "")),
        enemies=enemies,
        assault_info=assault_info,
        assault_fields=assault,
        assault_diffs=assault_diffs,
        env_conditions=env_conditions,
        holo_indicators=parse_holo_indicators(page.wikitext),
        has_contracts=bool(re.search(r"\{\{合约详情\s*\}\}", page.wikitext)),
        source_url=page_url(page.title),
        map_url=map_url(stage_id),
    )


# 查询链总预算（秒）：直查 + 搜索回退逐页解析 + Cargo 补全共用一个 deadline
_QUERY_DEADLINE_SECONDS = 90.0


def _new_deadline() -> float:
    return time.monotonic() + _QUERY_DEADLINE_SECONDS


def _expired(deadline: float) -> bool:
    return time.monotonic() > deadline


async def query_stage(query: str) -> StageInfo | None:
    query = query.strip()
    if not query:
        return None
    deadline = _new_deadline()

    page = await parse_page(query, deadline=deadline)
    if page:
        stage = parse_stage_wikitext(page)
        if stage:
            return stage

    results = await search_pages(query, limit=8)
    for result in results:
        if _expired(deadline):
            break
        title = result.title
        if "/" in title and not re.search(r"\s\d+-\d+", title):
            continue
        page = await parse_page(title, deadline=deadline)
        if not page:
            continue
        stage = parse_stage_wikitext(page)
        if stage:
            return stage
    return None


# 等级变体块可从上一级继承的数值字段（index/描述/能力不继承）
_LEVEL_STAT_FIELDS = (
    "max_hp", "atk", "defense", "res", "move_speed", "attack_interval",
    "attack_radius", "weight", "hp_regen", "sp_regen", "ep_res",
    "ep_damage_res", "taunt",
) + tuple(attr for attr, _ in RESISTANCE_FIELDS)


def _inherit_level_stats(levels: list[EnemyLevel]) -> None:
    """等级 2+ 变体块的空数值字段从上一级继承（PRTS 只写差异字段）。"""
    for previous, current in zip(levels, levels[1:]):
        for field_name in _LEVEL_STAT_FIELDS:
            if not getattr(current, field_name):
                setattr(
                    current, field_name, getattr(previous, field_name)
                )


def _merge_level_abilities(
    fields: dict[str, str], prev_fields: dict[str, str] | None = None
) -> list[str]:
    """等级块机制字段重组：技能N/效果/初始/消耗 → 单条可读机制。

    PRTS 逐字段存（技能0：投枪 / 技能0效果：… / 技能0初始：15 / 技能0消耗：20），
    平铺会碎成 SP 参数各占一行；合并为「技能·投枪：效果（初始15·消耗20）」。
    等级 2+ 差异块常只有部分字段：技能名缺失从上一级回查（prev_fields），
    只展示本块声明的效果/SP 部分（差异语义，浮士德等级2 实证：技能0 只写
    效果+SP、技能1 只写 SP；霜星等级2 只有 SP——聚合后分别为
    「技能·名：新效果（初始X·消耗Y）」「技能·名（初始X·消耗Y）」，
    不再泄漏 技能N初始：X 裸字段行）。
    其余 能力/天赋/特殊 字段原样保留（键名：值），顺序与 wikitext 一致。
    """
    abilities: list[str] = []
    consumed: set[str] = set()

    def _skill_param(sid: str, suffix: str) -> str:
        return fields.get(f"技能{sid}{suffix}", "")

    def _sp_text(sp: dict[str, str]) -> str:
        parts = [
            f"{label}{sp[flag]}"
            for label, flag in (
                ("初始", "初始"), ("消耗", "消耗"), ("持续", "持续"),
            )
            if flag in sp
        ]
        return f"（{'·'.join(parts)}）" if parts else ""

    for key, value in fields.items():
        match = re.fullmatch(r"技能(\d+)", key)
        if not match:
            continue
        sid = match.group(1)
        name = value or (prev_fields or {}).get(key, "")
        effect = _skill_param(sid, "效果")
        sp = {
            suffix: _skill_param(sid, suffix)
            for suffix in ("初始", "消耗", "持续")
            if _skill_param(sid, suffix)
        }
        suffix_text = _sp_text(sp)
        if name and effect:
            abilities.append(f"技能·{name}：{effect}{suffix_text}")
        elif name or effect:
            text = f"{name}：{effect}" if name else effect
            abilities.append(f"技能·{text}{suffix_text}".rstrip("："))
        consumed.update(
            {
                key,
                f"技能{sid}效果",
                f"技能{sid}初始",
                f"技能{sid}消耗",
                f"技能{sid}持续",
            }
        )

    # 差异块孤儿字段（本块无 技能N 名字段）：按 sid 聚合 效果/初始/消耗/持续，
    # 技能名从上一级回查；条目顺序按各 sid 首个孤儿字段在块内的出现序
    orphans: dict[str, dict[str, str]] = {}
    for key, value in fields.items():
        if key in consumed or not value:
            continue
        match = re.fullmatch(r"技能(\d+)(效果|初始|消耗|持续)", key)
        if match:
            sid, suffix = match.groups()
            orphans.setdefault(sid, {})[suffix] = value
    for sid, parts in orphans.items():
        name = (prev_fields or {}).get(f"技能{sid}", "")
        effect = parts.get("效果", "")
        sp = {s: parts[s] for s in ("初始", "消耗", "持续") if s in parts}
        suffix_text = _sp_text(sp)
        if name and effect:
            abilities.append(f"技能·{name}：{effect}{suffix_text}")
        elif effect:
            abilities.append(f"技能·{effect}{suffix_text}")
        elif name and suffix_text:
            abilities.append(f"技能·{name}{suffix_text}")
        consumed.update(f"技能{sid}{suffix}" for suffix in parts)

    for key, value in fields.items():
        if key in consumed or not value:
            continue
        if any(mark in key for mark in ("能力", "技能", "天赋", "特殊")):
            abilities.append(f"{key}：{value}")
    return abilities


def parse_enemy_wikitext(page: PageWikitext) -> EnemyInfo | None:
    common = _first_template_fields(page.wikitext, ("敌人信息/common2", "敌人信息"))
    level_blocks = _template_lines(page.wikitext, ("敌人信息/levelcontent",))
    if not common and not level_blocks:
        return None

    levels: list[EnemyLevel] = []
    prev_fields: dict[str, str] | None = None
    for block in level_blocks:
        fields = _fields_from_template(block)
        abilities = _merge_level_abilities(fields, prev_fields)
        prev_fields = fields
        levels.append(
            EnemyLevel(
                index=fields.get("index", ""),
                max_hp=fields.get("最大生命值", ""),
                atk=fields.get("攻击力", ""),
                defense=fields.get("防御力", ""),
                res=fields.get("法术抗性", ""),
                move_speed=fields.get("移动速度", ""),
                attack_interval=fields.get("攻击间隔", ""),
                attack_radius=fields.get("攻击范围半径", ""),
                weight=fields.get("重量等级", ""),
                hp_regen=fields.get("生命恢复速度", ""),
                sp_regen=fields.get("sp恢复速度", ""),
                ep_res=fields.get("元素抗性", ""),
                ep_damage_res=fields.get("损伤抵抗", ""),
                taunt=fields.get("基础嘲讽等级", ""),
                stun_res=fields.get("眩晕抗性", ""),
                silence_res=fields.get("沉默抗性", ""),
                sleep_res=fields.get("沉睡抗性", ""),
                freeze_res=fields.get("冻结抗性", ""),
                levitate_res=fields.get("浮空抗性", ""),
                tremor_res=fields.get("战栗抗性", ""),
                fear_res=fields.get("恐惧抗性", ""),
                paralysis_res=fields.get("麻痹抗性", ""),
                lure_res=fields.get("诱导抗性", ""),
                teleport_res=fields.get("传送抗性", ""),
                bind_res=fields.get("缚地抗性", ""),
                abilities=abilities,
            )
        )

    # PRTS 的等级 2+ 变体块只写与上一级的差异字段（实测 爱国者 等级2 缺
    # 防御/法抗），空数值字段从上一级继承补全；能力/描述不继承（语义独立）。
    _inherit_level_stats(levels)

    return EnemyInfo(
        title=page.title,
        name=common.get("名称", page.title),
        enemy_id=common.get("id", ""),
        index=common.get("index", ""),
        rank=common.get("地位级别", ""),
        description=common.get("描述", ""),
        damage_type=common.get("伤害类型", ""),
        attack_type=common.get("攻击方式", ""),
        move_type=common.get("行动方式", ""),
        kind=common.get("种类", ""),
        event=common.get("登场活动", ""),
        levels=levels,
        source_url=page_url(page.title),
    )


def final_level(levels: list[SkillLevelDetail]) -> SkillLevelDetail | None:
    """满级展示级：最后一个带描述的等级（专3 优先；无专精技能回退 7 级）。"""
    for lv in reversed(levels):
        if lv.description:
            return lv
    return None


def select_owner_level(
    levels: list[SkillLevelDetail], specialize: int, main_lvl: int
) -> SkillLevelDetail | None:
    """玩家当前技能等级 → 对应级详情：专N 优先，否则按技能等级（1-7）。

    按等级标签匹配（解析层产出 "1"-"7"/"专1"-"专3"），不依赖 1-7+专1-3
    的十级满布局；选中级无描述（如无专精技能的专精位）时向前回退到最近
    的非空级。
    """
    if 1 <= specialize <= 3:
        label = f"专{specialize}"
    elif 1 <= main_lvl <= 7:
        label = str(main_lvl)
    else:
        return None
    for index, lv in enumerate(levels):
        if lv.level != label:
            continue
        while index >= 0 and not levels[index].description:
            index -= 1
        return levels[index] if index >= 0 else None
    return None


def parse_operator_wikitext(page: PageWikitext) -> OperatorInfo | None:
    """解析干员页：meta({{CharinfoV2}}) + 技能({{技能2}}) + 天赋({{天赋列表3}}) + 专精材料。

    干员页用 {{CharinfoV2}}（非 {{干员信息}}），现有 _top_level_fields 不覆盖，
    故 meta 单独从此模板取。技能逐级数值（1-7 + 专精1-3）全在 {{技能2}} 字段；
    专精材料在 {{技能升级材料}} 的 一/二/三{8,9,10} 键（按技能序号，8/9/10=专1/2/3）。
    """
    meta = _first_template_fields(page.wikitext, ("CharinfoV2", "干员信息"))
    if not meta:
        return None

    obtain_fields = _first_template_fields(page.wikitext, ("干员获得方式",))

    # === 技能（{{技能2}} 多块，每块一个技能）===
    skill_blocks = _template_lines(page.wikitext, ("技能2", "技能"))
    skills: list[OperatorSkill] = []
    for block in skill_blocks:
        fields = _fields_from_template(block)
        name = fields.get("技能名", "")
        if not name:
            continue
        skill_type = " / ".join(
            p for p in (fields.get("技能类型1", ""), fields.get("技能类型2", "")) if p
        )
        levels: list[SkillLevelDetail] = []
        for lvl in range(1, 8):
            levels.append(
                SkillLevelDetail(
                    level=str(lvl),
                    description=fields.get(f"技能{lvl}描述", ""),
                    sp_init=fields.get(f"技能{lvl}初始", ""),
                    sp_cost=fields.get(f"技能{lvl}消耗", ""),
                    duration=fields.get(f"技能{lvl}持续", ""),
                )
            )
        for mastery in range(1, 4):
            levels.append(
                SkillLevelDetail(
                    level=f"专{mastery}",
                    description=fields.get(f"技能专精{mastery}描述", ""),
                    sp_init=fields.get(f"技能专精{mastery}初始", ""),
                    sp_cost=fields.get(f"技能专精{mastery}消耗", ""),
                    duration=fields.get(f"技能专精{mastery}持续", ""),
                )
            )
        skills.append(
            OperatorSkill(
                name=name,
                skill_type=skill_type,
                range_=fields.get("技能范围", ""),
                levels=levels,
                remark=fields.get("备注", ""),
            )
        )

    # === 专精材料（{{技能升级材料}}，raw 提取保留 {{材料消耗}} 结构）===
    # 一/二/三 → 技能 0/1/2；8/9/10 → 专1/专2/专3
    mastery_blocks = _template_lines(page.wikitext, ("技能升级材料",))
    if mastery_blocks and skills:
        raw_mats = _fields_from_template_raw(mastery_blocks[0])
        prefix_to_idx = {"一": 0, "二": 1, "三": 2}
        num_to_mastery = {"8": "专1", "9": "专2", "10": "专3"}
        for key, value in raw_mats.items():
            if not key or key[0] not in prefix_to_idx:
                continue
            skill_idx = prefix_to_idx[key[0]]
            mastery_label = num_to_mastery.get(key[1:])
            if mastery_label is None or skill_idx >= len(skills):
                continue
            materials = _material_to_text(value)
            if materials:
                skills[skill_idx].mastery_costs.append(
                    SkillMasteryCost(mastery=mastery_label, materials=materials)
                )

    # === 天赋（{{天赋列表3}} 多块）===
    talent_blocks = _template_lines(page.wikitext, ("天赋列表3", "天赋列表"))
    talents: list[OperatorTalent] = []
    for block in talent_blocks:
        fields = _fields_from_template(block)
        slot = fields.get("天赋", "")
        base_name = fields.get("天赋1", "")
        if not base_name:
            continue
        # 天赋1-4：基础 + 模组逐级（条件/效果），全收集；数值在效果字段内
        stages: list[TalentStage] = []
        descriptions: list[str] = []
        for i in range(1, 5):
            cond = fields.get(f"天赋{i}条件", "")
            eff = fields.get(f"天赋{i}效果", "")
            if eff:
                stages.append(TalentStage(condition=cond, effect=eff))
                descriptions.append(f"{cond}：{eff}" if cond else eff)
        # 潜能增强（满潜强化，同样按 1-4 档）：取最高档作展示
        potential: TalentStage | None = None
        for i in range(1, 5):
            eff = fields.get(f"潜能增强_天赋{i}效果", "")
            if eff:
                potential = TalentStage(
                    condition=fields.get(f"潜能增强_天赋{i}条件", ""), effect=eff
                )
        talents.append(
            OperatorTalent(
                slot=slot,
                name=base_name,
                unlock=fields.get("天赋1条件", ""),
                description=" / ".join(descriptions),
                stages=stages,
                potential=potential,
            )
        )

    # === 属性面板（{{属性}}）===
    stats_fields = _first_template_fields(page.wikitext, ("属性",))
    panel: OperatorPanel | None = None
    if stats_fields:
        panel = OperatorPanel(
            redeploy=stats_fields.get("再部署", ""),
            deploy_cost=stats_fields.get("部署费用", ""),
            block=stats_fields.get("阻挡数", ""),
            attack_speed=stats_fields.get("攻击速度", ""),
            panels={k: v for k, v in stats_fields.items() if "精英" in k and v},
        )

    # === 模组（{{模组}} 多块）===
    module_blocks = _template_lines(page.wikitext, ("模组",))
    modules: list[OperatorModule] = []
    for block in module_blocks:
        mf = _fields_from_template(block)
        modules.append(
            OperatorModule(
                name=mf.get("名称", ""),
                branch=mf.get("分支", ""),
                type_="基础证章" if mf.get("基础证章") else "",
                description=mf.get("基础信息", ""),
            )
        )

    # === 潜能（{{潜能提升}}，潜能2-6）===
    potential_fields = _first_template_fields(page.wikitext, ("潜能提升",))
    potentials: list[str] = [
        potential_fields.get(f"潜能{i}", "") for i in range(2, 7)
    ]
    potentials = [p for p in potentials if p]

    # === 悖论模拟（{{悖论模拟}}）===
    paradox_fields = _first_template_fields(page.wikitext, ("悖论模拟",))
    paradox: ParadoxSimulation | None = None
    if paradox_fields and paradox_fields.get("name"):
        rewards: list[str] = []
        for i in range(1, 4):
            content = paradox_fields.get(f"报酬内容{i}", "")
            count = paradox_fields.get(f"报酬数量{i}", "")
            if content:
                rewards.append(f"{content} ×{count}" if count else content)
        paradox = ParadoxSimulation(
            name=paradox_fields.get("name", ""),
            description=paradox_fields.get("description", ""),
            elite=paradox_fields.get("精英化", ""),
            level=paradox_fields.get("等级", ""),
            rewards=rewards,
        )

    # === 后勤技能（{{后勤技能}}，名字+阶段；效果经 Cargo 补全）===
    base_fields = _first_template_fields(page.wikitext, ("后勤技能",))
    base_skills: list[BaseSkill] = []
    for key, val in base_fields.items():
        # key 形如 "后勤技能1-1"；配套 "后勤技能1-1阶段"/"后勤技能1-1等级" 是
        # 修饰键，不能当技能名（黑角页有 后勤技能2-1等级=30级）
        if (
            key.startswith("后勤技能")
            and "-" in key
            and not key.endswith("阶段")
            and not key.endswith("等级")
            and val
        ):
            stage = base_fields.get(f"{key}阶段", "")
            grade = base_fields.get(f"{key}等级", "")
            unlock = f"{stage} {grade}" if stage and grade else (stage or grade)
            base_skills.append(BaseSkill(name=val, unlock=unlock))

    faction = ", ".join(
        p for p in (meta.get("所属国家", ""), meta.get("所属组织", "")) if p
    )
    return OperatorInfo(
        title=page.title,
        name=meta.get("干员名", page.title),
        rarity=meta.get("稀有度", ""),
        profession=meta.get("职业", ""),
        branch=meta.get("分支", ""),
        position=meta.get("位置", ""),
        faction=faction,
        tags=meta.get("标签", ""),
        obtain=obtain_fields.get("获得方式", ""),
        trait=meta.get("特性", ""),
        skills=skills,
        talents=talents,
        panel=panel,
        modules=modules,
        potentials=potentials,
        paradox=paradox,
        base_skills=base_skills,
        summary=meta.get("特性", ""),
        source_url=page_url(page.title),
    )


async def query_enemy(query: str) -> EnemyInfo | None:
    query = query.strip()
    if not query:
        return None
    deadline = _new_deadline()

    page = await parse_page(query, deadline=deadline)
    if page:
        enemy = parse_enemy_wikitext(page)
        if enemy:
            return enemy

    results = await search_pages(query, limit=8)
    for result in results:
        if _expired(deadline):
            break
        if "/" in result.title:
            continue
        page = await parse_page(result.title, deadline=deadline)
        if not page:
            continue
        enemy = parse_enemy_wikitext(page)
        if enemy:
            return enemy
    return None


def _top_level_fields(wikitext: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for block in _template_lines(wikitext, ("干员信息", "材料信息", "道具信息", "敌人信息/common2")):
        fields.update(_fields_from_template(block))
    if fields:
        return fields

    for line in wikitext.splitlines():
        if line.startswith("|") and "=" in line:
            key, value = line[1:].split("=", 1)
            clean_key = key.strip()
            if clean_key and clean_key not in fields:
                fields[clean_key] = _clean_wiki_value(value)
    return fields


def parse_generic_page(page: PageWikitext) -> WikiPageSummary:
    fields = _top_level_fields(page.wikitext)
    preferred = []
    if fields.get("干员名"):
        preferred_keys = (
            "干员名",
            "稀有度",
            "职业",
            "分支",
            "位置",
            "标签",
            "所属势力",
            "获得方式",
        )
    else:
        preferred_keys = (
            "名称",
            "稀有度",
            "职业",
            "分支",
            "描述",
            "用途",
            "获取方式",
            "获得方式",
        )
    for key in preferred_keys:
        value = fields.get(key, "")
        if value:
            preferred.append(f"{key}：{value}")

    if preferred:
        summary = "\n".join(preferred[:8])
    else:
        text = _clean_wiki_value(page.wikitext)
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("=") or stripped.startswith("#redirect"):
                continue
            lines.append(stripped)
            if len("".join(lines)) > 260:
                break
        summary = "\n".join(lines)[:500]

    return WikiPageSummary(
        title=page.title,
        source_url=page_url(page.title),
        summary=summary,
        fields=fields,
    )


async def query_page(query: str) -> WikiPageSummary | None:
    query = query.strip()
    if not query:
        return None
    deadline = _new_deadline()

    page = await parse_page(query, deadline=deadline)
    if page:
        return parse_generic_page(page)

    results = await search_pages(query, limit=8)
    for result in results:
        if _expired(deadline):
            break
        if result.title.endswith(("/spine", "/BEG", "/END")):
            continue
        page = await parse_page(result.title, deadline=deadline)
        if page:
            return parse_generic_page(page)
    return None


async def query_base_skill(name: str) -> tuple[str, str] | None:
    """按技能名查后勤技能效果（PRTS Cargo 表 building_skill2）。

    干员页 {{后勤技能}} 只存名字，效果由模板渲染时查 Cargo 表——
    后端走同一数据源（action=cargoquery），返回 (room, description)。
    """
    name = name.strip()
    if not name:
        return None
    # 技能名来自 wiki 干员页（外部可编辑），MySQL 语义下单引号 doubling、
    # 反斜杠成对，防止 where 条件被拼接注入（注入向量只有 ' 与 \；
    # %/_ 在 = 比较中是字面量，不拦截）；控制字符不属于合法技能名，直接拒绝
    if re.search(r"[\x00-\x1f\x7f]", name):
        logger.warning(f"[ArknightsUID] 拒绝可疑技能名 Cargo 查询: {name[:16]!r}")
        return None
    safe_name = name.replace("\\", "\\\\").replace("'", "''")
    data = await _api(
        {
            "action": "cargoquery",
            "tables": "building_skill2",
            "fields": "name,room,icon,description",
            "where": f"name='{safe_name}'",
            "limit": "1",
        },
        cache_seconds=_STATIC_PAGE_CACHE_SECONDS,
    )
    if not data:
        return None
    rows = data.get("cargoquery") or []
    if not rows:
        return None
    title = rows[0].get("title") or {}
    room = _clean_wiki_value(str(title.get("room") or ""))
    description = _clean_wiki_value(str(title.get("description") or ""))
    if not description:
        return None
    return room, description


async def query_operator(query: str) -> OperatorInfo | None:
    """查询单干员富信息（技能/天赋/专精材料）。

    两阶段搜索回退（跳过含 / 的子页面，如 银灰/模组）。返回 None 表示非干员页；
    返回的 OperatorInfo 若 skills/talents 俱空，由命令层回退 query_page 通用格式。
    """
    query = query.strip()
    if not query:
        return None
    deadline = _new_deadline()

    page = await parse_page(query, deadline=deadline)
    op = parse_operator_wikitext(page) if page else None
    if not op:
        results = await search_pages(query, limit=8)
        for result in results:
            if _expired(deadline):
                break
            if "/" in result.title:
                continue
            page = await parse_page(result.title, deadline=deadline)
            if not page:
                continue
            op = parse_operator_wikitext(page)
            if op:
                break
    if not op:
        return None

    # 后勤技能效果补全（Cargo 表，逐技能一次查询，_api 缓存 1 天；
    # 同受查询链 deadline 约束，预算耗尽时留空由展示层降级）
    for base in op.base_skills:
        if base.description or _expired(deadline):
            continue
        effect = await query_base_skill(base.name)
        if effect:
            base.room, base.description = effect
    return op


# === 活动日历（SMW ask + Gameinfo LST）===

_BEIJING_TZ = timezone(timedelta(hours=8))
_SMW_RAW_DATE_RE = re.compile(r"^1/(\d+)/(\d+)/(\d+)/(\d+)/(\d+)/(\d+)/\d+$")
# SMW ask/printout 提取与 LST section 解析
_LST_SECTION_RE = re.compile(
    r'<section class="lst" begin=(\w+)\s*/>(.*?)<section class="lst" end=\1\s*/>',
    re.S,
)
# 日历类查询缓存 1 小时（活动数据按天变化，无需 1 天缓存）
_CALENDAR_CACHE_SECONDS = 3600

# 静态游戏数据页缓存 30 天：干员/敌人/关卡页面文本、基建技能表等内容
# 不随时间变化（干员数值无后续调整），增量只有新版本内容——新页面靠
# search（仍 24h）发现，新模组走 gamedata 表（独立 7 天 TTL），互不耦合。
_STATIC_PAGE_CACHE_SECONDS = 30 * 86400


def _parse_smw_raw_date(raw: str) -> tuple[str, float]:
    """SMW raw 日期 '1/2026/8/1/4/0/0/0' → ('2026-08-01 04:00', epoch)。

    PRTS 活动时间为北京时间（UTC+8），epoch 按 +8 换算。
    """
    match = _SMW_RAW_DATE_RE.match((raw or "").strip())
    if not match:
        return "", 0.0
    y, mo, d, h, mi, s = (int(part) for part in match.groups())
    try:
        dt = datetime(y, mo, d, h, mi, s, tzinfo=_BEIJING_TZ)
    except ValueError:
        return "", 0.0
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}", dt.timestamp()


def _smw_first_value(printout: Any) -> str:
    """SMW printout 列表首项 → 字符串（raw 或 fulltext）。"""
    if not isinstance(printout, list) or not printout:
        return ""
    first = printout[0]
    if isinstance(first, dict):
        return str(first.get("raw") or first.get("fulltext") or "")
    return str(first)


def _smw_datetime(printout: Any) -> tuple[str, float]:
    return _parse_smw_raw_date(_smw_first_value(printout))


async def _smw_ask(query: str) -> dict[str, Any] | None:
    """Semantic MediaWiki #ask 查询（PRTS 活动页带 SMW 属性）。

    不带 api_version 参数——v3 响应的 results 是 list，默认版（实测可用）
    是 dict，此处按默认版解析。limit 写进 query 串。
    """
    return await _api(
        {
            "action": "ask",
            "query": query,
            "format": "json",
        },
        cache_seconds=_CALENDAR_CACHE_SECONDS,
    )


async def query_activities(limit: int = 15, now_ts: float | None = None) -> list[ActivityInfo]:
    """进行中/未来的限时活动（按结束时间升序）。

    数据来自 PRTS 活动页的 SMW 属性（分类:有活动信息的页面），
    与 PRTS 首页轮播同源。now 取北京日期做过滤，已结束的由展示层兜底。
    now_ts 仅供测试注入固定时钟；None 用真实时间。
    """
    today = datetime.fromtimestamp(
        now_ts if now_ts is not None else time.time(), _BEIJING_TZ
    ).strftime("%Y-%m-%d")
    query = (
        f"[[分类:有活动信息的页面]][[活动结束时间::>{today}]]"
        "|?活动开始时间|?活动结束时间"
        "|sort=活动结束时间|order=asc"
        f"|limit={limit}"
    )
    data = await _smw_ask(query)
    if not data:
        return []
    results = (data.get("query") or {}).get("results") or {}
    now_ts = now_ts if now_ts is not None else time.time()
    activities: list[ActivityInfo] = []
    for title, info in results.items():
        prints = info.get("printouts") or {}
        start, start_ts = _smw_datetime(prints.get("活动开始时间"))
        end, end_ts = _smw_datetime(prints.get("活动结束时间"))
        # SMW 按日期过滤，当天早些时候结束的仍会返回，此处按精确时间剔除
        if not end or end_ts <= now_ts:
            continue
        activities.append(
            ActivityInfo(
                title=title,
                start=start,
                end=end,
                start_ts=start_ts,
                end_ts=end_ts,
                source_url=page_url(title),
            )
        )
    return activities


async def query_gacha_pools(
    limit: int = 10, now_ts: float | None = None
) -> list[ActivityInfo]:
    """进行中/未来的限定卡池（SMW：Category:国服寻访）。now_ts 仅供测试注入。

    SMW 结果键即卡池横幅的文件页（`文件:联合行动23.jpg`）——文件名经
    map_file_url（md5 散列路径）直接换出 media 直链，无需再查 imageinfo。
    """
    today = datetime.fromtimestamp(
        now_ts if now_ts is not None else time.time(), _BEIJING_TZ
    ).strftime("%Y-%m-%d")
    query = (
        f"[[Category:国服寻访]][[寻访关闭时间cn::>{today}]]"
        "|?卡池名|?寻访开启时间cn|?寻访关闭时间cn"
        "|sort=寻访关闭时间cn|order=asc"
        f"|limit={limit}"
    )
    data = await _smw_ask(query)
    if not data:
        return []
    results = (data.get("query") or {}).get("results") or {}
    now_ts = now_ts if now_ts is not None else time.time()
    pools: list[ActivityInfo] = []
    for title, info in results.items():
        prints = info.get("printouts") or {}
        # 卡池名 printout 或页面标题都可能带「寻访模拟/」前缀，取末段
        raw_name = _smw_first_value(prints.get("卡池名")) or title
        name = raw_name.split("/")[-1]
        banner_file = title.split(":", 1)[1].strip() if ":" in title else ""
        start, start_ts = _smw_datetime(prints.get("寻访开启时间cn"))
        end, end_ts = _smw_datetime(prints.get("寻访关闭时间cn"))
        if not end or end_ts <= now_ts:
            continue
        pools.append(
            ActivityInfo(
                title=name,
                start=start,
                end=end,
                start_ts=start_ts,
                end_ts=end_ts,
                source_url=page_url(title),
                image_url=map_file_url(banner_file),
            )
        )
    return pools


# 活动页信息框 {{活动信息}} 的标题图参数（`标题图文件名=主题图 xxx.png`）
_BANNER_FILE_RE = re.compile(r"^\s*\|\s*标题图文件名\s*=\s*(.+?)\s*$", re.MULTILINE)


async def activity_banner_urls(titles: list[str]) -> dict[str, str]:
    """活动页 → 官方标题图直链（批量，两次 API：revisions 提文件名 + imageinfo 换直链）。

    来源是 PRTS 活动页信息框的 `标题图文件名` 参数（官方主题图/宣传图，
    与 PRTS 页面顶部横幅同源）。无信息框或文件缺失的页面不出现在
    返回值里——调用方按无图降级，不影响列表本身。
    """
    titles = [t.strip() for t in titles if t and t.strip()]
    if not titles:
        return {}
    data = await _api(
        {
            "action": "query",
            "titles": "|".join(titles),
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
        }
    )
    pages = ((data or {}).get("query") or {}).get("pages") or {}
    file_by_title: dict[str, str] = {}
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        title = str(page.get("title") or "")
        revisions = page.get("revisions")
        slots = (
            revisions[0].get("slots") or {}
            if isinstance(revisions, list) and revisions and isinstance(revisions[0], dict)
            else {}
        )
        content = str((slots.get("main") or {}).get("*") or "")
        match = _BANNER_FILE_RE.search(content)
        if title and match and match.group(1).strip():
            file_by_title[title] = match.group(1).strip()
    if not file_by_title:
        return {}
    info = await _api(
        {
            "action": "query",
            "titles": "|".join(f"File:{name}" for name in file_by_title.values()),
            "prop": "imageinfo",
            "iiprop": "url",
        }
    )
    url_by_file: dict[str, str] = {}
    ipages = ((info or {}).get("query") or {}).get("pages") or {}
    for page in ipages.values():
        if not isinstance(page, dict):
            continue
        ii = page.get("imageinfo")
        if not (isinstance(ii, list) and ii and isinstance(ii[0], dict)):
            continue
        file_title = str(page.get("title") or "")
        # PRTS 会把 File: 规范化为中文命名空间 文件:，两种前缀都剥
        for prefix in ("File:", "文件:"):
            if file_title.startswith(prefix):
                url_by_file[file_title[len(prefix):]] = str(ii[0].get("url") or "")
                break
    return {
        title: url_by_file[name]
        for title, name in file_by_title.items()
        if url_by_file.get(name)
    }


def parse_lst_sections(wikitext: str) -> dict[str, str]:
    """解析 LST section 标签：`<section begin=key />值<section end=key />`。"""
    sections: dict[str, str] = {}
    for match in _LST_SECTION_RE.finditer(wikitext):
        key, value = match.group(1), match.group(2)
        cleaned = _clean_wiki_value(value)
        if key and cleaned:
            sections[key] = cleaned
    return sections


async def query_game_info() -> dict[str, str]:
    """PRTS:Gameinfo 数据（版本/更新日期/维护/资源与剿灭全开窗口）。

    多页合并为扁平 dict（cnver/cndate/cnmaintstart/...），1 小时缓存。
    """
    merged: dict[str, str] = {}
    for page in ("PRTS:Gameinfo/国服/基础", "PRTS:Gameinfo/国服/服务器维护"):
        parsed = await parse_page(page)
        if parsed:
            merged.update(parse_lst_sections(parsed.wikitext))
    return merged


# --- 干员语音（PRTS 子页面 {干员}/语音记录）与危机合约词条（SMW） ---

# 语音页模板参数：|标题N=交谈1 |台词N={{VoiceData/word|中文|……}}（多语言并列）
_VOICE_TITLE_RE = re.compile(r"\|\s*标题(\d+)\s*=\s*([^\n|]+)")
_VOICE_CN_RE = re.compile(
    r"\|\s*台词(\d+)\s*=\s*.*?\{\{VoiceData/word\|中文\|([^{}]*)\}\}",
    re.DOTALL,
)


def parse_voice_lines(wikitext: str) -> list[dict[str, str]]:
    """语音记录 wikitext → [{title, text}]（只取中文台词）。

    台词多语言并列在同一个参数值里，中文块用非贪婪配平花括号；
    标题与台词按下标配对，缺失任一侧即丢弃该条。
    """
    titles = {m.group(1): m.group(2).strip() for m in _VOICE_TITLE_RE.finditer(wikitext)}
    result: list[dict[str, str]] = []
    for m in _VOICE_CN_RE.finditer(wikitext):
        idx = m.group(1)
        title = titles.get(idx, "")
        text = html.unescape(m.group(2)).strip()
        if title and text:
            result.append({"title": title, "text": text})
    return result


async def query_voice_lines(char_name: str) -> list[dict[str, str]] | None:
    """干员中文语音台词（页面缓存复用 parse_page 管线）。

    页面不存在/无语音数据返回 None（调用方给"未收录"提示）。
    """
    parsed = await parse_page(f"{char_name}/语音记录")
    wikitext = (parsed.wikitext if parsed else "") or ""
    lines = parse_voice_lines(wikitext)
    return lines or None

