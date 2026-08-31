from __future__ import annotations

import unittest
from typing import Any

from ArknightsUID.arknightsuid_bind import bind_credential, remove_login, show_bind


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[Any, dict]] = []

    async def send(self, message: Any, **kwargs: Any) -> None:
        self.sent.append((message, kwargs))


class FakeEvent:
    def __init__(self, text: str = "", group_id: object = "g1") -> None:
        self.text = text
        self.group_id = group_id
        self.user_id = "u1"
        self.bot_id = "b1"


class PrivateOnlyTest(unittest.IsolatedAsyncioTestCase):
    """群聊拒绝并提示（私聊双重保险的 handler 内分支）。"""

    async def test_bind_rejected_in_group(self) -> None:
        bot, ev = FakeBot(), FakeEvent(text="x" * 32, group_id="g1")
        await bind_credential(bot, ev)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("私聊", str(bot.sent[0][0]))

    async def test_remove_rejected_in_group(self) -> None:
        bot, ev = FakeBot(), FakeEvent(text="", group_id="g1")
        await remove_login(bot, ev)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("私聊", str(bot.sent[0][0]))


class ShowBindTest(unittest.IsolatedAsyncioTestCase):
    async def test_show_bind_empty_hint(self) -> None:
        from unittest import mock
        from ArknightsUID.arknightsuid_bind import ark_store

        bot, ev = FakeBot(), FakeEvent(text="", group_id=None)  # 私聊
        with mock.patch.object(
            ark_store, "list_local_accounts",
            new=mock.AsyncMock(return_value=[]),
        ):
            await show_bind(bot, ev)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("未绑定", str(bot.sent[0][0]))


class GroupScanLoginToggleTest(unittest.IsolatedAsyncioTestCase):
    """群聊扫码开关（AllowGroupScanLogin）：默认拒、开了放行到扫码流程。

    ark绑定（裸凭据）不受开关影响，群聊恒拒——上面的 PrivateOnlyTest 已锁。
    """

    def setUp(self) -> None:
        from ArknightsUID.utils.config import ArkConfig

        self._cfg = ArkConfig.get_config("AllowGroupScanLogin")
        self._orig = self._cfg.data

    def tearDown(self) -> None:
        self._cfg.data = self._orig

    async def test_group_rejected_by_default(self) -> None:
        from ArknightsUID.arknightsuid_bind import scan_login

        self._cfg.data = False
        bot, ev = FakeBot(), FakeEvent(text="", group_id="g1")
        await scan_login(bot, ev)
        # 只有一条文字提示（未发二维码），且提示里点了开关名
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("AllowGroupScanLogin", str(bot.sent[0][0]))

    async def test_group_allowed_when_enabled(self) -> None:
        from unittest import mock

        from ArknightsUID.arknightsuid_bind import ark_auth, scan_login

        self._cfg.data = True
        bot, ev = FakeBot(), FakeEvent(text="", group_id="g1")
        with mock.patch.object(
            ark_auth, "get_scan_id", new=mock.AsyncMock(return_value=None)
        ):
            await scan_login(bot, ev)
        # 已通过群聊闸门进入扫码流程（scan_id 拉取失败文案为证）
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("获取扫码 ID 失败", str(bot.sent[0][0]))

    async def test_private_unaffected_by_toggle(self) -> None:
        from unittest import mock

        from ArknightsUID.arknightsuid_bind import ark_auth, scan_login

        self._cfg.data = False  # 私聊不需要开关
        bot, ev = FakeBot(), FakeEvent(text="", group_id=None)
        with mock.patch.object(
            ark_auth, "get_scan_id", new=mock.AsyncMock(return_value=None)
        ):
            await scan_login(bot, ev)
        self.assertIn("获取扫码 ID 失败", str(bot.sent[0][0]))

    async def test_group_notice_shown_with_qr(self) -> None:
        """群内放行时，二维码提示文案带公开风险说明。"""
        from unittest import mock

        from ArknightsUID import arknightsuid_bind as bind_mod

        self._cfg.data = True
        bot, ev = FakeBot(), FakeEvent(text="", group_id="g1")
        with mock.patch.object(
            bind_mod.ark_auth, "get_scan_id",
            new=mock.AsyncMock(return_value="scan123"),
        ), mock.patch.object(
            bind_mod, "get_qrcode_base64",
            new=mock.AsyncMock(return_value="b64img"),
        ), mock.patch.object(
            bind_mod.ark_auth, "get_scan_status",
            new=mock.AsyncMock(return_value=None),
        ):
            # 轮询 deadline 3 分钟，这里不打网络：scan_status 恒 None，
            # 直接把循环跑到超时太久——patch 掉轮询段外的等待
            import asyncio as _aio

            real_sleep = _aio.sleep

            async def fast_sleep(_t):
                await real_sleep(0)

            with mock.patch.object(bind_mod.asyncio, "sleep", fast_sleep):
                await bind_mod.scan_login(bot, ev)
        texts = [str(m) for m, _ in bot.sent]
        self.assertTrue(any("任何人扫码" in t for t in texts), texts)


if __name__ == "__main__":
    unittest.main()
