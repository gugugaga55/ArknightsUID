"""明日方舟手机号网页登录（移植自 EndUID end_bind/web_login.py）。

流程：用户发 ark网页登录 → bot 发一条 3 分钟有效的一次性链接
（{ArkLoginUrl}/ark/i/{auth}）→ 用户浏览器打开页面，填鹰角账号手机号，
服务端代调鹰角 send_phone_code 发短信 → 用户回填验证码 →
token_by_phone_code 换 token → 走与扫码相同的 OAuth 链换 cred →
persist_ark_login 落库。手机号与验证码只进网页表单，不经过聊天记录。

与 EndUID 的差异（对齐本插件口径）：
- 会话持久化到 SQLite，固定 3 分钟有效；进程间原子认领请求，重启可恢复；
- 已进入账号保存的中断会话不自动重放，请求者需重新发起登录；
- 群聊使用与扫码共用 AllowGroupScanLogin 开关（登录链接与二维码同属
  "谁完成登录就把谁的账号绑到发起人名下"的免密向量，默认私聊）；
- 登录成功回执由网页路由经 gss 主动推送（不依赖命令侧轮询协程存活），
  命令侧轮询只负责把失败原因转达回聊天。
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.web_app import app

from ..utils.config import ArkConfig
from ..utils.crypto import EncryptionUnavailableError
from ..utils.path import MAIN_PATH
from ..utils.skland.web_sessions import (
    EXPIRED,
    LOGIN_MAX_ATTEMPTS as LOGIN_MAX_ATTEMPTS,
    SEND_COOLDOWN as SEND_COOLDOWN,
    SEND_MAX as SEND_MAX,
    SESSION_MAX as SESSION_MAX,
    SESSION_TTL,
    SessionClaim,
    SessionRejected,
    SessionUnavailable,
    WebSessionStore,
)
from ..utils.render import ark_templates
from ..utils.skland import auth as ark_auth
from ..utils.skland import store as ark_store
from ._format import format_bind_success

LOGIN_FLOW = "ark_phone"

# 命令侧仅轮询回执；所有状态/次数/租约由 SQLite 模块原子处理。
POLL_INTERVAL = 1.0
POLL_DEADLINE = SESSION_TTL
store = WebSessionStore(MAIN_PATH / "web_login_sessions.sqlite3")


def _session_failure(exc: Exception) -> dict[str, Any]:
    logger.warning(f"[ArknightsUID·网页登录] 会话操作失败: {exc.__class__.__name__}")
    return {"success": False, "msg": "登录会话暂不可用，请稍后重新发起登录。"}


async def _heartbeat(backend: WebSessionStore, claim: SessionClaim) -> None:
    try:
        while True:
            await asyncio.sleep(max(0.01, backend.lease_seconds / 3))
            if not await asyncio.to_thread(backend.renew, claim):
                return
    except (SessionUnavailable, EncryptionUnavailableError) as exc:
        _session_failure(exc)


@asynccontextmanager
async def _claim_session(
    auth: str, operation: str
) -> AsyncIterator[tuple[WebSessionStore, SessionClaim]]:
    backend = store
    claim = await asyncio.to_thread(backend.claim, auth, operation)
    heartbeat = asyncio.create_task(_heartbeat(backend, claim))
    try:
        yield backend, claim
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        # done/failed 已清除 claim；只有未完成步骤需要释放。finalizing 只会
        # 转为 failed，绝不允许同一链接自动重复进行可能已经提交的账号写入。
        try:
            await asyncio.shield(asyncio.to_thread(backend.release, claim))
        except (SessionUnavailable, EncryptionUnavailableError) as exc:
            _session_failure(exc)


def get_token() -> str:
    """一次性登录令牌：128 bit 随机，URL-safe。"""
    return secrets.token_urlsafe(16)


def _configured_login_url() -> str:
    """读 ArkLoginUrl；未配置返回空串（命令侧据此回退扫码登录）。"""
    url = str(ArkConfig.get_config("ArkLoginUrl").data or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url.rstrip("/")


async def _send_text(bot: Bot, ev: Event, msg: str) -> None:
    await bot.send((" " if ev.group_id else "") + msg, at_sender=bool(ev.group_id))


_GROUP_LINK_NOTICE = (
    "⚠️ 链接在群内公开：3 分钟内任何人用此链接登录，都会把 TA 的账号"
    "绑定到你名下。确认周围是熟人再点。"
)


async def web_login_entry(bot: Bot, ev: Event) -> None:
    """ark登录 命令入口：发链接 + 轮询会话终态（失败转达回聊天）。

    ArkLoginUrl 未配置时回退扫码登录（EndUID end登录 同款行为）——
    回退放在群聊开关判定之前：群内未配置时由扫码流程给出自己的开关
    提示，而不是一条"网页链接"文案。
    """
    base_url = _configured_login_url()
    if not base_url:
        from . import scan_login

        return await scan_login(bot, ev)

    if ev.group_id and not bool(ArkConfig.get_config("AllowGroupScanLogin").data):
        return await _send_text(
            bot,
            ev,
            "网页登录链接默认仅私聊发放（链接等于一次免密登录入口）。\n"
            "管理员可在插件配置开启「允许群聊扫码登录」（AllowGroupScanLogin）"
            "后群内使用，或留空 ArkLoginUrl 回退为扫码登录。",
        )

    token = get_token()
    try:
        await asyncio.to_thread(
            store.create,
            token,
            {
                "flow": LOGIN_FLOW,
                "user_id": ev.user_id,
                "bot_id": ev.bot_id,
                "bot_self_id": getattr(ev, "bot_self_id", "") or "",
                "group_id": ev.group_id,
            },
        )
    except (SessionUnavailable, EncryptionUnavailableError) as exc:
        return await _send_text(bot, ev, _session_failure(exc)["msg"])

    notice = f"{_GROUP_LINK_NOTICE}\n" if ev.group_id else ""
    await _send_text(
        bot,
        ev,
        f"{notice}「明日方舟」手机号网页登录\n"
        f"您的id为【{ev.user_id}】\n"
        f"{base_url}/ark/i/{token}\n"
        "3 分钟内有效，请勿使用他人链接！",
    )
    await _await_completion(bot, ev, token)


async def _await_completion(bot: Bot, ev: Event, token: str) -> None:
    """轮询会话终态：失败把原因发回聊天；成功静默（网页侧已主动推送）。

    会话被新登录顶掉（get 为 None 且无终态）时静默退出。
    """
    deadline = asyncio.get_running_loop().time() + POLL_DEADLINE
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            state = await asyncio.to_thread(store.get, token)
        except (SessionUnavailable, EncryptionUnavailableError) as exc:
            return await _send_text(bot, ev, _session_failure(exc)["msg"])
        if state is None or state.get("flow") != LOGIN_FLOW:
            return
        phase = state.get("phase")
        if phase == "done":
            await asyncio.to_thread(store.delete, token)
            return
        if phase == "failed":
            await asyncio.to_thread(store.delete, token)
            return await _send_text(
                bot, ev, f"网页登录失败：{state.get('error_msg') or '未知原因'}"
            )


async def _push_text(state: dict, text: str) -> None:
    """经 gss 把网页登录结果主动推回发起会话（群/私聊）。

    网页请求上下文没有 bot/ev，登录完成的回执只能走这条链路；
    失败只打异常类名（message 可能带会话信息）。
    """
    from gsuid_core.gss import gss

    user_id = state.get("user_id")
    bot_id = state.get("bot_id")
    group_id = state.get("group_id")
    bot_self_id = state.get("bot_self_id") or ""
    for active_id in list(gss.active_bot):
        try:
            if group_id:
                await gss.active_bot[active_id].target_send(
                    text, "group", group_id, bot_id, bot_self_id, ""
                )
            else:
                await gss.active_bot[active_id].target_send(
                    text, "direct", user_id, bot_id, bot_self_id, ""
                )
            return
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·网页登录] 结果推送失败: {exc.__class__.__name__}"
            )


async def _notify_success(state: dict, roles: list[dict]) -> None:
    await _push_text(state, format_bind_success(roles))


class SendCodeRequest(BaseModel):
    auth: str
    phone: str


class LoginRequest(BaseModel):
    auth: str
    phone: str
    code: str


async def _live_session(auth: str) -> dict | None:
    """只向浏览器展示仍可提交的会话；原子认领检查由后端再次执行。"""
    state = await asyncio.to_thread(store.get, auth)
    if (
        isinstance(state, dict)
        and state.get("flow") == LOGIN_FLOW
        and state.get("phase") == "init"
    ):
        return state
    return None


def _render_page(
    auth: str = "", user_id: str = "", expired: bool = False
) -> HTMLResponse:
    template = ark_templates.get_template("ark_web_login.html")
    return HTMLResponse(template.render(auth=auth, user_id=user_id, expired=expired))


@app.get("/ark/i/{auth}")
async def ark_login_index(auth: str):
    try:
        state = await _live_session(auth)
    except (SessionUnavailable, EncryptionUnavailableError) as exc:
        _session_failure(exc)
        state = None
    if state is None:
        return _render_page(expired=True)
    return _render_page(auth=auth, user_id=str(state.get("user_id", "")))


@app.post("/ark/sendCode")
async def ark_login_send_code(data: SendCodeRequest):
    if not ark_auth.is_valid_phone(data.phone):
        return {"success": False, "msg": "手机号格式错误"}
    try:
        async with _claim_session(data.auth, "sending"):
            # 在请求之前预留次数和冷却；超时也可能已经发送，不退回配额。
            ok, msg = await ark_auth.send_phone_code(data.phone)
            return (
                {"success": True}
                if ok
                else {"success": False, "msg": msg or "验证码发送失败"}
            )
    except SessionRejected as exc:
        return {"success": False, "msg": str(exc)}
    except (SessionUnavailable, EncryptionUnavailableError) as exc:
        return _session_failure(exc)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·网页登录] 验证码请求异常: {exc.__class__.__name__}"
        )
        return {"success": False, "msg": "验证码发送失败，请稍后重试。"}


async def _fail(
    backend: WebSessionStore, claim: SessionClaim, err: str
) -> dict[str, Any]:
    await asyncio.to_thread(backend.complete, claim, error=err)
    return {"success": False, "msg": err}


@app.post("/ark/login")
async def ark_login_submit(data: LoginRequest):
    if not ark_auth.is_valid_phone(data.phone) or not data.code.strip().isdigit():
        return {"success": False, "msg": "手机号或验证码格式错误"}
    try:
        async with _claim_session(data.auth, "checking") as (backend, claim):
            token, token_msg = await ark_auth.get_token_by_phone_code(
                data.phone, data.code.strip()
            )
            if not token:
                # 错码释放认领回 init，但次数保留，且不延长链接有效期。
                return {"success": False, "msg": token_msg or "验证码错误或已过期"}
            cred_info, err = await ark_auth.get_cred_by_token(token)
            if err == ark_auth.CRED_ERR_UNAVAILABLE:
                return await _fail(
                    backend,
                    claim,
                    "当前服务暂时无法完成网页登录，请改用 ark扫码登录 或私聊 ark绑定 <cred>。",
                )
            if not cred_info or not cred_info.get("cred"):
                return await _fail(backend, claim, "获取 cred 失败，请重试")
            # 新链接顶替、TTL 到期或租约被另一进程接管后，旧请求不能开始保存。
            if not await asyncio.to_thread(backend.begin_finalization, claim):
                return {"success": False, "msg": EXPIRED}
            state = claim.state
            roles, err = await ark_store.persist_ark_login(
                state.get("user_id"),
                state.get("bot_id"),
                state.get("group_id"),
                cred_info["cred"],
                hg_token=token,
                skland_user_id=cred_info.get("skland_user_id", ""),
            )
            if err:
                return await _fail(backend, claim, err)
            if not await asyncio.to_thread(backend.complete, claim):
                return {
                    "success": False,
                    "msg": "登录会话已结束，账号保存可能已完成，请先查看绑定；需要时重新发起登录。",
                }
            asyncio.create_task(_notify_success(state, roles or []))
            return {"success": True}
    except SessionRejected as exc:
        return {"success": False, "msg": str(exc)}
    except (SessionUnavailable, EncryptionUnavailableError) as exc:
        return _session_failure(exc)
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·网页登录] 登录请求异常: {exc.__class__.__name__}"
        )
        return {"success": False, "msg": "登录处理已中断，请重新发起登录。"}
