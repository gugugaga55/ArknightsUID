"""物品图标（PRTS 带框道具图）：材料名 → MediaWiki md5 路径 → base64。

卡片「图文并茂」的图标层：养成缺口 / 专精材料 / 材料掉率卡使用。
URL 规则（MediaWiki 客观事实）：文件 `道具_带框_{材料名}.png` 的存储路径为
`media.prts.wiki/{md5[0]}/{md5[:2]}/{urlencode(文件名)}`，与游戏 item_table 的
iconId 无关（PRTS 按中文名建档）。示例：固源岩组 → /2/2e/道具_带框_固源岩组.png。

下载走 `render_utils.get_image_b64_with_cache`（webp 磁盘缓存），失败返回空串，
模板侧 `{% if icon %}` 优雅降级为纯文本行。
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse

from gsuid_core.logger import logger

from ._table_cache import GAMEDATA_CACHE_DIR

ITEM_ICON_CACHE_DIR = GAMEDATA_CACHE_DIR / "item_icons"

# 材料名字符串里的数量分隔符（PRTS 文本 "技巧概要·卷3 ×8" / "D32钢x6" 等写法）
_COUNT_SPLIT = re.compile(r"^(.*?)\s*[×xX\*]\s*(\d+)\s*$")


def prts_item_icon_url(item_name: str) -> str:
    """材料名 → PRTS 带框道具图 URL（MediaWiki md5 散列路径）。"""
    name = item_name.strip()
    if not name:
        return ""
    filename = f"道具_带框_{name}.png"
    digest = hashlib.md5(filename.encode("utf-8")).hexdigest()
    return (
        f"https://media.prts.wiki/{digest[0]}/{digest[:2]}/"
        f"{urllib.parse.quote(filename)}"
    )


async def item_icon_b64(item_name: str) -> str:
    """材料名 → base64 data URL（带磁盘缓存；失败/空名返回空串）。"""
    url = prts_item_icon_url(item_name)
    if not url:
        return ""
    try:
        from ..render import get_image_b64_with_cache

        return await get_image_b64_with_cache(url, ITEM_ICON_CACHE_DIR)
    except Exception as exc:
        logger.debug(f"[ArknightsUID] 物品图标下载失败 {item_name}: {exc}")
        return ""


def split_material_text(text: str) -> tuple[str, str]:
    """拆分 PRTS 材料文本 "技巧概要·卷3 ×8" → ("技巧概要·卷3", "8")。"""
    match = _COUNT_SPLIT.match(text.strip())
    if match:
        return match.group(1).strip(), match.group(2)
    return text.strip(), ""
