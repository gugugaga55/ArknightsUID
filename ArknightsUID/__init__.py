"""ArknightsUID plugin entry."""

from gsuid_core.sv import Plugins

from ArknightsUID.utils.registration import mark_registered, sync_sensitive_prefixes

try:
    from gsuid_core.logger import register_sensitive_commands  # noqa: F401
except ImportError:
    from gsuid_core.logger import logger

    logger.warning("[ArknightsUID] 当前 GsCore 不支持敏感命令保护，导入链接可能进入宿主日志或历史")
else:
    sync_sensitive_prefixes()

Plugins(
    name="ArknightsUID",
    force_prefix=["ark", "方舟", "明日方舟"],
    allow_empty_prefix=False,
    alias=["arknights", "arknightsuid"],
)
# 自身就绪标记：帮助模块据此区分"占位 Plugins（SV 先导入）"与
# "注册后被显式清空前缀配置"，前者暂用 ark 默认展示，后者跟实际裸命令
mark_registered()
sync_sensitive_prefixes()
