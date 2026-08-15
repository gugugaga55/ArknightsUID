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

import httpx

from gsuid_core.logger import logger

try:
    from ArknightsUID.ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH
except ModuleNotFoundError:
    from ArknightsUID.utils.resource.RESOURCE_PATH import CACHE_PATH


PRTS_API_URL = "https://prts.wiki/api.php"
PRTS_PAGE_URL = "https://prts.wiki/w/"
PRTS_MAP_URL = "https://mapen.ark-nights.com/map/"
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
    stun_res: str = ""
    silence_res: str = ""
    sleep_res: str = ""
    freeze_res: str = ""
    levitate_res: str = ""
    abilities: list[str] = field(default_factory=list)


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
class OperatorTalent:
    slot: str = ""              # 第一天赋 / 第二天赋
    name: str = ""
    unlock: str = ""            # 精英1 / 精英2 X模组2级 ...
    description: str = ""


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


def _cache_file(params: dict[str, Any]) -> Path:
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return PRTS_CACHE_PATH / f"{digest}.json"


def _load_cache(path: Path, cache_seconds: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if time.time() - float(data.get("_cache_time", 0)) <= cache_seconds:
        return data.get("payload")
    return None


def _load_stale_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else None


def _save_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(
                {"_cache_time": time.time(), "payload": payload},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug(
            "[ArknightsUID] PRTS cache save failed: "
            f"{exc.__class__.__name__}"
        )


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
                    f"{exc.__class__.__name__}"
                )
    except Exception as exc:
        logger.debug(
            "[ArknightsUID] PRTS cache cleanup scan failed: "
            f"{exc.__class__.__name__}"
        )


async def _api(
    params: dict[str, Any],
    *,
    cache_seconds: int = 86400,
) -> dict[str, Any] | None:
    cache_path = _cache_file(params)
    cached = _load_cache(cache_path, cache_seconds)
    if cached is not None:
        _cleanup_stale_cache()
        return cached

    request_params = {"format": "json", **params}
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = await client.get(PRTS_API_URL, params=request_params)
            response.raise_for_status()
            payload = response.json()
            _save_cache(cache_path, payload)
            _cleanup_stale_cache()
            return payload
    except Exception as exc:
        logger.warning(
            "[ArknightsUID] PRTS API request failed: "
            f"{exc.__class__.__name__}"
        )
        return _load_stale_cache(cache_path)


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


async def parse_page(title: str, *, follow_redirect: bool = True) -> PageWikitext | None:
    current = title.strip()
    if not current:
        return None
    seen: set[str] = set()
    for _ in range(3):
        if current in seen:
            return None
        seen.add(current)
        data = await _api(
            {
                "action": "parse",
                "page": current,
                "prop": "wikitext",
            },
            cache_seconds=86400,
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
            lambda m: m.group(1).split("|")[-1],
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
        description=normal.get("关卡描述", ""),
        first_drops=_split_list(normal.get("首次掉落", "")),
        drops=_split_list(normal.get("常规掉落", "")),
        extra_drops=_split_list(normal.get("额外物资", "")),
        enemies=enemies,
        assault_info=assault_info,
        assault_fields=assault,
        assault_diffs=assault_diffs,
        source_url=page_url(page.title),
        map_url=map_url(stage_id),
    )


async def query_stage(query: str) -> StageInfo | None:
    query = query.strip()
    if not query:
        return None

    page = await parse_page(query)
    if page:
        stage = parse_stage_wikitext(page)
        if stage:
            return stage

    results = await search_pages(query, limit=8)
    for result in results:
        title = result.title
        if "/" in title and not re.search(r"\s\d+-\d+", title):
            continue
        page = await parse_page(title)
        if not page:
            continue
        stage = parse_stage_wikitext(page)
        if stage:
            return stage
    return None


def parse_enemy_wikitext(page: PageWikitext) -> EnemyInfo | None:
    common = _first_template_fields(page.wikitext, ("敌人信息/common2", "敌人信息"))
    level_blocks = _template_lines(page.wikitext, ("敌人信息/levelcontent",))
    if not common and not level_blocks:
        return None

    levels: list[EnemyLevel] = []
    for block in level_blocks:
        fields = _fields_from_template(block)
        abilities = []
        for key, value in fields.items():
            if any(mark in key for mark in ("能力", "技能", "天赋", "特殊")) and value:
                abilities.append(f"{key}：{value}")
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
                stun_res=fields.get("眩晕抗性", ""),
                silence_res=fields.get("沉默抗性", ""),
                sleep_res=fields.get("沉睡抗性", ""),
                freeze_res=fields.get("冻结抗性", ""),
                levitate_res=fields.get("浮空抗性", ""),
                abilities=abilities,
            )
        )

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
        # 天赋1-4：基础 + 模组逐级（条件/效果），全收集
        descriptions: list[str] = []
        for i in range(1, 5):
            cond = fields.get(f"天赋{i}条件", "")
            eff = fields.get(f"天赋{i}效果", "")
            if eff:
                descriptions.append(f"{cond}：{eff}" if cond else eff)
        talents.append(
            OperatorTalent(
                slot=slot,
                name=base_name,
                unlock=fields.get("天赋1条件", ""),
                description=" / ".join(descriptions),
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

    page = await parse_page(query)
    if page:
        enemy = parse_enemy_wikitext(page)
        if enemy:
            return enemy

    results = await search_pages(query, limit=8)
    for result in results:
        if "/" in result.title:
            continue
        page = await parse_page(result.title)
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

    page = await parse_page(query)
    if page:
        return parse_generic_page(page)

    results = await search_pages(query, limit=8)
    for result in results:
        if result.title.endswith(("/spine", "/BEG", "/END")):
            continue
        page = await parse_page(result.title)
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
    data = await _api(
        {
            "action": "cargoquery",
            "tables": "building_skill2",
            "fields": "name,room,icon,description",
            "where": f"name='{name}'",
            "limit": "1",
        },
        cache_seconds=86400,
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

    page = await parse_page(query)
    op = parse_operator_wikitext(page) if page else None
    if not op:
        results = await search_pages(query, limit=8)
        for result in results:
            if "/" in result.title:
                continue
            page = await parse_page(result.title)
            if not page:
                continue
            op = parse_operator_wikitext(page)
            if op:
                break
    if not op:
        return None

    # 后勤技能效果补全（Cargo 表，逐技能一次查询，_api 缓存 1 天）
    for base in op.base_skills:
        if base.description:
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


async def query_activities(limit: int = 15) -> list[ActivityInfo]:
    """进行中/未来的限时活动（按结束时间升序）。

    数据来自 PRTS 活动页的 SMW 属性（分类:有活动信息的页面），
    与 PRTS 首页轮播同源。now 取北京日期做过滤，已结束的由展示层兜底。
    """
    today = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d")
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
    now_ts = time.time()
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


async def query_gacha_pools(limit: int = 10) -> list[ActivityInfo]:
    """进行中/未来的限定卡池（SMW：Category:国服寻访）。"""
    today = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d")
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
    now_ts = time.time()
    pools: list[ActivityInfo] = []
    for title, info in results.items():
        prints = info.get("printouts") or {}
        # 卡池名 printout 或页面标题都可能带「寻访模拟/」前缀，取末段
        raw_name = _smw_first_value(prints.get("卡池名")) or title
        name = raw_name.split("/")[-1]
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
            )
        )
    return pools


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
