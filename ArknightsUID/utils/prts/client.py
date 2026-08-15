from __future__ import annotations

import re
import json
import time
import html
import hashlib
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
USER_AGENT = "ArknightsUID/0.1 (+GsCore; PRTS MediaWiki API)"

PRTS_CACHE_PATH = CACHE_PATH / "prts"
PRTS_CACHE_PATH.mkdir(parents=True, exist_ok=True)


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


async def _api(
    params: dict[str, Any],
    *,
    cache_seconds: int = 86400,
) -> dict[str, Any] | None:
    cache_path = _cache_file(params)
    cached = _load_cache(cache_path, cache_seconds)
    if cached is not None:
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
    for index in range(1, 80):
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
