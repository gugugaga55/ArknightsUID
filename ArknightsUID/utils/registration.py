"""插件真实注册标记。

帮助展示按「当前生效前缀」生成（自定义 prefix / 禁 force_prefix 时不再
显示失效的 ark 命令）。前缀解析要区分两种同形状态：

- 占位 Plugins（SV 先于插件包 __init__ 导入，force/prefix/allow_empty
  全空是宿主 plugins_sample 默认值）→ 尚未真实注册，可暂用 ark 默认展示；
- 完成注册后被管理员显式清空 force/prefix 且 allow_empty=False → 宿主
  实际注册的是裸命令，展示必须跟裸命令一致，不得擅自补 ark。

两者只看 Plugins 字段无法区分，由本包 __init__ 在 Plugins(...) 执行后
置位 registered 提供自身就绪标记。
"""

from __future__ import annotations

import re

registered: bool = False

SENSITIVE_URL_COMMANDS = frozenset({"导入抽卡", "小黑盒导入", "小黑盒抽卡导入"})
SENSITIVE_CRED_COMMANDS = frozenset({"绑定", "绑定登录"})
_DEFAULT_FORCE_PREFIXES = ("ark", "方舟", "明日方舟")
_ARK_CMD_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_/:.-])ark(?=[\u4e00-\u9fff]|wiki\b|BOX\b|box\b)"
)


def mark_registered() -> None:
    global registered
    registered = True


def effective_prefix() -> str:
    """本次展示用的生效命令前缀（单一事实源，兼容占位期与裸命令模式）。"""
    from gsuid_core.sv import SL

    plugin = SL.plugins.get("ArknightsUID")
    if plugin is None:
        return "ark"
    prefixes: list[str] = []
    if not plugin.disable_force_prefix:
        prefixes.extend(p for p in plugin.force_prefix if p)
    prefixes.extend(p for p in plugin.prefix if p)
    if plugin.allow_empty_prefix:
        prefixes.append("")
    if not prefixes:
        return "" if registered else "ark"
    for prefix in prefixes:
        if prefix:
            return prefix
    return ""


def swap_ark_prefix(text: str, prefix: str | None = None) -> str:
    """将文本中的命令前缀 ark 动态替换为当前生效前缀（不误伤英文单词或 URL 路径）。"""
    active = effective_prefix() if prefix is None else prefix
    if active == "ark" or "ark" not in text:
        return text
    return _ARK_CMD_TOKEN_RE.sub(active, text)


def current_plugin_prefixes() -> tuple[str, ...]:
    """返回当前生效的插件前缀集合（含默认强制前缀兜底，供敏感词注册使用）。"""
    try:
        from gsuid_core.sv import SL

        plugin = SL.plugins.get("ArknightsUID")
    except Exception:
        plugin = None
    if plugin is None:
        return _DEFAULT_FORCE_PREFIXES
    prefixes: list[str] = []
    if not plugin.disable_force_prefix:
        prefixes.extend(p for p in plugin.force_prefix if p)
    prefixes.extend(p for p in plugin.prefix if p)
    for default_prefix in _DEFAULT_FORCE_PREFIXES:
        if default_prefix not in prefixes:
            prefixes.append(default_prefix)
    return tuple(prefixes)


def sync_sensitive_prefixes() -> None:
    """将当前插件前缀与敏感命令组合同步注册到宿主日志脱敏表。"""
    try:
        from gsuid_core.logger import register_sensitive_commands
    except ImportError:
        return
    register_sensitive_commands(*SENSITIVE_URL_COMMANDS, require_url=True)
    cred_words: list[str] = ["绑定登录"]
    for prefix in current_plugin_prefixes():
        for kw in SENSITIVE_CRED_COMMANDS:
            cred_words.append(f"{prefix}{kw}")
    try:
        from gsuid_core.sv import SL

        plugin = SL.plugins.get("ArknightsUID")
        if plugin is not None and plugin.allow_empty_prefix:
            cred_words.extend(SENSITIVE_CRED_COMMANDS)
    except Exception:
        pass
    register_sensitive_commands(*cred_words, require_url=False)


def register_sensitive_alias(alias: str, target: str) -> None:
    """自定义指令别名指向敏感命令时，同步向宿主登记脱敏保护。"""
    alias = (alias or "").strip()
    target = (target or "").strip()
    if not alias or not target:
        return
    try:
        from gsuid_core.logger import register_sensitive_commands
    except ImportError:
        return
    if target in SENSITIVE_URL_COMMANDS:
        register_sensitive_commands(alias, require_url=True)
    elif target in SENSITIVE_CRED_COMMANDS:
        prefixed = tuple(f"{p}{alias}" for p in current_plugin_prefixes())
        if len(alias) >= 3:
            register_sensitive_commands(alias, *prefixed, require_url=False)
        else:
            register_sensitive_commands(*prefixed, require_url=False)
