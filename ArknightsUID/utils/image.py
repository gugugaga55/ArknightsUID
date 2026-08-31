import asyncio
import os
import threading
from pathlib import Path

from PIL import Image

from gsuid_core.logger import logger

ICON = Path(__file__).parent.parent.parent / "ICON.png"

# per-文件 single-flight（审计 #19）：check 与 download 之间有 await，
# 并发协程会重复下载/双写同一路径，一方可能读到半写文件
_download_locks: dict[Path, asyncio.Lock] = {}
_download_locks_guard = asyncio.Lock()
# 锁表上限：随缓存规模增长（每张图一条），超限回收空闲锁防无界膨胀
_DOWNLOAD_LOCKS_MAX = 2048


def _lock_idle(lock: asyncio.Lock) -> bool:
    """未持有且无排队 waiter 才算空闲（locked() 的释放窗口不算，
    同 skland/client._lock_is_idle 的分析）。"""
    if lock.locked():
        return False
    return not getattr(lock, "_waiters", None)


async def _path_lock(target: Path) -> asyncio.Lock:
    async with _download_locks_guard:
        if len(_download_locks) > _DOWNLOAD_LOCKS_MAX:
            # 回收空闲锁；target 自身与持有中/有排队的锁绝不动
            for key, old in list(_download_locks.items()):
                if key != target and _lock_idle(old):
                    _download_locks.pop(key, None)
        lock = _download_locks.get(target)
        if lock is None:
            lock = asyncio.Lock()
            _download_locks[target] = lock
        return lock


def get_ICON():
    return Image.open(ICON)


async def pic_download_from_url(
    path: Path,
    pic_url: str,
) -> Image.Image:
    """下载（或读缓存）URL 图片 → RGBA Image。

    缓存键（审计 #20）：写入用 basename + URL 短哈希（不同 URL 末段
    相同不再互相覆盖串图）；读取先查新键、再回退纯 basename 旧键
    （存量缓存不失效、不重下）。
    """
    from .gamedata.path_safety import cache_names_for_url

    path.mkdir(parents=True, exist_ok=True)
    new_name, legacy_name = cache_names_for_url(pic_url)
    if not new_name:
        raise ValueError(f"URL 无有效文件名: {pic_url[:80]}")
    _path = path / new_name                  # 新键：下载/转换的写入目标
    webp_path = _path.with_suffix(".webp")
    legacy_path = path / legacy_name         # 旧键：只读回退（存量缓存）
    legacy_webp = legacy_path.with_suffix(".webp")

    def _candidates() -> list[Path]:
        return [webp_path, _path, legacy_webp, legacy_path]

    def _open_cached() -> Image.Image | None:
        """读缓存命中；损坏文件删除后继续查下一候选（交给下载分支重拉）。

        存在即信任的读法会让一张坏缓存（旧版本直写半文件/磁盘坏块）
        把对应卡片永久卡成无图——PIL 打不开即判损坏，删掉自愈。
        """
        for existing in _candidates():
            if not existing.exists():
                continue
            try:
                return Image.open(existing).convert("RGBA")
            except Exception:
                logger.warning(
                    f"[ARK·图像] 缓存损坏，删除并重下: {existing.name}"
                )
                try:
                    existing.unlink()
                except OSError:
                    pass
        return None

    cached = _open_cached()
    if cached is not None:
        return cached

    async with await _path_lock(_path):
        # 锁内复查：等锁期间其他协程可能已完成下载/修复
        cached = _open_cached()
        if cached is not None:
            return cached

        # 传入插件共享 client（审计 #34）：框架 download 的 sess=None
        # 分支每次新建 httpx.AsyncClient 且从不关闭，靠 GC 兜底
        from .http import get_client
        from gsuid_core.utils.download_resource.download_file import download

        await download(
            pic_url, path, new_name, sess=await get_client(), tag="[Ark]"
        )

        try:
            img = Image.open(_path).convert("RGBA")
        except Exception as e:
            # 刚下载的文件打不开（CDN 错误页/半截响应）：删掉毒药文件，
            # 否则下次调用在候选里命中它、永远失败
            _path.unlink(missing_ok=True)
            logger.warning(f"[ARK·图像] 打开图片失败: {_path}, {e}")
            raise

        # 原文件已经是 webp 则跳过转换
        if _path != webp_path:
            try:
                # 唯一 tmp + 原子替换：并发读者不会拿到半写的 webp
                tmp_path = _path.with_suffix(
                    f".{os.getpid()}.{threading.get_ident()}.webp.tmp"
                )
                img.save(tmp_path, "WEBP", quality=85)
                os.replace(tmp_path, webp_path)
                _path.unlink(missing_ok=True)
                logger.debug(f"[ARK·图像] 已将图片转为webp: {webp_path.name}")
            except Exception as e:
                logger.warning(f"[ARK·图像] 转换webp失败: {e}")

    return img
