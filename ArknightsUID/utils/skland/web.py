"""森空岛公开 Web API（P4：官方公告）。

链路（复刻 EndUID `_web_public_*` 机制，接口事实层面复用，无 UI）：
- `GET zonai.skland.com/web/v1/auth/refresh`：无需登录。用**空 token** 走
  森空岛签名（复用 skland.client._generate_sign），服务端颁发短期公共 token
- `GET zonai.skland.com/web/v1/home/index?gameId=&cateId=&pageSize=&sortType=2`：
  以公共 token 为 HMAC 密钥签名；code=10000 表示 token 失效，刷新后重试一次
- 明日方舟 gameId=1（channelMasterId），cateId 为公告分类号（探测得出，
  见 ARK_ANN_CATE_ID；终末地是 12）

真实联调状态：token 链单测全 mock；cateId 由 tools 探测脚本验证。
"""
from __future__ import annotations

import time
from typing import Any

from gsuid_core.logger import logger

from ..api.request_util import SIGN_VNAME, RespCode
from ..http import request_json
from . import client as skland_client

WEB_BASE = "https://zonai.skland.com"
WEB_REFRESH_PATH = "/web/v1/auth/refresh"
WEB_HOME_PATH = "/web/v1/home/index"
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# 明日方舟在森空岛 web 端的游戏/公告分类 ID（gameId=1 即 channelMasterId；
# cateId 探测候选：1=官方公告？以探测结果为准，默认 1）
ARK_GAME_ID = 1
ARK_ANN_CATE_ID = 1

# 请求异常码（token 失效）→ 刷新重试；与 RespCode.REQUEST_ERROR 同一业务码
_REQUEST_ERROR_CODE = int(RespCode.REQUEST_ERROR)

# 进程内公共 token 缓存（25 分钟，服务端短期）
_token_cache: dict[str, Any] = {"token": "", "ts": 0.0}
_TOKEN_TTL = 25 * 60


def _web_headers(sign_data: dict[str, str]) -> dict[str, str]:
    return {
        "platform": "3",
        "timestamp": sign_data["timestamp"],
        "dId": "",
        "vName": SIGN_VNAME,
        "sign": sign_data["sign"],
        "User-Agent": WEB_USER_AGENT,
        "Referer": "https://www.skland.com/",
        "Accept": "application/json",
    }


async def _http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any] | None:
    """GET → json（测试缝：单测 patch 此函数）。异常返回 None。"""
    result = await request_json("GET", url, headers=headers, timeout=20.0)
    return result.data if isinstance(result.data, dict) else None


async def refresh_public_web_token(force: bool = False) -> str:
    """获取/刷新公开 Web token（空 token 签名）。失败返回空串。"""
    now = time.time()
    if (
        not force
        and _token_cache["token"]
        and now - _token_cache["ts"] < _TOKEN_TTL
    ):
        return str(_token_cache["token"])
    sign_data = skland_client._generate_sign(
        token="", path=WEB_REFRESH_PATH, query_or_body=""
    )
    data = await _http_get_json(
        f"{WEB_BASE}{WEB_REFRESH_PATH}", _web_headers(sign_data)
    )
    if not isinstance(data, dict) or data.get("code") != 0:
        logger.warning(
            "[ArknightsUID·公告] web token 刷新失败: "
            f"code={data.get('code') if isinstance(data, dict) else '?'}"
        )
        return ""
    token = str((data.get("data") or {}).get("token") or "")
    if token:
        _token_cache["token"] = token
        _token_cache["ts"] = time.time()
    return token


def _query_string(params: dict[str, Any]) -> str:
    # 与签名要求一致：原始键值按序拼接（不做 URL 编码；参数仅含数字）
    return "&".join(f"{k}={v}" for k, v in params.items())


async def web_public_get(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """公开 Web GET（自动 token + 失效重试一次）。"""
    query = _query_string(params)

    async def do_request(token: str) -> dict[str, Any] | None:
        sign_data = skland_client._generate_sign(
            token=token, path=path, query_or_body=query
        )
        url = f"{WEB_BASE}{path}" + (f"?{query}" if query else "")
        return await _http_get_json(url, _web_headers(sign_data))

    token = await refresh_public_web_token()
    if not token:
        return None
    data = await do_request(token)
    if isinstance(data, dict) and data.get("code") == _REQUEST_ERROR_CODE:
        token = await refresh_public_web_token(force=True)
        if not token:
            return None
        data = await do_request(token)
    return data if isinstance(data, dict) else None


def parse_ann_list(response: dict[str, Any] | None, limit: int = 10) -> list[dict[str, str]]:
    """home/index 响应 → 公告列表（title/url/date，新→旧）。

    响应结构（与终末地同型）：data.list[] = {item: {title, url, date...}, user: {...}}
    """
    if not isinstance(response, dict) or response.get("code") != 0:
        return []
    entries = ((response.get("data") or {}).get("list")) or []
    result: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item = entry.get("item") or {}
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        result.append(
            {
                "id": str(item.get("id") or ""),
                "title": title,
                "url": str(item.get("url") or ""),
                "date": str(item.get("date") or ""),
            }
        )
        if len(result) >= limit:
            break
    return result


async def get_ark_ann_list(limit: int = 10) -> list[dict[str, str]]:
    """明日方舟官方公告列表（森空岛 web 端）。失败返回空列表。"""
    params = {
        "gameId": ARK_GAME_ID,
        "cateId": ARK_ANN_CATE_ID,
        "pageSize": "10",
        "sortType": "2",
    }
    response = await web_public_get(WEB_HOME_PATH, params)
    return parse_ann_list(response, limit)
