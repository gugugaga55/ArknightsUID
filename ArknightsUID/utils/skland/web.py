"""森空岛公开 Web API（P4：官方公告）。

链路（复刻 EndUID `_web_public_*` 机制，接口事实层面复用，无 UI）：
- `GET zonai.skland.com/web/v1/auth/refresh`：无需登录。用**空 token** 走
  森空岛签名（复用 skland.client._generate_sign），服务端颁发短期公共 token。
  2026-08-19 起服务端校验设备指纹：dId 为空或无效返回 code=10001「设备信息无效」，
  需带 smsdk.get_device_id() 生成的 web 端 dId，被拒时换新设备 ID 重试一次
  （对齐 EndUID ffe20cd "did"）
- `GET zonai.skland.com/web/v1/home/index?gameId=&cateId=&pageSize=&sortType=2`：
  以公共 token 为 HMAC 密钥签名；code=10000 表示 token 失效，刷新后重试一次。
  pageSize 服务端上限 10（超过直接 400 参数错误），更多条数用 pageToken 分页累计
  （对齐 EndUID b0a82ae 踩坑结论）
- `GET zonai.skland.com/web/v1/item?id=`：公告详情。textSlice 存储顺序是乱的，
  真实阅读顺序在 format 字段（JSON）里，按 paragraph.contentId 反查拼接；
  format 缺失/解析失败时兜底原始存储顺序（对齐 EndUID b0a82ae 段落排序修复）
- 明日方舟 gameId=1（channelMasterId），cateId 为公告分类号（探测得出，
  见 ARK_ANN_CATE_ID；终末地是 12）

真实联调状态：token 链单测全 mock；cateId 由 tools/probe_skland_ann.py 探测验证。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Optional

from gsuid_core.logger import logger

from ..api.request_util import SIGN_VNAME, RespCode
from ..api.smsdk import get_device_id
from ..http import request_json
from . import client as skland_client

WEB_BASE = "https://zonai.skland.com"
WEB_REFRESH_PATH = "/web/v1/auth/refresh"
WEB_HOME_PATH = "/web/v1/home/index"
WEB_ITEM_PATH = "/web/v1/item"
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

# 明日方舟在森空岛 web 端的游戏/公告分类 ID（gameId=1 即 channelMasterId；
# cateId 探测候选：1=官方公告？以探测结果为准，默认 1）
ARK_GAME_ID = 1
ARK_ANN_CATE_ID = 1

# 请求异常码（token 失效）→ 刷新重试；10001（设备信息无效）→ 换设备重试
_REQUEST_ERROR_CODE = int(RespCode.REQUEST_ERROR)
_CRED_INVALID_CODE = int(RespCode.CRED_INVALID)

# home/index 服务端 pageSize 上限，超过返回 400 参数错误
PAGE_SIZE_MAX = 10

# 进程内公共 token 缓存（25 分钟，服务端短期）
_token_cache: dict[str, Any] = {"token": "", "ts": 0.0}
_TOKEN_TTL = 25 * 60

# web 端设备 ID：进程内保持稳定（对应浏览器 localStorage 里的 dId）
_web_did: str = ""

# 公告列表缓存（10 分钟，对齐 EndUID ANN_LIST_CACHE_DURATION）
_list_cache: dict[str, Any] = {"data": [], "ts": 0.0}
_LIST_CACHE_TTL = 600

# 公告详情缓存（10 分钟；textKey → (ts, detail)）
_detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_DETAIL_CACHE_TTL = 600


def _get_web_did(force: bool = False) -> str:
    """获取 web 端设备 ID；服务端 2026-08-19 起校验，为空返回 10001。"""
    global _web_did
    if force or not _web_did:
        try:
            _web_did = get_device_id(
                user_agent=WEB_USER_AGENT,
                referer="https://www.skland.com/",
            )
        except Exception as e:
            logger.error(f"[ArknightsUID·公告] 生成 web 端设备 ID 失败: {e}")
            _web_did = ""
    return _web_did


def _web_headers(sign_data: dict[str, str], did: str) -> dict[str, str]:
    return {
        "platform": "3",
        "timestamp": sign_data["timestamp"],
        "dId": did,
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
    """获取/刷新公开 Web token（空 token + 设备 ID 签名）。失败返回空串。"""
    now = time.time()
    if (
        not force
        and _token_cache["token"]
        and now - _token_cache["ts"] < _TOKEN_TTL
    ):
        return str(_token_cache["token"])

    async def do_request(did: str) -> dict[str, Any] | None:
        sign_data = skland_client._generate_sign(
            token="", path=WEB_REFRESH_PATH, query_or_body="", did=did
        )
        return await _http_get_json(
            f"{WEB_BASE}{WEB_REFRESH_PATH}", _web_headers(sign_data, did)
        )

    did = _get_web_did()
    if not did:
        return ""
    data = await do_request(did)

    # 设备 ID 被服务端判定无效（10001 设备信息无效）时，换新设备 ID 重试一次
    if isinstance(data, dict) and data.get("code") == _CRED_INVALID_CODE:
        logger.warning(
            "[ArknightsUID·公告] web 设备 ID 被拒，重新生成后重试: "
            f"{data.get('message')}"
        )
        did = _get_web_did(force=True)
        if not did:
            return ""
        data = await do_request(did)

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
    """公开 Web GET（自动 token/设备 ID；token 失效或设备被拒各重试一次）。"""
    query = _query_string(params)

    async def do_request(token: str, did: str) -> dict[str, Any] | None:
        sign_data = skland_client._generate_sign(
            token=token, path=path, query_or_body=query, did=did
        )
        url = f"{WEB_BASE}{path}" + (f"?{query}" if query else "")
        return await _http_get_json(url, _web_headers(sign_data, did))

    token = await refresh_public_web_token()
    if not token:
        return None
    data = await do_request(token, _get_web_did())

    if isinstance(data, dict) and data.get("code") == _REQUEST_ERROR_CODE:
        # token 失效 → 强刷 token 后重试（设备不变）
        token = await refresh_public_web_token(force=True)
        if not token:
            return None
        data = await do_request(token, _get_web_did())

    if isinstance(data, dict) and data.get("code") == _CRED_INVALID_CODE:
        # 设备 ID 被判定无效（10001）→ 换新设备 ID 用同一 token 重试
        logger.warning(f"[ArknightsUID·公告] web 设备 ID 被拒，重新生成后重试: {path}")
        if _get_web_did(force=True):
            data = await do_request(token, _get_web_did())

    return data if isinstance(data, dict) else None


def _format_ts(ts: Any) -> str:
    """时间戳（秒/毫秒自适应）→ YYYY-MM-DD；非法返回空串。"""
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value > 10**12:  # 毫秒
        value //= 1000
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d")


def parse_ann_list(response: dict[str, Any] | None, limit: int = 10) -> list[dict[str, str]]:
    """home/index 响应 → 公告列表（id/title/url/date，新→旧）。

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
        date = str(item.get("date") or "").strip()
        if not date:
            date = _format_ts(item.get("publishedAtTs") or item.get("timestamp"))
        url = str(item.get("url") or "").strip()
        if not url and item.get("id"):
            # 明日方舟列表项无 url 字段，统一拼 web 端文章页
            url = f"https://www.skland.com/article?id={item['id']}"
        result.append(
            {
                "id": str(item.get("id") or ""),
                "title": title,
                "url": url,
                "date": date,
            }
        )
        if len(result) >= limit:
            break
    return result


async def get_ark_ann_list(limit: int = 10) -> list[dict[str, str]]:
    """明日方舟官方公告列表（森空岛 web 端，10 分钟缓存）。

    pageSize 服务端上限 10，超过会 400；目标条数更大时用 pageToken 分页累计。
    失败返回空列表（有旧缓存时返回旧缓存）。
    """
    limit = max(1, min(int(limit), 50))
    now = time.time()
    cached = _list_cache["data"]
    if cached and now - _list_cache["ts"] < _LIST_CACHE_TTL and len(cached) >= limit:
        return list(cached[:limit])

    collected: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    page_token = ""
    while len(collected) < limit:
        params: dict[str, Any] = {
            "gameId": ARK_GAME_ID,
            "cateId": ARK_ANN_CATE_ID,
            "pageSize": str(PAGE_SIZE_MAX),
            "sortType": "2",
        }
        if page_token:
            params["pageToken"] = page_token
        response = await web_public_get(WEB_HOME_PATH, params)
        if not isinstance(response, dict) or response.get("code") != 0:
            logger.warning(
                "[ArknightsUID·公告] 公告列表拉取失败: "
                f"code={response.get('code') if isinstance(response, dict) else '?'}"
            )
            break
        for ann in parse_ann_list(response, limit=PAGE_SIZE_MAX):
            if ann["id"] in seen_ids:
                continue
            seen_ids.add(ann["id"])
            collected.append(ann)
        page_token = str(((response.get("data") or {}).get("pageToken")) or "")
        if not page_token:
            break

    if collected:
        _list_cache["data"] = collected
        _list_cache["ts"] = time.time()
        return collected[:limit]
    return list(cached[:limit])  # 拉取失败回退旧缓存（可能过期，好过空白）


def parse_ann_detail(response: dict[str, Any] | None) -> dict[str, Any] | None:
    """item 响应 → 公告详情（正文段落按 format 还原阅读顺序）。

    textSlice 的存储顺序是乱的，真实显示顺序在 format 字段（JSON）里：
    按 format.data 遍历 paragraph 节点，用 contents[].contentId 反查 textSlice.id
    拼接每个段落内的 text/link 片段。format 缺失/解析失败时退回原始存储顺序。
    """
    if not isinstance(response, dict) or response.get("code") != 0:
        return None
    data = response.get("data") or {}
    item = data.get("item") or {}
    user = data.get("user") or {}
    if not isinstance(item, dict):
        return None

    text_lookup = {
        str(t.get("id")): str(t.get("c") or "")
        for t in (item.get("textSlice") or [])
        if isinstance(t, dict)
    }
    ordered_text: list[str] = []
    fmt_raw = str(item.get("format") or "")
    if fmt_raw:
        try:
            fmt_obj = json.loads(fmt_raw)
            for node in (fmt_obj.get("data") or []):
                if not isinstance(node, dict) or node.get("type") != "paragraph":
                    continue
                parts: list[str] = []
                for content in (node.get("contents") or []):
                    if not isinstance(content, dict):
                        continue
                    if content.get("type") in ("text", "link"):
                        cid = content.get("contentId")
                        if cid is not None:
                            parts.append(text_lookup.get(str(cid), ""))
                ordered_text.append("".join(parts))
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            logger.warning(f"[ArknightsUID·公告] format 解析失败: {e}")
            ordered_text = []
    if not ordered_text:
        # 兜底：format 缺失或解析失败时退回原始存储顺序
        ordered_text = list(text_lookup.values())

    images = [
        {
            "url": str(img.get("url") or ""),
            "width": img.get("width") or 0,
            "height": img.get("height") or 0,
        }
        for img in (item.get("imageListSlice") or [])
        if isinstance(img, dict)
    ]
    videos = [
        {
            "url": str(v.get("url") or ""),
            "coverUrl": str(((v.get("cover") or {}).get("url")) or ""),
        }
        for v in (item.get("videoListSlice") or [])
        if isinstance(v, dict)
    ]

    created_ts = item.get("publishedAtTs") or item.get("timestamp") or 0
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "createdAtTs": created_ts,
        "date": _format_ts(created_ts),
        "userName": str(user.get("nickname") or ""),
        "userAvatar": str(user.get("avatar") or ""),
        "userIpLocation": str(user.get("latestIpLocation") or ""),
        "paragraphs": [p for p in ordered_text if p.strip()],
        "images": images,
        "videos": videos,
    }


async def get_ann_detail(post_id: Any) -> Optional[dict[str, Any]]:
    """公告详情（10 分钟缓存）。失败返回 None。"""
    key = str(post_id)
    cached = _detail_cache.get(key)
    if cached and time.time() - cached[0] < _DETAIL_CACHE_TTL:
        return cached[1]

    response = await web_public_get(WEB_ITEM_PATH, {"id": key})
    detail = parse_ann_detail(response)
    if detail is None:
        logger.warning(
            "[ArknightsUID·公告] 公告详情拉取失败: "
            f"id={key} code={response.get('code') if isinstance(response, dict) else '?'}"
        )
        return None
    _detail_cache[key] = (time.time(), detail)
    return detail
