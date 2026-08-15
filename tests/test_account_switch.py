"""多账号切换（account_switch）存储 + resolve_credential 默认账号接线测试。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# 让测试能直接 import ArknightsUID 包
sys.path.insert(0, str(Path(__file__).parents[1]))

from ArknightsUID.utils import account_switch as switch_mod
from ArknightsUID.utils.enduid.adapter import (
    AccountResolveResult,
    ArkCredential,
    resolve_credential,
)


def _account(uid: str, nickname: str = "") -> ArkCredential:
    return ArkCredential(
        uid=uid,
        nickname=nickname,
        server_id="1",
        cookie_status="",
        cred=f"cred-{uid}",
        token="",
        hg_token="",
        skland_user_id="",
    )


class AccountSwitchStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = switch_mod._STATE_PATH
        switch_mod._STATE_PATH = Path(self._tmp.name) / "account_switch.json"

    def tearDown(self) -> None:
        switch_mod._STATE_PATH = self._orig_path
        self._tmp.cleanup()

    def test_default_unset(self) -> None:
        self.assertEqual(switch_mod.get_default_uid("bot", "user"), "")

    def test_set_and_get(self) -> None:
        self.assertTrue(switch_mod.set_default_uid("bot", "user", "111"))
        self.assertEqual(switch_mod.get_default_uid("bot", "user"), "111")
        # 其他用户/机器人互不影响
        self.assertEqual(switch_mod.get_default_uid("bot", "user2"), "")
        self.assertEqual(switch_mod.get_default_uid("bot2", "user"), "")

    def test_set_empty_clears(self) -> None:
        switch_mod.set_default_uid("bot", "user", "111")
        switch_mod.set_default_uid("bot", "user", "")
        self.assertEqual(switch_mod.get_default_uid("bot", "user"), "")

    def test_clear_scoped_by_uid(self) -> None:
        switch_mod.set_default_uid("bot", "user", "111")
        # 指向别的 uid 时不清除
        self.assertFalse(switch_mod.clear_default_uid("bot", "user", uid="222"))
        self.assertEqual(switch_mod.get_default_uid("bot", "user"), "111")
        # 恰好指向该 uid 时清除
        self.assertTrue(switch_mod.clear_default_uid("bot", "user", uid="111"))
        self.assertEqual(switch_mod.get_default_uid("bot", "user"), "")

    def test_clear_all(self) -> None:
        switch_mod.set_default_uid("bot", "user", "111")
        self.assertTrue(switch_mod.clear_default_uid("bot", "user"))
        self.assertEqual(switch_mod.get_default_uid("bot", "user"), "")

    def test_corrupt_state_file(self) -> None:
        switch_mod._STATE_PATH.write_text("not json", encoding="utf-8")
        self.assertEqual(switch_mod.get_default_uid("bot", "user"), "")
        # 写入后恢复正常
        switch_mod.set_default_uid("bot", "user", "111")
        self.assertEqual(switch_mod.get_default_uid("bot", "user"), "111")


class ResolveWithDefaultTest(unittest.IsolatedAsyncioTestCase):
    """resolve_credential 未指定 uid/序号时优先取默认账号。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = switch_mod._STATE_PATH
        switch_mod._STATE_PATH = Path(self._tmp.name) / "account_switch.json"
        self._orig_resolve = sys.modules[
            "ArknightsUID.utils.enduid.adapter"
        ].resolve_accounts

    def tearDown(self) -> None:
        switch_mod._STATE_PATH = self._orig_path
        sys.modules[
            "ArknightsUID.utils.enduid.adapter"
        ].resolve_accounts = self._orig_resolve
        self._tmp.cleanup()

    def _mock_accounts(self, *accounts: ArkCredential) -> None:
        async def fake_resolve(user_id: str, bot_id: str) -> AccountResolveResult:
            return AccountResolveResult(
                installed=True,
                reason="",
                uids=[a.uid for a in accounts],
                accounts=list(accounts),
            )

        sys.modules[
            "ArknightsUID.utils.enduid.adapter"
        ].resolve_accounts = fake_resolve

    async def test_default_account_wins(self) -> None:
        self._mock_accounts(_account("111", "一号"), _account("222", "二号"))
        switch_mod.set_default_uid("bot", "user", "222")
        credential = await resolve_credential("user", "bot")
        self.assertIsInstance(credential, ArkCredential)
        assert isinstance(credential, ArkCredential)
        self.assertEqual(credential.uid, "222")

    async def test_falls_back_to_first_usable_when_default_missing(self) -> None:
        self._mock_accounts(_account("111", "一号"), _account("222", "二号"))
        switch_mod.set_default_uid("bot", "user", "gone")
        credential = await resolve_credential("user", "bot")
        self.assertIsInstance(credential, ArkCredential)
        assert isinstance(credential, ArkCredential)
        self.assertEqual(credential.uid, "111")

    async def test_unusable_default_errors(self) -> None:
        broken = _account("222", "二号")
        broken.cred = ""
        self._mock_accounts(_account("111", "一号"), broken)
        switch_mod.set_default_uid("bot", "user", "222")
        result = await resolve_credential("user", "bot")
        self.assertIsInstance(result, str)
        assert isinstance(result, str)
        self.assertIn("ark切换", result)

    async def test_explicit_uid_beats_default(self) -> None:
        self._mock_accounts(_account("111", "一号"), _account("222", "二号"))
        switch_mod.set_default_uid("bot", "user", "222")
        credential = await resolve_credential("user", "bot", uid="111")
        self.assertIsInstance(credential, ArkCredential)
        assert isinstance(credential, ArkCredential)
        self.assertEqual(credential.uid, "111")


if __name__ == "__main__":
    unittest.main()
