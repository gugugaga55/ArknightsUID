"""鹰角官网 OAuth 链客户端（P3）：抽卡记录 + 礼包码兑换。

链路（字段映射参考 gxy12345/arknights-plugin model/hypergryphApi.js，属数据层
客观接口事实）：

  hg_token（森空岛 24 位登录 token）
    ↓ POST as.hypergryph.com/user/oauth2/v2/grant
    │     body {appCode: "be36d44aa36bfb5b"(明日方舟), token, type: 1}
    grant_token（status==0，data.token）
    ↓ POST binding-api-account-prod.hypergryph.com/account/binding/v1/u8_token_by_uid
    │     body {uid: record_uid(森空岛账号 uid，非游戏 UID), token: grant_token}
    u8_token（status==0，data.token）
    ↓ POST ak.hypergryph.com/user/api/role/login  body {token: u8_token}
    ak-user-center cookie（code==0，取 Set-Cookie）
    ↓ GET/POST ak.hypergryph.com/user/api/inquiry/gacha/*、gift/exchange
      头：X-Account-Token=<hg_token>，X-Role-Token=<u8_token>，Cookie ak-user-center

注意 as.hypergryph.com 系列成功字段是 status==0；ak.hypergryph.com 系列是 code==0。
真实凭据 联调待做（单测全 mock httpx）。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import httpx

from gsuid_core.logger import logger

if TYPE_CHECKING:
    from ..enduid.adapter import ArkCredential

# 明日方舟 appCode（森空岛平台是 4ca99fa6b56cc2ba，两者不能混用）
ARK_APP_CODE = "be36d44aa36bfb5b"

GRANT_API = "https://as.hypergryph.com/user/oauth2/v2/grant"
U8_TOKEN_API = (
    "https://binding-api-account-prod.hypergryph.com"
    "/account/binding/v1/u8_token_by_uid"
)
AK_ROLE_LOGIN_API = "https://ak.hypergryph.com/user/api/role/login"
GACHA_CATE_API = "https://ak.hypergryph.com/user/api/inquiry/gacha/cate"
GACHA_HISTORY_API = "https://ak.hypergryph.com/user/api/inquiry/gacha/history"
GIFT_EXCHANGE_API = "https://ak.hypergryph.com/user/api/gift/exchange"

_AK_COOKIE_RE = re.compile(r"ak-user-center=([^;]+)")

# 单页条数（官方页面上限 100）
GACHA_PAGE_SIZE = 100


async def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None, list[str]]:
    """通用请求 → (status_code, json, set_cookie 头列表)。异常/非 json 的 json 为 None。"""
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=False) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers, params=params)
            else:
                resp = await client.post(url, headers=headers, json=body)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·hypergryph] {method} {url} error: {exc.__class__.__name__}"
        )
        return 0, None, []
    try:
        data = resp.json()
    except Exception:
        logger.warning(
            f"[ArknightsUID·hypergryph] {url} non-json, HTTP {resp.status_code}"
        )
        return resp.status_code, None, []
    cookies = resp.headers.get_list("set-cookie") or []
    return resp.status_code, data, cookies


async def get_grant_token(hg_token: str) -> str:
    """hg_token → grant_token（明日方舟 appCode，type=1）。失败返回空串。"""
    if not hg_token:
        return ""
    _, data, _ = await _request(
        "POST",
        GRANT_API,
        body={"appCode": ARK_APP_CODE, "token": hg_token, "type": 1},
    )
    if not isinstance(data, dict) or data.get("status") != 0:
        logger.warning("[ArknightsUID·hypergryph] OAuth grant 失败")
        return ""
    return str((data.get("data") or {}).get("token") or "")


async def get_u8_token(record_uid: str, hg_token: str) -> str:
    """grant → u8_token。record_uid 是森空岛账号 uid（非游戏内 UID）。失败返回空串。"""
    grant_token = await get_grant_token(hg_token)
    if not grant_token:
        return ""
    _, data, _ = await _request(
        "POST", U8_TOKEN_API, body={"uid": record_uid, "token": grant_token}
    )
    if not isinstance(data, dict) or data.get("status") != 0:
        logger.warning("[ArknightsUID·hypergryph] u8_token_by_uid 失败")
        return ""
    return str((data.get("data") or {}).get("token") or "")


async def get_ak_cookie(u8_token: str) -> str:
    """u8_token → ak-user-center cookie（role/login，code==0）。失败返回空串。"""
    _, data, cookies = await _request(
        "POST", AK_ROLE_LOGIN_API, body={"token": u8_token}
    )
    if not isinstance(data, dict) or data.get("code") != 0:
        logger.warning("[ArknightsUID·hypergryph] role/login 失败")
        return ""
    for header in cookies:
        match = _AK_COOKIE_RE.search(header)
        if match:
            return match.group(1)
    return ""


class ArkGachaAuth:
    """一次登录链产物（hg_token + u8_token + cookie 三件套）。"""

    __slots__ = ("hg_token", "u8_token", "ak_cookie")

    def __init__(self, hg_token: str, u8_token: str, ak_cookie: str) -> None:
        self.hg_token = hg_token
        self.u8_token = u8_token
        self.ak_cookie = ak_cookie

    @property
    def ready(self) -> bool:
        return bool(self.hg_token and self.u8_token and self.ak_cookie)

    def headers(self) -> dict[str, str]:
        return {
            "X-Account-Token": self.hg_token,
            "X-Role-Token": self.u8_token,
            "Cookie": f"ak-user-center={self.ak_cookie}",
        }


async def resolve_record_uid(credential: "ArkCredential") -> str:
    """取森空岛账号 uid：优先 credential.record_id，缺则拉 binding 反查。"""
    record_uid = getattr(credential, "record_id", "") or ""
    if record_uid:
        return record_uid
    try:
        from ..skland import client as skland_client

        response = await skland_client.get_binding(credential)
        if isinstance(response, dict) and response.get("code") == 0:
            for binding_item in (response.get("data") or {}).get("list") or []:
                for entry in binding_item.get("bindingList") or []:
                    roles = entry.get("bindingList") or []
                    for role in roles:
                        if str(role.get("roleId", "")) == credential.uid:
                            return str(entry.get("uid") or "")
                    # 无角色明细时退回外层 uid
                    if str(entry.get("uid") or ""):
                        return str(entry["uid"])
    except Exception as exc:
        logger.warning(
            "[ArknightsUID·hypergryph] binding 反查 record_uid 失败: "
            f"{exc.__class__.__name__}"
        )
    return ""


async def login_gacha_chain(credential: "ArkCredential") -> ArkGachaAuth | str:
    """跑完整条登录链。失败返回中文提示（str）。"""
    hg_token = credential.hg_token
    if not hg_token:
        return (
            "该账号缺少森空岛登录 token，无法访问抽卡接口。"
            "请用 ark扫码登录 或 ark绑定 <24位token> 重新登录。"
        )
    record_uid = await resolve_record_uid(credential)
    if not record_uid:
        return "未找到森空岛账号绑定信息，无法换取官网访问凭据。"
    u8_token = await get_u8_token(record_uid, hg_token)
    if not u8_token:
        return "鹰角官网授权失败（u8_token 获取失败），请稍后重试。"
    ak_cookie = await get_ak_cookie(u8_token)
    if not ak_cookie:
        return "鹰角官网登录失败（cookie 获取失败），请稍后重试。"
    return ArkGachaAuth(hg_token, u8_token, ak_cookie)


async def get_gacha_categories(
    credential: "ArkCredential", auth: ArkGachaAuth
) -> list[dict[str, Any]] | None:
    """抽卡卡池类别列表（cate）。元素至少含 id；name/count 有则透传。"""
    _, data, _ = await _request(
        "GET",
        GACHA_CATE_API,
        params={"uid": credential.uid},
        headers=auth.headers(),
    )
    if not isinstance(data, dict) or data.get("code") != 0:
        return None
    payload = data.get("data")
    return payload if isinstance(payload, list) else []


async def get_gacha_history_page(
    credential: "ArkCredential",
    auth: ArkGachaAuth,
    category: str,
    gacha_ts: int | None = None,
    pos: int | None = None,
) -> dict[str, Any] | None:
    """拉一页抽卡历史（游标分页：传上一页最后一条的 gachaTs+pos）。

    返回 {list: [...], hasMore: bool}；请求失败返回 None。
    """
    params: dict[str, Any] = {
        "uid": credential.uid,
        "category": category,
        "size": GACHA_PAGE_SIZE,
    }
    if gacha_ts is not None:
        params["gachaTs"] = gacha_ts
    if pos is not None:
        params["pos"] = pos
    _, data, _ = await _request(
        "GET", GACHA_HISTORY_API, params=params, headers=auth.headers()
    )
    if not isinstance(data, dict) or data.get("code") != 0:
        return None
    payload = data.get("data") or {}
    if not isinstance(payload, dict):
        return None
    return payload


async def exchange_gift_code(
    credential: "ArkCredential", auth: ArkGachaAuth, gift_code: str
) -> dict[str, Any] | None:
    """礼包码兑换。成功/业务失败都返回响应 dict（调用方按 code 提示）。"""
    _, data, _ = await _request(
        "POST",
        GIFT_EXCHANGE_API,
        params={"uid": credential.uid},
        headers=auth.headers(),
        body={"giftCode": gift_code},
    )
    return data if isinstance(data, dict) else None
