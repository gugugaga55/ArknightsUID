"""全部下载资源（ark下载资源）：枚举渲染图片全集并批量预下载。

各卡片渲染走惰性下载（首次查询慢），而图片资源绝大多数永不变化、只随
版本新增。本模块按与惰性路径**完全相同**的 URL 构造规则枚举全集批量
预下载；已存在的文件跳过并刷新 mtime（缓存清理按 30 天 mtime 删除，
周期性重跑既保活全部预下载文件，也只补新增 diff）。

存量完整性：默认模式只看存在性（快）；带「校验」参数进入校验模式——
已存在文件逐个过 PIL 结构校验，损坏/0 字节残档删除后重下。惰性路径
（pic_download_from_url）自带自愈：缓存打不开即删除重下一次。

清单来源（与惰性路径同源，保证落盘文件名一致、运行时直接命中缓存）：
- hycdn 皮肤头像/立绘/技能图标：skin_table / skill_table（gamedata 缓存）
- torappu 模组图标：uniequip_table；肉鸽地图预览：tomimi 期关卡元数据
- PRTS media 道具/敌人/职业图标：allimages 前缀枚举（只列实际存在文件，
  避免按名构造 URL 打出成片 404）；主线地图：stage_table code/name

404（源站确无此档）写与惰性路径同名的 .miss 负缓存标记，渲染期间跳过
不再重试；网络类失败不写标记，下次重跑自然补齐。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable

from gsuid_core.logger import logger

from ..http import download_bytes_status
from ..path import (
    AVATAR_CACHE_PATH,
    BAKE_PATH,
    CACHE_BASE,
    CHAR_CACHE_PATH,
    EQUIP_CACHE_PATH,
    SKILL_CACHE_PATH,
)

# 主线/教学类关卡代号（PRTS `{代号}_{名}_地图.png` 命名规律的覆盖范围；
# 活动代号 CW- 等无此命名文件，不枚举）
_MAINLINE_STAGE_CODE = re.compile(r"^(\d+-\d+|H\d+-\d+|S\d+-\d+|TR-\d+)$")

# 并发下载数：共享 httpx client 上限 20 连接，留余量给其他业务请求
_DOWNLOAD_CONCURRENCY = 8

# 分类键 → 展示名（下载顺序即小体积高频类在前，立绘体积最大放最后）
CATEGORY_LABELS: dict[str, str] = {
    "avatar": "干员头像",
    "skill": "技能图标",
    "profession": "职业图标",
    "item": "物品图标",
    "enemy": "敌人头像",
    "equip": "模组图标",
    "map": "关卡地图",
    "rogue_map": "肉鸽地图",
    "portrait": "干员立绘",
}
ALL_CATEGORY_KEYS = list(CATEGORY_LABELS)

# 命令参数 → 分类键（中文别名 + 英文键名均可）
CATEGORY_ALIASES: dict[str, str] = {
    "头像": "avatar",
    "皮肤头像": "avatar",
    "立绘": "portrait",
    "皮肤": "portrait",
    "技能": "skill",
    "技能图标": "skill",
    "职业": "profession",
    "职业图标": "profession",
    "物品": "item",
    "物品图标": "item",
    "材料": "item",
    "材料图标": "item",
    "敌人": "enemy",
    "敌人头像": "enemy",
    "模组": "equip",
    "模组图标": "equip",
    "地图": "map",
    "关卡地图": "map",
    "关卡": "map",
    "肉鸽": "rogue_map",
    "肉鸽地图": "rogue_map",
}
CATEGORY_ALIASES.update(CATEGORY_LABELS)


@dataclass(slots=True)
class ResourceTarget:
    """单个待下载图片：URL + 落盘目录 + 404 负缓存标记文件名。"""

    url: str
    cache_dir: Path
    # 404 时写入的 .miss 标记（与惰性路径的标记命名一致才有效），空 = 不写
    miss_marker: str = ""


@dataclass(slots=True)
class CategoryResult:
    name: str
    label: str
    error: str = ""
    total: int = 0
    skipped: int = 0
    downloaded: int = 0
    missing: int = 0
    failed: int = 0
    elapsed: float = 0.0


@dataclass(slots=True)
class BulkSummary:
    results: list[CategoryResult] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def elapsed(self) -> float:
        return self.finished_at - self.started_at


class DownloadPlan(list):
    """分类键列表 + 校验模式标志。

    list 子类：与既有的 == 比较 / 迭代 / 顺序契约完全兼容；错误时
    parse_category_args 仍返回 str（调用方 isinstance 区分）。
    """

    def __init__(self, keys: list[str], verify: bool = False) -> None:
        super().__init__(keys)
        self.verify = verify


# 校验模式修饰词（不占分类位）：ark下载资源 校验 / ark下载资源 立绘 校验
_VERIFY_TOKENS = ("校验", "检查", "check", "verify")


def parse_category_args(text: str) -> DownloadPlan | str:
    """命令参数 → DownloadPlan；空/「全部」= 全部分类，未知词返回错误文案。

    「校验 / check」为修饰词：开启存量校验模式——已存在的文件逐个过
    PIL 结构校验，损坏/0 字节残档删除后重下（默认模式只看存在性，
    存量坏档永远发现不了）。
    """
    tokens = [t for t in re.split(r"[\s,，、/]+", (text or "").strip()) if t]
    verify = any(t.lower() in _VERIFY_TOKENS for t in tokens)
    tokens = [t for t in tokens if t.lower() not in _VERIFY_TOKENS]
    if not tokens or any(t in ("全部", "全量", "all") for t in tokens):
        return DownloadPlan(ALL_CATEGORY_KEYS, verify)
    keys: list[str] = []
    for token in tokens:
        key = CATEGORY_ALIASES.get(token) or CATEGORY_ALIASES.get(token.lower())
        if not key:
            valid = "、".join(f"{v}({k})" for k, v in CATEGORY_LABELS.items())
            return (
                f"未知资源分类「{token}」，可用：{valid}；"
                "不带参数则全部下载；附加「校验」可对存量逐档验完整性。"
            )
        if key not in keys:
            keys.append(key)
    # 按固定顺序执行（小文件高频类在前，立绘最后）
    return DownloadPlan([k for k in ALL_CATEGORY_KEYS if k in keys], verify)


def _touch(path: Path) -> None:
    """刷新 mtime（缓存清理按 mtime 删除；失败不阻断）。"""
    try:
        os.utime(path, None)
    except OSError:
        pass


def _is_nonempty_file(path: Path) -> bool:
    """存在且非 0 字节才算已下载（0 字节残档按缺失处理，重下覆盖）。"""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


async def _verify_image(path: Path) -> bool:
    """存量图片完整性校验：PIL verify（读结构不整解码，快）。"""

    def _check() -> bool:
        try:
            from PIL import Image

            with Image.open(path) as img:
                img.verify()
            return True
        except Exception:
            return False

    return await asyncio.to_thread(_check)


def _save_image_sync(cache_dir: Path, url_last: str, content: bytes) -> None:
    """落盘为 webp（与 pic_download_from_url 的转换规则一致，quality=85）。

    PIL 打不开（非图片/损坏）时**拒绝落盘**（审计 #17）：captive portal /
    CDN 故障页是 HTTP 200 的 HTML/JSON——原样写成 .png 会成为永久坏缓存
    （存在即 skip、惰性加载也不会重下），相关卡片持续无图。

    tmp + 原子替换（审计 #39）：直写最终路径时，惰性路径的并发读者
    （_open_cached）会读到半写文件、误判坏档删掉正在写的文件（写者
    随即把内容写进已 unlink 的 inode，返回 ok 但文件实际不存在）。
    """
    import os
    import threading
    from io import BytesIO

    raw_path = cache_dir / url_last
    tmp_suffix = f".{os.getpid()}.{threading.get_ident()}{raw_path.suffix}.bulk.tmp"
    tmp_path = raw_path.with_suffix(tmp_suffix)
    try:
        if raw_path.suffix.lower() == ".webp":
            tmp_path.write_bytes(content)
            os.replace(tmp_path, raw_path)
            return
        from PIL import Image

        img = Image.open(BytesIO(content))
        target = raw_path.with_suffix(".webp")
        tmp_path = target.with_suffix(tmp_suffix)
        img.save(tmp_path, "WEBP", quality=85)
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)


_MISS_TTL = 7 * 86400  # .miss 负缓存有效期（与 wiki 地图口径一致）


def _write_miss(target: ResourceTarget) -> None:
    if not target.miss_marker:
        return
    try:
        target.cache_dir.mkdir(parents=True, exist_ok=True)
        (target.cache_dir / target.miss_marker).write_text("", encoding="utf-8")
    except OSError:
        pass


def _clear_miss(target: ResourceTarget) -> None:
    """资源恢复（200 落盘成功）后清除旧 .miss（审计 #16：残留会在 TTL
    内继续压住惰性路径，明明有图却返回空）。"""
    if not target.miss_marker:
        return
    try:
        (target.cache_dir / target.miss_marker).unlink(missing_ok=True)
    except OSError:
        pass


async def download_target(target: ResourceTarget, verify: bool = False) -> str:
    """下载单个目标 → skip / ok / miss(404) / fail。已存在则刷新 mtime。

    .miss 命中（TTL 内）直接 skip（审计 #16：此前只查文件不查 .miss，
    已知 404 的目标每轮重跑都再打一次 CDN）。

    存量完整性：0 字节残档一律删除重下（存在即跳过的口径会让它永久
    占坑）；verify=True 时再逐档过 PIL 结构校验，坏档删除重下。
    """
    import time as _time

    from ..gamedata.path_safety import cache_names_for_url

    url_last = cache_names_for_url(target.url)[1]  # 批量落盘保持旧键名
    if not url_last:
        return "fail"
    raw_path = target.cache_dir / url_last
    webp_path = raw_path.with_suffix(".webp")
    # 存在性判定（审计 #20 双查）：旧键（bulk 自己写的）或新键
    # （惰性路径 pic_download_from_url 写的 basename+URL 哈希）任一在即命中
    new_name = cache_names_for_url(target.url)[0]
    new_path = target.cache_dir / new_name
    for existing in (webp_path, raw_path, new_path.with_suffix(".webp"), new_path):
        if not existing.exists():
            continue
        if not _is_nonempty_file(existing):
            try:
                existing.unlink()
            except OSError:
                pass
            continue
        if verify and not await _verify_image(existing):
            logger.warning(
                f"[ArknightsUID·资源] 校验发现损坏，删除重下: {existing.name}"
            )
            try:
                existing.unlink()
            except OSError:
                pass
            continue
        _touch(existing)
        _clear_miss(target)  # 文件在：清掉可能残留的旧 .miss
        return "skip"

    miss_path = target.cache_dir / target.miss_marker if target.miss_marker else None
    if miss_path is not None and miss_path.exists():
        try:
            fresh = _time.time() - miss_path.stat().st_mtime < _MISS_TTL
        except OSError:
            fresh = False
        if fresh:
            return "skip"  # TTL 内已知 404，不重复请求
        try:
            miss_path.unlink()
        except OSError:
            pass

    # quiet_404：预下载枚举出的 URL 上游未必都有（PRTS 未上传的关卡地图、
    # torappu 未覆盖的肉鸽预览等），404 计入 missing 即可，不打 warning
    status, content = await download_bytes_status(
        target.url, timeout=60.0, quiet_404=True
    )
    if status == 200 and content:
        try:
            target.cache_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                _save_image_sync, target.cache_dir, url_last, content
            )
            _clear_miss(target)
            return "ok"
        except (OSError, ValueError) as exc:
            # ValueError：200 但内容不是图片（审计 #17，计失败可重试）
            logger.warning(
                f"[ArknightsUID·资源] 写盘失败 {url_last}: {exc.__class__.__name__}"
            )
            return "fail"
    if status == 404:
        _write_miss(target)
        return "miss"
    return "fail"


# ---------------------------------------------------------------------------
# 各分类清单枚举（全部复用惰性路径的 URL 构造函数，保证缓存命中一致）
# ---------------------------------------------------------------------------


def _dedup(targets: list[ResourceTarget]) -> list[ResourceTarget]:
    seen: set[tuple[str, str]] = set()
    result: list[ResourceTarget] = []
    for t in targets:
        key = (t.url, str(t.cache_dir))
        if key not in seen:
            seen.add(key)
            result.append(t)
    return result


def _portrait_miss_marker(url_last: str) -> str:
    """立绘 404 标记：与 char_portrait_b64 的 `{charId}%23{tier}.miss` 同名。

    只对基础形态写标记——惰性路径只读基础档标记（时装编码后含 %40）。
    """
    if "%40" in url_last:
        return ""
    return re.sub(r"\.png$", "", url_last) + ".miss"


async def _skin_key_targets() -> list[str]:
    """skin_table charSkins 全部键（基础形态 + 时装）。

    只保留可获得干员（charindex 命中）的皮肤：trap_/token_ 装置召唤物
    无头像/立绘可下，全量枚举只会打出成片 404。索引不可用时退回全量。
    """
    from ..gamedata._table_cache import ensure_skin_table
    from ..gamedata.charinfo import load_char_index

    skin_table = await ensure_skin_table()
    char_skins = skin_table.get("charSkins") if isinstance(skin_table, dict) else None
    if not isinstance(char_skins, dict) or not char_skins:
        raise RuntimeError("skin_table 拉取失败")
    char_index = await load_char_index()
    if not char_index:
        return [str(key) for key in char_skins]
    return [
        str(key)
        for key, skin in char_skins.items()
        if isinstance(skin, dict) and str(skin.get("charId") or "") in char_index
    ]


async def build_avatar_targets() -> list[ResourceTarget]:
    """皮肤头像全集 → cache/avatar（卡片）+ bake/skin_avatars（皮肤卡）。"""
    from ..player.context import skin_avatar_url

    targets: list[ResourceTarget] = []
    for skin_id in await _skin_key_targets():
        url = skin_avatar_url(skin_id)
        if url:
            targets.append(ResourceTarget(url, AVATAR_CACHE_PATH))
            targets.append(ResourceTarget(url, BAKE_PATH / "skin_avatars"))
    if not targets:
        raise RuntimeError("skin_table 无皮肤条目")
    return _dedup(targets)


async def build_portrait_targets() -> list[ResourceTarget]:
    """皮肤立绘全集（含各精英化档）→ cache/char。"""
    from ..gamedata.path_safety import safe_url_basename
    from ..player.context import skin_portrait_url

    targets: list[ResourceTarget] = []
    for skin_id in await _skin_key_targets():
        url = skin_portrait_url(skin_id)
        if url:
            url_last = safe_url_basename(url)
            targets.append(
                ResourceTarget(url, CHAR_CACHE_PATH, _portrait_miss_marker(url_last))
            )
    if not targets:
        raise RuntimeError("skin_table 无皮肤条目")
    return _dedup(targets)


async def build_skill_targets() -> list[ResourceTarget]:
    """全部技能图标 → cache/skill。"""
    from ..gamedata._table_cache import ensure_skill_table
    from ..player.context import skill_icon_url

    skill_table = await ensure_skill_table()
    if not isinstance(skill_table, dict) or not skill_table:
        raise RuntimeError("skill_table 拉取失败")
    targets = [
        ResourceTarget(url, SKILL_CACHE_PATH)
        for skill_id in skill_table
        if (url := skill_icon_url(str(skill_id)))
    ]
    if not targets:
        raise RuntimeError("skill_table 无技能条目")
    return targets


async def build_equip_targets() -> list[ResourceTarget]:
    """全部模组图标 → cache/equip。"""
    from ..gamedata._table_cache import ensure_uniequip_table
    from ..player.context import equip_icon_url

    uniequip = await ensure_uniequip_table()
    equip_dict = uniequip.get("equipDict") if isinstance(uniequip, dict) else None
    if not isinstance(equip_dict, dict) or not equip_dict:
        raise RuntimeError("uniequip_table 拉取失败")
    targets = [
        ResourceTarget(url, EQUIP_CACHE_PATH)
        for equip_id in equip_dict
        if (url := equip_icon_url(str(equip_id)))
    ]
    if not targets:
        raise RuntimeError("uniequip_table 无模组条目")
    return targets


async def build_item_targets() -> list[ResourceTarget]:
    """PRTS 带框道具图全集（allimages 枚举）→ gamedata/item_icons。"""
    from ..gamedata.itemicon import ITEM_ICON_CACHE_DIR
    from ..gamedata.path_safety import safe_url_basename
    from ..prts.client import all_image_titles, map_file_url

    titles = await all_image_titles("道具_带框_")
    targets = [
        ResourceTarget(url, ITEM_ICON_CACHE_DIR, url_last + ".miss")
        for title in titles
        if (url := map_file_url(title)) and (url_last := safe_url_basename(url))
    ]
    if not targets:
        raise RuntimeError("PRTS 道具图标枚举失败")
    return targets


async def build_enemy_targets() -> list[ResourceTarget]:
    """PRTS 敌人头像全集（allimages 枚举）→ gamedata/item_icons。"""
    from ..gamedata.itemicon import ITEM_ICON_CACHE_DIR
    from ..gamedata.path_safety import safe_url_basename
    from ..prts.client import all_image_titles, map_file_url

    titles = await all_image_titles("头像_敌人_")
    targets = [
        ResourceTarget(url, ITEM_ICON_CACHE_DIR, url_last + ".miss")
        for title in titles
        if (url := map_file_url(title)) and (url_last := safe_url_basename(url))
    ]
    if not targets:
        raise RuntimeError("PRTS 敌人头像枚举失败")
    return targets


async def build_profession_targets() -> list[ResourceTarget]:
    """职业图标（固定 8 职业）→ gamedata/item_icons。"""
    from ..gamedata.itemicon import (
        ITEM_ICON_CACHE_DIR,
        prts_profession_icon_url,
    )
    from ..gamedata.path_safety import safe_url_basename
    from ..player.parse import PROFESSION_NAMES

    targets = [
        ResourceTarget(url, ITEM_ICON_CACHE_DIR, url_last + ".miss")
        for name in PROFESSION_NAMES.values()
        if (url := prts_profession_icon_url(name))
        and (url_last := safe_url_basename(url))
    ]
    return targets


async def build_map_targets() -> list[ResourceTarget]:
    """主线/教学关地图（stage_table × PRTS 命名规律）→ wiki/map。"""
    from ..gamedata._table_cache import ensure_stage_table
    from ..gamedata.path_safety import safe_url_basename
    from ..prts.client import map_candidate_titles, map_file_url

    stage_table = await ensure_stage_table()
    stages = stage_table.get("stages") if isinstance(stage_table, dict) else None
    if not isinstance(stages, dict) or not stages:
        raise RuntimeError("stage_table 拉取失败")
    cache_dir = CACHE_BASE / "wiki" / "map"
    targets: list[ResourceTarget] = []
    for stage in stages.values():
        if not isinstance(stage, dict):
            continue
        code = str(stage.get("code") or "")
        name = str(stage.get("name") or "")
        if not code or not name or not _MAINLINE_STAGE_CODE.match(code):
            continue
        for title in map_candidate_titles(code, name):
            url = map_file_url(title)
            if not url:
                continue
            url_last = safe_url_basename(url)
            # 与 wiki 模块 _url_miss 同名（`{url_last}.miss`）
            targets.append(ResourceTarget(url, cache_dir, url_last + ".miss"))
    if not targets:
        raise RuntimeError("stage_table 无主线关卡")
    return _dedup(targets)


async def build_rogue_map_targets() -> list[ResourceTarget]:
    """各期肉鸽 torappu 地图预览全集 → wiki/map_rogue。"""
    from ..gamedata.rogue_enemy import _THEME_ROGUE_NUM, _load_stage_meta
    from ..prts.client import rogue_map_preview_urls

    cache_dir = CACHE_BASE / "wiki" / "map_rogue"
    targets: list[ResourceTarget] = []
    for theme_dir in _THEME_ROGUE_NUM:
        meta = await _load_stage_meta(theme_dir)
        for level_id in meta:
            for url in rogue_map_preview_urls(level_id):
                targets.append(ResourceTarget(url, cache_dir))
    if not targets:
        raise RuntimeError("肉鸽关卡元数据拉取失败")
    return _dedup(targets)


CATEGORY_BUILDERS: dict[str, Callable[[], Awaitable[list[ResourceTarget]]]] = {
    "avatar": build_avatar_targets,
    "portrait": build_portrait_targets,
    "skill": build_skill_targets,
    "profession": build_profession_targets,
    "item": build_item_targets,
    "enemy": build_enemy_targets,
    "equip": build_equip_targets,
    "map": build_map_targets,
    "rogue_map": build_rogue_map_targets,
}


# ---------------------------------------------------------------------------
# 批量执行
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[CategoryResult], Awaitable[None]]


async def _run_category(
    key: str, semaphore: asyncio.Semaphore, verify: bool = False
) -> CategoryResult:
    label = CATEGORY_LABELS[key]
    result = CategoryResult(name=key, label=label)
    started = time.monotonic()
    try:
        targets = await CATEGORY_BUILDERS[key]()
    except Exception as exc:
        result.error = f"{label}清单枚举失败: {exc}"
        result.elapsed = time.monotonic() - started
        return result

    result.total = len(targets)

    # 分批入队 + 信号量限并发；单文件失败只计数，不中断整类
    async def _one(target: ResourceTarget) -> None:
        async with semaphore:
            status = await download_target(target, verify=verify)
        if status == "ok":
            result.downloaded += 1
        elif status == "skip":
            result.skipped += 1
        elif status == "miss":
            result.missing += 1
        else:
            result.failed += 1

    await asyncio.gather(*(_one(t) for t in targets))
    result.elapsed = time.monotonic() - started
    return result


async def run_bulk_download(
    category_keys: list[str],
    concurrency: int = _DOWNLOAD_CONCURRENCY,
    on_category_done: ProgressCallback | None = None,
    verify: bool = False,
) -> BulkSummary:
    """按序执行各分类：枚举 → 并发下载 → 回调进度。

    单分类枚举/下载失败记录在 CategoryResult.error / failed，不中断其余
    分类（网络故障下次重跑自然补齐，已下载文件会被跳过）。
    verify=True：存量逐档 PIL 校验，坏档删除重下（校验模式）。
    """
    summary = BulkSummary(started_at=time.time())
    semaphore = asyncio.Semaphore(concurrency)
    for key in category_keys:
        result = await _run_category(key, semaphore, verify=verify)
        summary.results.append(result)
        if on_category_done is not None:
            try:
                await on_category_done(result)
            except Exception:
                logger.debug("[ArknightsUID·资源] 进度回调失败", exc_info=True)
    summary.finished_at = time.time()
    return summary


def format_category_line(result: CategoryResult) -> str:
    """单分类进度行（命令回报用）。"""
    if result.error:
        return f"⚠️ {result.label}：{result.error}"
    parts = [
        f"✅ {result.label}：共 {result.total}",
        f"新增 {result.downloaded}",
        f"已有 {result.skipped}",
    ]
    if result.missing:
        parts.append(f"源站缺档 {result.missing}")
    if result.failed:
        parts.append(f"失败 {result.failed}")
    parts.append(f"{result.elapsed:.0f}s")
    return "，".join(parts)


def format_summary(summary: BulkSummary) -> str:
    """总回报文案。"""
    total_downloaded = sum(r.downloaded for r in summary.results)
    total_missing = sum(r.missing for r in summary.results)
    total_failed = sum(r.failed for r in summary.results)
    errors = [r for r in summary.results if r.error]
    lines = [
        f"「明日方舟」资源下载完成："
        f"新增 {total_downloaded} 张，用时 {summary.elapsed:.0f}s。"
    ]
    if total_missing:
        lines.append(f"另有 {total_missing} 张源站无此档（已记负缓存，不影响使用）。")
    if total_failed:
        lines.append(f"失败 {total_failed} 张（网络波动，重跑本命令会自动补齐）。")
    if errors:
        names = "、".join(r.label for r in errors)
        lines.append(f"{names}清单枚举失败，可稍后单独重试该分类。")
    lines.append(
        "资源绝大多数不会变动，之后重跑只补新增；建议每月跑一次防止缓存清理误删。"
    )
    return "\n".join(lines)


# 全局运行标志：防止两条 ark下载资源 并发打满连接池。检查与置位之间
# 无 await（单事件循环），不存在竞态窗口；进程重启自动复位。
_running = False


def try_acquire_run() -> bool:
    """占用运行标志；已在跑返回 False（调用方负责提示）。"""
    global _running
    if _running:
        return False
    _running = True
    return True


def release_run() -> None:
    global _running
    _running = False
