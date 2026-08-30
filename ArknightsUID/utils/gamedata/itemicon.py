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
import time
import urllib.parse
from pathlib import Path

from gsuid_core.logger import logger

from ._table_cache import GAMEDATA_CACHE_DIR

ITEM_ICON_CACHE_DIR = GAMEDATA_CACHE_DIR / "item_icons"

# 图标失败负缓存（1h）：URL 按名字散列构造、不查文件存在性，名字对不上
# 时（如带修饰的敌人名「源石虫·α」）每次都打 CDN；关卡敌人数值一次最多
# 取 10 个头像，不缓存会把失败请求放大。1h 平衡瞬时网络失败的误伤。
_ICON_MISS_TTL = 3600.0


async def _fetch_icon_with_negcache(url: str, cache_dir: Path) -> str:
    """图标下载 + 失败 .miss 负缓存（与立绘 char_portrait_b64 同款机制）。"""
    try:
        from ..render import get_image_b64_with_cache
    except Exception:
        return ""
    from .path_safety import safe_url_basename

    miss_path = cache_dir / f"{safe_url_basename(url)}.miss"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if miss_path.exists():
            if time.time() - miss_path.stat().st_mtime < _ICON_MISS_TTL:
                return ""
            miss_path.unlink()
        b64 = await get_image_b64_with_cache(url, cache_dir)
        if b64:
            return b64
        miss_path.write_text("", encoding="utf-8")
        return ""
    except Exception:
        return ""


# 材料名字符串里的数量分隔符（PRTS 文本 "技巧概要·卷3 ×8" / "D32钢x6" 等写法）
_COUNT_SPLIT = re.compile(r"^(.*?)\s*[×xX\*]\s*(\d+)\s*$")


def prts_item_icon_url(item_name: str) -> str:
    """材料名 → PRTS 带框道具图 URL（MediaWiki md5 散列路径）。

    PRTS 掉落文本自带掉落方式后缀（"固源岩:概率掉落"/"全新装置:小概率"），
    图标文件名只有净材料名——先剥后缀，否则拼出 404 的 URL（道具_带框_固源岩:概率掉落.png 不存在）。
    材料名本身不含冒号，剥离安全；显示文本不受影响（调用方各自保留原文）。
    """
    name = item_name.split(":")[0].split("：")[0].strip()
    if not name:
        return ""
    filename = f"道具_带框_{name}.png"
    digest = hashlib.md5(filename.encode("utf-8")).hexdigest()
    return (
        f"https://media.prts.wiki/{digest[0]}/{digest[:2]}/"
        f"{urllib.parse.quote(filename)}"
    )


async def item_icon_b64(item_name: str) -> str:
    """材料名 → base64 data URL（带磁盘缓存 + 失败负缓存；失败/空名返回空串）。"""
    url = prts_item_icon_url(item_name)
    if not url:
        return ""
    return await _fetch_icon_with_negcache(url, ITEM_ICON_CACHE_DIR)


def split_material_text(text: str) -> tuple[str, str]:
    """拆分 PRTS 材料文本 "技巧概要·卷3 ×8" → ("技巧概要·卷3", "8")。"""
    match = _COUNT_SPLIT.match(text.strip())
    if match:
        return match.group(1).strip(), match.group(2)
    return text.strip(), ""


def prts_enemy_icon_url(enemy_name: str) -> str:
    """敌人名 → PRTS 敌人头像 URL（`头像_敌人_{名字}.png`，同一 md5 规则）。"""
    name = enemy_name.strip()
    if not name:
        return ""
    filename = f"头像_敌人_{name}.png"
    digest = hashlib.md5(filename.encode("utf-8")).hexdigest()
    return (
        f"https://media.prts.wiki/{digest[0]}/{digest[:2]}/"
        f"{urllib.parse.quote(filename)}"
    )


async def enemy_icon_b64(enemy_name: str) -> str:
    """敌人头像 base64（带磁盘缓存 + 失败负缓存；失败/空名返回空串）。"""
    url = prts_enemy_icon_url(enemy_name)
    if not url:
        return ""
    return await _fetch_icon_with_negcache(url, ITEM_ICON_CACHE_DIR)


def prts_profession_icon_url(profession_name: str) -> str:
    """职业名 → PRTS 职业图标 URL（`图标_职业_{职业名}.png`，同一 md5 规则）。"""
    name = profession_name.strip()
    if not name:
        return ""
    filename = f"图标_职业_{name}.png"
    digest = hashlib.md5(filename.encode("utf-8")).hexdigest()
    return (
        f"https://media.prts.wiki/{digest[0]}/{digest[:2]}/"
        f"{urllib.parse.quote(filename)}"
    )


async def profession_icon_b64(profession_name: str) -> str:
    """职业图标 base64（带磁盘缓存；失败/空名返回空串）。"""
    url = prts_profession_icon_url(profession_name)
    if not url:
        return ""
    try:
        from ..render import get_image_b64_with_cache

        return await get_image_b64_with_cache(url, ITEM_ICON_CACHE_DIR)
    except Exception as exc:
        logger.debug(f"[ArknightsUID] 职业图标下载失败 {profession_name}: {exc}")
        return ""
