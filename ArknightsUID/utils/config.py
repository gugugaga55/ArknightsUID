"""ArknightsUID 最小配置类。

P0 阶段仅声明渲染层必需的几个配置项，后续随功能迭代逐步扩充。
基于 GsCore 的 StringConfig，保证与 EndUID / 其他插件配置风格一致。
"""
from __future__ import annotations

from typing import Dict

from gsuid_core.utils.plugins_config.models import GSC, GsBoolConfig, GsStrConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .path import CONFIG_PATH

CONFIG_DEFAULT: Dict[str, GSC] = {
    # === 渲染层（P0 必需）===
    "RemoteRenderEnable": GsBoolConfig(
        "启用外置渲染",
        "开启后 HTML→图片渲染走外置 RemoteRender 服务，本地无需安装 playwright/chromium。",
        False,
    ),
    "RemoteRenderUrl": GsStrConfig(
        "外置渲染服务地址",
        "RemoteRender 服务的完整 URL，如 http://127.0.0.1:3000/render。",
        "http://127.0.0.1:3000/render",
        secret=True,
    ),
    "FontCssUrl": GsStrConfig(
        "字体 CSS 地址",
        "HTML 渲染使用的拉丁显示字体 CSS URL（中文 MiSans 由模板内置 CDN 提供）。本地默认走 /ark/fonts/fonts.css；外置渲染可填公网字体 CDN。",
        "https://fonts.loli.net/css2?family=Noto+Serif+SC:wght@700;900&family=Google+Sans+Flex:opsz,wght@6..144,500..700&family=JetBrains+Mono:wght@500;700&display=swap",
        secret=True,
    ),

    # === 查询行为 ===
    "AtCheck": GsBoolConfig(
        "允许@查询他人",
        "开启后查询类指令若@了他人则查询被@用户的数据；关闭则始终查询发送者自己。",
        True,
    ),
    "HideUid": GsBoolConfig(
        "隐藏 UID",
        "开启后渲染卡片中的 UID 脱敏显示（前 2 位 + **** + 后 2 位）。",
        False,
    ),

    # === 网络 ===
    "LocalProxyUrl": GsStrConfig(
        "本地代理地址",
        "访问森空岛/鹰角接口使用的 HTTP 代理，留空则直连。",
        "",
        secret=True,
    ),
}

ArkConfig = StringConfig(
    "ArknightsUID",
    CONFIG_PATH,
    CONFIG_DEFAULT,
)
