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
import logging
import re
import socket
import time
from contextvars import ContextVar
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

_private_download: ContextVar[bool] = ContextVar("ark_private_download", default=False)


class _PrivateDownloadLogFilter(logging.Filter):
    """httpx 的 INFO 请求日志含完整 URL，受限下载期间不输出该日志。"""

    _ark_private_download_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_download.get()


_httpx_logger = logging.getLogger("httpx")
for _filter in list(_httpx_logger.filters):
    if getattr(_filter, "_ark_private_download_filter", False):
        _httpx_logger.removeFilter(_filter)
_httpx_logger.addFilter(_PrivateDownloadLogFilter())

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()
# 共享 client 创建时的路由策略快照（与缓存 client 的实际挂载一致；
# 不在请求时实时读环境去猜已缓存 client 的行为）：
# - config：ArkConfig.LocalProxyUrl 显式代理，全部走代理
# - env：环境代理参与路由，逐目标按 _client_route_rules 判定
#       （NO_PROXY 命中的目标实际直连，仍需 IP 绑定）
# - direct：无任何代理
# - ""：共享 client 尚未创建（生产路径 safe_download 先经 get_client()，
#       不会见到该值；此时受限下载按"策略未知"拒绝）
_client_proxy_mode: str = ""
_client_route_rules: list[_RouteRule] | None = None


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


def _safe_proxy_desc(proxy_url: str) -> str:
    """代理配置的日志安全描述：仅 scheme/host/port。

    带 userinfo 的代理 URL（http://user:pass@host:port）绝不能整串进日志
    ——与 Cookie/Stoken 同级的凭据保护口径。解析失败时只写“已配置”，
    不回退打印原文。
    """
    try:
        parts = urlsplit(proxy_url)
        host = parts.hostname or ""
        if not host:
            return "已配置（主机名未解析）"
        desc = f"{parts.scheme or 'http'}://{host}"
        if parts.port:
            desc += f":{parts.port}"
        return desc
    except ValueError:
        return "已配置（URL 无法安全解析）"


def _is_ip_hostname(hostname: str, *, v6: bool) -> bool:
    clean = hostname.strip()
    if clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1]
    try:
        addr = ipaddress.ip_address(clean.split("/")[0])
    except ValueError:
        return False
    return isinstance(addr, ipaddress.IPv6Address) if v6 else addr.version == 4


def _parse_cidr_network(
    hostname: str,
) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    clean = hostname.strip()
    if "/" not in clean:
        return None
    if clean.startswith("[") and "]" in clean:
        ip_part, _, prefix = clean[1:].partition("]/")
        clean = f"{ip_part}/{prefix}"
    try:
        return ipaddress.ip_network(clean, strict=False)
    except ValueError:
        return None


def _normalize_rule_host(raw_host: str) -> str:
    host = raw_host.strip().lower().rstrip(".")
    if not host or host == "*":
        return ""
    prefix = ""
    core = host
    if host.startswith("*."):
        prefix, core = "*.", host[2:]
    elif host.startswith("*"):
        prefix, core = "*", host[1:]
    if core:
        try:
            core = httpx.URL(f"http://{core}").host
        except Exception:
            pass
    return f"{prefix}{core}" if core else ""


class _RouteRule:
    """httpx._utils.URLPattern 的同构镜像（生产代码不 import httpx 私有模块）。

    覆盖 httpx 环境代理挂载的全部形态：scheme 挂载（http://、https://）、
    all:// 通配、*.domain / *domain / 精确域名、IPv4/IPv6/[::1]/CIDR 与端口。
    与真实 httpx 的行为漂移由测试对拍真实 client 的挂载选择兜底。
    """

    def __init__(
        self,
        pattern: str,
        *,
        proxy: bool,
        network: ipaddress.IPv4Network | ipaddress.IPv6Network | None = None,
    ) -> None:
        self.proxy = proxy
        self.network = network
        if network is not None:
            self.scheme = ""
            self.host = str(network)
            self.port = None
            self.regex = None
            return
        parts = urlsplit(pattern)
        self.scheme = "" if parts.scheme == "all" else parts.scheme.lower()
        raw_host = parts.hostname or ""
        if _is_ip_hostname(raw_host, v6=True) or _is_ip_hostname(raw_host, v6=False):
            self.host = raw_host.lower()
        else:
            self.host = _normalize_rule_host(raw_host)
        port = parts.port
        # httpx.URL 对默认端口（http:80 / https:443）返回 url.port is None，
        # 规则侧必须同样归一到 None，否则 http://host:80 规则永不命中默认端口请求。
        if (self.scheme == "http" and port == 80) or (
            self.scheme == "https" and port == 443
        ):
            port = None
        self.port = port
        self.regex: re.Pattern[str] | None = None
        if self.host:
            if self.host.startswith("*."):
                domain = re.escape(self.host[2:])
                self.regex = re.compile(rf"^.+\.{domain}$")
            elif self.host.startswith("*"):
                domain = re.escape(self.host[1:])
                self.regex = re.compile(rf"^(.+\.)?{domain}$")
            else:
                self.regex = re.compile(rf"^{re.escape(self.host)}$")

    def matches(self, url: httpx.URL) -> bool:
        if self.scheme and self.scheme != url.scheme:
            return False
        url_host = url.host.rstrip(".")
        if self.network is not None:
            try:
                addr = ipaddress.ip_address(url_host.strip("[]"))
            except ValueError:
                return False
            return addr in self.network
        if self.regex is not None and not self.regex.match(url_host):
            return False
        if self.port is not None and self.port != url.port:
            return False
        return True

    def _priority(self) -> tuple[int, int, int]:
        # 与 URLPattern.priority 相同：端口 > 主机长度 > scheme 长度
        return (
            0 if self.port is not None else 1,
            -len(self.host),
            -len(self.scheme),
        )


def _build_env_route_rules() -> list[_RouteRule] | None:
    """镜像 httpx._utils.get_environment_proxies 的挂载构造。

    返回 None = 环境没有任何 http(s)/all 代理与 no_proxy 条目（全直连）；
    否则返回按 httpx 同款优先级排序的逐目标规则（proxy=False 表示该目标
    实际直连——NO_PROXY 命中时必须仍走 IP 绑定，不能因"环境里有代理"
    一律跳过）。单条畸形 NO_PROXY 规则安全跳过，绝不阻塞共享 client 初始化。
    """
    import urllib.request

    try:
        proxy_info = urllib.request.getproxies()
    except Exception:
        return None
    mounts: dict[str, str | None] = {}
    cidr_rules: list[_RouteRule] = []
    for scheme in ("http", "https", "all"):
        if proxy_info.get(scheme):
            hostname = proxy_info[scheme]
            mounts[f"{scheme}://"] = (
                hostname if "://" in hostname else f"http://{hostname}"
            )
    no_proxy_hosts = [h.strip() for h in proxy_info.get("no", "").split(",")]
    for hostname in no_proxy_hosts:
        if hostname == "*":
            return []  # NO_PROXY=*：全部实际直连
        if not hostname:
            continue
        net = _parse_cidr_network(hostname)
        if net is not None:
            cidr_rules.append(_RouteRule("", proxy=False, network=net))
            continue
        if "://" in hostname:
            mounts[hostname] = None
        elif _is_ip_hostname(hostname, v6=False):
            mounts[f"all://{hostname}"] = None
        elif _is_ip_hostname(hostname, v6=True):
            bare_v6 = hostname[1:-1] if hostname.startswith("[") and hostname.endswith("]") else hostname
            mounts[f"all://[{bare_v6}]"] = None
        elif hostname.lower() == "localhost":
            mounts[f"all://{hostname}"] = None
        else:
            mounts[f"all://*{hostname}"] = None
    if not mounts and not cidr_rules:
        return None
    rules: list[_RouteRule] = list(cidr_rules)
    for pattern, value in mounts.items():
        try:
            rules.append(_RouteRule(pattern, proxy=value is not None))
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·http] 忽略无法解析的环境代理规则: {exc.__class__.__name__}"
            )
    rules.sort(key=_RouteRule._priority)
    return rules


def _route_decision(url: httpx.URL) -> str:
    """该 URL 经共享 client 的实际路由："direct" / "proxy"；未知返回 ""。

    依据是共享 client 创建时的快照（config 代理 / 环境代理逐目标规则），
    与缓存 client 的挂载一致；不实时读环境。"" 表示策略未知——受限下载
    必须拒绝，不能假称安全。
    """
    if _client_proxy_mode == "config":
        return "proxy"
    if _client_proxy_mode == "direct":
        return "direct"
    if _client_proxy_mode == "env":
        rules = _client_route_rules
        if rules is None:
            return "direct"
        for rule in rules:
            if rule.matches(url):
                return "proxy" if rule.proxy else "direct"
        return "direct"  # 无规则命中 → client 默认传输（直连池）
    return ""


async def get_client() -> httpx.AsyncClient:
    """获取共享 AsyncClient（懒初始化；被关闭后自动重建，热重载安全）。

    LocalProxyUrl 配置非空时统一走代理（森空岛/鹰角/PRTS/一图流全部请求）；
    代理在 client 创建时读取，修改配置需重启/热重载后生效。
    """
    global _client, _client_proxy_mode, _client_route_rules
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
                    # 日志只写安全解析出的 scheme/host/port：代理 userinfo
                    # 是与 Cookie 同级的凭据，绝不能整串落普通日志
                    logger.info(
                        f"[ArknightsUID·http] 共享客户端启用代理: "
                        f"{_safe_proxy_desc(proxy_url)}"
                    )
                    client_kwargs["proxy"] = proxy_url
                    _client_proxy_mode = "config"
                    _client_route_rules = None
                else:
                    rules = _build_env_route_rules()
                    _client_route_rules = rules
                    _client_proxy_mode = "direct" if rules is None else "env"
                _client = httpx.AsyncClient(**client_kwargs)
    return _client


async def aclose() -> None:
    """关闭共享 client 与受限下载绑定 client（测试/进程退出用）。"""
    global _client, _client_proxy_mode, _client_route_rules, _pinned_client, _pinned_transport
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
    _client_proxy_mode = ""
    _client_route_rules = None
    if _pinned_client is not None and not _pinned_client.is_closed:
        await _pinned_client.aclose()
    elif _pinned_client is None and _pinned_transport is not None:
        await _pinned_transport.aclose()
    _pinned_client = None
    _pinned_transport = None


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


async def _validated_target(url: str) -> tuple[str, list[str]]:
    """校验 http(s) URL 的主机解析到公网地址；返回 (主机名, 全部公网 IP)。

    拒绝回环/私网/链路本地（含云元数据 169.254.169.254）/多播/保留地址
    ——SSRF 防护。getaddrinfo 下放线程，全部解析结果都要求 is_global。
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
        # getaddrinfo 与 httpx 的 IDNA 规则可能不同；校验与拨号必须使用
        # httpx 实际请求的 ASCII Origin，避免解析和绑定不同的域名。
        host = httpx.URL(url).raw_host.decode("ascii")
    except (ValueError, httpx.InvalidURL, UnicodeError) as exc:
        raise UnsafeUrlError("invalid hostname") from exc
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError as exc:
        raise UnsafeUrlError(f"DNS resolve failed: {exc.__class__.__name__}") from exc
    addresses: list[str] = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            raise UnsafeUrlError(f"unparseable address: {addr!r}") from None
        # is_global 不含多播/保留段的判定（224.0.0.1 等 is_global=True），显式拒绝
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise UnsafeUrlError(f"non-public address: {ip}")
        addresses.append(str(ip))
    if not addresses:
        raise UnsafeUrlError("DNS returned no addresses")
    return host, list(dict.fromkeys(addresses))


async def assert_public_http_url(url: str) -> str:
    """校验 http(s) URL 的主机解析到公网地址；不合规抛 UnsafeUrlError。"""
    await _validated_target(url)
    return url


_pinned_dial_ctx: ContextVar[dict[str, str] | None] = ContextVar(
    "ark_pinned_dial_ctx", default=None
)


class _PinnedDialBackend:
    """网络后端包装：httpcore 传入的 host 是原始 Origin 域名，本层把
    实际拨号目标替换为该 host 预检批准的 IP。

    连接池与 TLS 身份保持原域名——SNI/证书校验、连接复用都按原 Origin
    隔离：两个不同域名即使解析到同一 IP:port，也不会共享 TLS 连接。
    并发任务各自通过 ContextVar 携带 pin 映射，避免双栈同域名并发下载时
    互相覆盖拨号 IP；实例级字典仅保留供单测直接调用 pin/unpin 兼容。
    strict=True 时未 pin 的 host 直接拒绝；兼容模式可原样交给内层后端。
    """

    def __init__(self, inner, *, strict: bool = False) -> None:
        self._inner = inner
        self._strict = strict
        self._pinned: dict[str, str] = {}
        self._pin_refs: dict[str, int] = {}

    def pin(self, host: str, ip: str) -> None:
        current_ctx = _pinned_dial_ctx.get()
        updated = dict(current_ctx) if current_ctx else {}
        updated[host] = ip
        _pinned_dial_ctx.set(updated)
        self._pinned[host] = ip
        self._pin_refs[host] = self._pin_refs.get(host, 0) + 1

    def unpin(self, host: str) -> None:
        current_ctx = _pinned_dial_ctx.get()
        if current_ctx and host in current_ctx:
            updated = dict(current_ctx)
            updated.pop(host, None)
            _pinned_dial_ctx.set(updated or None)
        remaining = self._pin_refs.get(host, 0) - 1
        if remaining > 0:
            self._pin_refs[host] = remaining
        else:
            self._pin_refs.pop(host, None)
            self._pinned.pop(host, None)

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        current_ctx = _pinned_dial_ctx.get()
        target = (current_ctx or {}).get(host)
        if target is None:
            target = self._pinned.get(host)
        if target is None and self._strict:
            raise OSError("destination has no validated address")
        dial = target if target is not None else host
        return await self._inner.connect_tcp(
            dial,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """受限下载专用直连传输：仅替换拨号目标，其余与默认传输一致。

    httpx 没有公开的 network_backend 注入点——这里在构造后接管
    `self._pool._network_backend`（httpcore 公开参数，经 httpx 私有属性
    挂入）。能力探测失败（httpx 升级改变内部结构）时 `_seam_ok=False`，
    受限下载显式拒绝而不是退回未绑定直连。
    """

    def __init__(self) -> None:
        super().__init__()
        pool_backend = getattr(self._pool, "_network_backend", None)
        self._seam_ok = pool_backend is not None
        self.pinned_backend = _PinnedDialBackend(pool_backend, strict=True)
        if self._seam_ok:
            self._pool._network_backend = self.pinned_backend


_pinned_transport: _PinnedTransport | None = None
_pinned_client: httpx.AsyncClient | None = None


def _get_pinned_transport() -> _PinnedTransport | None:
    """绑定传输单例；能力不可用返回 None。"""
    global _pinned_transport
    if _pinned_transport is None:
        _pinned_transport = _PinnedTransport()
    return _pinned_transport if _pinned_transport._seam_ok else None


def _get_pinned_client() -> httpx.AsyncClient | None:
    """绑定 client 单例（显式 transport：无任何代理挂载，恒直连）。"""
    global _pinned_client
    if _pinned_client is None or _pinned_client.is_closed:
        transport = _get_pinned_transport()
        if transport is None:
            return None
        _pinned_client = httpx.AsyncClient(transport=transport)
    return _pinned_client


async def safe_download_bytes(
    url: str,
    *,
    max_bytes: int = SAFE_DOWNLOAD_MAX_BYTES,
    timeout: float = SAFE_DOWNLOAD_TIMEOUT,
    max_redirects: int = SAFE_DOWNLOAD_MAX_REDIRECTS,
) -> bytes | None:
    """用户可控 URL 的受限下载（OCR 截图等）。

    与 download_bytes 的差异（SSRF / 资源耗尽边界）：
    - 仅 http(s)，且主机必须解析为公网地址（_validated_target）
    - 不自动跟随重定向：逐跳重新校验目标，最多 max_redirects 跳
    - 逐目标路由判定（_route_decision，与共享 client 挂载一致）：
      实际直连 → 经 _PinnedTransport 发送，拨号绑定到已批准 IP，
      连接池/TLS 身份保持原域名（关闭 DNS 重绑定窗口，不同域名不共享
      TLS 连接）；实际代理 → 原 URL 经共享 client 透传，目标策略由
      可信代理/下载出口的 DNS 与 ACL 保证；策略未知或绑定能力不可用
      → 拒绝下载，不假称安全
    - 流式读取并强制 max_bytes 上限（超出立即中止，不整读进内存）

    校验不通过/网络失败/超限：打 warning 返回 None（不抛，便于多图循环降级）。
    """
    client = await get_client()  # 同时确保路由快照已建立
    deadline = time.monotonic() + timeout
    current = url
    for _hop in range(max_redirects + 1):
        if deadline - time.monotonic() <= 0:
            logger.warning("[ArknightsUID·http] 安全下载总耗时超时")
            return None
        try:
            _host, addresses = await _validated_target(current)
        except UnsafeUrlError as exc:
            # URL 的路径、查询串和异常文本都可能含用户导出凭据。
            logger.warning(f"[ArknightsUID·http] 拒绝不安全下载地址: {exc.__class__.__name__}")
            return None
        try:
            parsed_url = httpx.URL(current)
            route = _route_decision(parsed_url)
            # httpcore 拨号时使用 IDNA 编码后的 host；必须以同一表示 pin。
            host = parsed_url.raw_host.decode("ascii")
        except (ValueError, httpx.InvalidURL) as exc:
            # 畸形 punycode 域名等：URL 解析/IDNA 解码在 try 外裸抛会打穿
            # "校验失败返回 None 不抛"契约（IDNAError 是 ValueError 子类）
            logger.warning(f"[ArknightsUID·http] 拒绝无法解析的下载地址: {exc.__class__.__name__}")
            return None
        transport = None
        if route == "direct":
            pinned_client = _get_pinned_client()
            transport = _get_pinned_transport()
            if pinned_client is None or transport is None:
                logger.error(
                    "[ArknightsUID·http] 直连 IP 绑定能力不可用"
                    "（httpx 传输层结构变化），拒绝用户可控 URL 下载"
                )
                return None
            request_client = pinned_client
            # 优先 IPv4；仅使用本轮全部通过公网校验的 IP，不再重新解析。
            targets: list[str | None] = sorted(
                dict.fromkeys(addresses), key=lambda ip: ipaddress.ip_address(ip).version
            )
        elif route == "proxy":
            request_client = client  # 原 URL 透传，代理侧负责目标策略
            targets = [None]
        else:
            logger.error("[ArknightsUID·http] 无法确定下载地址的实际路由策略，已拒绝")
            return None
        redirected = False
        for target in targets:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            backend = transport.pinned_backend if transport is not None else None
            if backend is not None and target is not None:
                backend.pin(host, target)
            log_token = _private_download.set(True)
            try:
                async with asyncio.timeout(remaining):
                    async with request_client.stream(
                        "GET", current, timeout=remaining, follow_redirects=False
                    ) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            location = resp.headers.get("location", "")
                            if not location:
                                logger.warning("[ArknightsUID·http] 重定向缺 Location")
                                return None
                            current = str(httpx.URL(current).join(location))
                            redirected = True
                            break
                        resp.raise_for_status()
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes(64 * 1024):
                            buf.extend(chunk)
                            if len(buf) > max_bytes:
                                logger.warning(
                                    f"[ArknightsUID·http] 响应超过 {max_bytes} 字节上限"
                                )
                                return None
                        return bytes(buf)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # 只在连接阶段失败时换下一个地址；响应错误不跨地址重试。
                continue
            except Exception as exc:
                logger.warning(f"[ArknightsUID·http] 安全下载失败: {exc.__class__.__name__}")
                return None
            finally:
                _private_download.reset(log_token)
                if backend is not None:
                    unpin = getattr(backend, "unpin", None)
                    if unpin is not None:
                        unpin(host)
        if redirected:
            continue
        logger.warning("[ArknightsUID·http] 安全下载连接失败或超时")
        return None
    logger.warning(f"[ArknightsUID·http] 重定向超过 {max_redirects} 跳")
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
