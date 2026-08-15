from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ArknightsUID.utils.enduid import adapter


class ResolvePriorityTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_hit_skips_enduid(self) -> None:
        """自有表命中时不应触碰 EndUID（_enduid_models 被调即抛错）。"""
        local = [
            SimpleNamespace(
                uid="111", game_id=1, nickname="阿米娅", server_id="1",
                cookie_status="", cookie="CRED", token="", hg_token="",
                skland_user_id="SUID",
            )
        ]

        def boom() -> None:
            raise AssertionError("自有命中不应回退到 EndUID")

        with mock.patch.object(
            adapter.ark_store, "list_local_accounts",
            new=mock.AsyncMock(return_value=local),
        ), mock.patch.object(adapter, "_enduid_models", new=boom):
            result = await adapter.resolve_accounts("u", "b")

        self.assertTrue(result.installed)
        self.assertEqual(result.reason, "")
        self.assertEqual(len(result.accounts), 1)
        self.assertEqual(result.accounts[0].uid, "111")
        self.assertEqual(result.accounts[0].cred, "CRED")
        self.assertEqual(result.accounts[0].nickname, "阿米娅")
        self.assertEqual(result.accounts[0].skland_user_id, "SUID")

    async def test_empty_local_falls_back_to_enduid_hint(self) -> None:
        with mock.patch.object(
            adapter.ark_store, "list_local_accounts",
            new=mock.AsyncMock(return_value=[]),
        ), mock.patch.object(
            adapter, "_enduid_models", new=mock.Mock(return_value=None)
        ):
            result = await adapter.resolve_accounts("u", "b")

        self.assertFalse(result.installed)
        self.assertIn("ark绑定", result.reason)


if __name__ == "__main__":
    unittest.main()
