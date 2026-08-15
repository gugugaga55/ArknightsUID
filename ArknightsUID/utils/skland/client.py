"""Skland 明日方舟接口客户端。

P0 升级要点：
1. UA / vName / RespCode 等常量统一来自 `utils/api/request_util`，
   避免与 smsdk / 后续 OAuth 链路常量重复。
2. dId 不再使用空字符串，改由 `smsdk.get_device_id()` 真正生成，
   提升登录态与签名链路的风控健壮性。
3. `ARKNIGHTS_GAME_ID` 重命名为 `ARKNIGHTS_CHANNEL_OFFICIAL`，并加注释说明
   它实际是 channelMasterId（官服渠道 ID），不是"游戏 ID"——这个命名误导
   在 EndUID 注释"为啥到这就是1了"里有同样的困惑。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from gsuid_core.logger import logger

from ..api.request_util import (
    ANDROID_USER_AGENT,
    ARK_SIGN_VNAME,
    IOS_USER_AGENT,
    SIGN_VNAME,
    RespCode,
    get_device_id,
)

if TYPE_CHECKING:
    from ..enduid.adapter import ArkCredential


# === Skland 明日方舟接口 URL ===
MAIN_URL = "https://zonai.skland.com/"
API_VERSION = "v1"

REFRESH_TOKEN_URL = f"{MAIN_URL}api/{API_VERSION}/auth/refresh"
GAME_PLAYER_INFO_URL = f"{MAIN_URL}api/{API_VERSION}/game/player/info"
GAME_ATTENDANCE_URL = f"{MAIN_URL}api/{API_VERSION}/game/attendance"

# 明日方舟在 Skland 的"游戏 ID"实际是 channelMasterId（官服渠道 ID）。
# 这个值跟 EndUID 的 ENDFIELD_GAME_ID=3 并存于同一套森空岛账号体系。
# 命名沿用"channel official"避免后续开发者误以为是游戏产品 ID。
ARKNIGHTS_CHANNEL_OFFICIAL = 1

# 兼容旧引用（暂时保留，后续逐步迁移到新名称）
ARKNIGHTS_GAME_ID = ARKNIGHTS_CHANNEL_OFFICIAL

# 拿到这些 code 表示 token 失效，需要 refresh 后重试一次
TOKEN_INVALID_CODES = {int(RespCode.TOKEN_INVALID), int(RespCode.LOGIN_EXPIRED)}


def _safe_error_type(exc: Exception) -> str:
    return exc.__class__.__name__


def _generate_sign(
    token: str,
    path: str,
    query_or_body: str = "",
    *,
    platform: str = "3",
    vname: str = SIGN_VNAME,
    did: str = "",
) -> dict[str, str]:
    """Skland 签名：HMAC-SHA256(token, payload) → MD5。

    与 EndUID 的 `api/ds.py:generate_sign` 算法完全一致；与 gxy12345 的
    `model/sklandReq.js:generateSign` 算法完全一致。
    """
    timestamp = str(int(time.time()))
    header_for_sign = {
        "platform": str(platform),
        "timestamp": timestamp,
        "dId": did,
        "vName": vname,
    }
    header_json = json.dumps(header_for_sign, separators=(",", ":"))
    sign_string = path + query_or_body + timestamp + header_json
    hmac_hash = hmac.new(
        token.encode("utf-8"),
        sign_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "timestamp": timestamp,
        "sign": hashlib.md5(hmac_hash.encode("utf-8")).hexdigest(),
    }


def _query_string(params: dict[str, Any] | None) -> str:
    # 刻意不做 URL 编码：Skland 官方签名要求按原始键值拼接（sorted by key），
    # 编码会破坏签名一致性。调用方应仅传入无特殊字符的参数（如 uid/gameId 数字）。
    if not params:
        return ""
    return "&".join(f"{key}={value}" for key, value in sorted(params.items()))


def _base_headers(
    *,
    cred: str,
    timestamp: str,
    sign: str,
    platform: int,
    vname: str,
    user_agent: str,
    uid: str = "",
    game_id: int | None = None,
    did: str = "",
    accept_encoding: str = "gzip",
) -> dict[str, str]:
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
    if uid and game_id is not None:
        headers["sk-game-role"] = f"{platform}_{uid}_{game_id}"
    return headers


async def refresh_token(cred: str) -> str:
    """刷新 Skland token。失败返回空字符串。"""
    if not cred:
        return ""
    headers = {
        "cred": cred,
        "User-Agent": IOS_USER_AGENT,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(REFRESH_TOKEN_URL, headers=headers)
    except Exception as exc:
        logger.warning(
            "[ArknightsUID] Skland token refresh request failed: "
            f"{_safe_error_type(exc)}"
        )
        return ""

    if response.status_code != 200:
        logger.warning(
            "[ArknightsUID] Skland token refresh failed: "
            f"HTTP {response.status_code}"
        )
        return ""

    try:
        data = response.json()
    except Exception as exc:
        logger.warning(
            "[ArknightsUID] Skland token refresh non-json response: "
            f"HTTP {response.status_code} ({_safe_error_type(exc)})"
        )
        return ""

    if data.get("code") == int(RespCode.OK):
        token = (data.get("data") or {}).get("token")
        if token:
            return str(token)
    logger.warning(
        "[ArknightsUID] Skland token refresh returned non-ok code: "
        f"{data.get('code')}"
    )
    return ""


async def _signed_request(
    credential: "ArkCredential",
    *,
    url: str,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    uid: str = "",
    game_id: int | None = None,
    platform: int = 3,
    vname: str = SIGN_VNAME,
    user_agent: str = ANDROID_USER_AGENT,
    accept_encoding: str = "gzip",
    use_device_id: bool = False,
) -> dict[str, Any] | None:
    """签名请求主流程。

    token 失效（220/10002）会自动 refresh + 重试一次（比 EndUID 的 request()
    更积极，EndUID 依赖 3 分钟缓存过期触发 refresh）。

    Args:
        use_device_id: True 时为本次请求生成真实 dId（高敏感接口建议开启）。
                      False 时 dId 留空（普通查询足够，且省一次加密开销）。
    """
    token = credential.token or await refresh_token(credential.cred)
    if not token:
        return None

    parsed = urlparse(url)
    path = parsed.path
    query = _query_string(params)
    body_string = ""
    if body is not None:
        body_string = json.dumps(body, separators=(",", ":"))
    payload_string = query if method == "GET" else f"{query}{body_string}"

    # 请求级别的 dId（每个请求重新生成，符合 smsdk 的设计）
    did = (
        get_device_id(
            user_agent=user_agent,
            referer="https://www.skland.com/",
        )
        if use_device_id
        else ""
    )

    async def do_request(request_token: str) -> dict[str, Any] | None:
        sign_data = _generate_sign(
            request_token,
            path,
            payload_string,
            platform=str(platform),
            vname=vname,
            did=did,
        )
        headers = _base_headers(
            cred=credential.cred,
            timestamp=sign_data["timestamp"],
            sign=sign_data["sign"],
            platform=platform,
            vname=vname,
            user_agent=user_agent,
            uid=uid,
            game_id=game_id,
            did=did,
            accept_encoding=accept_encoding,
        )
        request_url = url
        if method == "GET" and query:
            request_url = f"{url}?{query}"
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
                if method == "GET":
                    response = await client.get(request_url, headers=headers)
                else:
                    response = await client.post(
                        request_url,
                        headers=headers,
                        content=body_string if body is not None else None,
                    )
            if not response.content:
                return None
            try:
                data = response.json()
            except Exception:
                logger.warning(
                    f"[ArknightsUID] Skland non-json response: HTTP {response.status_code}"
                )
                return None
            if response.status_code in {400, 401, 403}:
                return data
            if response.status_code != 200:
                logger.warning(
                    f"[ArknightsUID] Skland request failed: HTTP {response.status_code}"
                )
                return None
            return data
        except Exception as exc:
            logger.warning(
                "[ArknightsUID] Skland request error: "
                f"{_safe_error_type(exc)}"
            )
            return None

    result = await do_request(token)
    if isinstance(result, dict) and result.get("code") in TOKEN_INVALID_CODES:
        fresh_token = await refresh_token(credential.cred)
        if fresh_token:
            result = await do_request(fresh_token)
    return result


async def get_player_info(credential: "ArkCredential") -> dict[str, Any] | None:
    """获取玩家信息（核心富数据 dump）。"""
    return await _signed_request(
        credential,
        url=GAME_PLAYER_INFO_URL,
        method="GET",
        uid=credential.uid,
        game_id=ARKNIGHTS_CHANNEL_OFFICIAL,
        params={"uid": credential.uid, "gameId": ARKNIGHTS_CHANNEL_OFFICIAL},
    )


async def ark_attendance(credential: "ArkCredential") -> dict[str, Any] | None:
    """明日方舟每日签到。"""
    return await _signed_request(
        credential,
        url=GAME_ATTENDANCE_URL,
        method="POST",
        uid=credential.uid,
        game_id=ARKNIGHTS_CHANNEL_OFFICIAL,
        body={"uid": credential.uid, "gameId": ARKNIGHTS_CHANNEL_OFFICIAL},
        platform=1,
        vname=ARK_SIGN_VNAME,
        user_agent=IOS_USER_AGENT,
        accept_encoding="gzip",
    )
