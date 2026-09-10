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
import ipaddress
import socket
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import httpx

from gsuid_core.logger import logger

# 默认请求超时（秒）；个别慢接口（gamedata 全量表 / 外置渲染）按调用方覆盖
DEFAULT_TIMEOUT = 25.0

# GET 的瞬时故障重试：传输异常 / 网关类 5xx（502/503/504）默认重试 1 次。
# POST 不重试——登录/换 token 等非幂等接口，重试可能重复提交。
TRANSIENT_STATUSES = frozenset({502, 503, 504})
TRANSIENT_RETRY_DELAY = 0.5

# ── 用户可控 URL 的安全下载边界（OCR 截图等）───────────────────────
SAFE_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024  # 单文件 8MB（截图远小于此）
SAFE_DOWNLOAD_MAX_REDIRECTS = 3
SAFE_DOWNLOAD_TIMEOUT = 15.0

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


class UnsafeUrlError(ValueError):
    """URL 未通过安全校验（非 http(s) / 主机解析到非公网地址等）。"""


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
        code: int | None = None,
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
    data: Any | None  # 响应 JSON；非 JSON 响应为 None
    cookies: list[str]  # set-cookie 头列表（无则空）


def describe_error(exc: Exception) -> str:
    """异常描述：类名 + message（替代只打类名的旧日志格式）。"""
    text = str(exc).strip()
    name = exc.__class__.__name__
    return f"{name}: {text}" if text else name


async def get_client() -> httpx.AsyncClient:
    """获取共享 AsyncClient（懒初始化；被关闭后自动重建，热重载安全）。

    LocalProxyUrl 配置非空时统一走代理（森空岛/鹰角/PRTS/一图流全部请求）；
    代理在 client 创建时读取，修改配置需重启/热重载后生效。
    """
    global _client
    if _client is None or _client.is_closed:
        async with _client_lock:
            if _client is None or _client.is_closed:
                proxy_url = ""
                try:
                    from .config import ArkConfig

                    proxy_url = str(
                        ArkConfig.get_config("LocalProxyUrl").data or ""
                    ).strip()
                except Exception:
                    proxy_url = ""
                client_kwargs: dict = dict(
                    timeout=DEFAULT_TIMEOUT,
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_connections=20,
                        max_keepalive_connections=10,
                    ),
                )
                if proxy_url:
                    logger.info(f"[ArknightsUID·http] 共享客户端启用代理: {proxy_url}")
                    client_kwargs["proxy"] = proxy_url
                _client = httpx.AsyncClient(**client_kwargs)
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
    transient_retries: int = 1,
) -> RequestResult:
    """统一 GET/POST → (status, json, set_cookies)。

    网络异常 → (0, None, []) 并打 warning（含异常 message）；
    非 JSON 响应 → (status, None, []) 并打 warning。业务码判断留给调用方。

    GET 的瞬时故障（传输异常 / 502/503/504）默认重试 1 次（0.5s 退避）；
    POST 一律不重试（非幂等）。
    """
    client = await get_client()
    is_get = method.upper() == "GET"
    attempts = 1 + (transient_retries if is_get and transient_retries > 0 else 0)
    last_exc: Exception | None = None
    resp = None
    for attempt in range(1, attempts + 1):
        try:
            if is_get:
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
            last_exc = exc
        else:
            last_exc = None
            if not (is_get and resp.status_code in TRANSIENT_STATUSES):
                break
        if attempt < attempts:
            logger.debug(
                f"[ArknightsUID·http] {method} {url} 瞬时故障"
                f"（第 {attempt} 次），{TRANSIENT_RETRY_DELAY}s 后重试"
            )
            await asyncio.sleep(TRANSIENT_RETRY_DELAY)
    if last_exc is not None:
        logger.warning(
            f"[ArknightsUID·http] {method} {url} 请求失败: {describe_error(last_exc)}"
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
        resp = await client.get(url, timeout=timeout, follow_redirects=follow_redirects)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.warning(f"[ArknightsUID·http] 下载失败 {url}: {describe_error(exc)}")
        return None


async def download_bytes_status(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
    quiet_404: bool = False,
) -> tuple[int, bytes | None]:
    """下载二进制内容并附带 HTTP 状态码，供调用方区分 404 与网络故障。

    网络异常 → (0, None)；4xx/5xx → (status, None)；成功 → (200, content)。
    quiet_404=True 时 404 降为 debug——供"上游渐进上传资源"的调用方
    （如最新期肉鸽藏品图标）把预期缺口从 warning 噪音里摘出来。
    """
    client = await get_client()
    try:
        resp = await client.get(url, timeout=timeout, follow_redirects=follow_redirects)
    except Exception as exc:
        logger.warning(f"[ArknightsUID·http] 下载失败 {url}: {describe_error(exc)}")
        return 0, None
    if resp.status_code >= 400:
        if resp.status_code == 404 and quiet_404:
            logger.debug(
                f"[ArknightsUID·http] 下载缺失 {url}: HTTP 404（上游未上传）"
            )
        else:
            logger.warning(
                f"[ArknightsUID·http] 下载失败 {url}: HTTP {resp.status_code}"
            )
        return resp.status_code, None
    return resp.status_code, resp.content


async def download_bytes_conditional(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    etag: str | None = None,
    last_modified: str | None = None,
    quiet_404: bool = False,
) -> tuple[int, bytes | None, dict[str, str]]:
    """条件 GET：带 If-None-Match/If-Modified-Since 的 diff 复核。

    供"内容近永久、只在版本更新时变化"的大表（敌人库/全量表）使用：
    上游未变 → (304, None, {})，调用方仅续期缓存时间戳，零下载零解析；
    已变化 → (200, content, {"etag":…, "last_modified":…})，校验器随缓存
    落盘供下次复核（校验器与具体 URL 绑定，多镜像不互通，调用方需
    一并记来源 URL）。不带校验器时等价普通 GET（仅回收校验器）。

    quiet_404 语义同 download_bytes_status（404 预期缺口降 debug）。
    网络异常 → (0, None, {})；4xx/5xx → (status, None, {})。
    """
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    client = await get_client()
    try:
        resp = await client.get(
            url, timeout=timeout, headers=headers or None, follow_redirects=True
        )
    except Exception as exc:
        logger.warning(f"[ArknightsUID·http] 下载失败 {url}: {describe_error(exc)}")
        return 0, None, {}
    if resp.status_code == 304:
        logger.debug(f"[ArknightsUID·http] 上游未变化（304）: {url}")
        return 304, None, {}
    if resp.status_code >= 400:
        if resp.status_code == 404 and quiet_404:
            logger.debug(
                f"[ArknightsUID·http] 下载缺失 {url}: HTTP 404（上游未上传）"
            )
        else:
            logger.warning(
                f"[ArknightsUID·http] 下载失败 {url}: HTTP {resp.status_code}"
            )
        return resp.status_code, None, {}
    validators: dict[str, str] = {}
    et = resp.headers.get("ETag") or resp.headers.get("etag")
    lm = resp.headers.get("Last-Modified") or resp.headers.get("last-modified")
    if et:
        validators["etag"] = et
    if lm:
        validators["last_modified"] = lm
    return resp.status_code, resp.content, validators


async def assert_public_http_url(url: str) -> str:
    """校验 http(s) URL 的主机解析到公网地址；不合规抛 UnsafeUrlError。

    拒绝回环/私网/链路本地（含云元数据 169.254.169.254）/多播/保留地址
    ——SSRF 防护。getaddrinfo 下放线程，全部解析结果都要求 is_global。
    注意解析与实际连接之间存在 DNS 重绑定 TOCTOU 窗口，由调用方
    （safe_download_bytes）的"逐跳校验 + 禁自动重定向"收窄。
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        # urlsplit 对 authority 含裸 [ ] 等畸形 URL 抛裸 ValueError
        # （如 http://[evil.com]/x.png）。转成 UnsafeUrlError，让
        # "校验失败返回 None 不抛"的契约对所有调用方成立（审计 #39）
        raise UnsafeUrlError(f"malformed url: {exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"scheme not allowed: {parts.scheme!r}")
    host = parts.hostname or ""
    if not host:
        raise UnsafeUrlError("missing host")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError as exc:
        raise UnsafeUrlError(f"DNS resolve failed: {exc.__class__.__name__}") from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            raise UnsafeUrlError(f"unparseable address: {addr!r}") from None
        # is_global 不含多播/保留段的判定（224.0.0.1 等 is_global=True），显式拒绝
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise UnsafeUrlError(f"non-public address: {ip}")
    return url


async def safe_download_bytes(
    url: str,
    *,
    max_bytes: int = SAFE_DOWNLOAD_MAX_BYTES,
    timeout: float = SAFE_DOWNLOAD_TIMEOUT,
    max_redirects: int = SAFE_DOWNLOAD_MAX_REDIRECTS,
) -> bytes | None:
    """用户可控 URL 的受限下载（OCR 截图等）。

    与 download_bytes 的差异（SSRF / 资源耗尽边界）：
    - 仅 http(s)，且主机必须解析为公网地址（assert_public_http_url）
    - 不自动跟随重定向：逐跳重新校验目标，最多 max_redirects 跳
    - 流式读取并强制 max_bytes 上限（超出立即中止，不整读进内存）

    校验不通过/网络失败/超限：打 warning 返回 None（不抛，便于多图循环降级）。
    """
    client = await get_client()
    current = url
    for _hop in range(max_redirects + 1):
        try:
            await assert_public_http_url(current)
        except UnsafeUrlError as exc:
            logger.warning(f"[ArknightsUID·http] 拒绝不安全 URL {url}: {exc}")
            return None
        try:
            async with client.stream(
                "GET", current, timeout=timeout, follow_redirects=False
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "")
                    if not location:
                        logger.warning(
                            f"[ArknightsUID·http] 重定向缺 Location: {current}"
                        )
                        return None
                    current = str(httpx.URL(current).join(location))
                    continue
                resp.raise_for_status()
                buf = bytearray()
                async for chunk in resp.aiter_bytes(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.warning(
                            f"[ArknightsUID·http] 响应超过 {max_bytes} 字节上限: {current}"
                        )
                        return None
                return bytes(buf)
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·http] 安全下载失败 {current}: {describe_error(exc)}"
            )
            return None
    logger.warning(f"[ArknightsUID·http] 重定向超过 {max_redirects} 跳: {url}")
    return None


def register_shutdown_hook() -> None:
    """注册进程退出钩子关闭共享 client（连接池/socket 不依赖 GC 兜底）。

    热重载（reload_plugin）不走 shutdown 钩子：旧 client 由
    `is_closed` 自检重建逻辑兜底（get_client 检测到已关闭即重建），
    这里只保证正常停机时干净释放（审计 #34）。
    """
    try:
        from gsuid_core.server import on_core_shutdown

        @on_core_shutdown
        async def _ark_http_close() -> None:  # pragma: no cover - 退出路径
            try:
                await aclose()
            except Exception:
                pass
    except Exception:
        pass


register_shutdown_hook()
