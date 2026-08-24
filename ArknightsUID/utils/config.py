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
        "RemoteRender 服务的完整 URL，如 http://127.0.0.1:3000/render。"
        "⚠️ 渲染请求会携带完整卡片 HTML（含昵称、UID、游戏数据）发送到该地址："
        "请仅指向自建或完全受信任的服务，公网部署务必使用 HTTPS，"
        "否则等于把所有查询内容明文交给第三方。",
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
    # （AtCheck「允许@查询他人」已于 2026-08 安全审计中移除：从未实现，
    #   留着会给使用者“该越权查询已获授权”的错误印象。）
    "HideUid": GsBoolConfig(
        "隐藏 UID",
        "开启后渲染卡片中的 UID 脱敏显示（前 2 位 + **** + 后 2 位）。",
        True,
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
