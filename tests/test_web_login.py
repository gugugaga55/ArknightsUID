"""ark网页登录（手机号验证码网页登录）测试。

覆盖：会话存储 TTL/淘汰/顶替、URL 归一化、命令入口开关与未配置分支、
sendCode 冷却与次数上限、login 成功/错码/爆破上限/405 分支、
命令侧轮询终态转达、凭据零泄漏口径。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest import mock

from ArknightsUID.arknightsuid_bind import web_login
from ArknightsUID.arknightsuid_bind.web_login import (
    LOGIN_FLOW,
    LOGIN_MAX_ATTEMPTS,
    LoginRequest,
    SendCodeRequest,
    _SessionStore,
)
from ArknightsUID.utils.skland.auth import is_valid_phone

SENTINEL_CRED = "C" * 32
SENTINEL_TOKEN = "T" * 24


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any, **kwargs: Any) -> None:
        self.sent.append(message)


class FakeEvent:
    def __init__(self, group_id: object = None) -> None:
        self.text = ""
        self.group_id = group_id
        self.user_id = "u1"
        self.bot_id = "b1"
        self.bot_self_id = "self1"


def _set_config(key: str, value: Any) -> None:
    web_login.ArkConfig.get_config(key).data = value


def _user_sessions() -> list[tuple[str, dict]]:
    return [
        (token, state)
        for token, (_, state) in web_login.store._data.items()
        if state.get("user_id") == "u1"
    ]


def _make_session(auth: str = "auth1", user_id: str = "u1") -> dict:
    state = {
        "flow": LOGIN_FLOW,
        "phase": "init",
        "user_id": user_id,
        "bot_id": "b1",
        "bot_self_id": "self1",
        "group_id": None,
    }
    web_login.store.set(auth, state)
    return state


class PhoneValidationTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertTrue(is_valid_phone("13800138000"))
        self.assertTrue(is_valid_phone(" 13800138000 "))  # 内部 strip

    def test_invalid(self) -> None:
        for bad in ("", "12345", "2380013800a", "1380013800a", "23800138000"):
            self.assertFalse(is_valid_phone(bad), bad)


class SessionStoreTest(unittest.TestCase):
    def _state(self, user_id: str = "u1") -> dict:
        return {"flow": LOGIN_FLOW, "phase": "init", "user_id": user_id}

    def test_roundtrip_and_delete(self) -> None:
        store = _SessionStore(ttl=100, maxsize=10, clock=lambda: 0.0)
        store.set("t1", self._state())
        self.assertIsNotNone(store.get("t1"))
        store.delete("t1")
        self.assertIsNone(store.get("t1"))

    def test_ttl_expiry(self) -> None:
        now = [0.0]
        store = _SessionStore(ttl=180.0, maxsize=10, clock=lambda: now[0])
        store.set("t1", self._state())
        now[0] = 100.0
        self.assertIsNotNone(store.get("t1"))
        now[0] = 181.0
        self.assertIsNone(store.get("t1"))

    def test_delete_where_by_user(self) -> None:
        store = _SessionStore(ttl=100, maxsize=10, clock=lambda: 0.0)
        store.set("t1", self._state("u1"))
        store.set("t2", self._state("u2"))
        removed = store.delete_where(
            lambda s: s.get("flow") == LOGIN_FLOW and s.get("user_id") == "u1"
        )
        self.assertEqual(removed, 1)
        self.assertIsNone(store.get("t1"))
        self.assertIsNotNone(store.get("t2"))

    def test_maxsize_evicts_oldest_live(self) -> None:
        now = [0.0]
        store = _SessionStore(ttl=1000.0, maxsize=2, clock=lambda: now[0])
        store.set("t1", self._state())
        now[0] = 1.0
        store.set("t2", self._state())
        now[0] = 2.0
        store.set("t3", self._state())
        self.assertIsNone(store.get("t1"))  # 最旧存活被淘汰
        self.assertIsNotNone(store.get("t2"))
        self.assertIsNotNone(store.get("t3"))


class ConfiguredUrlTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = web_login.ArkConfig.get_config("ArkLoginUrl").data

    def tearDown(self) -> None:
        _set_config("ArkLoginUrl", self._orig)

    def test_empty(self) -> None:
        _set_config("ArkLoginUrl", "")
        self.assertEqual(web_login._configured_login_url(), "")

    def test_bare_domain_gets_https(self) -> None:
        _set_config("ArkLoginUrl", "ark.example.com")
        self.assertEqual(web_login._configured_login_url(), "https://ark.example.com")

    def test_scheme_and_slash_normalized(self) -> None:
        _set_config("ArkLoginUrl", "http://192.168.1.5:18765/")
        self.assertEqual(web_login._configured_login_url(), "http://192.168.1.5:18765")
        _set_config("ArkLoginUrl", "https://ark.example.com/")
        self.assertEqual(web_login._configured_login_url(), "https://ark.example.com")


class WebLoginEntryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        web_login.store = _SessionStore()
        self._orig_url = web_login.ArkConfig.get_config("ArkLoginUrl").data
        self._orig_switch = web_login.ArkConfig.get_config("AllowGroupScanLogin").data
        _set_config("AllowGroupScanLogin", False)

    def tearDown(self) -> None:
        _set_config("ArkLoginUrl", self._orig_url)
        _set_config("AllowGroupScanLogin", self._orig_switch)
        web_login.store = _SessionStore()

    async def _run(self, ev: FakeEvent) -> FakeBot:
        bot = FakeBot()
        web_login.POLL_INTERVAL, web_login.POLL_DEADLINE = 0.001, 0.01
        try:
            await web_login.web_login_entry(bot, ev)
        finally:
            web_login.POLL_INTERVAL, web_login.POLL_DEADLINE = 1.0, 180.0
        return bot

    async def test_group_blocked_by_default(self) -> None:
        _set_config("ArkLoginUrl", "https://ark.example.com")
        bot = await self._run(FakeEvent(group_id="g1"))
        self.assertEqual(len(bot.sent), 1)
        text = str(bot.sent[0])
        self.assertIn("仅私聊", text)
        self.assertNotIn("/ark/i/", text)

    async def test_unconfigured_hint(self) -> None:
        _set_config("ArkLoginUrl", "")
        bot = await self._run(FakeEvent(group_id=None))
        self.assertEqual(len(bot.sent), 1)
        text = str(bot.sent[0])
        self.assertIn("ArkLoginUrl", text)
        self.assertIn("ark扫码登录", text)
        self.assertNotIn("/ark/i/", text)

    async def test_private_sends_link_and_evicts_old(self) -> None:
        _set_config("ArkLoginUrl", "https://ark.example.com/")
        bot = await self._run(FakeEvent(group_id=None))
        self.assertEqual(len(bot.sent), 1)
        text = str(bot.sent[0])
        self.assertIn("https://ark.example.com/ark/i/", text)
        self.assertIn("3 分钟内有效", text)
        self.assertIn("请勿使用他人链接", text)
        self.assertEqual(len(_user_sessions()), 1)
        # 再次发起：旧会话被顶掉，仍只有一条
        await self._run(FakeEvent(group_id=None))
        self.assertEqual(len(_user_sessions()), 1)

    async def test_group_allowed_with_warning(self) -> None:
        _set_config("ArkLoginUrl", "https://ark.example.com")
        _set_config("AllowGroupScanLogin", True)
        bot = await self._run(FakeEvent(group_id="g1"))
        text = str(bot.sent[0])
        self.assertIn("/ark/i/", text)
        self.assertIn("绑定到你名下", text)  # 群内明示风险文案


class SendCodeRouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        web_login.store = _SessionStore()

    def tearDown(self) -> None:
        web_login.store = _SessionStore()

    async def test_invalid_auth(self) -> None:
        res = await web_login.ark_login_send_code(
            SendCodeRequest(auth="nope", phone="13800138000")
        )
        self.assertFalse(res["success"])
        self.assertIn("超时", res["msg"])

    async def test_invalid_phone(self) -> None:
        _make_session("auth1")
        res = await web_login.ark_login_send_code(
            SendCodeRequest(auth="auth1", phone="12345")
        )
        self.assertFalse(res["success"])
        self.assertIn("手机号格式错误", res["msg"])

    async def test_send_ok_then_cooldown(self) -> None:
        _make_session("auth1")
        with mock.patch.object(
            web_login.ark_auth,
            "send_phone_code",
            new=mock.AsyncMock(return_value=(True, "OK")),
        ) as send:
            res1 = await web_login.ark_login_send_code(
                SendCodeRequest(auth="auth1", phone="13800138000")
            )
            self.assertTrue(res1["success"])
            send.assert_awaited_once_with("13800138000")
            res2 = await web_login.ark_login_send_code(
                SendCodeRequest(auth="auth1", phone="13800138000")
            )
            self.assertFalse(res2["success"])
            self.assertIn("秒后再获取", res2["msg"])
            send.assert_awaited_once()  # 冷却期内没打到上游

    async def test_send_count_limit(self) -> None:
        state = _make_session("auth1")
        state["send_count"] = 5
        web_login.store.set("auth1", state)
        res = await web_login.ark_login_send_code(
            SendCodeRequest(auth="auth1", phone="13800138000")
        )
        self.assertFalse(res["success"])
        self.assertIn("次数过多", res["msg"])

    async def test_upstream_failure_not_counted(self) -> None:
        _make_session("auth1")
        with mock.patch.object(
            web_login.ark_auth,
            "send_phone_code",
            new=mock.AsyncMock(return_value=(False, "发送过于频繁")),
        ):
            res = await web_login.ark_login_send_code(
                SendCodeRequest(auth="auth1", phone="13800138000")
            )
        self.assertFalse(res["success"])
        self.assertEqual(res["msg"], "发送过于频繁")
        # 失败不计次、不启动冷却
        state = web_login.store.get("auth1")
        self.assertEqual(state.get("send_count", 0), 0)
        self.assertEqual(state.get("last_send_ts", 0.0), 0.0)


class LoginRouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        web_login.store = _SessionStore()

    def tearDown(self) -> None:
        web_login.store = _SessionStore()

    async def test_invalid_auth(self) -> None:
        res = await web_login.ark_login_submit(
            LoginRequest(auth="nope", phone="13800138000", code="123456")
        )
        self.assertFalse(res["success"])
        self.assertIn("超时", res["msg"])

    async def test_terminal_phase_rejected(self) -> None:
        state = _make_session("auth1")
        state["phase"] = "done"
        web_login.store.set("auth1", state)
        res = await web_login.ark_login_submit(
            LoginRequest(auth="auth1", phone="13800138000", code="123456")
        )
        self.assertFalse(res["success"])
        self.assertIn("已结束", res["msg"])
        res2 = await web_login.ark_login_send_code(
            SendCodeRequest(auth="auth1", phone="13800138000")
        )
        self.assertFalse(res2["success"])
        # 终态会话的页面也显示失效
        page = await web_login.ark_login_index("auth1")
        self.assertIn("失效", page.body.decode())

    async def test_bad_format(self) -> None:
        _make_session("auth1")
        res = await web_login.ark_login_submit(
            LoginRequest(auth="auth1", phone="12345", code="123456")
        )
        self.assertFalse(res["success"])
        res = await web_login.ark_login_submit(
            LoginRequest(auth="auth1", phone="13800138000", code="12a456")
        )
        self.assertFalse(res["success"])
        self.assertIn("格式错误", res["msg"])

    async def test_wrong_code_retryable(self) -> None:
        _make_session("auth1")
        with mock.patch.object(
            web_login.ark_auth,
            "get_token_by_phone_code",
            new=mock.AsyncMock(return_value=(None, "验证码错误或已过期")),
        ):
            res = await web_login.ark_login_submit(
                LoginRequest(auth="auth1", phone="13800138000", code="000000")
            )
        self.assertFalse(res["success"])
        self.assertIn("验证码错误", res["msg"])
        state = web_login.store.get("auth1")
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(state["phase"], "init")  # 错码不作废会话

    async def test_attempts_exhausted_fails_session(self) -> None:
        state = _make_session("auth1")
        state["attempts"] = LOGIN_MAX_ATTEMPTS
        web_login.store.set("auth1", state)
        res = await web_login.ark_login_submit(
            LoginRequest(auth="auth1", phone="13800138000", code="123456")
        )
        self.assertFalse(res["success"])
        self.assertIn("次数过多", res["msg"])
        self.assertEqual(web_login.store.get("auth1")["phase"], "failed")

    async def test_unavailable_branch(self) -> None:
        _make_session("auth1")
        with mock.patch.object(
            web_login.ark_auth,
            "get_token_by_phone_code",
            new=mock.AsyncMock(return_value=(SENTINEL_TOKEN, "OK")),
        ), mock.patch.object(
            web_login.ark_auth,
            "get_cred_by_token",
            new=mock.AsyncMock(
                return_value=(None, web_login.ark_auth.CRED_ERR_UNAVAILABLE)
            ),
        ):
            res = await web_login.ark_login_submit(
                LoginRequest(auth="auth1", phone="13800138000", code="123456")
            )
        self.assertFalse(res["success"])
        self.assertIn("ark扫码登录", res["msg"])
        self.assertEqual(web_login.store.get("auth1")["phase"], "failed")

    async def test_success_persists_and_pushes(self) -> None:
        _make_session("auth1")
        roles = [{"nickname": "Dr", "role_id": "123456789"}]
        with mock.patch.object(
            web_login.ark_auth,
            "get_token_by_phone_code",
            new=mock.AsyncMock(return_value=(SENTINEL_TOKEN, "OK")),
        ), mock.patch.object(
            web_login.ark_auth,
            "get_cred_by_token",
            new=mock.AsyncMock(
                return_value=({"cred": SENTINEL_CRED, "skland_user_id": "sk1"}, "")
            ),
        ), mock.patch.object(
            web_login.ark_store,
            "persist_ark_login",
            new=mock.AsyncMock(return_value=(roles, None)),
        ) as persist, mock.patch.object(
            web_login,
            "_push_text",
            new=mock.AsyncMock(),
        ) as push:
            res = await web_login.ark_login_submit(
                LoginRequest(auth="auth1", phone="13800138000", code="123456")
            )
            self.assertTrue(res["success"])
            # 推送经 create_task 派发：在 patch 作用域内让出事件循环等它落地，
            # 否则 patch 先退出、任务里拿到的是真实 _push_text
            for _ in range(100):
                if push.await_count:
                    break
                await asyncio.sleep(0.001)
        persist.assert_awaited_once_with(
            "u1",
            "b1",
            None,
            SENTINEL_CRED,
            hg_token=SENTINEL_TOKEN,
            skland_user_id="sk1",
        )
        push.assert_awaited_once()
        pushed_text = str(push.await_args.args[1])
        self.assertIn("登录成功", pushed_text)
        self.assertIn("Dr", pushed_text)
        # 零泄漏：凭据不出现在推送文本或接口响应里
        self.assertNotIn(SENTINEL_CRED, pushed_text)
        self.assertNotIn(SENTINEL_TOKEN, pushed_text)
        self.assertNotIn(SENTINEL_CRED, str(res))
        self.assertEqual(web_login.store.get("auth1")["phase"], "done")

    async def test_persist_error_fails_session(self) -> None:
        _make_session("auth1")
        with mock.patch.object(
            web_login.ark_auth,
            "get_token_by_phone_code",
            new=mock.AsyncMock(return_value=(SENTINEL_TOKEN, "OK")),
        ), mock.patch.object(
            web_login.ark_auth,
            "get_cred_by_token",
            new=mock.AsyncMock(
                return_value=({"cred": SENTINEL_CRED, "skland_user_id": ""}, "")
            ),
        ), mock.patch.object(
            web_login.ark_store,
            "persist_ark_login",
            new=mock.AsyncMock(return_value=(None, "拉取森空岛绑定信息失败")),
        ):
            res = await web_login.ark_login_submit(
                LoginRequest(auth="auth1", phone="13800138000", code="123456")
            )
        self.assertFalse(res["success"])
        self.assertIn("绑定信息失败", res["msg"])
        self.assertEqual(web_login.store.get("auth1")["phase"], "failed")


class AwaitCompletionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        web_login.store = _SessionStore()
        self._orig_url = web_login.ArkConfig.get_config("ArkLoginUrl").data

    def tearDown(self) -> None:
        _set_config("ArkLoginUrl", self._orig_url)
        web_login.store = _SessionStore()

    async def _wait_for_session(self) -> tuple[str, dict]:
        for _ in range(500):
            await asyncio.sleep(0.001)
            sessions = _user_sessions()
            if sessions:
                return sessions[0]
        raise AssertionError("会话未建立")

    async def _run_entry_fast(self, bot: FakeBot, ev: FakeEvent) -> Any:
        web_login.POLL_INTERVAL, web_login.POLL_DEADLINE = 0.001, 0.2
        try:
            return await web_login.web_login_entry(bot, ev)
        finally:
            web_login.POLL_INTERVAL, web_login.POLL_DEADLINE = 1.0, 180.0

    async def test_failed_phase_relays_error(self) -> None:
        _set_config("ArkLoginUrl", "https://ark.example.com")
        bot, ev = FakeBot(), FakeEvent()
        task = asyncio.ensure_future(self._run_entry_fast(bot, ev))
        auth, state = await self._wait_for_session()
        state["phase"] = "failed"
        state["error_msg"] = "获取 cred 失败，请重试"
        web_login.store.set(auth, state)
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(len(bot.sent), 2)
        self.assertIn("网页登录失败", str(bot.sent[1]))
        self.assertIsNone(web_login.store.get(auth))

    async def test_done_phase_silent(self) -> None:
        _set_config("ArkLoginUrl", "https://ark.example.com")
        bot, ev = FakeBot(), FakeEvent()
        task = asyncio.ensure_future(self._run_entry_fast(bot, ev))
        auth, state = await self._wait_for_session()
        state["phase"] = "done"
        web_login.store.set(auth, state)
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual(len(bot.sent), 1)  # 只有链接；成功回执由网页侧推送
        self.assertIsNone(web_login.store.get(auth))


class ConfigAndHelpWiringTest(unittest.TestCase):
    def test_config_default_has_login_url(self) -> None:
        from ArknightsUID.utils.config import CONFIG_DEFAULT

        self.assertIn("ArkLoginUrl", CONFIG_DEFAULT)
        self.assertEqual(CONFIG_DEFAULT["ArkLoginUrl"].data, "")
        self.assertIn("AllowGroupScanLogin", CONFIG_DEFAULT)

    def test_help_mentions_web_login(self) -> None:
        from ArknightsUID.arknightsuid_help import HELP_TEXT

        self.assertIn("ark网页登录", HELP_TEXT)
