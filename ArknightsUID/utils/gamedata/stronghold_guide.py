"""卫戍协议攻略数据（森空岛 garrison guide 公开 Web API）。

数据源（game.skland.com/arknights/stronghold-guide H5 同源，匿名 GET 可用，
2026-11 真实探测验证；接口形态见 tools/probe_stronghold_guide.py）：

- ``GET https://zonai.skland.com/web/v1/game/garrison/guide/resource``
  静态资源：bonds（盟约 26：bondType 1=核心 / 2=附加，含效果与成员干员）、
  strategies（策略 61：band_* → 名称/发起干员 initiatorName/初始生命/效果）、
  chars（本期参战干员 218：技能/模组/特质/所属盟约）、items（道具 70）、
  seasons/versionInfo（act3autochess「规程」）
- ``GET https://zonai.skland.com/web/v1/game/garrison/guide/index``
  社区攻略列表（UGC，无需登录；source=COMMUNITY + sortType=1 推荐，
  sortType=2 最新需登录）。过滤参数（单数/复数易混，均为实测口径）：
  seasonBondId=核心盟约 / regularBondIds=附加盟约 / strategyId=策略 /
  titleKeyword=标题关键词 / seasonId / pageSize / pageToken

效果文本含官方富文本标记（``<@ba.vup>x</>`` 等），clean_garrison_text
只剥标记保留内容；``<在场6名干员>`` 这类字面尖括号不是标记，须保留。

resource ~540KB：磁盘缓存 24h（赛季内资源稳定，新一期靠 TTL 过期重拉）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from gsuid_core.logger import logger

from ..disk_cache import cache_read, cache_write, is_fresh
from ..http import get_client
from ..resource.RESOURCE_PATH import CACHE_PATH

GARRISON_BASE = "https://zonai.skland.com/web/v1/game/garrison/guide"
RESOURCE_URL = f"{GARRISON_BASE}/resource"
INDEX_URL = f"{GARRISON_BASE}/index"
# 匿名浏览器口径 UA（探测验证无需登录态/签名）
GARRISON_UA = (
    "Mozilla/5.0 (Linux; Android 12; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)

_GUIDE_CACHE_SECONDS = 24 * 3600
GARRISON_CACHE_DIR = CACHE_PATH / "stronghold_guide"
_RESOURCE_CACHE = GARRISON_CACHE_DIR / "resource.json"
GUIDE_DETAIL_URL = (
    "https://game.skland.com/arknights/stronghold-guide/detail?guideId="
)

# 查询意图前缀（「核心盟约 炎」「策略 阿米娅」这类带类型限定的输入）
_QUERY_PREFIXES = (
    "核心盟约",
    "附加盟约",
    "盟约",
    "策略",
    "干员",
)

# 组合查询分隔符：炎+不屈 / 炎、不屈 / 核心:炎|不屈（任意子集，条件间 AND）
_COMBO_SEPARATORS = "+＋、｜|"

# 富文本标记：<@ba.vup>、<$ba.stun>、<@autochess.gray>、</>（只剥标记，保留内文）
_MARKUP_OPEN = re.compile(r"<[@$][a-zA-Z0-9_.]+>")
_MARKUP_CLOSE = re.compile(r"</>")


def clean_garrison_text(text: str) -> str:
    """剥官方富文本标记；保留换行与字面尖括号文本。

    上游 effect 里混有两种换行：真实 ``\\n`` 与字面反斜杠+n 两字符
    （strategy.effect 实测后者），统一规整为真实换行再剥标记。
    """
    text = (text or "").replace("\\n", "\n")
    text = _MARKUP_OPEN.sub("", text)
    text = _MARKUP_CLOSE.sub("", text)
    return text.strip()


@dataclass(slots=True)
class GarrisonBondChar:
    """盟约成员干员引用。"""

    char_id: str = ""
    name: str = ""
    avatar: str = ""


@dataclass(slots=True)
class GarrisonBond:
    """盟约（核心 bondType=1 / 附加 bondType=2）。"""

    bond_id: str = ""
    name: str = ""
    bond_type: int = 0
    icon: str = ""
    active_count: int = 0
    effect: str = ""  # 清洗后（含换行）
    chars: list[GarrisonBondChar] = field(default_factory=list)

    @property
    def type_label(self) -> str:
        return "核心盟约" if self.bond_type == 1 else "附加盟约"


@dataclass(slots=True)
class GarrisonStrategy:
    """策略（初始策略，band_* 编号，发起干员 + 初始生命 + 效果）。"""

    strategy_id: str = ""
    name: str = ""
    initiator: str = ""  # 发起干员名（如 阿米娅）
    icon: str = ""
    hp: int = 0  # 初始生命值修正
    effect: str = ""  # 清洗后


@dataclass(slots=True)
class GarrisonCharSkill:
    skill_id: str = ""
    name: str = ""
    icon: str = ""


@dataclass(slots=True)
class GarrisonCharModule:
    module_id: str = ""
    name: str = ""
    icon: str = ""


@dataclass(slots=True)
class GarrisonTrait:
    trait_type: str = ""
    desc: str = ""  # 清洗后
    icon: str = ""
    merge_level: int = 0
    level: int = 0


@dataclass(slots=True)
class GarrisonChar:
    """本期参战干员（resource.chars 条目）。"""

    char_id: str = ""
    name: str = ""
    avatar: str = ""
    half_body: str = ""
    profession: str = ""
    sub_profession: str = ""
    skills: list[GarrisonCharSkill] = field(default_factory=list)
    modules: list[GarrisonCharModule] = field(default_factory=list)
    traits: list[GarrisonTrait] = field(default_factory=list)
    bonds: list[GarrisonBond] = field(default_factory=list)  # 该干员所属盟约
    is_diy: bool = False


@dataclass(slots=True)
class GarrisonGuideEntry:
    """一条社区攻略（guide/index 列表项的展示裁剪）。"""

    guide_id: str = ""
    title: str = ""
    scope: str = ""  # 适用范围
    idea: str = ""  # 运营思路（清洗后）
    poster: str = ""
    liked: str = ""
    viewed: str = ""
    strategy_ids: list[str] = field(default_factory=list)
    season_bond_ids: list[str] = field(default_factory=list)
    regular_bond_ids: list[str] = field(default_factory=list)
    url: str = ""


@dataclass(slots=True)
class GarrisonResource:
    """resource 静态数据 + 查询索引。"""

    bonds: dict[str, GarrisonBond] = field(default_factory=dict)
    strategies: dict[str, GarrisonStrategy] = field(default_factory=dict)
    chars: dict[str, GarrisonChar] = field(default_factory=dict)
    items: dict[str, dict[str, str]] = field(default_factory=dict)
    season_id: str = ""
    season_name: str = ""
    version_id: str = ""
    phase_name: str = ""


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _dict_entries(value: Any) -> list[dict[str, Any]]:
    """上游列表可能包含空项或被替换为其他类型。"""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _resource_well_formed(data: Any) -> bool:
    """仅缓存结构完整的资源；字段缺失或字段整体为 null 仍可降级。"""
    if not isinstance(data, dict):
        return False

    def valid_entries(value: Any) -> bool:
        return value is None or (
            isinstance(value, list) and all(isinstance(item, dict) for item in value)
        )

    for key in ("chars", "bonds", "strategies", "items", "seasons"):
        if not valid_entries(data.get(key)):
            return False
    if data.get("versionInfo") is not None and not isinstance(data["versionInfo"], dict):
        return False
    bonds = list(data.get("bonds") or [])
    for char in data.get("chars") or []:
        for key in ("skills", "modules", "traits", "bonds"):
            if not valid_entries(char.get(key)):
                return False
        bonds.extend(char.get("bonds") or [])
    return all(valid_entries(bond.get("chars")) for bond in bonds)


def _parse_bond(raw: dict[str, Any], name_lookup: dict[str, str]) -> GarrisonBond:
    members: list[GarrisonBondChar] = []
    for ref in _dict_entries(raw.get("chars")):
        char_id = str(ref.get("charId") or "")
        members.append(
            GarrisonBondChar(
                char_id=char_id,
                name=name_lookup.get(char_id, ""),
                avatar=str(ref.get("avatarUrl") or ""),
            )
        )
    return GarrisonBond(
        bond_id=str(raw.get("bondId") or ""),
        name=str(raw.get("name") or ""),
        bond_type=_to_int(raw.get("bondType")),
        icon=str(raw.get("iconUrl") or ""),
        active_count=_to_int(raw.get("activeCount")),
        effect=clean_garrison_text(str(raw.get("effect") or "")),
        chars=members,
    )


def parse_resource(data: dict[str, Any]) -> GarrisonResource:
    """解析 resource.data → GarrisonResource（纯函数）。"""
    res = GarrisonResource()
    chars_raw = _dict_entries(data.get("chars"))
    name_lookup = {
        str(c.get("charId") or ""): str(c.get("name") or "") for c in chars_raw
    }

    # 先解析顶层完整 bonds（chars[].bonds 在真实接口中为 effect="" 的浅层占位引用）
    for raw in _dict_entries(data.get("bonds")):
        bond = _parse_bond(raw, name_lookup)
        if bond.bond_id:
            res.bonds[bond.bond_id] = bond

    for raw in chars_raw:
        if not isinstance(raw, dict):
            continue
        char = GarrisonChar(
            char_id=str(raw.get("charId") or ""),
            name=str(raw.get("name") or ""),
            avatar=str(raw.get("avatarUrl") or ""),
            half_body=str(raw.get("halfBodyUrl") or ""),
            profession=str(raw.get("charProfession") or ""),
            sub_profession=str(raw.get("charSubProfession") or ""),
            is_diy=bool(raw.get("isDiyChar")),
        )
        for skill in _dict_entries(raw.get("skills")):
            char.skills.append(
                GarrisonCharSkill(
                    skill_id=str(skill.get("skillId") or ""),
                    name=str(skill.get("name") or ""),
                    icon=str(skill.get("iconUrl") or ""),
                )
            )
        for module in _dict_entries(raw.get("modules")):
            icon = module.get("icon")
            char.modules.append(
                GarrisonCharModule(
                    module_id=str(module.get("moduleId") or ""),
                    name=str(module.get("name") or ""),
                    icon=str((icon or {}).get("type") or "")
                    if isinstance(icon, dict)
                    else "",
                )
            )
        for trait in _dict_entries(raw.get("traits")):
            char.traits.append(
                GarrisonTrait(
                    trait_type=str(trait.get("traitType") or ""),
                    desc=clean_garrison_text(str(trait.get("traitDesc") or "")),
                    icon=str(trait.get("traitIconUrl") or ""),
                    merge_level=_to_int(trait.get("mergeLevel")),
                    level=_to_int(trait.get("level")),
                )
            )
        for bond_raw in _dict_entries(raw.get("bonds")):
            parsed_bond = _parse_bond(bond_raw, name_lookup)
            full_bond = res.bonds.get(parsed_bond.bond_id)
            if full_bond is not None:
                parsed_bond = GarrisonBond(
                    bond_id=parsed_bond.bond_id,
                    name=parsed_bond.name or full_bond.name,
                    bond_type=parsed_bond.bond_type or full_bond.bond_type,
                    icon=parsed_bond.icon or full_bond.icon,
                    active_count=parsed_bond.active_count or full_bond.active_count,
                    effect=parsed_bond.effect or full_bond.effect,
                    chars=parsed_bond.chars or list(full_bond.chars),
                )
            char.bonds.append(parsed_bond)
        if char.char_id:
            res.chars[char.char_id] = char

    for raw in _dict_entries(data.get("strategies")):
        strategy = GarrisonStrategy(
            strategy_id=str(raw.get("strategyId") or ""),
            name=str(raw.get("name") or ""),
            initiator=str(raw.get("initiatorName") or ""),
            icon=str(raw.get("iconUrl") or ""),
            hp=_to_int(raw.get("hp")),
            effect=clean_garrison_text(str(raw.get("effect") or "")),
        )
        if strategy.strategy_id:
            res.strategies[strategy.strategy_id] = strategy

    for raw in _dict_entries(data.get("items")):
        item_id = str(raw.get("itemId") or "")
        if item_id:
            res.items[item_id] = {
                "name": str(raw.get("name") or ""),
                "icon": str(raw.get("iconUrl") or ""),
                "effect": clean_garrison_text(str(raw.get("effectDesc") or "")),
            }

    seasons = _dict_entries(data.get("seasons"))
    if seasons:
        res.season_id = str(seasons[0].get("seasonId") or "")
        res.season_name = str(seasons[0].get("name") or "")
    version = data.get("versionInfo")
    version = version if isinstance(version, dict) else {}
    res.version_id = str(version.get("versionId") or "")
    res.phase_name = str(version.get("phaseName") or "")
    return res


async def _fetch_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """匿名 GET（公开接口；错误吞掉返回 None 由调用方兜底）。"""
    client = await get_client()
    try:
        response = await client.get(
            url,
            params=params,
            headers={"User-Agent": GARRISON_UA, "Accept": "application/json"},
            timeout=20.0,
        )
        if response.status_code != 200:
            logger.warning(
                f"[ArknightsUID] garrison guide 接口 HTTP {response.status_code}: {url}"
            )
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ArknightsUID] garrison guide 请求失败: {exc}")
        return None


async def fetch_resource(force: bool = False) -> GarrisonResource | None:
    """拉取（或读 24h 缓存的）卫戍攻略静态资源。

    拉取失败但存在过期缓存时降级用旧数据（赛季内资源高度稳定）。
    """
    cached, saved_ts = (None, 0.0)
    if not force:
        cached, saved_ts = await cache_read(_RESOURCE_CACHE)
        if cached is not None and not _resource_well_formed(cached):
            cached = None
        if cached is not None and is_fresh(saved_ts, _GUIDE_CACHE_SECONDS):
            return parse_resource(cached)

    payload = await _fetch_json(RESOURCE_URL)
    if payload is not None and payload.get("code") == 0:
        data = payload.get("data")
        if _resource_well_formed(data):
            GARRISON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            await cache_write(_RESOURCE_CACHE, data)
            return parse_resource(data)
        logger.warning("[ArknightsUID] 卫戍攻略资源结构异常，未写入缓存")
    if cached is not None:
        return parse_resource(cached)
    return None


def _parse_guide_entry(item: dict[str, Any]) -> GarrisonGuideEntry | None:
    guide = item.get("guide")
    if not isinstance(guide, dict):
        return None
    rts = item.get("rts") or {}
    guide_id = str(guide.get("guideId") or "")
    tabs = _dict_entries(guide.get("tabs"))

    raw_idea = str(guide.get("operationIdea") or "")
    if not raw_idea:
        for tab in tabs:
            tab_idea = str(tab.get("operationIdea") or "").strip()
            if tab_idea:
                raw_idea = tab_idea
                break

    season_bond_ids: list[str] = []
    regular_bond_ids: list[str] = []
    bond_sources: list[dict[str, Any]] = []
    if isinstance(guide.get("activeBonds"), dict):
        bond_sources.append(guide["activeBonds"])
    for tab in tabs:
        if isinstance(tab.get("activeBonds"), dict):
            bond_sources.append(tab["activeBonds"])
    for active in bond_sources:
        for s in active.get("seasonBondIds") or []:
            sid = str(s or "")
            if sid and sid not in season_bond_ids:
                season_bond_ids.append(sid)
        for s in active.get("regularBondIds") or []:
            rid = str(s or "")
            if rid and rid not in regular_bond_ids:
                regular_bond_ids.append(rid)

    return GarrisonGuideEntry(
        guide_id=guide_id,
        title=str(guide.get("title") or ""),
        scope=str(guide.get("applicableScope") or ""),
        idea=clean_garrison_text(raw_idea),
        poster=str((item.get("poster") or {}).get("nickname") or ""),
        liked=str(rts.get("liked") or "0"),
        viewed=str(rts.get("viewed") or "0"),
        strategy_ids=[str(s) for s in guide.get("strategyIds") or []],
        season_bond_ids=season_bond_ids,
        regular_bond_ids=regular_bond_ids,
        url=f"{GUIDE_DETAIL_URL}{guide_id}" if guide_id else "",
    )


async def fetch_guides(
    resource: GarrisonResource,
    *,
    title_keyword: str = "",
    season_bond_id: str = "",
    regular_bond_ids: list[str] | None = None,
    strategy_id: str = "",
    limit: int = 5,
) -> list[GarrisonGuideEntry]:
    """按过滤条件拉社区攻略列表（匿名=推荐排序；limit 服务端上限 10）。"""
    params: dict[str, Any] = {
        "source": "COMMUNITY",
        "sortType": 1,
        "seasonId": resource.season_id,
        "pageSize": max(1, min(limit, 10)),
        "pageToken": "",
    }
    if title_keyword:
        params["titleKeyword"] = title_keyword
    if season_bond_id:
        params["seasonBondId"] = season_bond_id
    if regular_bond_ids:
        params["regularBondIds"] = ",".join(regular_bond_ids)
    if strategy_id:
        params["strategyId"] = strategy_id

    payload = await _fetch_json(INDEX_URL, params)
    if payload is None or payload.get("code") != 0:
        return []
    entries: list[GarrisonGuideEntry] = []
    for item in (payload.get("data") or {}).get("list") or []:
        entry = _parse_guide_entry(item) if isinstance(item, dict) else None
        if entry is not None and entry.title:
            entries.append(entry)
    return entries[:limit]


def strip_query_prefix(text: str) -> tuple[str, str]:
    """「策略 阿米娅」→ ("策略", "阿米娅")；无前缀 → ("", 原文)。"""
    text = text.strip()
    for prefix in _QUERY_PREFIXES:
        if text.startswith(prefix):
            rest = text[len(prefix):].strip().lstrip("：: ").strip()
            if rest:
                return prefix, rest
    return "", text


def _match_exact_then_contains(
    items: list[tuple[str, str]], keyword: str
) -> tuple[str | None, list[str]]:
    """[(key, 名称)] → (唯一精确/包含命中 key, 候选名列表)。"""
    exact = [key for key, name in items if name == keyword]
    if len(exact) == 1:
        return exact[0], []
    contains = sorted(
        {name for _, name in items if keyword in name and keyword != name}
        | {name for _, name in items if name == keyword}
    )
    if len(exact) > 1:
        return None, contains
    forward = [key for key, name in items if keyword in name]
    if len(forward) == 1:
        return forward[0], []
    if len(forward) > 1:
        candidates = sorted({name for key, name in items if key in forward})
        return None, candidates[:8]
    hits = [key for key, name in items if name and name in keyword]
    if len(hits) == 1:
        return hits[0], []
    candidates = sorted({name for key, name in items if key in hits})
    return None, candidates[:8]


_COMBO_PREFIX_RE = re.compile(
    r"(?:^|\s)(" + "|".join(_QUERY_PREFIXES) + r")(?=\s*[:：]|\s+\S)"
)


def parse_combo_tokens(text: str) -> list[str] | None:
    """组合查询分词：返回 >1 个词条时进入组合模式，否则 None（单查询）。

    两种写法（实测服务端对 seasonBondId/regularBondIds/strategyId 是
    AND 语义，任意子集可组合）：
    - 分隔符：``炎+不屈+阿米娅`` / ``炎、不屈``
    - 多前缀：``核心盟约 炎 附加盟约 不屈``（每个位于词首且后接分隔符的前缀切一段，
      不会把「盟约 协防干员」里的词尾「干员」误切开）
    """
    text = text.strip()
    if not text:
        return None
    chunks = [c.strip() for c in re.split(f"[{_COMBO_SEPARATORS}]", text)]
    chunks = [c for c in chunks if c]
    if len(chunks) > 1:
        return chunks

    prefix_spots = [m.start(1) for m in _COMBO_PREFIX_RE.finditer(text)]
    if len(prefix_spots) > 1:
        parts: list[str] = []
        for i, start in enumerate(prefix_spots):
            end = prefix_spots[i + 1] if i + 1 < len(prefix_spots) else len(text)
            parts.append(text[start:end].strip())
        return [p for p in parts if p]
    return None


def resolve_combo(
    resource: GarrisonResource, tokens: list[str]
) -> dict[str, Any]:
    """多条件解析 → {ok, season_bond, regular_bonds, strategy, labels} / {ok:False, error}。

    服务端口径：核心盟约 ≤1、附加盟约 可多个、策略 ≤1，条件间 AND；
    同维度冲突（两个核心盟约）与干员词条（无法服务端过滤）直接报错。
    """
    season_bond: GarrisonBond | None = None
    regular_bonds: list[GarrisonBond] = []
    strategy: GarrisonStrategy | None = None
    errors: list[str] = []

    for token in tokens:
        prefix, keyword = strip_query_prefix(token)
        if prefix == "干员":
            errors.append(f"「{keyword}」：组合查询不支持干员（干员走单查询）")
            continue
        # 带类型前缀时用通用盟约池解析后自行核对类型，错型给明确报错
        # （「核心盟约 不屈」：不屈其实是附加盟约）
        lookup_prefix = "盟约" if prefix in ("核心盟约", "附加盟约") else (
            prefix or "盟约"
        )
        result = resolve_query(resource, keyword, lookup_prefix)
        kind = result["kind"]
        if kind == "bond":
            bond: GarrisonBond = result["bond"]
            if prefix == "核心盟约" and bond.bond_type != 1:
                errors.append(f"「{bond.name}」是附加盟约，不是核心盟约")
                continue
            if prefix == "附加盟约" and bond.bond_type != 2:
                errors.append(f"「{bond.name}」是核心盟约，不是附加盟约")
                continue
            if bond.bond_type == 1:
                if season_bond is not None and season_bond.bond_id != bond.bond_id:
                    errors.append(
                        f"核心盟约冲突：{season_bond.name} / {bond.name}（仅能选一个）"
                    )
                elif season_bond is None:
                    season_bond = bond
            elif all(b.bond_id != bond.bond_id for b in regular_bonds):
                regular_bonds.append(bond)
        elif kind == "strategy":
            found: GarrisonStrategy = result["strategy"]
            if strategy is not None and strategy.strategy_id != found.strategy_id:
                errors.append(
                    f"策略冲突：{strategy.name} / {found.name}（仅能选一个）"
                )
            elif strategy is None:
                strategy = found
        elif kind == "ambiguous":
            errors.append(f"「{keyword}」有多个候选：{'、'.join(result['candidates'][:5])}")
        else:
            # 单词在盟约池未命中时回落到策略（裸词组合的常见形态：
            # 「炎+不屈+阿米娅」中的 阿米娅 是策略发起人）
            fallback = resolve_query(resource, keyword, "策略")
            if fallback["kind"] == "strategy":
                found = fallback["strategy"]
                if strategy is not None and strategy.strategy_id != found.strategy_id:
                    errors.append(
                        f"策略冲突：{strategy.name} / {found.name}（仅能选一个）"
                    )
                elif strategy is None:
                    strategy = found
            else:
                errors.append(f"未识别「{keyword}」")

    if errors:
        return {"ok": False, "error": "；".join(errors)}

    labels: list[dict[str, str]] = []
    if season_bond is not None:
        labels.append({"label": "核心盟约", "value": season_bond.name})
    for bond in regular_bonds:
        labels.append({"label": "附加盟约", "value": bond.name})
    if strategy is not None:
        value = strategy.name
        if strategy.initiator and strategy.initiator != strategy.name:
            value = f"{strategy.name}·{strategy.initiator}"
        labels.append({"label": "策略", "value": value})
    return {
        "ok": True,
        "season_bond": season_bond,
        "regular_bonds": regular_bonds,
        "strategy": strategy,
        "labels": labels,
    }


def resolve_query(
    resource: GarrisonResource, keyword: str, prefix: str = ""
) -> dict[str, Any]:
    """查询解析：意图限定（prefix）或全维度匹配。

    返回 {kind: bond|strategy|char|ambiguous|none, ...}；kind=ambiguous 时
    带 candidates（候选名列表）；命中时带对应对象与（策略/盟约的）关联攻略
    需另行 fetch（调用方控制网络次数）。
    """
    want_bond = prefix in ("核心盟约", "附加盟约", "盟约")
    want_strategy = prefix == "策略"
    want_char = prefix == "干员"

    if want_bond:
        pool = [
            (b.bond_id, b.name)
            for b in resource.bonds.values()
            if prefix != "核心盟约" or b.bond_type == 1
        ]
        if prefix == "附加盟约":
            pool = [(i, n) for i, n in pool
                    if resource.bonds[i].bond_type == 2]
        key, candidates = _match_exact_then_contains(pool, keyword)
        if key:
            return {"kind": "bond", "bond": resource.bonds[key]}
        return {"kind": "ambiguous" if candidates else "none",
                "candidates": candidates}

    if want_strategy:
        # 策略支持两种命中：策略名 / 发起干员名（如「策略 阿米娅」）
        by_initiator = [
            s.strategy_id
            for s in resource.strategies.values()
            if s.initiator == keyword
        ]
        if len(by_initiator) == 1:
            return {"kind": "strategy",
                    "strategy": resource.strategies[by_initiator[0]]}
        pool = [(s.strategy_id, s.name) for s in resource.strategies.values()]
        key, candidates = _match_exact_then_contains(pool, keyword)
        if key:
            return {"kind": "strategy", "strategy": resource.strategies[key]}
        initiator_hits = sorted(
            {s.initiator for s in resource.strategies.values()
             if keyword in s.initiator}
        )
        return {"kind": "ambiguous" if candidates or initiator_hits else "none",
                "candidates": (candidates + initiator_hits)[:8]}

    if want_char:
        pool = [(c.char_id, c.name) for c in resource.chars.values()]
        key, candidates = _match_exact_then_contains(pool, keyword)
        if key:
            return {"kind": "char", "char": resource.chars[key]}
        return {"kind": "ambiguous" if candidates else "none",
                "candidates": candidates}

    # 无前缀：优先全维度精确匹配，其次正向子串（keyword in name），最后多字反向包含，
    # 防止单字盟约（如「炎」）劫持「炎客」「炎狱炎熔」等干员查询。
    bond_exact = [b for b in resource.bonds.values() if b.name == keyword]
    if len(bond_exact) == 1:
        return {"kind": "bond", "bond": bond_exact[0]}
    strat_exact = [s for s in resource.strategies.values() if s.name == keyword]
    if len(strat_exact) == 1:
        return {"kind": "strategy", "strategy": strat_exact[0]}
    strat_init_exact = [s for s in resource.strategies.values() if s.initiator == keyword]
    if len(strat_init_exact) == 1:
        return {"kind": "strategy", "strategy": strat_init_exact[0]}
    char_exact = [c for c in resource.chars.values() if c.name == keyword]
    if len(char_exact) == 1:
        return {"kind": "char", "char": char_exact[0]}
    if len(strat_init_exact) > 1:
        return {
            "kind": "ambiguous",
            "candidates": sorted({s.name for s in strat_init_exact})[:8],
        }

    # 第二轮：正向包含（keyword in name）按 盟约 → 策略 → 干员
    bond_fwd = [b for b in resource.bonds.values() if keyword in b.name]
    if len(bond_fwd) == 1:
        return {"kind": "bond", "bond": bond_fwd[0]}
    if len(bond_fwd) > 1:
        return {"kind": "ambiguous", "candidates": sorted({b.name for b in bond_fwd})[:8]}
    strat_fwd = [
        s
        for s in resource.strategies.values()
        if keyword in s.name or (s.initiator and keyword in s.initiator)
    ]
    if len(strat_fwd) == 1:
        return {"kind": "strategy", "strategy": strat_fwd[0]}
    if len(strat_fwd) > 1:
        return {"kind": "ambiguous", "candidates": sorted({s.name for s in strat_fwd})[:8]}
    char_fwd = [c for c in resource.chars.values() if keyword in c.name]
    if len(char_fwd) == 1:
        return {"kind": "char", "char": char_fwd[0]}
    if len(char_fwd) > 1:
        return {"kind": "ambiguous", "candidates": sorted({c.name for c in char_fwd})[:8]}

    # 第三轮：双字及以上反向包含兜底（单字如「炎」不做无前缀反向包含，防误伤未知词）
    for kind, pool_objs in (
        ("bond", [b for b in resource.bonds.values() if len(b.name) >= 2 and b.name in keyword]),
        ("strategy", [s for s in resource.strategies.values() if len(s.name) >= 2 and s.name in keyword]),
        ("char", [c for c in resource.chars.values() if len(c.name) >= 2 and c.name in keyword]),
    ):
        if len(pool_objs) == 1:
            return {"kind": kind, kind: pool_objs[0]}
        if len(pool_objs) > 1:
            return {
                "kind": "ambiguous",
                "candidates": sorted({o.name for o in pool_objs})[:8],
            }
    return {"kind": "none", "candidates": []}
