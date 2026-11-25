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

registered: bool = False

SENSITIVE_URL_COMMANDS = frozenset({"导入抽卡", "小黑盒导入", "小黑盒抽卡导入"})
SENSITIVE_CRED_COMMANDS = frozenset({"绑定", "绑定登录"})
_DEFAULT_FORCE_PREFIXES = ("ark", "方舟", "明日方舟")


def mark_registered() -> None:
    global registered
    registered = True


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
        prefixed = tuple(f"{p}{alias}" for p in _DEFAULT_FORCE_PREFIXES)
        if len(alias) >= 3:
            register_sensitive_commands(alias, *prefixed, require_url=False)
        else:
            register_sensitive_commands(*prefixed, require_url=False)
