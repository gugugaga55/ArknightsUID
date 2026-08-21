"""共享 HTTP 客户端与统一请求 helper。

此前每个接口模块（skland / hypergryph / prts / yituliu / gamedata / render）
各自 `async with httpx.AsyncClient(...)`，每次请求重建连接（TLS 握手不复用）、
超时与错误处理各写一套。本模块收口为：

- 进程级单例 `httpx.AsyncClient`（懒初始化，热重载/关闭后自动重建）
- `request_json()` 统一 GET/POST → JSON 的解析、异常日志（带异常 message，
  不再只打类名）与 set-cookie 提取
- `ApiError` 结构化错误（status/code/message），供上层区分失败原因

每请求的 timeout / follow_redirects 通过参数覆盖共享 client 默认值。
"""
from __future__ import annotations

import asyncio
from typing import Any, NamedTuple, Optional

import httpx

from gsuid_core.logger import logger

# 默认请求超时（秒）；个别慢接口（gamedata 全量表 / 外置渲染）按调用方覆盖
DEFAULT_TIMEOUT = 25.0

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


class ApiError(Exception):
    """接口请求失败（网络层或业务层）。

    status: HTTP 状态码（0 = 请求未送达/网络异常）
    code: 业务码（响应 JSON 的 code/status 字段，无则为 None）
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: Optional[int] = None,
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.url = url

    def __str__(self) -> str:
        parts = [self.message]
        if self.url:
            parts.append(self.url)
        detail = f"status={self.status}"
        if self.code is not None:
            detail += f" code={self.code}"
        parts.append(detail)
        return "（".join(parts) + "）" if len(parts) > 1 else parts[0]


class RequestResult(NamedTuple):
    """request_json 结果：status=0 表示请求未送达（网络异常）。"""

    status: int
    data: Any | None          # 响应 JSON；非 JSON 响应为 None
    cookies: list[str]        # set-cookie 头列表（无则空）


def describe_error(exc: Exception) -> str:
    """异常描述：类名 + message（替代只打类名的旧日志格式）。"""
    text = str(exc).strip()
    name = exc.__class__.__name__
    return f"{name}: {text}" if text else name


async def get_client() -> httpx.AsyncClient:
    """获取共享 AsyncClient（懒初始化；被关闭后自动重建，热重载安全）。"""
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=DEFAULT_TIMEOUT,
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=10,
                    ),
                )
    return _client


async def aclose() -> None:
    """关闭共享 client（测试/进程退出用；业务代码不需要调用）。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    content: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
) -> RequestResult:
    """统一 GET/POST → (status, json, set_cookies)。

    网络异常 → (0, None, []) 并打 warning（含异常 message）；
    非 JSON 响应 → (status, None, []) 并打 warning。业务码判断留给调用方。
    """
    client = await get_client()
    try:
        if method.upper() == "GET":
            resp = await client.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )
        else:
            resp = await client.post(
                url,
                headers=headers,
                params=params,
                json=json_body,
                content=content,
                timeout=timeout,
                follow_redirects=follow_redirects,
            )
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·http] {method} {url} 请求失败: {describe_error(exc)}"
        )
        return RequestResult(0, None, [])

    cookies = resp.headers.get_list("set-cookie") or []
    try:
        data = resp.json()
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·http] {url} 非 JSON 响应 "
            f"HTTP {resp.status_code}: {describe_error(exc)}"
        )
        return RequestResult(resp.status_code, None, cookies)
    return RequestResult(resp.status_code, data, cookies)


async def download_bytes(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
) -> bytes | None:
    """下载二进制内容（图标/静态文件）。失败打 warning 并返回 None。"""
    client = await get_client()
    try:
        resp = await client.get(
            url, timeout=timeout, follow_redirects=follow_redirects
        )
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·http] 下载失败 {url}: {describe_error(exc)}"
        )
        return None


async def download_bytes_status(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
) -> tuple[int, bytes | None]:
    """下载二进制内容并附带 HTTP 状态码，供调用方区分 404 与网络故障。

    网络异常 → (0, None)；4xx/5xx → (status, None)；成功 → (200, content)。
    """
    client = await get_client()
    try:
        resp = await client.get(
            url, timeout=timeout, follow_redirects=follow_redirects
        )
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·http] 下载失败 {url}: {describe_error(exc)}"
        )
        return 0, None
    if resp.status_code >= 400:
        logger.warning(
            f"[ArknightsUID·http] 下载失败 {url}: HTTP {resp.status_code}"
        )
        return resp.status_code, None
    return resp.status_code, resp.content
