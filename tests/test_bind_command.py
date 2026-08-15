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


if __name__ == "__main__":
    unittest.main()
