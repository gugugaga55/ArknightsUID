"""缓存目录清理（对齐 EndUID 的三件套：定时 + 启动 + 手动命令）。

只清 MAIN_PATH/cache 下的文件（头像/立绘/图标/wiki/一图流/游戏数据表等
可再生缓存），按修改时间早于 keep_days 天删除；目录结构保留。
玩家数据（players/）、推送状态、别名库一律不碰。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from gsuid_core.logger import logger

from .path import CACHE_BASE

# 保留天数（头像等体积小但数量多，30 天与 gamedata 静态缓存 TTL 对齐）
CACHE_DAYS_TO_KEEP = 30


def _clean_dir_sync(directory: Path, cutoff: float) -> tuple[int, int]:
    """删 directory 下早于 cutoff 的文件 → (删除数, 释放字节)。"""
    removed, freed = 0, 0
    if not directory.exists():
        return 0, 0
    for path in directory.rglob("*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                size = path.stat().st_size
                path.unlink()
                removed += 1
                freed += size
        except OSError:
            continue
    return removed, freed


async def clean_cache_directories(keep_days: int = CACHE_DAYS_TO_KEEP) -> str:
    """清理缓存目录，返回结果文案（供手动命令与定时日志共用）。"""
    cutoff = time.time() - keep_days * 86400
    total_removed, total_freed = 0, 0
    for child in sorted(CACHE_BASE.iterdir()) if CACHE_BASE.exists() else []:
        if not child.is_dir():
            continue
        removed, freed = await asyncio.to_thread(_clean_dir_sync, child, cutoff)
        total_removed += removed
        total_freed += freed
    if total_removed:
        logger.info(
            f"[ArknightsUID·缓存清理] 删除 {total_removed} 个文件，"
            f"释放 {total_freed / 1024 / 1024:.1f}MB（保留 {keep_days} 天内）"
        )
    return (
        f"「明日方舟」缓存清理完成：删除 {total_removed} 个文件，"
        f"释放 {total_freed / 1024 / 1024:.1f}MB（保留 {keep_days} 天内）。"
    )
