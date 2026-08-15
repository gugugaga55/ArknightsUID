"""HTTP 请求工具和常量。

复用自 EndUID 的 request_util.py，但移除了终末地 Web 端专属头
（`get_endfield_web_headers`）。其余部分（RespCode、UA 常量、签名相关 header
构造）对明日方舟同样适用。
"""
from __future__ import annotations

import time
from enum import IntEnum
from typing import Optional

from .smsdk import get_device_id  # 设备 ID（dId）


class RespCode(IntEnum):
    """森空岛 API 响应码。"""
    OK = 0                    # 请求成功
    OK_HTTP = 200             # HTTP 成功

    # 认证相关
    TOKEN_INVALID = 220       # 登录过期
    CRED_INVALID = 10001      # Cred 失效或已签到
    LOGIN_EXPIRED = 10002     # 登录失效
    TIME_ERROR = 10003        # 时间错误

    # 请求相关
    REQUEST_ERROR = 10000     # 请求异常
    BAD_REQUEST = 400         # 错误请求
    FORBIDDEN = 403           # 禁止访问
    METHOD_NOT_ALLOWED = 405  # 方法不允许（可能是 IP 受限）

    # 验证码相关
    CAPTCHA_ERROR = 130       # 验证码错误
    CAPTCHA_EXPIRED = 132     # 验证码过期


# === User-Agent ===
# iOS Skland 1.21.0（明日方舟签名链路用）
IOS_USER_AGENT = (
    "Skland/1.21.0 (com.hypergryph.skland; build:102100065; iOS 17.6.0) "
    "Alamofire/5.7.1"
)
# Android Skland 1.52.1
ANDROID_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; SM-S9280 Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/101.0.4951.61 Mobile Safari/537.36; SKLand/1.52.1"
)
# Skland App 原生 UA
SKLAND_APP_USER_AGENT = (
    "Skland/1.52.1 (com.hypergryph.skland; build:105201003; Android 32; ) "
    "Okhttp/4.11.0"
)
# Web UA（用于 cred 兑换）
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# === vName / 平台 ===
SIGN_VNAME = "1.0.0"          # Web 接口默认
ARK_SIGN_VNAME = "1.21.0"     # 明日方舟签到/查询专用（iOS 1.21.0）

SKLAND_APP_VNAME = "1.52.1"
SKLAND_APP_VCODE = "105201003"
SKLAND_APP_PLATFORM = 1

SKLAND_APP_LANGUAGE = "zh-cn"
SKLAND_APP_OS = "32"
SKLAND_APP_NID = "1"
SKLAND_APP_CHANNEL = "OF"
SKLAND_APP_MANUFACTURER = "Samsung"


def get_base_header(
    cred: str,
    timestamp: str,
    sign: str,
    platform: int = 3,
    uid: Optional[str] = None,
    game_id: Optional[int] = None,
    vname: str = SIGN_VNAME,
    did: str = "",
    user_agent: Optional[str] = None,
    accept_encoding: str = "gzip",
) -> dict:
    """构造签名请求的基础请求头。

    Args:
        cred: 用户凭证
        timestamp: 时间戳（字符串）
        sign: 签名
        platform: 平台 ID（1=iOS App, 3=Web 默认）
        uid: 用户游戏 UID
        game_id: 游戏 ID（明日方舟官服渠道 ID = 1）
        vname: 签名版本号
        did: 设备指纹（建议由 `get_device_id()` 生成，留空则不发）
        user_agent: User-Agent
        accept_encoding: Accept-Encoding

    Returns:
        完整的 HTTP 请求头字典
    """
    if not user_agent:
        user_agent = ANDROID_USER_AGENT

    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": accept_encoding,
        "Content-Type": "application/json",
        "cred": cred,
        "timestamp": timestamp,
        "sign": sign,
        "vName": vname,
        "dId": did,
        "platform": str(platform),
    }

    # 携带角色信息（sk-game-role 是森空岛识别"哪个游戏哪个角色"的关键头）
    if uid and game_id:
        headers["sk-game-role"] = f"{platform}_{uid}_{game_id}"

    return headers


def _guess_manufacturer(user_agent: Optional[str]) -> str:
    """从 UA 推断手机厂商（Skland App 接口用）。"""
    if not user_agent:
        return SKLAND_APP_MANUFACTURER
    ua = user_agent.lower()
    if "samsung" in ua or "sm-" in ua:
        return "Samsung"
    if "xiaomi" in ua or "mi " in ua or "redmi" in ua or "poco" in ua:
        return "Xiaomi"
    if "huawei" in ua or "honor" in ua:
        return "Huawei"
    if "oneplus" in ua:
        return "OnePlus"
    if "oppo" in ua:
        return "Oppo"
    if "vivo" in ua:
        return "Vivo"
    return SKLAND_APP_MANUFACTURER


def get_skland_app_headers(user_agent: Optional[str] = None) -> dict:
    """Skland App 接口所需的额外请求头。"""
    return {
        "language": SKLAND_APP_LANGUAGE,
        "os": SKLAND_APP_OS,
        "nId": SKLAND_APP_NID,
        "vCode": SKLAND_APP_VCODE,
        "channel": SKLAND_APP_CHANNEL,
        "manufacturer": _guess_manufacturer(user_agent),
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }


def get_refresh_header(cred: str) -> dict:
    """Token 刷新请求头。

    Token 刷新不需要签名，只需要 cred。
    """
    return {
        "cred": cred,
        "User-Agent": IOS_USER_AGENT,
        "Content-Type": "application/json",
    }


def get_oauth_header() -> dict:
    """OAuth 请求头（扫码登录用）。"""
    return {
        "User-Agent": IOS_USER_AGENT,
        "Content-Type": "application/json;charset=utf-8",
    }


def get_cred_header() -> dict:
    """CRED 兑换请求头（扫码/手机号登录换 cred 时用）。

    注意：此请求头会带 dId（设备指纹），是登录链路的风控关键。
    """
    accept_language = "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7"
    return {
        "content-type": "application/json",
        "user-agent": WEB_USER_AGENT,
        "referer": "https://www.skland.com/",
        "origin": "https://www.skland.com",
        "dId": get_device_id(
            user_agent=WEB_USER_AGENT,
            accept_language=accept_language,
            referer="https://www.skland.com/",
        ),
        "platform": "3",
        "timestamp": str(int(time.time())),
        "vName": "1.0.0",
    }
