"""HTTP 请求工具和常量。

复用自 EndUID 的 request_util.py，但移除了终末地 Web 端专属头
（`get_endfield_web_headers`）。其余部分（RespCode、UA 常量、签名相关 header
构造）对明日方舟同样适用。
"""

from __future__ import annotations

import re
import time
from enum import IntEnum
from typing import Any

from gsuid_core.logger import logger

from .smsdk import get_device_id  # 设备 ID（dId）


class RespCode(IntEnum):
    """森空岛 API 业务码（响应 JSON 的 code 字段）。

    只保留本插件实际判定用到的码；EndUID 原版的 TIME_ERROR/
    CAPTCHA_* 从未使用，已随迁移移除。
    """

    OK = 0  # 请求成功

    # 认证相关
    TOKEN_INVALID = 220  # 登录过期
    CRED_INVALID = 10001  # Cred 失效或已签到
    LOGIN_EXPIRED = 10002  # 登录失效

    # 请求相关
    REQUEST_ERROR = 10000  # 请求异常


# === User-Agent ===
# iOS Skland 1.21.0（明日方舟签名链路用）
IOS_USER_AGENT = (
    "Skland/1.21.0 (com.hypergryph.skland; build:102100065; iOS 17.6.0) Alamofire/5.7.1"
)
# Android Skland 1.52.1
ANDROID_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; SM-S9280 Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/101.0.4951.61 Mobile Safari/537.36; SKLand/1.52.1"
)
# Skland App 原生 UA
SKLAND_APP_USER_AGENT = (
    "Skland/1.52.1 (com.hypergryph.skland; build:105201003; Android 32; ) Okhttp/4.11.0"
)
# Web UA（用于 cred 兑换）
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# === vName / 平台 ===
SIGN_VNAME = "1.0.0"  # Web 接口默认
ARK_SIGN_VNAME = "1.21.0"  # 明日方舟签到/查询专用（iOS 1.21.0）

SKLAND_APP_VNAME = "1.52.1"
SKLAND_APP_VCODE = "105201003"
SKLAND_APP_PLATFORM = 1

SKLAND_APP_LANGUAGE = "zh-cn"
SKLAND_APP_OS = "32"
SKLAND_APP_NID = "1"
SKLAND_APP_CHANNEL = "OF"
SKLAND_APP_MANUFACTURER = "Samsung"


def _guess_manufacturer(user_agent: str | None) -> str:
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


def get_skland_app_headers(user_agent: str | None = None) -> dict:
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


# === Skland/Hypergryph 请求共享原语（原先在 client/web/hypergryph 三处重复）===

# Skland 签名查询串的"安全字符"白名单（键/值仅允许这些，其余告警不阻断）
_UNSAFE_QUERY_RE = re.compile(r"[^0-9A-Za-z_.\-]")


def build_query_string(params: dict[str, Any] | None) -> str:
    """Skland 签名用查询串（原 skland/client 与 skland/web 各写一份）。

    刻意不做 URL 编码：Skland 官方签名要求按原始键值拼接（sorted by key），
    编码会破坏签名一致性。调用方应仅传入无特殊字符的参数（如 uid/gameId 数字）；
    出现其它字符说明调用方破坏了这一契约，打 warning 暴露问题（不阻断）。
    """
    if not params:
        return ""
    pairs: list[str] = []
    for key, value in sorted(params.items()):
        pair = f"{key}={value}"
        # '=' 是键值分隔符不属于参数内容，需对 key/value 分别校验；
        # 对拼接串整体匹配会把每个请求都误报（'gameId=1' 事件）
        if _UNSAFE_QUERY_RE.search(str(key)) or _UNSAFE_QUERY_RE.search(str(value)):
            logger.warning(
                f"[ArknightsUID] Skland 签名参数含特殊字符，签名可能失效: {pair!r}"
            )
        pairs.append(pair)
    return "&".join(pairs)


def envelope_ok(data: Any, *, key: str = "code") -> bool:
    """响应信封校验谓词：data 为 dict 且 data[key] == 0。

    Skland/公告网关用 ``code``，Hypergryph OAuth 网关用 ``status``。
    """
    return isinstance(data, dict) and data.get(key) == 0


def require_envelope(
    data: Any,
    *,
    action: str,
    key: str = "code",
    log_prefix: str = "[ArknightsUID]",
) -> dict[str, Any] | None:
    """信封校验（带日志版）：通过返回 data 本身，否则 warning（含 code）并返回 None。

    原先「not isinstance(data, dict) or data.get(...) != 0 → warning → 返回空」
    的守卫在 skland/web 与 hypergryph/client 里近乎逐字重复了约十处。
    """
    if envelope_ok(data, key=key):
        return data
    code = data.get(key) if isinstance(data, dict) else "?"
    logger.warning(f"{log_prefix} {action}: code={code}")
    return None
