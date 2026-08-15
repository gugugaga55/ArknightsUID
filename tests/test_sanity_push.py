"""理智回满推送测试：状态机纯函数 + 轮询任务（全 mock，无网络）。"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID import arknightsuid_notify as notify_mod
from ArknightsUID.arknightsuid_notify import (
    _extract_target,
    check_sanity_push,
    evaluate_sanity,
    format_push_message,
    load_push_state,
)


class FakeCredential:
    def __init__(self, uid: str) -> None:
        self.uid = uid
        self.nickname = f"角色{uid}"
        self.server_id = "1"
        self.cookie_status = ""
        self.cred = f"cred-{uid}"
        self.token = ""
        self.hg_token = ""
        self.skland_user_id = ""


class FakeSubscribe:
    def __init__(self, uid: str, user_id: str = "10001", bot_id: str = "bot") -> None:
        self.uid = uid
        self.user_id = user_id
        self.bot_id = bot_id
        self.group_id = None
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


def _envelope(ap_current: int, ap_max: int, name: str = "刀客塔") -> dict[str, Any]:
    """构造能让 parse_player_info + effective_ap 得出 ap_current 的响应。

    completeRecoveryTime 取未来时间戳（now + 缺口×360），缺口 = max - current。
    """
    import time

    gap = max(0, ap_max - ap_current)
    recovery = int(time.time()) + gap * 360 if gap else 0
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "status": {
                "name": name,
                "uid": "123456789",
                "level": 120,
                "ap": {
                    "current": ap_current,
                    "max": ap_max,
                    "completeRecoveryTime": recovery,
                },
            },
            "chars": [],
        },
    }


class EvaluateSanityTest(unittest.TestCase):
    def test_full_and_unnotified_pushes(self) -> None:
        self.assertEqual(evaluate_sanity(135, 135, False), (True, True))

    def test_full_but_notified_suppresses(self) -> None:
        self.assertEqual(evaluate_sanity(135, 135, True), (True, False))

    def test_not_full_never_pushes(self) -> None:
        self.assertEqual(evaluate_sanity(80, 135, False), (False, False))
        self.assertEqual(evaluate_sanity(80, 135, True), (False, False))

    def test_zero_max_is_noop(self) -> None:
        self.assertEqual(evaluate_sanity(0, 0, False), (False, False))


class HelperTest(unittest.TestCase):
    def test_extract_target(self) -> None:
        self.assertEqual(_extract_target("123456789"), ("123456789", 0))
        self.assertEqual(_extract_target(" 2"), ("", 2))
        self.assertEqual(_extract_target("第3号"), ("", 3))
        self.assertEqual(_extract_target(""), ("", 0))

    def test_format_push_message(self) -> None:
        message = format_push_message("刀客塔", 135, "123***789")
        self.assertIn("理智已回满", message)
        self.assertIn("135/135", message)
        self.assertIn("刀客塔", message)
        self.assertIn("123***789", message)


class CheckSanityPushTest(unittest.IsolatedAsyncioTestCase):
    """轮询任务端到端（mock 凭据解析/数据拉取），验证推送-去抖-复位全流程。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_state_path = notify_mod._STATE_PATH
        notify_mod._STATE_PATH = Path(self._tmp.name) / "sanity_push_state.json"

        # 打桩 notify 模块命名空间内的依赖（import 时绑定的引用）
        self._orig_resolve = notify_mod.resolve_credential
        self._orig_fetch = notify_mod.fetch_cached_player_info
        self._orig_get_subscribe = notify_mod.gs_subscribe.get_subscribe
        self._orig_random = notify_mod.random

    def tearDown(self) -> None:
        notify_mod.resolve_credential = self._orig_resolve
        notify_mod.fetch_cached_player_info = self._orig_fetch
        notify_mod.gs_subscribe.get_subscribe = self._orig_get_subscribe
        notify_mod.random = self._orig_random
        notify_mod._STATE_PATH = self._orig_state_path
        self._tmp.cleanup()

    def _install(
        self,
        subscribes: list[FakeSubscribe],
        envelopes: dict[str, dict[str, Any] | str],
    ) -> None:
        async def fake_get_subscribe(task_name: str) -> list[FakeSubscribe]:
            return subscribes

        async def fake_resolve(user_id: str, bot_id: str, uid: str = "") -> Any:
            return FakeCredential(uid)

        async def fake_fetch(credential: Any) -> Any:
            return envelopes.get(credential.uid, "拉取失败")

        notify_mod.gs_subscribe.get_subscribe = fake_get_subscribe
        notify_mod.resolve_credential = fake_resolve
        notify_mod.fetch_cached_player_info = fake_fetch
        # 替换整个 random 引用（避免污染全局 random 模块），轮询间隔归零
        notify_mod.random = types.SimpleNamespace(uniform=lambda a, b: 0.0)

    async def test_push_once_then_suppress_then_rearm(self) -> None:
        subscribe = FakeSubscribe("111")
        self._install([subscribe], {})
        state = load_push_state()
        self.assertEqual(state, {})

        # 第一轮：回满 → 推送一次
        self._install([subscribe], {"111": _envelope(135, 135)})
        await check_sanity_push()
        self.assertEqual(len(subscribe.sent), 1)
        self.assertIn("135/135", subscribe.sent[0])
        state = load_push_state()
        self.assertTrue(state["bot|10001|111"]["notified"])

        # 第二轮：仍回满 → 不重复推送
        await check_sanity_push()
        self.assertEqual(len(subscribe.sent), 1)

        # 第三轮：用掉理智（未回满）→ 复位
        self._install([subscribe], {"111": _envelope(60, 135)})
        await check_sanity_push()
        self.assertEqual(len(subscribe.sent), 1)
        self.assertFalse(load_push_state()["bot|10001|111"]["notified"])

        # 第四轮：再次回满 → 重新推送
        self._install([subscribe], {"111": _envelope(135, 135)})
        await check_sanity_push()
        self.assertEqual(len(subscribe.sent), 2)

    async def test_partial_sanity_never_pushes(self) -> None:
        subscribe = FakeSubscribe("222")
        self._install([subscribe], {"222": _envelope(80, 135)})
        await check_sanity_push()
        self.assertEqual(subscribe.sent, [])
        # 未回满不产生状态键
        self.assertEqual(load_push_state(), {})

    async def test_fetch_failure_skips_without_state(self) -> None:
        subscribe = FakeSubscribe("333")
        self._install([subscribe], {"333": "森空岛玩家信息接口请求失败。"})
        await check_sanity_push()
        self.assertEqual(subscribe.sent, [])
        self.assertEqual(load_push_state(), {})

    async def test_unusable_credential_skips(self) -> None:
        subscribe = FakeSubscribe("444")

        async def fake_resolve(user_id: str, bot_id: str, uid: str = "") -> Any:
            return "该账号的凭据为空或已失效，请重新登录。"

        self._install([subscribe], {"444": _envelope(135, 135)})
        notify_mod.resolve_credential = fake_resolve
        await check_sanity_push()
        self.assertEqual(subscribe.sent, [])
        self.assertEqual(load_push_state(), {})

    async def test_no_subscriptions_noop(self) -> None:
        self._install([], {})
        await check_sanity_push()
        self.assertEqual(load_push_state(), {})


if __name__ == "__main__":
    unittest.main()
