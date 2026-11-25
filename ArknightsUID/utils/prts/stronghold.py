"""PRTS「卫戍协议」攻略页解析（模式总览卡数据源）。

页面结构（prts.wiki/w/卫戍协议，wikitext 2026-11 抓取样例）：

- ``== 卫戍协议简述 ==``：一段玩法导语 + ``{{登场敌人}}`` 聚合模板
  （Lua 渲染，正文拿不到列表，整块剥离）
- ``== 物资的调配 ==``：导语 + ``=== 干员/盟约/特质/道具 ===`` 四小节，
  正文是 prose + ``*`` 列表 + ``:`` 缩进注记
- ``== 模拟流程 ==``：``* 模拟准备/选择策略/作战与结算`` 顶层列表；
  ``=== 开始模拟 ===`` 子节含联机/难度两张 ``sp-mode-table`` wikitable

攻略页随赛季改写，缓存走 24h（parse_page 默认静态页 30 天不适用）。
解析全部为纯函数（输入 wikitext），网络只集中在 fetch_stronghold_guide。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .client import _clean_wiki_value, page_url, parse_page

STRONGHOLD_PAGE = "卫戍协议"
# 攻略正文随赛季更新：24h 复核（非静态页口径）
_GUIDE_CACHE_SECONDS = 24 * 3600

# 卡片可读性上限：列表条数与单条长度（超出截断加省略号）
_MAX_BULLETS = 6
_MAX_BULLET_CHARS = 90
_MAX_PROSE_CHARS = 140

_MODE_TABLE_OPEN = '{| class="sp-mode-table"'


@dataclass(slots=True)
class GuideEntry:
    """联机方式/难度模式表里的一个条目。"""

    name: str = ""
    tagline: str = ""
    bullets: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StrongholdGuide:
    """卫戍协议模式总览（PRTS 口径）。"""

    intro: str = ""  # 简述导语
    supply_sections: list[tuple[str, str, list[str]]] = field(
        default_factory=list
    )  # 物资的调配：[(小节名, 导语, bullets)]
    flow_bullets: list[str] = field(default_factory=list)  # 模拟流程三阶段
    coop_modes: list[GuideEntry] = field(default_factory=list)  # 联机方式
    difficulty_modes: list[GuideEntry] = field(default_factory=list)  # 难度模式
    source_url: str = ""


def _strip_block_templates(text: str) -> str:
    """剥离整块型模板/HTML：登场敌人聚合、#widget、导航、HTML 注释。"""
    text = re.sub(r"\{\{登场敌人[\s\S]*?\}\}", "", text)
    text = re.sub(r"\{\{卫戍协议导航\}\}", "", text)
    # #widget 行内联 CSS 花括号会干扰模板匹配，按行丢弃整行
    text = "\n".join(
        line for line in text.splitlines() if "{{#widget:" not in line
    )
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    return text


def _clean(text: str) -> str:
    return _clean_wiki_value(_strip_block_templates(text))


def _trim(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _extract_bullets(body: str) -> list[str]:
    """``*`` 列表 → 纯文本 bullets；``:`` 缩进注记并入上一条。"""
    bullets: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("*"):
            text = _trim(_clean(stripped.lstrip("*")), _MAX_BULLET_CHARS)
            if text:
                bullets.append(text)
        elif stripped.startswith(":") and bullets:
            note = _trim(_clean(stripped.lstrip(":*")), 60)
            if note:
                bullets[-1] = _trim(f"{bullets[-1].rstrip('。')}：{note}", _MAX_BULLET_CHARS)
        if len(bullets) >= _MAX_BULLETS:
            break
    return bullets


def _first_prose(body: str) -> str:
    """第一段非空非列表正文（用作小节导语）。

    道具等小节 prose 与 ``*`` 列表之间没有空行，段内出现列表行时
    只取列表之前的部分。
    """
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para or para.startswith(("*", ":", "{|", "=")):
            continue
        head = re.split(r"(?m)^\*", para, maxsplit=1)[0]
        text = _trim(_clean(head), _MAX_PROSE_CHARS)
        if text:
            return text
    return ""


def _split_sections(
    wikitext: str,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """按 ``==``/``===`` 标题分层切节。

    返回 ({二级节名: {"": 节导语, 三级节名: 正文}}, 二级节名顺序)。
    """
    sections: dict[str, dict[str, str]] = {}
    order: list[str] = []
    top = ""
    sub = ""
    buf: list[str] = []

    def flush() -> None:
        if not top:
            return
        body = "\n".join(buf).strip()
        bucket = sections.setdefault(top, {})
        if sub:
            bucket[sub] = (bucket.get(sub, "") + "\n" + body).strip()
        else:
            bucket[""] = (bucket.get("", "") + "\n" + body).strip()

    for line in wikitext.splitlines():
        match = re.fullmatch(r"(={2,4})\s*(.+?)\s*\1", line.strip())
        if match:
            flush()
            buf = []
            if len(match.group(1)) == 2:
                top = match.group(2)
                sub = ""
                if top not in order:
                    order.append(top)
            else:
                sub = match.group(2)
            continue
        buf.append(line)
    flush()
    return sections, order


def _parse_mode_table(block: str) -> list[GuideEntry]:
    """sp-mode-table wikitable → 条目列表。

    表结构（该页两表通用）：``|-`` 一行一格，每条目依次为
    名字格（粗体 ``'''名'''``）/ 简介格 / ``*`` 列表格。
    名字格按**原始 wikitext 含粗体标记**判定——简介行可能比名字还短
    （「单人游玩的模拟作战。」），按长度猜会把简介错认成新条目。
    同盟模拟的 AKCollapse 展开块自成格子，卡片不需要，整格丢弃。
    """
    entries: list[GuideEntry] = []
    current: GuideEntry | None = None
    for raw_cell in re.split(r"(?m)^\|-.*$", block):
        cell = raw_cell.strip()
        if not cell or cell.startswith("{|") or cell.startswith("!"):
            continue
        if "AKCollapse" in cell:
            continue
        # 去掉单元格属性前缀 |style="…"|（属性段内无 |）；裸格 |内容
        cell = re.sub(
            r'^\|(?:style|class)=?"?[^"|]*"?[^|]*\|', "", cell, count=1
        )
        if cell.startswith("|"):
            cell = cell[1:]
        if not cell.strip():
            continue
        is_name = "'''" in cell
        if cell.lstrip().startswith("*"):
            if current is not None:
                current.bullets.extend(_extract_bullets(cell))
            continue
        text = _trim(_clean(cell), 80)
        if not text:
            continue
        if is_name:
            current = GuideEntry(name=text)
            entries.append(current)
        elif current is not None:
            current.tagline = _trim(f"{current.tagline} {text}".strip(), 80)
    return [entry for entry in entries if entry.name]


def parse_stronghold_guide(wikitext: str) -> StrongholdGuide:
    """解析「卫戍协议」页 wikitext → StrongholdGuide（纯函数）。"""
    guide = StrongholdGuide(source_url=page_url(STRONGHOLD_PAGE))
    sections, order = _split_sections(_strip_block_templates(wikitext))

    for name in order:
        bucket = sections[name]
        if name.endswith("简述"):
            guide.intro = _first_prose(bucket.get("", ""))
        elif "调配" in name:
            for sub_name, body in bucket.items():
                if not sub_name:
                    continue
                guide.supply_sections.append(
                    (sub_name, _first_prose(body), _extract_bullets(body))
                )
        elif "流程" in name:
            for sub_name, body in bucket.items():
                if "开始模拟" in sub_name:
                    for table in re.findall(
                        re.escape(_MODE_TABLE_OPEN) + r"[\s\S]*?\|\}",
                        body,
                    ):
                        entries = _parse_mode_table(table)
                        if any(
                            "独立" in e.name or "同盟" in e.name for e in entries
                        ):
                            guide.coop_modes = entries
                        elif entries:
                            guide.difficulty_modes = entries
                elif not sub_name:
                    guide.flow_bullets = _extract_bullets(body)
    return guide


async def fetch_stronghold_guide() -> StrongholdGuide | None:
    """抓取并解析 PRTS 卫戍协议页（24h 缓存）。失败返回 None。"""
    page = await parse_page(STRONGHOLD_PAGE, cache_seconds=_GUIDE_CACHE_SECONDS)
    if page is None or not page.wikitext.strip():
        return None
    return parse_stronghold_guide(page.wikitext)
