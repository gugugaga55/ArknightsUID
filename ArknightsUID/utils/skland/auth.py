"""森空岛登录链（脱离 EndUID）。

请求走共享 HTTP 层（utils/http）；header 复用本地 request_util 的
get_oauth_header / get_cred_header（已是本地复刻，非 import EndUID）。

两类接口成功字段不同：
- as.hypergryph.com 系列（OAuth grant / scan / token_by_scan）：status == 0
- zonai.skland.com 系列（cred / binding）：code == 0
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gsuid_core.logger import logger

from ..api.request_util import get_cred_header, get_oauth_header
from ..http import request_json
from . import client as skland_client

# === 登录链 URL ===
# 森空岛平台 appCode，产生的 cred 覆盖全游戏（含明日方舟 gameId=1），无需方舟专用码。
# 注意：这是逆向自森空岛 Web 端的公开协议常量（所有第三方工具通用），
# 不是运营方机密，不构成密钥泄漏。
APP_CODE = "4ca99fa6b56cc2ba"
OAUTH_API = "https://as.hypergryph.com/user/oauth2/v2/grant"
CRED_API = "https://zonai.skland.com/api/v1/user/auth/generate_cred_by_code"
SCAN_LOGIN_API = "https://as.hypergryph.com/general/v1/gen_scan/login"
SCAN_STATUS_API = "https://as.hypergryph.com/general/v1/scan_status"
TOKEN_BY_SCAN_CODE_API = "https://as.hypergryph.com/user/auth/v1/token_by_scan_code"

# get_cred_by_token 的错误码（结构化，替代此前的字面量 "405" 三态返回）
CRED_ERR_OK = ""
CRED_ERR_UNAVAILABLE = "unavailable"   # 服务暂不可用（HTTP 405：IP 受限/风控）
CRED_ERR_FAILED = "failed"             # 网络/业务失败


async def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """通用请求，返回 (status_code, json_or_None)。异常/非 json 返回 (status, None)。"""
    result = await request_json(
        method, url, headers=headers, json_body=body, timeout=25.0
    )
    data = result.data if isinstance(result.data, dict) else None
    return result.status, data


@dataclass(slots=True)
class _CredOnlyCredential:
    """仅带 cred 的最小凭证（duck typing 兼容 _signed_request 的字段访问）。"""

    cred: str
    token: str = ""
    uid: str = ""


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


async def get_cred_by_token(token: str) -> tuple[dict[str, str] | None, str]:
    """token → ({"cred", "skland_user_id"}, CRED_ERR_OK)。

    失败返回 (None, 错误码)：
    - CRED_ERR_UNAVAILABLE：OAuth API 405（当前服务暂时无法使用 token）
    - CRED_ERR_FAILED：网络/业务失败（token 无效等）
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
        return None, CRED_ERR_UNAVAILABLE
    if status != 200 or not data:
        return None, CRED_ERR_FAILED
    if data.get("status") != 0:
        return None, CRED_ERR_FAILED
    code = (data.get("data") or {}).get("code")
    if not code:
        return None, CRED_ERR_FAILED

    # 2. OAuth code → cred
    status, data = await _request(
        "POST",
        CRED_API,
        headers=get_cred_header(),
        body={"kind": 1, "code": code},
    )
    if status != 200 or not data:
        return None, CRED_ERR_FAILED
    if data.get("code") != 0:
        return None, CRED_ERR_FAILED
    payload = data.get("data") or {}
    cred = payload.get("cred")
    if not cred:
        return None, CRED_ERR_FAILED
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
    }, CRED_ERR_OK


async def list_ark_roles_by_cred(
    cred: str,
) -> list[dict[str, str]] | None:
    """用 cred 拉绑定列表，筛 gameId==1 的明日方舟角色。

    不重写签名：构造最小 credential 调现成 client.get_binding，
    token 空会自动 refresh（含缓存/回写，见 client._token_for_cred）。

    roles 元素的 record_uid 是森空岛账号 uid（抽卡 OAuth 链要用），
    hypergryph.resolve_record_uid 也复用本函数，避免同一接口两套解析。
    """
    cred_obj = _CredOnlyCredential(cred=cred)
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
