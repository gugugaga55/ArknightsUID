"""肉鸽关卡敌人路线示意图（关卡卡的补充图，与官方地图互补）。

官方 torappu 预览图是纯地形布局（无敌人标记）；wiki 上传的带敌地图仅
少数关有。本模块用游戏解包关卡数据（ArknightsGameData levels json）确定性
绘制：格子地形 + 敌方行进路线 + 出生点敌人头像/数量——不依赖像素校准，
全期关卡通用。视觉口径参考 PRTS.Map（map.ark-nights.com）的示意风格：
亮色地形格、路线折线、出生点红标。

数据源（均为静态解包数据，永不过期；ark刷新 清理）：
- 格子/路由/出怪：gamedata/levels/obt/roguelike/ro{N}/{levelId}.json
- 敌人名字/头像：tomimi 敌库（load_enemy_db / enemy_icon_b64，已有缓存）
"""

from __future__ import annotations

import base64
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from gsuid_core.logger import logger

from .path_safety import resolve_inside, safe_cache_stem
from ._table_cache import GAMEDATA_CACHE_DIR

# ArknightsGameData 关卡直链（jsdelivr CDN；raw 直链核心下载器会 5s 超时）
_LEVELS_BASE = (
    "https://cdn.jsdelivr.net/gh/Kengxxiao/ArknightsGameData@master"
    "/zh_CN/gamedata/levels/obt/roguelike"
)
_LEVELS_CACHE_DIR = GAMEDATA_CACHE_DIR / "levels"
_LEVEL_ID_RE = re.compile(r"^level_rogue(\d+)_")
# 数据直链 404（该关无解包文件/剧情关）负缓存：1 天后重试
_LEVEL_MISS_TTL = 86400.0

# 画布参数
_CELL = 76
_MARGIN = 44
_TITLE_H = 64
_LEGEND_H = 40
_MAX_WIDTH = 2600

# 游戏语义配色（tile_start=敌人入口红 / tile_end=保护目标蓝，与游戏内一致）
TILE_COLORS: dict[str, tuple[int, int, int]] = {
    "tile_road": (226, 219, 205),
    "tile_floor": (208, 201, 188),
    "tile_wall": (96, 89, 82),
    "tile_fence_bound": (66, 62, 58),
    "tile_hole": (16, 14, 16),
    "tile_start": (178, 58, 50),
    "tile_end": (58, 108, 168),
    "tile_forbidden": (30, 32, 36),
}
_ROUTE_RGBA = (255, 150, 40, 120)


def _level_url(level_id: str) -> str | None:
    match = _LEVEL_ID_RE.match(level_id or "")
    if not match:
        return None
    return f"{_LEVELS_BASE}/ro{match.group(1)}/{level_id}.json"


def _cache_paths(level_id: str) -> tuple[Path, Path] | tuple[None, None]:
    stem = safe_cache_stem(level_id)
    if not stem:
        return None, None
    path = _LEVELS_CACHE_DIR / f"{stem}.json"
    return path, resolve_inside(_LEVELS_CACHE_DIR, f"{stem}.json.miss")


async def load_level_data(level_id: str) -> dict[str, Any] | None:
    """关卡解包 json（格子/路由/出怪）；无文件/网络失败返回 None。

    静态解包数据永不过期（与 gacha_table 同口径）；404/网络失败走负缓存。
    """
    import time

    from ..http import download_bytes_status
    from ..disk_cache import cache_read, cache_write

    url = _level_url(level_id)
    path, miss_path = _cache_paths(level_id)
    if not url or path is None or miss_path is None:
        return None
    payload, _saved = await cache_read(path)
    if payload is not None:
        return payload
    try:
        if miss_path.exists() and time.time() - miss_path.stat().st_mtime < _LEVEL_MISS_TTL:
            return None
    except OSError:
        pass
    status, data = await download_bytes_status(url, timeout=20.0, quiet_404=True)
    if not data:
        if status == 404:
            miss_path.parent.mkdir(parents=True, exist_ok=True)
            miss_path.write_text("", encoding="utf-8")
        return None
    try:
        import json

        payload = json.loads(data)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    await cache_write(path, payload)
    logger.debug(f"[ArknightsUID·肉鸽] 关卡示意图数据就绪: {level_id}")
    return payload


def _spawn_actions(level: dict[str, Any]) -> list[dict[str, Any]]:
    """全部波次的 SPAWN 动作（真实敌人出怪；装置 ACTIVATE_PREDEFINED 不算）。"""
    spawns: list[dict[str, Any]] = []
    for wave in level.get("waves") or []:
        if not isinstance(wave, dict):
            continue
        for fragment in wave.get("fragments") or []:
            if not isinstance(fragment, dict):
                continue
            for action in fragment.get("actions") or []:
                if (
                    isinstance(action, dict)
                    and action.get("actionType") == "SPAWN"
                    and action.get("key")
                ):
                    spawns.append(action)
    return spawns


def _route_signature(route: dict[str, Any]) -> tuple:
    """路由去重签名：起点 + MOVE 检查点序列 + 终点。"""
    if not isinstance(route, dict):
        return ()
    pts = [route.get("startPosition") or {}]
    for cp in route.get("checkpoints") or []:
        if isinstance(cp, dict) and cp.get("type") == "MOVE":
            pts.append(cp.get("position") or {})
    pts.append(route.get("endPosition") or {})
    result = []
    for point in pts:
        if not isinstance(point, dict) or "row" not in point or "col" not in point:
            return ()
        try:
            result.append((int(point["row"]), int(point["col"])))
        except (TypeError, ValueError, OverflowError):
            return ()
    return tuple(result)


def render_schematic_bytes(
    level: dict[str, Any],
    name: str,
    code: str,
    icons: dict[str, bytes | None],
) -> bytes | None:
    """关卡数据 → 示意图 PNG bytes（纯同步绘制；数据由调用方备齐）。"""
    import io

    from PIL import Image, ImageDraw, ImageFont

    def _font(size: int):
        from ..path import TEMPLATES_PATH

        font_file = TEMPLATES_PATH / "fonts" / "SourceHanSansCN-Regular.otf"
        try:
            return ImageFont.truetype(str(font_file), size)
        except Exception:
            return ImageFont.load_default()

    md = level.get("mapData") or {}
    grid, tiles = md.get("map"), md.get("tiles")
    routes = level.get("routes") or []
    if not isinstance(grid, list) or not grid or not isinstance(tiles, list):
        return None
    rows, cols = len(grid), len(grid[0])

    spawns = _spawn_actions(level)
    if not spawns:
        return None

    W = cols * _CELL + _MARGIN * 2
    H = rows * _CELL + _MARGIN * 2 + _TITLE_H + _LEGEND_H
    img = Image.new("RGB", (W, H), (18, 20, 24))
    d = ImageDraw.Draw(img, "RGBA")
    ox, oy = _MARGIN, _MARGIN + _TITLE_H

    d.text((_MARGIN, 18), f"{code} · {name}", font=_font(30), fill=(232, 228, 220))
    d.text(
        (W - _MARGIN, 26), "敌人路线示意", font=_font(16), fill=(140, 136, 130),
        anchor="ra",
    )

    def cell_center(row: int, col: int) -> tuple[int, int]:
        # 解包关卡 mapData.map 按顶→底排列（grid[0] 为最上行），而 routes 中
        # startPosition / checkpoints / endPosition 的 row 为 Unity 底→顶坐标
        # （row=0 在最下行），绘制到画布需翻转 Y 轴：rows - 1 - row。
        return (
            ox + col * _CELL + _CELL // 2,
            oy + (rows - 1 - row) * _CELL + _CELL // 2,
        )

    # 地形格
    for r in range(rows):
        for c in range(cols):
            idx = grid[r][c]
            if not isinstance(idx, int) or idx >= len(tiles):
                continue
            tile = tiles[idx] or {}
            key = str(tile.get("tileKey") or "tile_forbidden")
            color = TILE_COLORS.get(key, (30, 32, 36))
            x0, y0 = ox + c * _CELL, oy + r * _CELL
            d.rectangle([x0, y0, x0 + _CELL - 2, y0 + _CELL - 2], fill=color)
    for c in range(cols + 1):
        d.line(
            [ox + c * _CELL, oy, ox + c * _CELL, oy + rows * _CELL],
            fill=(60, 62, 66), width=1,
        )
    for r in range(rows + 1):
        d.line(
            [ox, oy + r * _CELL, ox + cols * _CELL, oy + r * _CELL],
            fill=(60, 62, 66), width=1,
        )

    # 敌方路由（去重后绘制）
    drawn: set[tuple] = set()
    for action in spawns:
        try:
            ri = int(action.get("routeIndex") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if ri < 0 or ri >= len(routes):
            continue
        route = routes[ri] or {}
        sig = _route_signature(route)
        if not sig or sig in drawn:
            continue
        drawn.add(sig)
        pts = [cell_center(sig[0][0], sig[0][1])]
        for row, col in sig[1:-1]:
            pts.append(cell_center(row, col))
        pts.append(cell_center(*sig[-1]))
        d.line(pts, fill=_ROUTE_RGBA, width=7, joint="curve")
        d.ellipse(
            [pts[-1][0] - 7, pts[-1][1] - 7, pts[-1][0] + 7, pts[-1][1] + 7],
            fill=(255, 150, 40, 200),
        )

    # 出生点：按 (格, 敌人) 合并数量，一格最多并列 3 个头像
    per_cell: dict[tuple[int, int], Counter] = defaultdict(Counter)
    for action in spawns:
        try:
            ri = int(action.get("routeIndex") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if ri < 0 or ri >= len(routes):
            continue
        signature = _route_signature(routes[ri] or {})
        if not signature:
            continue
        try:
            count = int(action.get("count") or 1)
        except (TypeError, ValueError, OverflowError):
            continue
        per_cell[signature[0]][str(action["key"])] += count
    badge_font_cache: dict[int, Any] = {}

    def _badge_font(total: int):
        size = 18 if total < 100 else 15
        if size not in badge_font_cache:
            badge_font_cache[size] = _font(size)
        return badge_font_cache[size]

    for (row, col), enemy_counts in sorted(per_cell.items()):
        cx, cy = cell_center(row, col)
        size = 60
        entries = list(enemy_counts.items())[:3]
        for k, (key, total) in enumerate(entries):
            icon = icons.get(key)
            slot_cx = cx + (k - (len(entries) - 1) / 2) * (size + 6)
            if icon:
                head = Image.open(io.BytesIO(icon)).convert("RGBA").resize((size, size))
                img.paste(head, (int(slot_cx - size / 2), cy - size // 2), head)
            badge = 28
            bx = int(slot_cx + size / 2 - badge + 2)
            by = cy + size // 2 - badge // 2
            d.ellipse([bx, by, bx + badge, by + badge], fill=(198, 48, 42))
            d.text(
                (bx + badge / 2, by + badge / 2 - 1), str(total),
                font=_badge_font(total), fill=(255, 255, 255), anchor="mm",
            )

    # 图例
    ly = oy + rows * _CELL + 14
    lx = _MARGIN
    for color, label in (
        ((178, 58, 50), "敌人入口"),
        ((58, 108, 168), "保护目标"),
        ((255, 150, 40), "行进路线"),
    ):
        d.rectangle([lx, ly + 6, lx + 18, ly + 22], fill=color)
        d.text((lx + 24, ly + 6), label, font=_font(16), fill=(200, 196, 190))
        lx += 24 + _font(16).getlength(label) + 22

    if W > _MAX_WIDTH:
        ratio = _MAX_WIDTH / W
        img = img.resize((_MAX_WIDTH, int(H * ratio)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def render_stage_schematic(
    level_id: str, name: str, code: str
) -> bytes | None:
    """肉鸽关卡敌人路线示意图（PNG bytes）；数据/图标不可用返回 None。"""
    level = await load_level_data(level_id)
    if not level:
        return None
    spawns = _spawn_actions(level)
    if not spawns:
        return None
    try:
        from .rogue_enemy import enemy_icon_b64

        keys = {str(a["key"]) for a in spawns}
        icons: dict[str, bytes | None] = {}
        for key in keys:
            data_uri = await enemy_icon_b64(key)
            icons[key] = (
                base64.b64decode(data_uri.split(",", 1)[1]) if data_uri else None
            )
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·肉鸽] 示意图图标加载失败（降级无头像）: "
            f"{exc.__class__.__name__}"
        )
        icons = {}
    try:
        import asyncio

        return await asyncio.to_thread(
            render_schematic_bytes, level, name, code, icons
        )
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·肉鸽] 示意图绘制失败: {exc.__class__.__name__}: {exc}"
        )
        return None
