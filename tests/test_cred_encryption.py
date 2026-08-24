"""凭据静态加密回归测试（2026-08 安全审计加固）。

覆盖：
- utils/crypto 往返 / 明文透传（旧库兼容）/ 篡改自愈 / 幂等加密
- store.persist_ark_login 落库值必须是密文（cred + hg_token）
- adapter.resolve_accounts 读取时解密；EndUID 风格明文行透传
- models 的按 cred 匹配辅助 cred_equals（密文/明文/空值）
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.fernet import Fernet

from ArknightsUID.utils import crypto as ark_crypto
from ArknightsUID.utils.crypto import (
    ENC_PREFIX,
    cred_equals,
    decrypt_secret,
    encrypt_secret,
)

CRED = "c" * 32
HG_TOKEN = "t" * 24
SHORT_TOKEN = "s" * 20


class CryptoRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_env = os.environ.get("ARK_UID_ENC_KEY")
        os.environ["ARK_UID_ENC_KEY"] = Fernet.generate_key().decode()
        ark_crypto._reset_cache()

    def tearDown(self) -> None:
        # 恢复 conftest 的原始密钥，避免非法 key 泄漏到后续测试类
        if self._orig_env is None:
            os.environ.pop("ARK_UID_ENC_KEY", None)
        else:
            os.environ["ARK_UID_ENC_KEY"] = self._orig_env
        ark_crypto._reset_cache()

    def test_encrypt_then_decrypt_round_trip(self) -> None:
        stored = encrypt_secret(CRED)
        self.assertTrue(stored.startswith(ENC_PREFIX))
        self.assertNotIn(CRED, stored)
        self.assertEqual(decrypt_secret(stored), CRED)

    def test_legacy_plaintext_passthrough(self) -> None:
        self.assertEqual(decrypt_secret(CRED), CRED)
        self.assertEqual(decrypt_secret(""), "")
        self.assertEqual(decrypt_secret(None), "")

    def test_encrypt_empty_and_idempotent(self) -> None:
        self.assertEqual(encrypt_secret(""), "")
        once = encrypt_secret(CRED)
        self.assertEqual(encrypt_secret(once), once)  # 已加密不重复包一层

    def test_tampered_ciphertext_returns_empty(self) -> None:
        stored = encrypt_secret(CRED)
        tampered = stored[:-4] + ("AAAA" if not stored.endswith("AAAA") else "BBBB")
        self.assertEqual(decrypt_secret(tampered), "")

    def test_wrong_key_returns_empty(self) -> None:
        stored = encrypt_secret(CRED)
        os.environ["ARK_UID_ENC_KEY"] = Fernet.generate_key().decode()
        ark_crypto._reset_cache()
        self.assertEqual(decrypt_secret(stored), "")

    def test_env_key_rejected_invalid_falls_back_to_file(self) -> None:
        # 非法环境变量 → 回退密钥文件（指向临时目录，不污染真实数据目录）
        os.environ["ARK_UID_ENC_KEY"] = "not-a-fernet-key"
        ark_crypto._reset_cache()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                ark_crypto, "_key_file", new=lambda: Path(tmp) / "k"
            ):
                stored = encrypt_secret(CRED)
        self.assertTrue(stored.startswith(ENC_PREFIX))
        self.assertEqual(decrypt_secret(stored), CRED)

    def test_cred_equals_matching(self) -> None:
        stored = encrypt_secret(CRED)
        self.assertTrue(cred_equals(stored, CRED))
        self.assertFalse(cred_equals(stored, "x" * 32))
        # 旧明文行也能匹配（透传语义）
        self.assertTrue(cred_equals(CRED, CRED))
        self.assertFalse(cred_equals("", ""))
        self.assertFalse(cred_equals(CRED, ""))


class StorePersistEncryptionTest(unittest.IsolatedAsyncioTestCase):
    async def test_persist_stores_ciphertext_not_plaintext(self) -> None:
        from ArknightsUID.utils.skland import store as ark_store

        roles = [
            {"role_id": "111", "nickname": "阿米娅", "record_uid": "r", "server_id": "1"}
        ]
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred", new=mock.AsyncMock(return_value=roles)
        ), mock.patch.object(
            ark_store.ArkUser, "insert_data", new=mock.AsyncMock(return_value=0)
        ) as m_user, mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ):
            result, err = await ark_store.persist_ark_login(
                "u", "b", None, CRED, hg_token=HG_TOKEN
            )

        self.assertIsNone(err)
        kwargs = m_user.await_args.kwargs
        # 落库值必须是密文且不含明文凭据
        self.assertTrue(kwargs["cookie"].startswith(ENC_PREFIX))
        self.assertNotIn(CRED, kwargs["cookie"])
        self.assertTrue(kwargs["hg_token"].startswith(ENC_PREFIX))
        self.assertNotIn(HG_TOKEN, kwargs["hg_token"])
        # 且可解密还原（读取链路依赖）
        self.assertEqual(decrypt_secret(kwargs["cookie"]), CRED)
        self.assertEqual(decrypt_secret(kwargs["hg_token"]), HG_TOKEN)


class AdapterDecryptTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_rows_decrypted_into_credential(self) -> None:
        from ArknightsUID.utils.enduid import adapter as ark_adapter

        row = SimpleNamespace(
            uid="111",
            nickname="阿米娅",
            server_id="1",
            cookie_status="",
            cookie=encrypt_secret(CRED),
            token=encrypt_secret(SHORT_TOKEN),
            hg_token=encrypt_secret(HG_TOKEN),
            skland_user_id="SUID",
            record_id="REC",
        )
        with mock.patch.object(
            ark_adapter.ark_store,
            "list_local_accounts",
            new=mock.AsyncMock(return_value=[row]),
        ):
            result = await ark_adapter.resolve_accounts("u", "b")

        self.assertTrue(result.installed)
        self.assertEqual(len(result.accounts), 1)
        account = result.accounts[0]
        self.assertEqual(account.cred, CRED)
        self.assertEqual(account.token, SHORT_TOKEN)
        self.assertEqual(account.hg_token, HG_TOKEN)
        self.assertTrue(account.usable)

    async def test_plaintext_rows_still_resolve(self) -> None:
        """旧明文库 / EndUID 明文行：透传不解密也不报错。"""
        from ArknightsUID.utils.enduid import adapter as ark_adapter

        row = SimpleNamespace(
            uid="222",
            nickname="德克萨斯",
            server_id="1",
            cookie_status="",
            cookie=CRED,
            token=None,
            hg_token=HG_TOKEN,
            skland_user_id="",
            record_id="",
        )
        with mock.patch.object(
            ark_adapter.ark_store,
            "list_local_accounts",
            new=mock.AsyncMock(return_value=[row]),
        ):
            result = await ark_adapter.resolve_accounts("u", "b")

        account = result.accounts[0]
        self.assertEqual(account.cred, CRED)
        self.assertEqual(account.hg_token, HG_TOKEN)
        self.assertEqual(account.token, "")

    async def test_broken_ciphertext_marks_unusable(self) -> None:
        """换密钥后解密失败 → cred 为空 → usable=False（引导重新登录）。"""
        from ArknightsUID.utils.enduid import adapter as ark_adapter

        row = SimpleNamespace(
            uid="333",
            nickname="x",
            server_id="1",
            cookie_status="",
            cookie=ENC_PREFIX + Fernet(Fernet.generate_key()).encrypt(b"junk").decode(),
            token=None,
            hg_token="",
            skland_user_id="",
            record_id="",
        )
        with mock.patch.object(
            ark_adapter.ark_store,
            "list_local_accounts",
            new=mock.AsyncMock(return_value=[row]),
        ):
            result = await ark_adapter.resolve_accounts("u", "b")

        account = result.accounts[0]
        self.assertEqual(account.cred, "")
        self.assertFalse(account.usable)


class ModelsLiveSqliteTest(unittest.IsolatedAsyncioTestCase):
    """重写的按凭据查询方法过真 SQLite（绕开 with_session 的全局会话工厂，
    经 __wrapped__ 直接注入临时引擎的 session）。"""

    async def test_select_and_update_by_cred_on_real_db(self) -> None:
        import tempfile

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.orm import sessionmaker

        from ArknightsUID.utils.database.models import ArkUser

        cred_a, cred_b, cred_c = "a" * 32, "b" * 32, "c" * 32
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}/t.db")
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(lambda c: ArkUser.__table__.create(c))
                maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
                async with maker() as s:
                    s.add(ArkUser(user_id="u1", bot_id="bot", uid="111", cookie=encrypt_secret(cred_a)))
                    s.add(ArkUser(user_id="u2", bot_id="bot", uid="222", cookie=encrypt_secret(cred_c)))
                    s.add(ArkUser(user_id="u3", bot_id="bot", uid="333", cookie=cred_b))  # 旧明文行
                    await s.commit()

                async with maker() as s:
                    row = await ArkUser.select_data_by_cred.__wrapped__(
                        ArkUser, session=s, cred=cred_a
                    )
                    self.assertIsNotNone(row)
                    self.assertEqual(row.uid, "111")
                    # 旧明文行透传命中
                    row = await ArkUser.select_data_by_cred.__wrapped__(
                        ArkUser, session=s, cred=cred_b
                    )
                    self.assertIsNotNone(row)
                    self.assertEqual(row.uid, "333")
                    # 不存在的凭据不误命中
                    row = await ArkUser.select_data_by_cred.__wrapped__(
                        ArkUser, session=s, cred="z" * 32
                    )
                    self.assertIsNone(row)

                async with maker() as s:
                    matched = await ArkUser.update_token_by_cred.__wrapped__(
                        ArkUser, session=s, cred=cred_a, token="NEW_TOK"
                    )
                    await s.commit()
                    self.assertEqual(matched, 1)
                async with maker() as s:
                    by_uid = {
                        r.uid: r
                        for r in (await s.execute(select(ArkUser))).scalars().all()
                    }
                    self.assertTrue(by_uid["111"].token.startswith(ENC_PREFIX))
                    self.assertEqual(decrypt_secret(by_uid["111"].token), "NEW_TOK")
                    self.assertIn(by_uid["222"].token, (None, ""))  # 未匹配不动
                    self.assertIn(by_uid["333"].token, (None, ""))  # 未匹配不动
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
