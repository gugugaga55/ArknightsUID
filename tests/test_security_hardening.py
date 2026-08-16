"""安全加固回归测试：2026-08 安全审计修复点。

覆盖：
- 回复脱敏共享化（utils/reply_safety）+ gacha 兑换码回复接入
- bind 成功文案 / notify 推送文案不泄漏明文 UID
- role_id 非纯数字拒写入（防 uid 拼路径逃逸）+ 路径入口防御
- PRTS Cargo where 单引号转义
- cultivate charId 兜底白名单
"""
from __future__ import annotations

import unittest
from unittest import mock

from ArknightsUID.utils.reply_safety import safe_external_message

SENTINEL_CRED = "SENTINEL_CRED_DO_NOT_LEAK"
SENTINEL_TOKEN = "SENTINEL_TOKEN_DO_NOT_LEAK"


class ReplySafetySharedTest(unittest.TestCase):
    def test_shared_module_redacts_assignments(self) -> None:
        raw = f"登录失败 cred={SENTINEL_CRED} 请重试"
        self.assertNotIn(SENTINEL_CRED, safe_external_message(raw))

    def test_account_module_keeps_alias(self) -> None:
        from ArknightsUID.arknightsuid_account import _safe_external_message

        self.assertNotIn(
            SENTINEL_TOKEN, _safe_external_message(f"token: {SENTINEL_TOKEN}")
        )


class GiftReplyRedactionTest(unittest.TestCase):
    def test_error_message_with_token_redacted(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _gift_reply

        reply = _gift_reply(
            {"code": 1001, "message": f"token={SENTINEL_TOKEN} 无效"}
        )
        self.assertNotIn(SENTINEL_TOKEN, reply)
        self.assertIn("已隐藏敏感内容", reply)

    def test_error_message_with_cred_field_redacted(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _gift_reply

        reply = _gift_reply({"code": 1001, "message": f"cred: {SENTINEL_CRED}"})
        self.assertNotIn(SENTINEL_CRED, reply)

    def test_success_message_normal_text_kept(self) -> None:
        from ArknightsUID.arknightsuid_gacha import _gift_reply

        reply = _gift_reply({"code": 0, "message": "兑换成功，已发放到邮箱"})
        self.assertIn("兑换成功", reply)


class ReplyNoLeakTest(unittest.TestCase):
    def test_bind_success_masks_uid(self) -> None:
        from ArknightsUID.arknightsuid_bind import _format_bind_success

        raw_uid = "100000012345"
        text = _format_bind_success([{"nickname": "博士", "role_id": raw_uid}])
        self.assertNotIn(raw_uid, text)
        self.assertIn("100***345", text)

    def test_push_message_uses_masked_uid(self) -> None:
        from ArknightsUID.arknightsuid_notify import format_push_message
        from ArknightsUID.utils.enduid.adapter import hide_uid

        raw_uid = "100000012345"
        text = format_push_message("博士", 130, hide_uid(raw_uid))
        self.assertNotIn(raw_uid, text)
        self.assertIn("100***345", text)


class StoreRoleValidationTest(unittest.IsolatedAsyncioTestCase):
    async def test_persist_rejects_non_digit_role_id(self) -> None:
        from ArknightsUID.utils.skland import store as ark_store

        roles = [
            {
                "role_id": "../../evil",
                "nickname": "x",
                "record_uid": "r",
                "server_id": "1",
            }
        ]
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred", new=mock.AsyncMock(return_value=roles)
        ), mock.patch.object(
            ark_store.ArkUser, "insert_data", new=mock.AsyncMock(return_value=0)
        ) as m_user, mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ):
            result, err = await ark_store.persist_ark_login("u", "b", None, "CRED")

        self.assertIsNone(result)
        self.assertIsNotNone(err)
        m_user.assert_not_awaited()

    async def test_persist_filters_bad_role_keeps_good(self) -> None:
        from ArknightsUID.utils.skland import store as ark_store

        good = {"role_id": "111", "nickname": "a", "record_uid": "r1", "server_id": "1"}
        bad = {"role_id": "../evil", "nickname": "b", "record_uid": "r2", "server_id": "1"}
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred",
            new=mock.AsyncMock(return_value=[good, bad]),
        ), mock.patch.object(
            ark_store.ArkUser, "insert_data", new=mock.AsyncMock(return_value=0)
        ) as m_user, mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ):
            result, err = await ark_store.persist_ark_login("u", "b", None, "CRED")

        self.assertIsNone(err)
        self.assertEqual(result, [good])
        self.assertEqual(m_user.await_count, 1)


class UidPathGuardTest(unittest.TestCase):
    def test_cache_path_rejects_traversal(self) -> None:
        from ArknightsUID.utils.player import service as player_service

        with self.assertRaises(ValueError):
            player_service._cache_path("../../evil")

    def test_logs_path_rejects_traversal(self) -> None:
        from ArknightsUID.utils.hypergryph import gacha as hg_gacha

        with self.assertRaises(ValueError):
            hg_gacha._logs_path("../../evil")

    def test_valid_uid_stays_inside_player_path(self) -> None:
        from ArknightsUID.utils.player import service as player_service

        path = player_service._cache_path("100000012345")
        self.assertEqual(path.parent, player_service.PLAYER_PATH / "100000012345")


class CargoWhereEscapeTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_base_skill_escapes_quotes(self) -> None:
        from ArknightsUID.utils.prts import client as prts_client

        captured: dict = {}

        async def fake_api(params, **_kw):
            captured.update(params)
            return {"cargoquery": [{"title": {"room": "贸易站", "description": "d"}}]}

        with mock.patch.object(prts_client, "_api", new=fake_api):
            await prts_client.query_base_skill("贸易站' OR name LIKE '%")

        # 单引号 doubling 后无法再闭合字符串字面量构造 OR 注入
        self.assertEqual(captured["where"], "name='贸易站'' OR name LIKE ''%'")

    async def test_query_base_skill_escapes_backslash(self) -> None:
        from ArknightsUID.utils.prts import client as prts_client

        captured: dict = {}

        async def fake_api(params, **_kw):
            captured.update(params)
            return {"cargoquery": [{"title": {"room": "r", "description": "d"}}]}

        with mock.patch.object(prts_client, "_api", new=fake_api):
            await prts_client.query_base_skill("a\\b'")

        self.assertEqual(captured["where"], "name='a\\\\b'''")

    async def test_query_base_skill_normal_name_unchanged(self) -> None:
        from ArknightsUID.utils.prts import client as prts_client

        captured: dict = {}

        async def fake_api(params, **_kw):
            captured.update(params)
            return {"cargoquery": [{"title": {"room": "r", "description": "d"}}]}

        with mock.patch.object(prts_client, "_api", new=fake_api):
            await prts_client.query_base_skill("雷达部署")

        self.assertEqual(captured["where"], "name='雷达部署'")


class CharIdWhitelistTest(unittest.TestCase):
    def test_accepts_canonical_char_id(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _CHAR_ID_RE

        self.assertIsNotNone(_CHAR_ID_RE.fullmatch("char_123_silverash"))

    def test_rejects_param_pollution_and_traversal(self) -> None:
        from ArknightsUID.arknightsuid_cultivate import _CHAR_ID_RE

        for bad in ("char_x&foo=bar", "银灰", "../../evil", "char_1#", "a b"):
            self.assertIsNone(_CHAR_ID_RE.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()
