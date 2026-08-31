"""维护命令（对齐 EndUID 的可运维小功能）：

- `ark清理缓存`（pm=1）：手动清理过期缓存；另注册每天 03:30 定时 + 启动延迟清理
- `ark更新记录`（pm=1）：展示插件最近 git 提交记录
- `ark删除无效凭据`（pm=1）：清理 cookie_status=无效 的本地登录行
- `ark刷新`：删除游戏数据表缓存（干员/技能/物品等），下次查询自动重新下载
- `ark下载资源 [分类…] [校验]`（pm=1）：批量预下载全部渲染图片（已存在跳过，重跑只补新增；带「校验」逐档验完整性，坏档自动重下；进度不逐类刷屏，结束后以一条转发消息汇总）
- `ark安装依赖 [名字]`（pm=1）：兜底 core 自动装依赖失败的补装（白名单：ocr/takumi）
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

        plugin_dir = Path(__file__).resolve().parents[2]
        # 先确认插件目录自己就是 git 仓库根：插件以普通文件形式落在
        # core 检出里（无独立 .git）时，git 会向上命中 gsuid_core 的仓库，
        # 把 core 的提交历史当成插件的展示——宁缺毋错，明确提示
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--show-toplevel",
            cwd=str(plugin_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        toplevel = stdout.decode("utf-8", errors="replace").strip()
        if (
            proc.returncode != 0
            or not toplevel
            or Path(toplevel).resolve() != plugin_dir
        ):
            return await _send_text(
                bot, ev,
                "更新记录仅对独立 git 部署可用（当前部署未检测到插件自身的"
                " .git，插件可能随 core 仓库一同部署）。",
            )
        proc = await asyncio.create_subprocess_exec(
            "git", "log", "--oneline", "-10",
            cwd=str(plugin_dir),
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


# ---------------------------------------------------------------------------
# 安装依赖：core AutoInstallDep 失败时的聊天内兜底（白名单，见 dep_install）
# ---------------------------------------------------------------------------

@maintenance_sv.on_command(("安装依赖", "安装组件"), block=True)
async def install_dependency_command(bot: Bot, ev: Event) -> None:
    try:
        from ArknightsUID.ArknightsUID.utils import dep_install
    except ModuleNotFoundError:
        from ArknightsUID.utils import dep_install

    arg = (ev.text or "").strip()
    if not arg:
        lines = ["用法：ark安装依赖 <名字>。可选依赖："]
        for entry in dep_install.dependency_status():
            state = "✅ 已安装" if entry.installed else "❌ 未安装"
            keys = "/".join(entry.keys)
            lines.append(f"- {keys}（{entry.spec}）：{state}")
        lines.append("依赖通常由 core 启动时自动安装（AutoInstallDep），本命令兜底补装。")
        return await _send_text(bot, ev, "\n".join(lines))

    resolved = dep_install.resolve_dependency(arg)
    if resolved is None:
        return await _send_text(
            bot, ev, f"未知依赖名「{arg}」，发送 ark安装依赖 查看可用列表。"
        )
    spec, module = resolved
    if dep_install.is_installed(module):
        return await _send_text(bot, ev, f"{module} 已安装，无需重复安装。")
    await _send_text(
        bot, ev, f"正在安装 {spec}（多镜像回退，约需 1-2 分钟）…"
    )
    ok, msg = await dep_install.install_dependency(arg)
    await _send_text(bot, ev, msg)


# ---------------------------------------------------------------------------
# 下载资源：批量预下载全部渲染图片（已存在跳过，重跑只补 diff）
# ---------------------------------------------------------------------------

@maintenance_sv.on_command(
    ("下载资源", "全部下载", "下载全部资源", "预下载资源"), block=True
)
async def bulk_download_resources(bot: Bot, ev: Event) -> None:
    try:
        from ArknightsUID.ArknightsUID.utils.resource import bulk_download
    except ModuleNotFoundError:
        from ArknightsUID.utils.resource import bulk_download

    category_keys = bulk_download.parse_category_args(ev.text or "")
    if isinstance(category_keys, str):
        return await _send_text(bot, ev, category_keys)

    if not bulk_download.try_acquire_run():
        return await _send_text(
            bot, ev, "资源下载正在进行中，请等本轮结束再试。"
        )
    try:
        labels = "、".join(
            bulk_download.CATEGORY_LABELS[key] for key in category_keys
        )
        verify_note = (
            "；校验模式：存量文件逐个验完整性，坏档自动重下（耗时更长）"
            if category_keys.verify else ""
        )
        await _send_text(
            bot, ev,
            f"「明日方舟」开始预下载资源（{labels}{verify_note}）："
            "已存在的图片会跳过，首次运行需先拉取游戏数据表，"
            "耗时较长，结束后汇总一条转发消息回报，期间请耐心等待…",
        )

        # 进度不逐类刷屏：收集各分类回报行，跑完连总结一起打包成
        # 一条转发消息发回本会话（本命令 pm=1，会话即管理员）
        summary = await bulk_download.run_bulk_download(
            category_keys, verify=category_keys.verify,
        )
        forward_lines = [
            bulk_download.format_category_line(result)
            for result in summary.results
        ] + [bulk_download.format_summary(summary)]
        try:
            from gsuid_core.segment import MessageSegment

            await bot.send(MessageSegment.node(forward_lines))
        except Exception:
            # 转发不被平台支持等场景：退回纯文本逐行发送
            logger.debug("[ArknightsUID·维护] 转发汇总发送失败，回退文本")
            await _send_text(bot, ev, "\n".join(forward_lines))
    finally:
        bulk_download.release_run()
