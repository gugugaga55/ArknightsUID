"""Skland 明日方舟接口客户端。

P0 升级要点：
1. UA / vName / RespCode 等常量统一来自 `utils/api/request_util`，
   避免与 smsdk / 后续 OAuth 链路常量重复。
2. dId 不再使用空字符串，改由 `smsdk.get_device_id()` 真正生成，
   提升登录态与签名链路的风控健壮性。
3. `ARKNIGHTS_GAME_ID` 重命名为 `ARKNIGHTS_CHANNEL_OFFICIAL`，并加注释说明
   它实际是 channelMasterId（官服渠道 ID），不是"游戏 ID"——这个命名误导
   在 EndUID 注释"为啥到这就是1了"里有同样的困惑。

token 管理（v3，对齐 EndUID）：
- 本插件常与 EndUID 共存且共用同一 cred；Skland 的 refresh 会吊销旧
  token，EndUID 每 3 分钟主动刷新。沿用旧的 25 分钟缓存会让本插件
  长期持有已被吊销的 token：首请求必 220，重试刷新又反杀 EndUID
  刚拿到的新 token，两边互相打死。
- 因此对齐 EndUID（requests.py:refresh_token）的策略：token 新鲜度
  窗口 180 秒，请求前不新鲜就刷新并回写 ArkUser（token + 时间戳）；
  游戏请求返回 220/10002 时仍强制刷新重试一次（EndUID 没有的兜底）；
  refresh 被拒（10001/220/10002）说明 cred 本身失效，标记账号无效。
- 同一 cred 的 refresh 持进程锁（推送任务与用户命令并发时只刷一次）
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from gsuid_core.logger import logger

from ..api.request_util import (
    ANDROID_USER_AGENT,
    ARK_SIGN_VNAME,
    IOS_USER_AGENT,
    SIGN_VNAME,
    SKLAND_APP_PLATFORM,
    SKLAND_APP_USER_AGENT,
    SKLAND_APP_VNAME,
    RespCode,
    get_device_id,
    get_skland_app_headers,
)
from ..http import describe_error, get_client

if TYPE_CHECKING:
    from ..enduid.adapter import ArkCredential


# === Skland 明日方舟接口 URL ===
MAIN_URL = "https://zonai.skland.com/"
API_VERSION = "v1"

REFRESH_TOKEN_URL = f"{MAIN_URL}api/{API_VERSION}/auth/refresh"
GAME_PLAYER_INFO_URL = f"{MAIN_URL}api/{API_VERSION}/game/player/info"
GAME_ATTENDANCE_URL = f"{MAIN_URL}api/{API_VERSION}/game/attendance"

# 补全接口：user/binding/cultivate 系列均为 GET，套 _signed_request。
USER_INFO_URL = f"{MAIN_URL}api/{API_VERSION}/user"
GAME_BINDING_URL = f"{MAIN_URL}api/{API_VERSION}/game/player/binding"
# attendance 状态查询复用签到端点（GET 变体，与 POST ark_attendance 同 URL 不同方法）。
CULTIVATE_CHARACTER_URL = f"{MAIN_URL}api/{API_VERSION}/game/cultivate/character"
CULTIVATE_INFO_URL = f"{MAIN_URL}api/{API_VERSION}/game/cultivate/info"
CULTIVATE_PLAYER_URL = f"{MAIN_URL}api/{API_VERSION}/game/cultivate/player"

# 明日方舟在 Skland 的"游戏 ID"实际是 channelMasterId（官服渠道 ID）。
# 这个值跟 EndUID 的 ENDFIELD_GAME_ID=3 并存于同一套森空岛账号体系。
# 命名沿用"channel official"避免后续开发者误以为是游戏产品 ID。
ARKNIGHTS_CHANNEL_OFFICIAL = 1

# 兼容旧引用（暂时保留，后续逐步迁移到新名称）
ARKNIGHTS_GAME_ID = ARKNIGHTS_CHANNEL_OFFICIAL

# 拿到这些 code 表示 token 失效，需要 refresh 后重试一次
TOKEN_INVALID_CODES = {int(RespCode.TOKEN_INVALID), int(RespCode.LOGIN_EXPIRED)}

# refresh 接口返回这些 code 表示 cred 本身失效（对齐 EndUID 的判定），
# 不再重试，调用方应把账号标记为无效
CRED_INVALID_REFRESH_CODES = {
    int(RespCode.CRED_INVALID),
    int(RespCode.TOKEN_INVALID),
    int(RespCode.LOGIN_EXPIRED),
}


class CredentialInvalidError(Exception):
    """凭证失效（refresh 被 10001/220/10002 拒绝）。调用方应标记账号无效。"""


# === token 管理：进程内缓存 + per-cred refresh 锁 ===
# 新鲜度窗口 180 秒，对齐 EndUID（token 实际寿命更长，但共用 cred 的
# 任何一方 refresh 都会吊销旧 token，窗口必须压到 EndUID 同量级）
_TOKEN_CACHE_TTL = 180
# refresh 网络/未知错误重试（对齐 EndUID：最多 3 次，间隔 1 秒）
_REFRESH_MAX_ATTEMPTS = 3
_REFRESH_RETRY_DELAY = 1.0
_token_cache: dict[str, tuple[str, float]] = {}
_refresh_locks: dict[str, asyncio.Lock] = {}
_UNSAFE_QUERY_RE = re.compile(r"[^0-9A-Za-z_.\-]")


def _safe_error(exc: Exception, *secrets: str) -> str:
    """异常描述（类名 + message）并对已知凭据脱敏。

    旧实现只打类名，线上排障无法区分超时/SSL/连接错误；直接打 message
    又可能把异常文本里携带的 cred/token 泄漏进日志（签名请求的合成异常
    含完整 headers）。折中：保留 message，把当前请求的敏感值替换为 ***。
    """
    text = describe_error(exc)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


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
    # 编码会破坏签名一致性。调用方应仅传入无特殊字符的参数（如 uid/gameId 数字）；
    # 出现其它字符说明调用方破坏了这一契约，打 warning 暴露问题（不阻断）。
    if not params:
        return ""
    pairs: list[str] = []
    for key, value in sorted(params.items()):
        pair = f"{key}={value}"
        # '=' 是键值分隔符不属于参数内容，需对 key/value 分别校验；
        # 对拼接串整体匹配会把每个请求都误报（'gameId=1' 事件）
        if _UNSAFE_QUERY_RE.search(str(key)) or _UNSAFE_QUERY_RE.search(
            str(value)
        ):
            logger.warning(
                "[ArknightsUID] Skland 签名参数含特殊字符，签名可能失效: "
                f"{pair!r}"
            )
        pairs.append(pair)
    return "&".join(pairs)


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
    extra_headers: dict[str, str] | None = None,
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
    if extra_headers:
        headers.update(extra_headers)
    return headers


async def refresh_token(cred: str) -> str:
    """刷新 Skland token（纯网络操作，无缓存）。

    对齐 EndUID：网络/未知错误最多重试 3 次；返回 10001/220/10002 说明
    cred 本身已失效，不重试、抛 CredentialInvalidError（调用方应标记账号
    无效）。其余失败重试耗尽后返回空字符串。
    """
    if not cred:
        return ""
    headers = {
        "cred": cred,
        "User-Agent": IOS_USER_AGENT,
        "Content-Type": "application/json",
    }
    client = await get_client()
    for attempt in range(1, _REFRESH_MAX_ATTEMPTS + 1):
        is_last = attempt == _REFRESH_MAX_ATTEMPTS
        try:
            response = await client.get(
                REFRESH_TOKEN_URL, headers=headers, timeout=20.0
            )
        except Exception as exc:
            if not is_last:
                logger.debug(
                    "[ArknightsUID] Skland token refresh 网络异常，重试 "
                    f"{attempt}/{_REFRESH_MAX_ATTEMPTS - 1}: "
                    f"{_safe_error(exc, cred)}"
                )
                await asyncio.sleep(_REFRESH_RETRY_DELAY)
                continue
            logger.warning(
                "[ArknightsUID] Skland token refresh request failed: "
                f"{_safe_error(exc, cred)}"
            )
            return ""

        if response.status_code != 200:
            if not is_last:
                await asyncio.sleep(_REFRESH_RETRY_DELAY)
                continue
            logger.warning(
                "[ArknightsUID] Skland token refresh failed: "
                f"HTTP {response.status_code}"
            )
            return ""

        try:
            data = response.json()
        except Exception as exc:
            # 解析类异常的 message 无法穷举脱敏（可能携带任意响应片段）：
            # warning 只打类名，脱敏详情放 debug
            if not is_last:
                await asyncio.sleep(_REFRESH_RETRY_DELAY)
                continue
            logger.warning(
                "[ArknightsUID] Skland token refresh non-json response: "
                f"HTTP {response.status_code} ({exc.__class__.__name__})"
            )
            logger.debug(
                "[ArknightsUID] Skland token refresh non-json detail: "
                f"{_safe_error(exc, cred)}"
            )
            return ""

        code = data.get("code")
        if code == int(RespCode.OK):
            token = (data.get("data") or {}).get("token")
            if token:
                return str(token)
            # code=0 但缺 token：当未知错误继续重试
        elif code in CRED_INVALID_REFRESH_CODES:
            logger.warning(
                "[ArknightsUID] Skland token refresh 被拒（cred 失效 "
                f"code={code}）"
            )
            raise CredentialInvalidError(f"cred 失效 code={code}")

        if not is_last:
            await asyncio.sleep(_REFRESH_RETRY_DELAY)
            continue
        logger.warning(
            "[ArknightsUID] Skland token refresh returned non-ok code: "
            f"{data.get('code')}"
        )
        return ""
    return ""


async def _persist_token(cred: str, token: str) -> None:
    """refresh 产物回写 ArkUser（token + last_cred_request_time）；失败仅 debug。"""
    try:
        from ..database.models import ArkUser

        await ArkUser.update_token_by_cred(cred, token, cred_time=int(time.time()))
    except Exception as exc:
        logger.debug(
            f"[ArknightsUID] token 回写失败（不影响本次请求）: {describe_error(exc)}"
        )


async def _mark_invalid(cred: str) -> None:
    """cred 被 refresh 拒绝 → 标记 ArkUser 行 cookie_status=无效。

    EndUID 回退账号没有本地行，无行可标时静默（EndUID 侧自会标记）；
    后续命令走 adapter.usable 判定，提示重新登录而不是反复撞 refresh。
    """
    try:
        from ..database.models import ArkUser

        await ArkUser.mark_invalid_by_cred(cred)
    except Exception as exc:
        logger.debug(
            f"[ArknightsUID] 标记凭证无效失败（不影响本次请求）: {describe_error(exc)}"
        )


async def _token_for_cred(cred: str, *, force: bool = False, stale: str = "") -> str:
    """取该 cred 的新鲜 token：窗口内缓存命中直接返回，否则持锁 refresh 一次。

    对齐 EndUID refresh_token 的新鲜度语义（180 秒窗口）；refresh 被拒时
    CredentialInvalidError 原样上抛，由调用方标记账号无效。
    并发调用（推送任务 + 用户命令同时踩到 token 过期）只会产生一次网络
    refresh（per-cred 锁 + 双检）；成功后写进程缓存并回写数据库。
    force=True 表示缓存里的 token 刚被服务端判失效（220），跳过缓存强制刷新；
    stale 传入刚失败的那个 token——持锁后若缓存已被并发协程换成别的新 token，
    直接复用，不再重复 refresh（避免旧 token 被第二次 refresh 吊销）。
    """
    now = time.time()
    if not force:
        cached = _token_cache.get(cred)
        if cached and now - cached[1] < _TOKEN_CACHE_TTL:
            return cached[0]

    lock = _refresh_locks.setdefault(cred, asyncio.Lock())
    async with lock:
        cached = _token_cache.get(cred)
        if cached and time.time() - cached[1] < _TOKEN_CACHE_TTL:
            if not force or cached[0] != stale:
                return cached[0]
        token = await refresh_token(cred)
        if token:
            _token_cache[cred] = (token, time.time())
            await _persist_token(cred, token)
            _prune_token_state(keep=cred)
        return token


def _prune_token_state(*, keep: str = "") -> None:
    """清理过期 token 缓存与不再需要的 refresh 锁（防无界增长）。

    删除登录后 cred 对应条目不会立刻消失，这里在 refresh 成功的时机
    顺带清扫：过期的缓存条目直接删；空闲（未加锁）且 cred 已无缓存
    的锁也一并回收。持有中的锁绝不删除（保证互斥语义）。
    """
    now = time.time()
    for key, (_token, ts) in list(_token_cache.items()):
        if key != keep and now - ts >= _TOKEN_CACHE_TTL:
            _token_cache.pop(key, None)
    for key, lock in list(_refresh_locks.items()):
        if key != keep and key not in _token_cache and not lock.locked():
            _refresh_locks.pop(key, None)


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
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """签名请求主流程。

    token 获取对齐 EndUID：只信 180 秒窗口内的缓存，窗口外一律 refresh；
    不再直接使用调用方快照里的 credential.token——与 EndUID 共用 cred 时，
    那个 token 大概率已被 EndUID 的 refresh 吊销，用它首发必 220。
    游戏请求返回 220/10002 仍强制 refresh + 重试一次（EndUID 没有的兜底）。
    refresh 被拒（cred 失效）→ 标记账号无效并中止本次请求。

    Args:
        use_device_id: True 时为本次请求生成真实 dId（高敏感接口建议开启）。
                      False 时 dId 留空（普通查询足够，且省一次加密开销）。
    """
    try:
        token = await _token_for_cred(credential.cred)
    except CredentialInvalidError:
        logger.warning("[ArknightsUID] Skland 凭证失效，请求中止")
        await _mark_invalid(credential.cred)
        return None
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
            extra_headers=extra_headers,
        )
        request_url = url
        if method == "GET" and query:
            request_url = f"{url}?{query}"
        client = await get_client()
        try:
            if method == "GET":
                response = await client.get(
                    request_url, headers=headers, timeout=25.0
                )
            else:
                response = await client.post(
                    request_url,
                    headers=headers,
                    content=body_string if body is not None else None,
                    timeout=25.0,
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
                f"{_safe_error(exc, credential.cred, request_token)}"
            )
            return None

    result = await do_request(token)
    if isinstance(result, dict) and result.get("code") in TOKEN_INVALID_CODES:
        try:
            fresh_token = await _token_for_cred(
                credential.cred, force=True, stale=token
            )
        except CredentialInvalidError:
            logger.warning("[ArknightsUID] Skland 凭证失效，请求中止")
            await _mark_invalid(credential.cred)
            return None
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


async def get_user_info(credential: "ArkCredential") -> dict[str, Any] | None:
    """获取森空岛用户基础信息（cred 作用域，不带角色头）。

    签名组合照搬 EndUID get_user_info：platform=SKLAND_APP_PLATFORM(1) /
    vName=SKLAND_APP_VNAME / SKLAND_APP_USER_AGENT + get_skland_app_headers
    风控头（language/os/nId/vCode/channel/manufacturer）+ use_device_id。
    """
    return await _signed_request(
        credential,
        url=USER_INFO_URL,
        method="GET",
        use_device_id=True,
        platform=SKLAND_APP_PLATFORM,
        vname=SKLAND_APP_VNAME,
        user_agent=SKLAND_APP_USER_AGENT,
        extra_headers=get_skland_app_headers(SKLAND_APP_USER_AGENT),
    )


async def get_binding(credential: "ArkCredential") -> dict[str, Any] | None:
    """获取玩家绑定列表（cred 作用域，不带角色头）。

    签名组合与 EndUID get_binding 一致：默认 platform=3 / SIGN_VNAME /
    Android UA + use_device_id=True（带 dId 设备指纹）。
    """
    return await _signed_request(
        credential,
        url=GAME_BINDING_URL,
        method="GET",
        use_device_id=True,
    )


async def ark_attendance_query(credential: "ArkCredential") -> dict[str, Any] | None:
    """查询明日方舟签到状态（GET 变体，与 POST ark_attendance 同端点不同方法）。

    签名参数与 POST ark_attendance 保持一致（platform=1 / vName=ARK_SIGN_VNAME
    / iOS UA），params 带 uid+gameId（POST body 同款）。
    实测 code=0（2026-08-14 真实凭据 联调）。
    """
    return await _signed_request(
        credential,
        url=GAME_ATTENDANCE_URL,
        method="GET",
        uid=credential.uid,
        game_id=ARKNIGHTS_CHANNEL_OFFICIAL,
        params={"uid": credential.uid, "gameId": ARKNIGHTS_CHANNEL_OFFICIAL},
        platform=1,
        vname=ARK_SIGN_VNAME,
        user_agent=IOS_USER_AGENT,
        accept_encoding="gzip",
    )


async def get_cultivate_character(
    credential: "ArkCredential", character_id: str
) -> dict[str, Any] | None:
    """查询单干员养成材料（按 characterId，绑定角色）。"""
    return await _signed_request(
        credential,
        url=CULTIVATE_CHARACTER_URL,
        method="GET",
        uid=credential.uid,
        game_id=ARKNIGHTS_CHANNEL_OFFICIAL,
        params={"characterId": character_id},
    )


async def get_cultivate_info(credential: "ArkCredential") -> dict[str, Any] | None:
    """查询干员/材料通用信息（cred 作用域，接近静态）。

    签名同 get_binding/get_player_info（zonai.skland.com GET 同源默认签名）。
    实测 code=0（2026-08-14 真实凭据 联调；EndUID 无此接口，本插件首发验证）。
    """
    return await _signed_request(
        credential,
        url=CULTIVATE_INFO_URL,
        method="GET",
    )


async def get_cultivate_player(credential: "ArkCredential") -> dict[str, Any] | None:
    """查询玩家养成进度（按 uid，绑定角色）。"""
    return await _signed_request(
        credential,
        url=CULTIVATE_PLAYER_URL,
        method="GET",
        uid=credential.uid,
        game_id=ARKNIGHTS_CHANNEL_OFFICIAL,
        params={"uid": credential.uid},
    )

