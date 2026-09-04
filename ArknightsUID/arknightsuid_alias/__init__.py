"""指令别名（bot 主人管理）：让一个命令有多个触发词，如 ark招募 ≡ ark公招推荐。

机制：向 gsuid_core 的触发器表（SV.TL）运行时插入「转发 Trigger」——
别名命中后查目标命令 Trigger 并直接调用，ev.text 的参数部分与原命令
完全一致（"ark招募 术师" 与 "ark公招推荐 术师" 的 handler 视角相同），
因此对所有 ark 命令通用，无需改动任何业务 handler。

要点：
- 仅 bot 主人可增删（gsuid_core 的 masters 配置，即 user_pm=0；
  群管理员无此权限）。所有人都能用别名触发命令。
- 别名持久化在 ArkCommandAlias 表；模块加载时惰性重放注册（首条消息
  或管理命令触发时），热重载后模块重导入自动重放。
- 内置别名（招募=公招推荐）不落库：删除时写 tombstone（target=""），
  重启不复活；列表里标注来源。
"""

from __future__ import annotations

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.sv import SV

# 触发器表深耦合面（gsuid_core 内部结构）：改名/挪动时别名功能整体降级，
# 而不是模块加载崩溃拖垮整个插件——运行期由 _runtime_ok() 兜底
try:
    from gsuid_core.sv import SL, Trigger, modify_func
except ImportError:  # pragma: no cover - 仅 core 内部重构时触发
    SL = None
    Trigger = None
    modify_func = None

from ArknightsUID.utils.card import send_text as _send_text

alias_sv = SV("ArknightsUID指令别名", priority=5)

# 内置别名（开箱即用；主人删除后写 tombstone 永久停用）
_BUILTIN_ALIASES: dict[str, str] = {
    "招募": "公招推荐",
}

# 运行时别名表（alias → target；内置 ∪ DB 行，DB tombstone 覆盖为删除）
_alias_map: dict[str, str] = {}
_installed = False


def _plugin_prefixes() -> list[str]:
    """本插件的命令前缀集合（复刻 SV._on 的展开规则：配置 prefix +
    force_prefix，allow_empty_prefix 关闭）。"""
    plugin = alias_sv.plugins
    prefixes = list(plugin.prefix)
    if "" in prefixes:
        prefixes.remove("")
    if not plugin.disable_force_prefix:
        prefixes.extend(plugin.force_prefix)
    if plugin.allow_empty_prefix:
        prefixes.append("")
    return list(set(prefixes))


def _func_belongs_to_plugin(func) -> bool:
    """函数（含 wraps 包装）是否属于本插件模块。"""
    module = getattr(func, "__module__", "") or ""
    return "ArknightsUID" in module.split(".")


def _runtime_ok() -> bool:
    """core 触发器内部结构可用性哨兵（别名转发机制的必要条件）。"""
    if SL is None or Trigger is None or modify_func is None:
        logger.warning(
            "[ArknightsUID·别名] 当前 core 版本缺少 SL/Trigger/modify_func，"
            "别名转发不可用（其余命令不受影响）"
        )
        return False
    return True


def _find_target_trigger(target: str):
    """按命令词在本插件范围内找已注册的 command Trigger；找不到返回 None。"""
    for sv in SL.lst.values():
        for trigger in sv.TL.get("command", {}).values():
            if trigger.keyword == target and _func_belongs_to_plugin(trigger.func):
                return trigger
    return None


def _all_registered_keys() -> set[str]:
    """当前所有 sv 全部触发键（任意类型），用于别名冲突检测。

    排除本模块转发器自己的键——覆盖注册（改目标）不应算冲突；管理命令
    键（指令别名/列表/删除）不排除，别名不得抢占。
    """
    keys: set[str] = set()
    for sv in SL.lst.values():
        for trigger_dict in sv.TL.values():
            keys.update(trigger_dict.keys())
    own_forward_keys = {
        key
        for key, trigger in alias_sv.TL.get("command", {}).items()
        if getattr(trigger.func, "__wrapped__", None) is forward_command
    }
    keys -= own_forward_keys
    return keys


def _alias_conflicts(alias: str) -> bool:
    """别名的任一前缀展开键是否与现存触发词冲突（防抢其他命令）。"""
    expanded = {p + alias for p in _plugin_prefixes()}
    return bool(expanded & _all_registered_keys())


def _install_alias_trigger(alias: str) -> None:
    """把别名注册进触发器表（block 与业务命令一致；重复注册覆盖旧键）。"""
    if not _runtime_ok():
        return
    table = alias_sv.TL.setdefault("command", {})
    for prefix in _plugin_prefixes():
        table[prefix + alias] = Trigger(
            "command", alias, modify_func(forward_command), prefix, True, False
        )


def _uninstall_alias_trigger(alias: str) -> None:
    table = alias_sv.TL.get("command", {})
    for prefix in _plugin_prefixes():
        table.pop(prefix + alias, None)


async def forward_command(bot: Bot, ev: Event) -> None:
    """别名触发词命中 → 转发到目标命令 handler（参数原样透传）。"""
    if not _runtime_ok():
        await _send_text(bot, ev, "别名转发与当前 core 版本不兼容，请联系维护者。")
        return
    target = _alias_map.get(ev.command)
    if not target:
        await _send_text(bot, ev, f"指令别名「{ev.command}」已失效，请主人重新设置。")
        return
    trigger = _find_target_trigger(target)
    if trigger is None:
        await _send_text(
            bot,
            ev,
            f"别名「{ev.command}」的目标指令「{target}」当前不存在"
            "（可能被禁用或改名），请主人用 ark删除指令别名 清理。",
        )
        return
    # 直接调用目标 handler：ev.text 已剥掉 前缀+别名，与原命令的
    # 参数视角一致（"ark招募 术师 输出" ≡ "ark公招推荐 术师 输出"）
    await trigger.func(bot, ev)


async def ensure_aliases_installed() -> None:
    """从 DB 重放别名注册（幂等；DB 未就绪时保持未安装，下条消息重试）。"""
    global _installed, _alias_map
    if _installed:
        return
    try:
        from ArknightsUID.utils.database.models import ArkCommandAlias

        merged = dict(_BUILTIN_ALIASES)
        for row in await ArkCommandAlias.list_aliases():
            merged[row.alias] = row.target  # tombstone（target=""）= 停用
        _alias_map = {a: t for a, t in merged.items() if t}
        for alias in _alias_map:
            _install_alias_trigger(alias)
        _installed = True
    except Exception:
        logger.debug(
            "[ArknightsUID] 指令别名注册重放失败，等待下条消息重试", exc_info=True
        )


def _is_bot_master(ev: Event) -> bool:
    """gsuid_core 主人判定（masters 配置 → user_pm=0；非群管理员体系）。"""
    return getattr(ev, "user_pm", 6) == 0


def _strip_known_prefix(text: str) -> str:
    """目标命令词容错：剥掉用户误带的插件前缀（ark招募/方舟招募 → 招募）。"""
    for prefix in sorted(_plugin_prefixes(), key=len, reverse=True):
        if prefix and text.startswith(prefix):
            return text[len(prefix) :]
    return text


async def register_command_alias(alias: str, target: str) -> str | None:
    """注册别名（校验 + 落库 + 即时生效）；失败返回中文文案。"""
    alias = (alias or "").strip()
    target = _strip_known_prefix((target or "").strip())
    if not alias or not target:
        return "用法：ark指令别名 别名=命令（如 ark指令别名 招募=公招推荐）"
    if "=" in alias or any(ch.isspace() for ch in alias):
        return "别名不能包含「=」或空白字符。"
    if len(alias) > 12:
        return "别名过长（≤12 字符）。"
    if _find_target_trigger(target) is None:
        return (
            f"未找到本插件指令「{target}」。目标必须是 ArknightsUID 已注册的"
            "命令词（不带 ark/方舟 前缀，如 公招推荐 / 干员 / 练度卡）。"
        )
    if alias == target:
        return "别名与目标相同，无需设置。"
    if _alias_conflicts(alias):
        return f"「{alias}」与现有指令/别名冲突，请换一个词。"
    from ArknightsUID.utils.database.models import ArkCommandAlias

    await ArkCommandAlias.set_alias(alias, target)
    _alias_map[alias] = target
    _install_alias_trigger(alias)
    return None


async def remove_command_alias(alias: str) -> str | None:
    """删除别名（含内置——写 tombstone 防复活）；失败返回中文文案。"""
    alias = (alias or "").strip()
    if not alias:
        return "用法：ark删除指令别名 别名（如 ark删除指令别名 招募）"
    if alias not in _alias_map:
        return f"没有别名「{alias}」，可用 ark指令别名列表 查看。"
    from ArknightsUID.utils.database.models import ArkCommandAlias

    if alias in _BUILTIN_ALIASES:
        # 内置别名：写 tombstone（target=""）持久化停用，重启不复活
        await ArkCommandAlias.set_alias(alias, "")
    else:
        await ArkCommandAlias.delete_alias(alias)
    _alias_map.pop(alias, None)
    _uninstall_alias_trigger(alias)
    return None


# 注册顺序约束：列表/删除 必须先于「指令别名」注册（startswith 匹配下
# 短词会抢走「ark指令别名列表/删除」——与练度卡/练度 同款踩坑）。


@alias_sv.on_command(("指令别名列表", "别名列表"), block=True)
async def list_command_aliases(bot: Bot, ev: Event) -> None:
    await ensure_aliases_installed()
    if not _alias_map:
        await _send_text(
            bot, ev, "当前没有启用的指令别名。主人可用 ark指令别名 别名=命令 添加。"
        )
        return
    lines = ["「明日方舟」指令别名（所有人可用，主人管理）："]
    prefixes = "/".join(sorted(_plugin_prefixes()))
    for alias, target in sorted(_alias_map.items()):
        builtin = (alias, target) in _BUILTIN_ALIASES.items()
        lines.append(
            f"  {prefixes}{alias} → {target}（{'内置' if builtin else '自定义'}）"
        )
    lines.append("主人管理：ark指令别名 别名=命令 / ark删除指令别名 别名")
    await _send_text(bot, ev, "\n".join(lines))


@alias_sv.on_command(("删除指令别名", "指令别名删除", "移除指令别名"), block=True)
async def delete_alias_handler(bot: Bot, ev: Event) -> None:
    await ensure_aliases_installed()
    if not _is_bot_master(ev):
        await _send_text(
            bot, ev, "仅 bot 主人（gsuid_core masters）可管理指令别名，群管理员不行哦。"
        )
        return
    error = await remove_command_alias(ev.text.strip())
    if error:
        await _send_text(bot, ev, error)
        return
    await _send_text(bot, ev, f"已删除指令别名「{ev.text.strip()}」。")


@alias_sv.on_command(("指令别名", "命令别名"), block=True)
async def manage_command_alias(bot: Bot, ev: Event) -> None:
    await ensure_aliases_installed()
    query = (ev.text or "").strip()
    if not _is_bot_master(ev):
        await _send_text(
            bot,
            ev,
            "仅 bot 主人（gsuid_core masters）可管理指令别名；"
            "现有别名见 ark指令别名列表。",
        )
        return
    if not query or "=" not in query:
        await _send_text(
            bot,
            ev,
            "用法：ark指令别名 别名=命令（如 ark指令别名 招募=公招推荐；"
            "目标为本插件命令词，可带或不带 ark 前缀）。列表：ark指令别名列表；"
            "删除：ark删除指令别名 别名。",
        )
        return
    alias, _, target = query.partition("=")
    error = await register_command_alias(alias, target)
    if error:
        await _send_text(bot, ev, error)
        return
    prefixes = "/".join(sorted(_plugin_prefixes()))
    await _send_text(
        bot,
        ev,
        f"已设置指令别名：{prefixes}{alias.strip()} → {target.strip()}，即时生效。",
    )


# 惰性重放兜底：首条消息到达时（此时全部模块均已加载）注册别名触发词；
# 每条消息仅做一次标志位判断，开销可忽略。
@alias_sv.on_message()
async def _install_on_first_message(bot: Bot, ev: Event) -> None:
    await ensure_aliases_installed()


# 插件加载尾段尝试提前注册（模块导入顺序在 wiki 等模块之前时，转发器
# 不依赖目标存在，可安全先注册；DB 未就绪由 on_message 兜底重试）
try:
    import asyncio as _asyncio

    _asyncio.get_running_loop().create_task(ensure_aliases_installed())
except RuntimeError:
    pass  # 无事件循环（如单测 import）：交给 on_message 兜底
