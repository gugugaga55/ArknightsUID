"""明日方舟手机号网页登录（移植自 EndUID end_bind/web_login.py）。

流程：用户发 ark网页登录 → bot 发一条 3 分钟有效的一次性链接
（{ArkLoginUrl}/ark/i/{auth}）→ 用户浏览器打开页面，填鹰角账号手机号，
服务端代调鹰角 send_phone_code 发短信 → 用户回填验证码 →
token_by_phone_code 换 token → 走与扫码相同的 OAuth 链换 cred →
persist_ark_login 落库。手机号与验证码只进网页表单，不经过聊天记录。

与 EndUID 的差异（对齐本插件口径）：
- 会话为内存 TTL 存储，不持久化：3 分钟窗口内 core 重启仅使链接失效；
- 群聊使用与扫码共用 AllowGroupScanLogin 开关（登录链接与二维码同属
  "谁完成登录就把谁的账号绑到发起人名下"的免密向量，默认私聊）；
- 登录成功回执由网页路由经 gss 主动推送（不依赖命令侧轮询协程存活），
  命令侧轮询只负责把失败原因转达回聊天。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any
from collections.abc import Callable

from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.web_app import app

from ..utils.config import ArkConfig
from ..utils.render import ark_templates
from ..utils.skland import auth as ark_auth
from ..utils.skland import store as ark_store
from ._format import format_bind_success

LOGIN_FLOW = "ark_phone"

# 会话参数：链接 3 分钟有效（与页面提示一致）；同用户发起新登录会作废旧会话
SESSION_TTL = 180.0
SESSION_MAX = 20
# 短信验证码：60 秒重发冷却，单会话最多 5 次（鹰角侧另有频控，这是本地兜底）
SEND_COOLDOWN = 60
SEND_MAX = 5
# 验证码提交尝试上限（错码重试计入），超限作废会话防爆破
LOGIN_MAX_ATTEMPTS = 5
# 命令侧轮询节奏（测试可 patch 加速）
POLL_INTERVAL = 1.0
POLL_DEADLINE = 180.0


class _SessionStore:
    """内存 TTL 会话表：懒过期 + 容量淘汰 + 按条件批量删除。

    单线程事件循环内使用，无锁；clock 可注入便于测试。
    """

    def __init__(
        self,
        ttl: float = SESSION_TTL,
        maxsize: int = SESSION_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._maxsize = maxsize
        self._clock = clock
        self._data: dict[str, tuple[float, dict]] = {}

    def set(self, token: str, state: dict) -> None:
        self._data[token] = (self._clock(), state)
        # 先清过期，仍超容量则按插入序淘汰最旧存活会话
        if len(self._data) > self._maxsize:
            self._sweep()
        if len(self._data) > self._maxsize:
            oldest = min(self._data, key=lambda key: self._data[key][0])
            del self._data[oldest]

    def get(self, token: str) -> dict | None:
        entry = self._data.get(token)
        if entry is None:
            return None
        created, state = entry
        if self._clock() - created > self._ttl:
            del self._data[token]
            return None
        return state

    def delete(self, token: str) -> None:
        self._data.pop(token, None)

    def delete_where(self, predicate: Callable[[dict], bool]) -> int:
        removed = [
            token
            for token, (_, state) in self._data.items()
            if predicate(state)
        ]
        for token in removed:
            del self._data[token]
        return len(removed)

    def _sweep(self) -> None:
        now = self._clock()
        for token in [
            token
            for token, (created, _) in self._data.items()
            if now - created > self._ttl
        ]:
            del self._data[token]


store = _SessionStore()


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

    if ev.group_id and not bool(
        ArkConfig.get_config("AllowGroupScanLogin").data
    ):
        return await _send_text(
            bot,
            ev,
            "网页登录链接默认仅私聊发放（链接等于一次免密登录入口）。\n"
            "管理员可在插件配置开启「允许群聊扫码登录」（AllowGroupScanLogin）"
            "后群内使用，或留空 ArkLoginUrl 回退为扫码登录。",
        )

    # 同一新登录使旧链接失效：链接是"绑定到发起人"的入口，不允许并存
    store.delete_where(
        lambda state: state.get("flow") == LOGIN_FLOW
        and state.get("user_id") == ev.user_id
    )
    token = get_token()
    store.set(
        token,
        {
            "flow": LOGIN_FLOW,
            "phase": "init",
            "user_id": ev.user_id,
            "bot_id": ev.bot_id,
            "bot_self_id": getattr(ev, "bot_self_id", "") or "",
            "group_id": ev.group_id,
        },
    )

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
        state = store.get(token)
        if state is None or state.get("flow") != LOGIN_FLOW:
            return
        phase = state.get("phase")
        if phase == "done":
            store.delete(token)
            return
        if phase == "failed":
            store.delete(token)
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


def _live_session(auth: str) -> dict | None:
    """取处于 init 阶段的会话；不存在/已终态（done/failed）返回 None。"""
    state = store.get(auth)
    if (
        isinstance(state, dict)
        and state.get("flow") == LOGIN_FLOW
        and state.get("phase") == "init"
    ):
        return state
    return None


def _render_page(auth: str = "", user_id: str = "", expired: bool = False) -> HTMLResponse:
    template = ark_templates.get_template("ark_web_login.html")
    return HTMLResponse(
        template.render(auth=auth, user_id=user_id, expired=expired)
    )


@app.get("/ark/i/{auth}")
async def ark_login_index(auth: str):
    state = _live_session(auth)
    if state is None:
        return _render_page(expired=True)
    return _render_page(auth=auth, user_id=str(state.get("user_id", "")))


@app.post("/ark/sendCode")
async def ark_login_send_code(data: SendCodeRequest):
    state = _live_session(data.auth)
    if state is None:
        return {"success": False, "msg": "登录会话已超时或已结束，请重新发起命令"}
    if not ark_auth.is_valid_phone(data.phone):
        return {"success": False, "msg": "手机号格式错误"}

    now = time.monotonic()
    wait = SEND_COOLDOWN - int(now - state.get("last_send_ts", 0.0))
    if wait > 0:
        return {"success": False, "msg": f"请 {wait} 秒后再获取验证码"}
    if state.get("send_count", 0) >= SEND_MAX:
        return {"success": False, "msg": "获取验证码次数过多，请重新发起登录"}

    ok, msg = await ark_auth.send_phone_code(data.phone)
    if not ok:
        return {"success": False, "msg": msg or "验证码发送失败"}

    state["last_send_ts"] = now
    state["send_count"] = state.get("send_count", 0) + 1
    store.set(data.auth, state)
    return {"success": True}


def _fail(state: dict, auth: str, err: str) -> dict[str, Any]:
    state["phase"] = "failed"
    state["error_msg"] = err
    store.set(auth, state)
    return {"success": False, "msg": err}


@app.post("/ark/login")
async def ark_login_submit(data: LoginRequest):
    state = _live_session(data.auth)
    if state is None:
        return {"success": False, "msg": "登录会话已超时或已结束，请重新发起命令"}
    if not ark_auth.is_valid_phone(data.phone) or not data.code.strip().isdigit():
        return {"success": False, "msg": "手机号或验证码格式错误"}
    if state.get("attempts", 0) >= LOGIN_MAX_ATTEMPTS:
        return _fail(state, data.auth, "验证码尝试次数过多，请重新发起登录")

    state["attempts"] = state.get("attempts", 0) + 1
    store.set(data.auth, state)

    token, token_msg = await ark_auth.get_token_by_phone_code(
        data.phone, data.code.strip()
    )
    if not token:
        # 错码可重试：只记账不作废会话
        return {"success": False, "msg": token_msg or "验证码错误或已过期"}

    cred_info, err = await ark_auth.get_cred_by_token(token)
    if err == ark_auth.CRED_ERR_UNAVAILABLE:
        return _fail(
            state,
            data.auth,
            "当前服务暂时无法完成网页登录，请改用 ark扫码登录 或私聊 ark绑定 <cred>。",
        )
    if not cred_info or not cred_info.get("cred"):
        return _fail(state, data.auth, "获取 cred 失败，请重试")

    roles, err = await ark_store.persist_ark_login(
        state.get("user_id"),
        state.get("bot_id"),
        state.get("group_id"),
        cred_info["cred"],
        hg_token=token,
        skland_user_id=cred_info.get("skland_user_id", ""),
    )
    if err:
        return _fail(state, data.auth, err)

    asyncio.create_task(_notify_success(state, roles or []))
    state["phase"] = "done"
    store.set(data.auth, state)
    return {"success": True}
