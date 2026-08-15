"""森空岛登录链（脱离 EndUID）。

httpx 风格照搬 client.refresh_token；header 复用本地 request_util 的
get_oauth_header / get_cred_header（已是本地复刻，非 import EndUID）。

两类接口成功字段不同：
- as.hypergryph.com 系列（OAuth grant / scan / token_by_scan）：status == 0
- zonai.skland.com 系列（cred / binding）：code == 0

待真实凭据 联调（环境无真实账号，单测全 mock httpx）。
"""
from __future__ import annotations

import types
from typing import Any

import httpx

from gsuid_core.logger import logger

from ..api.request_util import get_cred_header, get_oauth_header
from . import client as skland_client

# === 登录链 URL ===
# 森空岛平台 appCode，产生的 cred 覆盖全游戏（含明日方舟 gameId=1），无需方舟专用码。
APP_CODE = "4ca99fa6b56cc2ba"
OAUTH_API = "https://as.hypergryph.com/user/oauth2/v2/grant"
CRED_API = "https://zonai.skland.com/api/v1/user/auth/generate_cred_by_code"
SCAN_LOGIN_API = "https://as.hypergryph.com/general/v1/gen_scan/login"
SCAN_STATUS_API = "https://as.hypergryph.com/general/v1/scan_status"
TOKEN_BY_SCAN_CODE_API = "https://as.hypergryph.com/user/auth/v1/token_by_scan_code"


async def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """通用 httpx 请求，返回 (status_code, json_or_None)。异常/非 json 返回 (status, None)。"""
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            else:
                resp = await client.post(url, headers=headers, json=body)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·auth] {method} {url} error: {exc.__class__.__name__}"
        )
        return 0, None
    try:
        data = resp.json()
    except Exception:
        logger.warning(
            f"[ArknightsUID·auth] {url} non-json response, HTTP {resp.status_code}"
        )
        return resp.status_code, None
    return resp.status_code, data


def parse_credential(text: str) -> tuple[str | None, str]:
    """判定凭证类型：len==32→cred，len==24→token，否则 (None, value)。"""
    value = (text or "").strip()
    if len(value) == 32:
        return "cred", value
    if len(value) == 24:
        return "token", value
    return None, value


async def get_scan_id() -> str | None:
    """申请扫码登录 scanId。失败返回 None。"""
    status, data = await _request(
        "POST", SCAN_LOGIN_API, headers=get_oauth_header(), body={}
    )
    if status != 200 or not data:
        return None
    if data.get("status") != 0 or data.get("msg") != "OK":
        return None
    return data["data"]["scanId"]


async def get_scan_status(scan_id: str) -> str | None:
    """轮询扫码状态。已确认返回 scanCode，未扫/超时返回 None。"""
    url = f"{SCAN_STATUS_API}?scanId={scan_id}"
    status, data = await _request("GET", url)
    if status != 200 or not data:
        return None
    # 未扫码时会返回 status != 0，属正常
    if data.get("status") != 0:
        return None
    return data["data"]["scanCode"]


async def get_token_by_scan_code(scan_code: str) -> dict[str, str] | None:
    """scanCode → {"token", "device_token"}。失败返回 None。"""
    status, data = await _request(
        "POST",
        TOKEN_BY_SCAN_CODE_API,
        headers=get_oauth_header(),
        body={"scanCode": scan_code},
    )
    if status != 200 or not data:
        return None
    if data.get("status") != 0:
        return None
    payload = data.get("data") or {}
    return {
        "token": payload.get("token", ""),
        "device_token": payload.get("deviceToken", ""),
    }


async def get_cred_by_token(token: str) -> dict[str, str] | str | None:
    """token → {"cred", "skland_user_id"}。

    token 暂不可用时返回字面量 "405"；网络/业务失败返回 None。
    """
    # 1. token → OAuth code
    status, data = await _request(
        "POST",
        OAUTH_API,
        headers=get_oauth_header(),
        body={"appCode": APP_CODE, "token": token, "type": 0},
    )
    if status == 405:
        logger.warning("[ArknightsUID·auth] OAuth API 405，当前服务暂时无法使用 token")
        return "405"
    if status != 200 or not data:
        return None
    if data.get("status") != 0:
        return None
    code = (data.get("data") or {}).get("code")
    if not code:
        return None

    # 2. OAuth code → cred
    status, data = await _request(
        "POST",
        CRED_API,
        headers=get_cred_header(),
        body={"kind": 1, "code": code},
    )
    if status != 200 or not data:
        return None
    if data.get("code") != 0:
        return None
    payload = data.get("data") or {}
    cred = payload.get("cred")
    if not cred:
        return None
    skland_user_id = (
        payload.get("userId")
        or payload.get("user_id")
        or payload.get("uid")
        or payload.get("sklandUserId")
        or payload.get("skland_user_id")
    )
    return {
        "cred": cred,
        "skland_user_id": str(skland_user_id) if skland_user_id else "",
    }


async def list_ark_roles_by_cred(
    cred: str,
) -> list[dict[str, str]] | None:
    """用 cred 拉绑定列表，筛 gameId==1 的明日方舟角色。

    不重写签名：构造临时 credential（duck typing，_signed_request 仅用 .cred/.token）
    调现成 client.get_binding，token 空会自动 refresh。签名待联调收敛到 client.py。
    """
    cred_obj = types.SimpleNamespace(cred=cred, token="", uid="")
    response = await skland_client.get_binding(cred_obj)
    if not response or response.get("code") != 0:
        return None
    binding_list = (response.get("data") or {}).get("list") or []
    roles: list[dict[str, str]] = []
    for binding_item in binding_list:
        for bind_entry in binding_item.get("bindingList", []):
            if bind_entry.get("gameId") != 1:
                continue
            default_role = bind_entry.get("defaultRole")
            if not default_role and bind_entry.get("roles"):
                default_role = bind_entry["roles"][0]
            if default_role:
                role_id = default_role.get("roleId")
                if not role_id:
                    continue
                nickname = (
                    default_role.get("nickname")
                    or bind_entry.get("nickName")
                    or "明日方舟角色"
                )
                server_id = str(default_role.get("serverId", "1"))
            else:
                # 方舟可能没有 roles/defaultRole，UID 在外层 bind_entry.uid
                role_id = bind_entry.get("uid")
                if not role_id:
                    continue
                nickname = bind_entry.get("nickName") or "明日方舟角色"
                server_id = "1"
            roles.append(
                {
                    "role_id": str(role_id),
                    "nickname": nickname,
                    "record_uid": str(bind_entry.get("uid") or ""),
                    "server_id": server_id,
                }
            )
    return roles
