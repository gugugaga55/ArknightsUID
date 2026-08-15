from __future__ import annotations

import json
import time
import hmac
import hashlib
from typing import Any
from urllib.parse import urlparse

import httpx

from gsuid_core.logger import logger

if False:
    from ..enduid.adapter import ArkCredential


MAIN_URL = "https://zonai.skland.com/"
API_VERSION = "v1"

REFRESH_TOKEN_URL = f"{MAIN_URL}api/{API_VERSION}/auth/refresh"
GAME_PLAYER_INFO_URL = f"{MAIN_URL}api/{API_VERSION}/game/player/info"
GAME_ATTENDANCE_URL = f"{MAIN_URL}api/{API_VERSION}/game/attendance"

ARKNIGHTS_GAME_ID = 1

IOS_USER_AGENT = (
    "Skland/1.21.0 (com.hypergryph.skland; build:102100065; "
    "iOS 17.6.0) Alamofire/5.7.1"
)
ANDROID_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; SM-S9280 Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/101.0.4951.61 Mobile Safari/537.36; SKLand/1.52.1"
)

SIGN_VNAME = "1.0.0"
ARK_SIGN_VNAME = "1.21.0"

TOKEN_INVALID_CODES = {220, 10002}


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
        "dId": "",
        "platform": str(platform),
    }
    if uid and game_id is not None:
        headers["sk-game-role"] = f"{platform}_{uid}_{game_id}"
    return headers


async def refresh_token(cred: str) -> str:
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

    if data.get("code") == 0:
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
) -> dict[str, Any] | None:
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

    async def do_request(request_token: str) -> dict[str, Any] | None:
        sign_data = _generate_sign(
            request_token,
            path,
            payload_string,
            platform=str(platform),
            vname=vname,
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
    return await _signed_request(
        credential,
        url=GAME_PLAYER_INFO_URL,
        method="GET",
        uid=credential.uid,
        game_id=ARKNIGHTS_GAME_ID,
        params={"uid": credential.uid, "gameId": ARKNIGHTS_GAME_ID},
    )


async def ark_attendance(credential: "ArkCredential") -> dict[str, Any] | None:
    return await _signed_request(
        credential,
        url=GAME_ATTENDANCE_URL,
        method="POST",
        uid=credential.uid,
        game_id=ARKNIGHTS_GAME_ID,
        body={"uid": credential.uid, "gameId": ARKNIGHTS_GAME_ID},
        platform=1,
        vname=ARK_SIGN_VNAME,
        user_agent=IOS_USER_AGENT,
        accept_encoding="gzip",
    )
