"""维护命令（对齐 EndUID 的可运维小功能）：

- `ark清理缓存`（pm=1）：手动清理过期缓存；另注册每天 03:30 定时 + 启动延迟清理
- `ark更新记录`（pm=1）：展示插件最近 git 提交记录
- `ark删除无效凭据`（pm=1）：清理 cookie_status=无效 的本地登录行
- `ark刷新`：删除游戏数据表缓存（干员/技能/物品等），下次查询自动重新下载
"""
from __future__ import annotations

import asyncio

from gsuid_core.aps import scheduler
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.server import on_core_start
from gsuid_core.sv import SV

try:
    from ArknightsUID.ArknightsUID.utils.cache_clean import (
        CACHE_DAYS_TO_KEEP,
        clean_cache_directories,
    )
    from ArknightsUID.ArknightsUID.utils.database.models import ArkUser
    from ArknightsUID.ArknightsUID.utils.enduid.adapter import hide_uid
except ModuleNotFoundError:
    from ArknightsUID.utils.cache_clean import (
        CACHE_DAYS_TO_KEEP,
        clean_cache_directories,
    )
    from ArknightsUID.utils.database.models import ArkUser
    from ArknightsUID.utils.enduid.adapter import hide_uid

maintenance_sv = SV("ArknightsUID维护", pm=1, priority=5)
refresh_sv = SV("ArknightsUID刷新", priority=5)


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


# ---------------------------------------------------------------------------
# 缓存清理：手动命令 + 定时 + 启动
# ---------------------------------------------------------------------------

@maintenance_sv.on_fullmatch(("清理缓存", "删除缓存"), block=True)
async def clean_cache_command(bot: Bot, ev: Event) -> None:
    result = await clean_cache_directories(CACHE_DAYS_TO_KEEP)
    await _send_text(bot, ev, result)


@scheduler.scheduled_job(
    "cron", hour=3, minute=30, id="ark_cache_clean_daily"
)
async def ark_cache_clean_daily() -> None:
    await clean_cache_directories(CACHE_DAYS_TO_KEEP)


@on_core_start
async def ark_cache_clean_on_startup() -> None:
    """启动后延迟 10 秒清一次（避开启动 IO 高峰）。"""
    await asyncio.sleep(10)
    await clean_cache_directories(CACHE_DAYS_TO_KEEP)


# ---------------------------------------------------------------------------
# 更新记录：最近 git 提交
# ---------------------------------------------------------------------------

@maintenance_sv.on_fullmatch(("更新记录", "更新日志"), block=True)
async def show_update_log(bot: Bot, ev: Event) -> None:
    try:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", "-10",
            cwd=str(repo_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·维护] 更新记录获取失败: {exc.__class__.__name__}"
        )
        return await _send_text(bot, ev, "更新记录获取失败（git 不可用）。")
    if proc.returncode != 0 or not stdout:
        return await _send_text(bot, ev, "更新记录获取失败（非 git 部署？）。")
    text = stdout.decode("utf-8", errors="replace").strip()
    lines = [f"「明日方舟」ArknightsUID 最近更新："] + [
        f"- {line.strip()}" for line in text.splitlines() if line.strip()
    ]
    await _send_text(bot, ev, "\n".join(lines))


# ---------------------------------------------------------------------------
# 删除无效凭据：清理 cookie_status=无效 的本地登录
# ---------------------------------------------------------------------------

@maintenance_sv.on_command(("删除无效凭据", "清理无效账号"), block=True)
async def delete_invalid_creds(bot: Bot, ev: Event) -> None:
    rows = await ArkUser.select_rows(cookie_status="无效")
    if not rows:
        return await _send_text(bot, ev, "没有标记为无效的本地登录。")
    uids = []
    for row in rows:
        uid = (getattr(row, "uid", "") or "").strip()
        if uid:
            uids.append(uid)
        await ArkUser.delete_by_user(
            getattr(row, "user_id", ""), getattr(row, "bot_id", ""), uid
        )
    shown = "、".join(hide_uid(u) for u in uids[:10])
    more = f" 等 {len(uids)} 个" if len(uids) > 10 else ""
    await _send_text(bot, ev, f"已删除 {len(rows)} 条无效登录：{shown}{more}")


# ---------------------------------------------------------------------------
# 刷新：删游戏数据表缓存，下次查询重新下载
# ---------------------------------------------------------------------------

@refresh_sv.on_fullmatch(("刷新", "刷新数据", "更新数据"), block=True)
async def refresh_gamedata_cache(bot: Bot, ev: Event) -> None:
    from pathlib import Path

    try:
        from ArknightsUID.ArknightsUID.utils.path import CACHE_BASE
    except ModuleNotFoundError:
        from ArknightsUID.utils.path import CACHE_BASE

    gamedata_dir = CACHE_BASE / "gamedata"
    removed = 0
    if gamedata_dir.exists():
        for path in gamedata_dir.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    await _send_text(
        bot, ev,
        f"「明日方舟」已清理 {removed} 个游戏数据缓存文件，"
        "下次查询会自动重新下载最新数据（首次会稍慢）。",
    )
