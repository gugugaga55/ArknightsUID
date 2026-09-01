from __future__ import annotations

import types
import unittest
from typing import Any
from unittest import mock

from ArknightsUID.utils.enduid import adapter as enduid_adapter


class RecordingLogger:
    def __init__(self) -> None:
        self.debugs: list[str] = []
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


class Row:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeEndBind:
    bind: Row | None = None

    @classmethod
    async def get_data_by_user_id(cls, user_id: str, bot_id: str) -> Row | None:
        return cls.bind


class FakeEndUser:
    users: list[Row] = []

    @classmethod
    async def select_data_list(cls, **kwargs: Any) -> list[Row]:
        return cls.users


def fake_models() -> types.SimpleNamespace:
    return types.SimpleNamespace(EndBind=FakeEndBind, EndUser=FakeEndUser)


def user(
    uid: str,
    *,
    game_id: Any = enduid_adapter.ARKNIGHTS_GAME_ID,
    nickname: str = "",
    cookie_status: str = "有效",
    cookie: str = "SECRET_COOKIE",
    token: str = "SECRET_TOKEN",
    hg_token: str = "SECRET_HG",
    skland_user_id: str = "SECRET_SKLAND_USER",
    server_id: str = "",
) -> Row:
    return Row(
        uid=uid,
        game_id=game_id,
        nickname=nickname,
        cookie_status=cookie_status,
        cookie=cookie,
        token=token,
        hg_token=hg_token,
        skland_user_id=skland_user_id,
        server_id=server_id,
    )


class EnduidAdapterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.logger = RecordingLogger()
        self.original_logger = enduid_adapter.logger
        self.original_enduid_models = enduid_adapter._enduid_models
        self.original_import_module = enduid_adapter.import_module
        enduid_adapter.logger = self.logger
        FakeEndBind.bind = None
        FakeEndUser.users = []
        # resolve_accounts 改造后先查自有 ArkUser 表；本测试套件聚焦 EndUID
        # 回退分支，故把自有账号置空，避免触碰真实数据库 session。
        self._local_patcher = mock.patch.object(
            enduid_adapter.ark_store,
            "list_local_accounts",
            new=mock.AsyncMock(return_value=[]),
        )
        self._local_patcher.start()
        self.addCleanup(self._local_patcher.stop)

    def tearDown(self) -> None:
        enduid_adapter.logger = self.original_logger
        enduid_adapter._enduid_models = self.original_enduid_models
        enduid_adapter.import_module = self.original_import_module
        FakeEndBind.bind = None
        FakeEndUser.users = []

    def use_fake_models(self) -> None:
        enduid_adapter._enduid_models = fake_models

    def test_hide_uid_masks_by_length(self) -> None:
        self.assertEqual(enduid_adapter.hide_uid("1234"), "1234")
        self.assertEqual(enduid_adapter.hide_uid("12345678"), "1***8")
        self.assertEqual(enduid_adapter.hide_uid("123456789"), "12***89")

    def test_split_uid_list_deduplicates_preserving_order(self) -> None:
        result = enduid_adapter._split_uid_list(" 11111 _22222_11111__33333 ")
        self.assertEqual(result, ["11111", "22222", "33333"])

    async def test_resolve_accounts_reports_missing_enduid(self) -> None:
        enduid_adapter._enduid_models = lambda: None

        result = await enduid_adapter.resolve_accounts("user", "bot")

        self.assertFalse(result.installed)
        self.assertEqual(result.uids, [])
        self.assertEqual(result.accounts, [])
        self.assertIn("ark绑定", result.reason)

    async def test_resolve_accounts_handles_bound_uids_without_credentials(self) -> None:
        self.use_fake_models()
        FakeEndBind.bind = Row(ark_uid="123456789_987654321")
        FakeEndUser.users = []

        result = await enduid_adapter.resolve_accounts("user", "bot")

        self.assertTrue(result.installed)
        self.assertEqual(result.uids, ["123456789", "987654321"])
        self.assertEqual(result.accounts, [])

    async def test_resolve_accounts_filters_deduplicates_and_sorts_accounts(self) -> None:
        self.use_fake_models()
        FakeEndBind.bind = Row(ark_uid="222222222_111111111_444444444")
        FakeEndUser.users = [
            user("111111111", nickname="First"),
            user("333333333", nickname="Appended"),
            user("111111111", nickname="Duplicate"),
            user("222222222", nickname="BoundFirst", cookie_status="无效"),
            user("999999999", game_id=2, nickname="OtherGame"),
            user("", nickname="MissingUid"),
        ]

        result = await enduid_adapter.resolve_accounts("user", "bot")

        self.assertTrue(result.installed)
        self.assertEqual(
            result.uids,
            ["222222222", "111111111", "444444444", "333333333"],
        )
        self.assertEqual(
            [account.uid for account in result.accounts],
            ["222222222", "111111111", "333333333"],
        )
        self.assertEqual([account.nickname for account in result.accounts], ["BoundFirst", "First", "Appended"])
        self.assertFalse(result.accounts[0].usable)
        self.assertTrue(result.accounts[1].usable)

    async def test_resolve_credential_selects_first_usable_account_by_default(self) -> None:
        self.use_fake_models()
        FakeEndBind.bind = Row(ark_uid="111111111_222222222")
        FakeEndUser.users = [
            user("111111111", cookie_status="无效"),
            user("222222222", nickname="Usable"),
        ]

        credential = await enduid_adapter.resolve_credential("user", "bot")

        self.assertIsInstance(credential, enduid_adapter.ArkCredential)
        self.assertEqual(credential.uid, "222222222")

    async def test_resolve_credential_uses_one_based_index_and_refuses_unusable(self) -> None:
        self.use_fake_models()
        FakeEndBind.bind = Row(ark_uid="111111111_222222222")
        FakeEndUser.users = [
            user("111111111", cookie_status="无效"),
            user("222222222"),
        ]

        first = await enduid_adapter.resolve_credential("user", "bot", index=1)
        second = await enduid_adapter.resolve_credential("user", "bot", index=2)
        out_of_range = await enduid_adapter.resolve_credential("user", "bot", index=3)

        self.assertIsInstance(first, str)
        self.assertIn("凭据为空或已失效", first)
        self.assertIsInstance(second, enduid_adapter.ArkCredential)
        self.assertEqual(second.uid, "222222222")
        self.assertEqual(out_of_range, "未找到第 3 个明日方舟账号，请使用 ark绑定列表 查看可用账号。")

    async def test_resolve_credential_matches_exact_uid_and_masks_missing_uid(self) -> None:
        self.use_fake_models()
        FakeEndBind.bind = Row(ark_uid="111111111_222222222")
        FakeEndUser.users = [
            user("111111111"),
            user("222222222"),
        ]

        exact = await enduid_adapter.resolve_credential("user", "bot", uid="222222222")
        missing = await enduid_adapter.resolve_credential("user", "bot", uid="333333333")

        self.assertIsInstance(exact, enduid_adapter.ArkCredential)
        self.assertEqual(exact.uid, "222222222")
        self.assertEqual(missing, "未找到 UID 33***33 的可用凭据。")
        self.assertNotIn("333333333", missing)

    async def test_resolve_credential_reports_only_bound_uid_without_raw_uid(self) -> None:
        self.use_fake_models()
        FakeEndBind.bind = Row(ark_uid="123456789")
        FakeEndUser.users = []

        result = await enduid_adapter.resolve_credential("user", "bot")

        self.assertIsInstance(result, str)
        self.assertIn("12***89", result)
        self.assertNotIn("123456789", result)
        self.assertIn("没有找到对应的 EndUID 凭据记录", result)

    async def test_resolve_credential_reports_all_credentials_unusable(self) -> None:
        self.use_fake_models()
        FakeEndBind.bind = Row(ark_uid="111111111_222222222")
        FakeEndUser.users = [
            user("111111111", cookie=""),
            user("222222222", cookie_status="无效"),
        ]

        result = await enduid_adapter.resolve_credential("user", "bot")

        self.assertEqual(result, "已找到明日方舟账号，但 EndUID 凭据为空或已失效，请重新使用 EndUID 登录。")

    def test_import_first_logs_module_and_exception_type_without_raw_exception_text(self) -> None:
        secret = (
            "cookie=SECRET_COOKIE cred=SECRET_CRED token=SECRET_TOKEN "
            "hg_token=SECRET_HG skland_user_id=SECRET_SKLAND_USER /secret/path"
        )

        def fake_import_module(module_name: str) -> types.ModuleType:
            if module_name == "missing.module":
                raise ModuleNotFoundError(secret)
            raise RuntimeError(secret)

        enduid_adapter.import_module = fake_import_module

        result = enduid_adapter._import_first(("missing.module", "broken.module"))
        logs = "\n".join(self.logger.debugs + self.logger.warnings)

        self.assertIsNone(result)
        self.assertIn("missing.module", logs)
        self.assertIn("ModuleNotFoundError", logs)
        self.assertIn("broken.module", logs)
        self.assertIn("RuntimeError", logs)
        for leaked in (
            "SECRET_COOKIE",
            "SECRET_CRED",
            "SECRET_TOKEN",
            "SECRET_HG",
            "SECRET_SKLAND_USER",
            "/secret/path",
        ):
            self.assertNotIn(leaked, logs)


if __name__ == "__main__":
    unittest.main()
