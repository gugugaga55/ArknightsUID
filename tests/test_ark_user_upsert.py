"""ArkUser 多账号 upsert 回归测试（2026-08 审计问题 #1）。

背景 bug：登录链走基类 insert_data，其 update 分支只按 (user_id, bot_id)
更新（base_models.update_data），多账号用户重登其中一个账号会把该用户
**全部**账号行改写成当前 uid——凭据互相覆盖且留下重复行。

本文件走真实 SQLite ORM（隔离临时库，经 ``__wrapped__`` 注入 session），
覆盖：
- 重登只更新目标账号行，兄弟账号原样保留
- 新 uid 走插入分支；连续 upsert 幂等（仍一行）
- (bot_id, user_id, uid) 唯一约束阻断重复行
- ensure_ark_user_unique_index 对存量重复库的去重迁移（旧库无约束 →
  建索引 → 重复插入被拒 → 幂等重跑）
- 加密不可用时 token 回写被拒绝（fail-closed，配套 crypto 改动）
- persist_ark_login 在加密不可用时拒绝登录落库并返回可读错误
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from ArknightsUID.utils.crypto import EncryptionUnavailableError, encrypt_secret


def _make_engine(tmp: str):
    return create_async_engine(f"sqlite+aiosqlite:///{tmp}/t.db")


async def _create_table(engine) -> None:
    from ArknightsUID.utils.database.models import ArkUser

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: ArkUser.__table__.create(c, checkfirst=True))


def _maker(engine):
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _all_rows(session) -> dict[str, "object"]:
    from ArknightsUID.utils.database.models import ArkUser

    result = await session.execute(select(ArkUser).order_by(ArkUser.uid))
    return {row.uid: row for row in result.scalars().all()}


class UpsertByUidRealDbTest(unittest.IsolatedAsyncioTestCase):
    """upsert_by_uid 在真实 ORM 上的多账号语义。"""

    async def test_relogin_updates_only_target_row(self) -> None:
        """审计 #1 核心场景：重登 A，B 行必须原样保留。"""
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                await _create_table(engine)
                maker = _maker(engine)
                async with maker() as s:
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                                  nickname="账号A", cookie=encrypt_secret("a" * 32)))
                    s.add(ArkUser(user_id="u", bot_id="b", uid="222",
                                  nickname="账号B", cookie=encrypt_secret("b" * 32)))
                    await s.commit()

                # 重新登录 111：新 cred + 新昵称
                async with maker() as s:
                    ok = await ArkUser.upsert_by_uid.__wrapped__(
                        ArkUser, session=s, user_id="u", bot_id="b", uid="111",
                        nickname="账号A改", cookie=encrypt_secret("new" * 8),
                    )
                    await s.commit()
                self.assertTrue(ok)

                async with maker() as s:
                    rows = await _all_rows(s)
                    self.assertEqual(len(rows), 2, "重登不得增删行")
                    self.assertEqual(rows["111"].nickname, "账号A改")
                    self.assertEqual(rows["222"].nickname, "账号B")
                    self.assertNotEqual(rows["222"].cookie, rows["111"].cookie)
            finally:
                await engine.dispose()

    async def test_upsert_inserts_new_uid_and_is_idempotent(self) -> None:
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                await _create_table(engine)
                maker = _maker(engine)
                async with maker() as s:
                    ok = await ArkUser.upsert_by_uid.__wrapped__(
                        ArkUser, session=s, user_id="u", bot_id="b", uid="111",
                        nickname="A",
                    )
                    await s.commit()
                self.assertTrue(ok)

                # 再次 upsert 同 uid：幂等，仍是一行
                async with maker() as s:
                    await ArkUser.upsert_by_uid.__wrapped__(
                        ArkUser, session=s, user_id="u", bot_id="b", uid="111",
                        nickname="A2",
                    )
                    await s.commit()
                async with maker() as s:
                    rows = await _all_rows(s)
                self.assertEqual(list(rows), ["111"])
                self.assertEqual(rows["111"].nickname, "A2")
            finally:
                await engine.dispose()

    async def test_unique_constraint_blocks_duplicate_row(self) -> None:
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                await _create_table(engine)
                maker = _maker(engine)
                async with maker() as s:
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111", nickname="1"))
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111", nickname="2"))
                    with self.assertRaises(IntegrityError):
                        await s.commit()
            finally:
                await engine.dispose()

    async def test_non_unique_integrity_error_is_raised(self) -> None:
        """审计 P2-1：非唯一键的 IntegrityError 必须如实上抛，不伪装成功。"""
        from ArknightsUID.utils.database.models import ArkUser

        class _RaisingFlushSession:
            """包装真 session：flush 抛与唯一键无关的 IntegrityError。"""

            def __init__(self, inner) -> None:
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def flush(self):
                raise IntegrityError(
                    "FOREIGN KEY constraint failed", None, Exception("orig")
                )

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                await _create_table(engine)
                maker = _maker(engine)
                async with maker() as s:
                    wrapped = _RaisingFlushSession(s)
                    with self.assertRaises(IntegrityError):
                        await ArkUser.upsert_by_uid.__wrapped__(
                            ArkUser, session=wrapped,
                            user_id="u", bot_id="b", uid="111", nickname="A",
                        )
                # 行不存在（插入失败且未伪装成功）
                async with maker() as s:
                    rows = await _all_rows(s)
                self.assertEqual(rows, {})
            finally:
                await engine.dispose()

    async def test_race_conflict_converges_when_row_exists(self) -> None:
        """并发竞态语义：flush 撞约束但竞争者行已存在 → 收敛为更新成功。"""
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                await _create_table(engine)
                maker = _maker(engine)
                # 预置竞争者已插入的行（模拟另一协程抢先 INSERT 后提交）
                async with maker() as s:
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                                  nickname="竞争者"))
                    await s.commit()

                class _RaceFlushSession:
                    def __init__(self, inner) -> None:
                        self._inner = inner
                        self.flushed = False

                    def __getattr__(self, name):
                        return getattr(self._inner, name)

                    async def flush(self):
                        if not self.flushed:
                            self.flushed = True
                            # 先让竞争者行可见，再模拟撞唯一索引
                            await self._inner.flush()
                            raise IntegrityError(
                                "UNIQUE constraint failed: ArkUser.uid",
                                None, Exception("orig"),
                            )
                        return await self._inner.flush()

                async with maker() as s:
                    ok = await ArkUser.upsert_by_uid.__wrapped__(
                        ArkUser, session=_RaceFlushSession(s),
                        user_id="u", bot_id="b", uid="111", nickname="我方新值",
                    )
                    await s.commit()
                self.assertTrue(ok)
                async with maker() as s:
                    rows = await _all_rows(s)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows["111"].nickname, "我方新值")
            finally:
                await engine.dispose()


class UniqueIndexMigrationTest(unittest.IsolatedAsyncioTestCase):
    """存量库（无唯一约束的旧 schema）→ ensure_ark_user_unique_index 迁移。"""

    async def test_migration_dedupes_and_enforces(self) -> None:
        import sqlalchemy as sa

        from ArknightsUID.utils.database import auto_migrate
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                # 复刻旧 schema：同名表、同列，但不带新加的表级唯一约束/索引
                legacy = sa.Table(
                    "ArkUser", sa.MetaData(),
                    sa.Column("id", sa.Integer, primary_key=True),
                    *[
                        col._copy()
                        for col in ArkUser.__table__.columns
                        if col.name != "id"
                    ],
                )
                async with engine.begin() as conn:
                    await conn.run_sync(lambda c: legacy.create(c))
                maker = _maker(engine)
                # 旧 bug 遗留：111 两行（旧行 + 被覆盖后的重写行）
                async with maker() as s:
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                                  nickname="旧", cookie="old"))
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                                  nickname="新", cookie="new"))
                    s.add(ArkUser(user_id="u", bot_id="b", uid="222",
                                  nickname="账号B", cookie="b"))
                    await s.commit()

                ok = await auto_migrate.ensure_ark_user_unique_index(
                    engine_override=engine
                )
                self.assertTrue(ok)

                async with maker() as s:
                    rows = await _all_rows(s)
                    self.assertEqual(len(rows), 2)
                    # 每组保留 id 最大（最新写入）的一行
                    self.assertEqual(rows["111"].nickname, "新")
                    self.assertEqual(rows["111"].cookie, "new")

                    # 迁移后重复插入被数据库拒绝
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111", nickname="dup"))
                    with self.assertRaises(IntegrityError):
                        await s.commit()

                # 幂等：重跑不再改动、不报错
                self.assertTrue(
                    await auto_migrate.ensure_ark_user_unique_index(
                        engine_override=engine
                    )
                )
            finally:
                await engine.dispose()

    async def test_fresh_table_constraint_recognized(self) -> None:
        """新建库（表级 UniqueConstraint）：等价唯一性被识别，直接就绪不误删。"""
        from ArknightsUID.utils.database import auto_migrate
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                await _create_table(engine)  # 带 UniqueConstraint 的新 schema
                maker = _maker(engine)
                async with maker() as s:
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111", nickname="A"))
                    s.add(ArkUser(user_id="u", bot_id="b", uid="222", nickname="B"))
                    await s.commit()

                self.assertTrue(
                    await auto_migrate.ensure_ark_user_unique_index(
                        engine_override=engine
                    )
                )
                async with maker() as s:
                    rows = await _all_rows(s)
                self.assertEqual(len(rows), 2, "识别到等价约束时不得触发去重")
            finally:
                await engine.dispose()


class TokenWritebackFailClosedTest(unittest.IsolatedAsyncioTestCase):
    """加密不可用时 token 回写 fail-closed（不明文落库）。"""

    async def test_update_token_refused_when_encryption_unavailable(self) -> None:
        from ArknightsUID.utils.database import models as ark_models
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                await _create_table(engine)
                maker = _maker(engine)
                cred = "c" * 32
                async with maker() as s:
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                                  cookie=encrypt_secret(cred), token=None))
                    await s.commit()

                with mock.patch.object(
                    ark_models,
                    "encrypt_secret",
                    side_effect=EncryptionUnavailableError("加密不可用"),
                ):
                    async with maker() as s:
                        matched = await ArkUser.update_token_by_cred.__wrapped__(
                            ArkUser, session=s, cred=cred, token="SHORT_TOK"
                        )
                        await s.commit()
                self.assertEqual(matched, 0)
                async with maker() as s:
                    rows = await _all_rows(s)
                self.assertIn(rows["111"].token, (None, ""), "不得明文写 token")
            finally:
                await engine.dispose()


class PersistLoginFailClosedTest(unittest.IsolatedAsyncioTestCase):
    """persist_ark_login：加密不可用 → 拒绝保存并返回可读错误。"""

    async def test_persist_refused_without_plaintext_write(self) -> None:
        from ArknightsUID.utils.skland import store as ark_store

        roles = [
            {"role_id": "111", "nickname": "阿米娅", "record_uid": "r", "server_id": "1"}
        ]
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred",
            new=mock.AsyncMock(return_value=roles),
        ), mock.patch.object(
            ark_store, "encrypt_secret",
            side_effect=EncryptionUnavailableError("加密不可用"),
        ), mock.patch.object(
            ark_store.ArkUser, "upsert_by_uid", new=mock.AsyncMock(return_value=True)
        ) as m_user, mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ) as m_bind:
            result, err = await ark_store.persist_ark_login(
                "u", "b", None, "c" * 32, hg_token="t" * 24
            )

        self.assertIsNone(result)
        self.assertIsNotNone(err)
        self.assertIn("加密", err)
        self.assertIn("ark_cred_key", err)
        m_user.assert_not_awaited()
        m_bind.assert_not_awaited()

    async def test_persist_upserts_each_role_by_uid(self) -> None:
        """多 role 登录：每个 role 一行，按各自 uid upsert（不走全行覆盖）。"""
        from ArknightsUID.utils.crypto import decrypt_secret

        from ArknightsUID.utils.skland import store as ark_store

        roles = [
            {"role_id": "111", "nickname": "A", "record_uid": "r1", "server_id": "1"},
            {"role_id": "222", "nickname": "B", "record_uid": "r2", "server_id": "1"},
        ]
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred", new=mock.AsyncMock(return_value=roles)
        ), mock.patch.object(
            ark_store.ArkUser, "upsert_by_uid", new=mock.AsyncMock(return_value=True)
        ) as m_user, mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ):
            result, err = await ark_store.persist_ark_login(
                "u", "b", None, "c" * 32, hg_token="t" * 24
            )

        self.assertIsNone(err)
        self.assertEqual(m_user.await_count, 2)
        upserted_uids = [call.kwargs["uid"] for call in m_user.await_args_list]
        self.assertEqual(upserted_uids, ["111", "222"])
        # cred/hg_token 密文落库且可解密还原
        for call in m_user.await_args_list:
            self.assertEqual(decrypt_secret(call.kwargs["cookie"]), "c" * 32)
            self.assertEqual(decrypt_secret(call.kwargs["hg_token"]), "t" * 24)

    async def test_persist_reports_error_when_upsert_unconfirmed(self) -> None:
        """审计 P2-1：upsert 未确认时不得向用户报成功。"""
        from ArknightsUID.utils.skland import store as ark_store

        roles = [
            {"role_id": "111", "nickname": "A", "record_uid": "r1", "server_id": "1"}
        ]
        with mock.patch.object(
            ark_store, "list_ark_roles_by_cred", new=mock.AsyncMock(return_value=roles)
        ), mock.patch.object(
            ark_store.ArkUser, "upsert_by_uid", new=mock.AsyncMock(return_value=False)
        ), mock.patch.object(
            ark_store.ArkBind, "insert_uid", new=mock.AsyncMock(return_value=0)
        ) as m_bind:
            result, err = await ark_store.persist_ark_login(
                "u", "b", None, "c" * 32, hg_token="t" * 24
            )

        self.assertIsNone(result)
        self.assertIsNotNone(err)
        self.assertIn("保存失败", err)
        m_bind.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class ArkAliasUpsertTest(unittest.IsolatedAsyncioTestCase):
    """审计 #37：别名设置的数据库级原子性（唯一约束 + 撞约束收敛）。"""

    async def test_set_alias_atomic_and_unique(self) -> None:
        from ArknightsUID.utils.database.models import ArkAlias

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(
                        lambda c: ArkAlias.__table__.create(c, checkfirst=True)
                    )
                maker = _maker(engine)
                # 顺序覆盖：同键两次 set 仍一行，target 更新
                async with maker() as s:
                    await ArkAlias.set_alias.__wrapped__(
                        ArkAlias, session=s,
                        bot_id="b", user_id="u", alias="银羽毛", target="银灰",
                    )
                    await s.commit()
                async with maker() as s:
                    await ArkAlias.set_alias.__wrapped__(
                        ArkAlias, session=s,
                        bot_id="b", user_id="u", alias="银羽毛", target="银灰改",
                    )
                    await s.commit()
                async with maker() as s:
                    rows = (await s.execute(select(ArkAlias))).scalars().all()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].target, "银灰改")

                # 唯一约束：绕过 set_alias 直接插重复键 → IntegrityError
                async with maker() as s:
                    s.add(ArkAlias(bot_id="b", user_id="u",
                                   alias="银羽毛", target="dup"))
                    with self.assertRaises(IntegrityError):
                        await s.commit()
            finally:
                await engine.dispose()

    async def test_alias_index_migration_dedupes(self) -> None:
        import sqlalchemy as sa

        from ArknightsUID.utils.database import auto_migrate
        from ArknightsUID.utils.database.models import ArkAlias

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                legacy = sa.Table(
                    "ArkAlias", sa.MetaData(),
                    sa.Column("id", sa.Integer, primary_key=True),
                    *[
                        col._copy()
                        for col in ArkAlias.__table__.columns
                        if col.name != "id"
                    ],
                )
                async with engine.begin() as conn:
                    await conn.run_sync(lambda c: legacy.create(c))
                maker = _maker(engine)
                async with maker() as s:  # 旧"先查后插"竞态遗留的重复行
                    s.add(ArkAlias(bot_id="b", user_id="u",
                                   alias="银羽毛", target="旧"))
                    s.add(ArkAlias(bot_id="b", user_id="u",
                                   alias="银羽毛", target="新"))
                    await s.commit()

                self.assertTrue(
                    await auto_migrate.ensure_ark_alias_unique_index(
                        engine_override=engine
                    )
                )
                async with maker() as s:
                    rows = (await s.execute(select(ArkAlias))).scalars().all()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].target, "新")  # 保留最新写入
            finally:
                await engine.dispose()


class ReencryptPlaintextTest(unittest.IsolatedAsyncioTestCase):
    """审计 #4 收尾：存量明文凭据启动时一次性重加密。"""

    async def test_plaintext_rows_upgraded_encrypted_untouched(self) -> None:
        from sqlalchemy import select

        from ArknightsUID.utils.crypto import decrypt_secret
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                await _create_table(engine)
                maker = _maker(engine)
                async with maker() as s:
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                                  nickname="明文行", cookie="p" * 32,
                                  token="t" * 24, hg_token="h" * 24))
                    s.add(ArkUser(user_id="u", bot_id="b", uid="222",
                                  nickname="密文行",
                                  cookie=encrypt_secret("c" * 32),
                                  token=encrypt_secret("s" * 24),
                                  hg_token=encrypt_secret("g" * 24)))
                    await s.commit()

                async with maker() as s:
                    before = {
                        r.uid: (r.cookie, r.token, r.hg_token)
                        for r in (await s.execute(select(ArkUser))).scalars().all()
                    }

                async with maker() as s:
                    upgraded = await ArkUser.reencrypt_plaintext_credentials.__wrapped__(
                        ArkUser, session=s
                    )
                    await s.commit()
                self.assertEqual(upgraded, 1)

                async with maker() as s:
                    rows = {r.uid: r for r in
                            (await s.execute(select(ArkUser))).scalars().all()}
                # 明文行 → 密文，且可解密还原（值不变，只升级存储形态）
                self.assertTrue(rows["111"].cookie.startswith("enc:v1:"))
                self.assertEqual(decrypt_secret(rows["111"].cookie), "p" * 32)
                self.assertEqual(decrypt_secret(rows["111"].token), "t" * 24)
                # 密文行不被重复加密（幂等：值逐字节不变）
                self.assertEqual(
                    (rows["222"].cookie, rows["222"].token, rows["222"].hg_token),
                    before["222"],
                )
            finally:
                await engine.dispose()

    async def test_unavailable_encryption_keeps_plaintext(self) -> None:
        from sqlalchemy import select

        from ArknightsUID.utils.crypto import EncryptionUnavailableError
        from ArknightsUID.utils.database import models as ark_models
        from ArknightsUID.utils.database.models import ArkUser

        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            try:
                await _create_table(engine)
                maker = _maker(engine)
                async with maker() as s:
                    s.add(ArkUser(user_id="u", bot_id="b", uid="111",
                                  cookie="p" * 32))
                    await s.commit()

                with mock.patch.object(
                    ark_models, "encrypt_secret",
                    side_effect=EncryptionUnavailableError("不可用"),
                ):
                    async with maker() as s:
                        upgraded = await ArkUser.reencrypt_plaintext_credentials.__wrapped__(
                            ArkUser, session=s
                        )
                        await s.commit()
                self.assertEqual(upgraded, 0)  # 跳过而非失败
                async with maker() as s:
                    rows = (await s.execute(select(ArkUser))).scalars().all()
                self.assertEqual(rows[0].cookie, "p" * 32)  # 保持明文待下轮
            finally:
                await engine.dispose()
