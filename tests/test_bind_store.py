from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ArknightsUID.utils.skland import store as ark_store


def _row(
    uid: str,
    game_id: int = 1,
    nickname: str = "",
    cookie: str = "C",
    cookie_status: str = "",
    token: str = "",
    hg_token: str = "",
    skland_user_id: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        uid=uid, game_id=game_id, nickname=nickname, cookie=cookie,
        cookie_status=cookie_status, token=token, hg_token=hg_token,
        skland_user_id=skland_user_id,
    )


class StoreListTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_local_accounts_filters_game_id(self) -> None:
        with mock.patch.object(
            ark_store.ArkUser,
            "select_data_list",
            new=mock.AsyncMock(return_value=[_row("111"), _row("222", game_id=3)]),
        ):
            result = await ark_store.list_local_accounts("u", "b")
        self.assertEqual([r.uid for r in result], ["111"])


class StorePersistTest(unittest.IsolatedAsyncioTestCase):
    async def test_persist_writes_each_role(self) -> None:
        roles = [
            {"role_id": "111", "nickname": "阿米娅", "record_uid": "rec1", "server_id": "1"},
            {"role_id": "222", "nickname": "德克萨斯", "record_uid": "rec2", "server_id": "1"},
        ]
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred", new=mock.AsyncMock(return_value=roles)
        ), mock.patch.object(
            ark_store.ArkUser, "upsert_by_uid", new=mock.AsyncMock(return_value=True)
        ) as m_user, mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ) as m_bind:
            result, err = await ark_store.persist_ark_login("u", "b", None, "CRED")

        self.assertIsNone(err)
        self.assertEqual(result, roles)
        self.assertEqual(m_user.await_count, 2)
        self.assertEqual(m_bind.await_count, 2)

    async def test_persist_no_roles_returns_error(self) -> None:
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred", new=mock.AsyncMock(return_value=[])
        ):
            result, err = await ark_store.persist_ark_login("u", "b", None, "CRED")
        self.assertIsNone(result)
        self.assertIn("未找到", err)

    async def test_persist_request_fail_returns_error(self) -> None:
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred", new=mock.AsyncMock(return_value=None)
        ):
            result, err = await ark_store.persist_ark_login("u", "b", None, "CRED")
        self.assertIsNone(result)
        self.assertIn("失败", err)

    async def test_persist_passes_token_as_hg_token(self) -> None:
        roles = [{"role_id": "111", "nickname": "x", "record_uid": "r", "server_id": "1"}]
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred", new=mock.AsyncMock(return_value=roles)
        ), mock.patch.object(
            ark_store.ArkUser, "upsert_by_uid", new=mock.AsyncMock(return_value=True)
        ) as m_user, mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ):
            await ark_store.persist_ark_login(
                "u", "b", None, "CRED", hg_token="HG", skland_user_id="SUID"
            )
        # upsert_by_uid 的关键字参数应含 hg_token（密文）/ skland_user_id
        _args, kwargs = m_user.await_args
        # hg_token 2026-08 起密文落库（utils/crypto）：断言非明文且可解密还原
        from ArknightsUID.utils.crypto import decrypt_secret

        self.assertNotEqual(kwargs["hg_token"], "HG")
        self.assertEqual(decrypt_secret(kwargs["hg_token"]), "HG")
        self.assertEqual(kwargs["skland_user_id"], "SUID")


class StoreRemoveTest(unittest.IsolatedAsyncioTestCase):
    async def test_remove_by_uid(self) -> None:
        with mock.patch.object(
            ark_store.ArkUser, "delete_by_user", new=mock.AsyncMock(return_value=1)
        ) as m_del, mock.patch.object(
            ark_store.ArkBind, "delete_uid", new=mock.AsyncMock(return_value=0)
        ) as m_bind:
            count = await ark_store.remove_ark_login("u", "b", uid="111")
        self.assertEqual(count, 1)
        m_del.assert_awaited_once()
        m_bind.assert_awaited_once()

    async def test_remove_all_deletes_each_bind_then_user(self) -> None:
        with mock.patch.object(
            ark_store, "list_local_accounts",
            new=mock.AsyncMock(return_value=[_row("111"), _row("222")]),
        ), mock.patch.object(
            ark_store.ArkBind, "delete_uid", new=mock.AsyncMock(return_value=0)
        ) as m_bind, mock.patch.object(
            ark_store.ArkUser, "delete_by_user", new=mock.AsyncMock(return_value=2)
        ):
            count = await ark_store.remove_ark_login("u", "b")
        self.assertEqual(count, 2)
        self.assertEqual(m_bind.await_count, 2)


if __name__ == "__main__":
    unittest.main()
