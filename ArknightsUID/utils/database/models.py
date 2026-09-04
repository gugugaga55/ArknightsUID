"""ArknightsUID 数据库模型。

P0 阶段最小集：仅 `ArkUser` 和 `ArkBind`。
- `ArkUser`: 每行一个 (uid, game_id) 元组，存 cred/token 等认证信息
- `ArkBind`: QQ user_id 与游戏 UID 的绑定关系

设计参考 EndUID 的 `EndUser` / `EndBind`，但：
1. 表名用 `Ark` 前缀（避免与 EndUID 共存时撞表）
2. `game_id` 默认值改为 1（明日方舟官服 channelMasterId）
3. 移除终末地专属字段（stamina_bg_value 等）
4. 登录相关字段（hg_token / record_id 等）保留——P3 抽卡 OAuth 链要用

**表策略决策**：
当前为「独立建表」模式。若后续需要与 EndUID 共享 cred，
可以新增「跨插件读 EndUser」的 fallback（懒 import EndUID.models）。
"""

from __future__ import annotations


from sqlmodel import Field, SQLModel, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import Index, UniqueConstraint, update
from sqlalchemy.exc import IntegrityError

from gsuid_core.logger import logger
from gsuid_core.utils.database.base_models import Bind, User, with_session
from gsuid_core.server import on_core_start

from ..crypto import (
    ENC_PREFIX,
    EncryptionUnavailableError,
    cred_equals,
    encrypt_secret,
    warmup as crypto_warmup,
)
from .auto_migrate import (
    auto_add_missing_columns,
    ensure_ark_alias_unique_index,
    ensure_ark_user_unique_index,
)

# 明日方舟官服 channelMasterId（与 skland/client.py 的 ARKNIGHTS_CHANNEL_OFFICIAL 一致）
ARKNIGHTS_CHANNEL_OFFICIAL = 1


class ArkUser(User, table=True):
    """明日方舟用户表（每个 (uid, game_id) 一行）。"""

    __tablename__ = "ArkUser"
    __table_args__ = (
        Index("idx_ark_uid_user_bot", "uid", "user_id", "bot_id"),
        Index("idx_ark_cookie_status", "cookie_status"),
        # 每行一个 (bot_id, user_id, uid)：唯一约束是 upsert_by_uid 原子性的
        # 数据库级兜底（旧库由 ensure_ark_user_unique_index 去重后补建索引）
        UniqueConstraint("bot_id", "user_id", "uid", name="ux_ark_user_bot_uid"),
        {"extend_existing": True},
    )

    # 游戏 UID
    uid: str = Field(default="", title="明日方舟 UID")
    # 角色昵称（登录时从森空岛 binding 的 defaultRole.nickname 写入）
    nickname: str = Field(default="", title="昵称")
    # 森空岛 Cred（User 基类已有 cookie 字段，这里保留同步一份做查询索引）
    cookie: str = Field(default="", title="森空岛 Cred")

    # 游戏 ID（实际是 channelMasterId；1=明日方舟官服）
    game_id: int = Field(
        default=ARKNIGHTS_CHANNEL_OFFICIAL,
        title="GameID",
        nullable=False,
        sa_column_kwargs={"server_default": "1"},
    )

    # 认证信息
    token: str | None = Field(default=None, title="Skland API Token（短期）")
    hg_token: str = Field(default="", title="HyperGryph Login Token（抽卡用）")
    platform: str = Field(default="3", title="平台 ID")

    # 森空岛账号 ID（用于抽卡 OAuth 链换 u8_token）
    record_id: str | None = Field(default=None, title="森空岛账号 ID")
    server_id: str = Field(default="1", title="服务器 ID")
    skland_user_id: str = Field(default="", title="森空岛用户 ID")

    # Cookie 状态（""=正常, "无效"=失效）
    cookie_status: str = Field(default="", title="Cookie 状态")

    # 用户偏好
    hide_uid_self_value: str = Field(default="", title="隐藏 UID（用户级，覆盖全局）")

    # 时间戳（created/last_used 等使用统计字段从未接线，已移除；
    # last_cred_request_time 由 update_token_by_cred 写入，token 新鲜度记录）
    last_cred_request_time: int | None = Field(
        default=None, title="上次请求 cred 时间（秒）"
    )

    @classmethod
    @with_session
    async def upsert_by_uid(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str,
        **data,
    ) -> bool:
        """按 (user_id, bot_id, uid) 原子 upsert；重登幂等且不影响其他账号行。

        不能用基类 insert_data：它的 update 分支（base_models.update_data）
        只按 (user_id, bot_id) 更新，多账号用户重登其中一个账号时会把该用户
        **全部**账号行改写成当前 uid（凭据互相覆盖 + 留下重复行）。

        并发安全：UPDATE 未命中 → savepoint 内 INSERT，撞唯一索引（另一
        协程刚插入同键）时回滚 savepoint 并确认竞争者行确实存在才收敛为
        UPDATE；**其他**完整性错误（长度/非空/外键等）如实上抛，不吞。
        返回 False = 未确认落库，调用方不得当成功。
        """
        stmt = (
            update(cls)
            .where(
                cls.user_id == user_id,
                cls.bot_id == bot_id,
                cls.uid == uid,
            )
            .values(**data)
            .execution_options(synchronize_session="fetch")
        )
        result = await session.execute(stmt)
        if result.rowcount:
            return True
        try:
            # savepoint：撞唯一约束时只回滚本次 INSERT，共享事务里
            # 此前协程的未提交改动不被牵连（旧实现 rollback 整段事务）
            async with session.begin_nested():
                session.add(cls(user_id=user_id, bot_id=bot_id, uid=uid, **data))
                await session.flush()
                return True
        except IntegrityError:
            # savepoint 已由 begin_nested 回滚；是否真是并发插入竞态：
            # 竞争者行必须已存在，否则属于其他完整性错误（字段非法等），
            # 如实上抛而不是伪装成功
            check = await session.execute(
                select(cls).where(
                    cls.user_id == user_id,
                    cls.bot_id == bot_id,
                    cls.uid == uid,
                )
            )
            if check.scalars().first() is None:
                raise
            result = await session.execute(stmt)
            return bool(result.rowcount)

    @classmethod
    @with_session
    async def select_data_by_cred(
        cls,
        session: AsyncSession,
        cred: str,
        game_id: int | None = ARKNIGHTS_CHANNEL_OFFICIAL,
    ) -> ArkUser | None:
        """通过 cred 查询用户行。

        cookie 落库为密文（enc:v1:，见 utils/crypto），无法 SQL 等值匹配；
        按 game_id 缩小范围后在 Python 侧解密比对（行数为账号数，量级小）。
        当前消费方是加密回归测试（test_cred_encryption）；曾规划的
        token 缓存索引未采用（token 回写走 update_token_by_cred）。
        """
        filters = []
        if game_id is not None:
            filters.append(cls.game_id == game_id)
        result = await session.execute(select(cls).where(*filters))
        data = result.scalars().all()
        for row in data:
            if cred_equals(row.cookie, cred):
                return row
        return None

    @classmethod
    @with_session
    async def update_token_by_cred(
        cls,
        session: AsyncSession,
        cred: str,
        token: str,
        game_id: int | None = ARKNIGHTS_CHANNEL_OFFICIAL,
        cred_time: int | None = None,
    ) -> int:
        """按 cred 更新 token（refresh 回写）。返回更新行数。

        token 是短期凭据：请求链路 refresh 成功后回写；cred_time 一并写入
        last_cred_request_time（对齐 EndUID，作为 token 新鲜度记录）。
        cookie 密文落库 → 同 select_data_by_cred，Python 侧解密比对。
        """
        filters = []
        if game_id is not None:
            filters.append(cls.game_id == game_id)
        result = await session.execute(select(cls).where(*filters))
        rows = result.scalars().all()
        matched = 0
        try:
            for row in rows:
                if not cred_equals(row.cookie, cred):
                    continue
                row.token = encrypt_secret(token)
                if cred_time is not None:
                    row.last_cred_request_time = cred_time
                matched += 1
        except EncryptionUnavailableError as exc:
            # 加密不可用时放弃回写：本次请求继续用内存里的 token，
            # 不明文落库（下次 refresh 会再尝试回写）
            logger.warning(f"[ArknightsUID·数据库] token 回写被拒绝: {exc}")
            return 0
        return matched

    @classmethod
    @with_session
    async def mark_invalid_by_cred(
        cls,
        session: AsyncSession,
        cred: str,
        game_id: int | None = ARKNIGHTS_CHANNEL_OFFICIAL,
    ) -> int:
        """按 cred 标记 cookie_status=无效（refresh 被拒 = cred 已死）。

        返回更新行数（EndUID 回退账号无本地行时为 0，静默即可）。
        """
        filters = []
        if game_id is not None:
            filters.append(cls.game_id == game_id)
        result = await session.execute(select(cls).where(*filters))
        rows = result.scalars().all()
        matched = 0
        for row in rows:
            if not cred_equals(row.cookie, cred):
                continue
            row.cookie_status = "无效"
            matched += 1
        return matched

    @classmethod
    @with_session
    async def reencrypt_plaintext_credentials(cls, session: AsyncSession) -> int:
        """存量明文凭据一次性重加密（审计 #4 收尾）。

        decrypt_secret 兼容读取历史明文，但明文行在"数据库文件被非授权
        读取"的威胁模型下仍是裸奔——启动时把 cookie/token/hg_token 中的
        明文值升级为 enc:v1 密文（幂等：已加密的行不动）。加密不可用时
        跳过本轮（下次启动再试），不影响启动。返回升级行数。
        """
        result = await session.execute(select(cls))
        rows = result.scalars().all()
        upgraded = 0
        for row in rows:
            changed = False
            for field in ("cookie", "token", "hg_token"):
                value = getattr(row, field, None)
                if value and not value.startswith(ENC_PREFIX):
                    try:
                        setattr(row, field, encrypt_secret(value))
                        changed = True
                    except EncryptionUnavailableError as exc:
                        logger.warning(
                            "[ArknightsUID·加密] 存量明文重加密被跳过"
                            f"（{exc}），下次启动再试"
                        )
                        return upgraded
            if changed:
                upgraded += 1
        if upgraded:
            logger.info(f"[ArknightsUID·加密] 存量明文凭据已重加密 {upgraded} 行")
        return upgraded

    @classmethod
    @with_session
    async def delete_by_user(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        uid: str = "",
    ) -> int:
        """删除该用户的 ArkUser 行；uid 非空则只删指定 uid 行。返回删除行数。"""
        stmt = select(cls).where(cls.user_id == user_id, cls.bot_id == bot_id)
        if uid:
            stmt = stmt.where(cls.uid == uid)
        result = await session.execute(stmt)
        rows = result.scalars().all()
        for row in rows:
            await session.delete(row)
        return len(rows)


class ArkBind(Bind, table=True):
    """明日方舟 UID 绑定表。

    继承 GsCore `Bind` 基类，已自带 user_id / bot_id / uid / group_id 字段。
    这里只追加明日方舟专属的索引。
    """

    __tablename__ = "ArkBind"
    __table_args__ = (
        Index("idx_ark_bind_uid", "uid"),
        {"extend_existing": True},
    )

    # `uid` 字段由 Bind 基类提供，存明日方舟 UID
    uid: str | None = Field(default=None, title="明日方舟 UID")

    # 曾有 get_bound_uid（取首个绑定 UID）：多账号下恒返回第一行导致
    # 配置写错账号，已由 resolve_accounts + 默认账号口径取代并删除


class ArkAlias(SQLModel, table=True):
    """用户自定义干员别名表（ark别名 银羽毛=银灰）。

    键为 (bot_id, user_id, alias)，别名统一小写存储（查询侧也小写化），
    target 存干员中文名（设置时校验过唯一可解析）。
    """

    __tablename__ = "ArkAlias"
    __table_args__ = (
        Index("idx_ark_alias_user", "bot_id", "user_id"),
        # (bot_id, user_id, alias) 唯一：set_alias 撞约束收敛的数据库级
        # 兜底（旧库由 ensure_ark_alias_unique_index 去重后补建索引）
        UniqueConstraint("bot_id", "user_id", "alias", name="ux_ark_alias_key"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    bot_id: str = Field(default="", title="Bot ID")
    user_id: str = Field(default="", title="用户 ID")
    alias: str = Field(default="", title="自定义别名（小写）", index=True)
    target: str = Field(default="", title="指向干员中文名")

    @classmethod
    @with_session
    async def set_alias(
        cls,
        session: AsyncSession,
        bot_id: str,
        user_id: str,
        alias: str,
        target: str,
    ) -> None:
        """设置别名（同键覆盖）。

        原子化（审计 #37）：旧实现"先查后插"，并发设置同一别名会产生
        重复行（.first() 命中不确定）。改为 UPDATE 未命中 → INSERT flush
        撞唯一约束（另一协程刚插入）→ 确认竞争者行存在后收敛为 UPDATE；
        其他完整性错误如实上抛。
        """
        stmt = (
            update(cls)
            .where(
                cls.bot_id == bot_id,
                cls.user_id == user_id,
                cls.alias == alias,
            )
            .values(target=target)
            .execution_options(synchronize_session="fetch")
        )
        result = await session.execute(stmt)
        if result.rowcount:
            await session.commit()
            return
        try:
            # savepoint（同 upsert_by_uid：只回滚本次 INSERT，不牵连共享事务）
            async with session.begin_nested():
                session.add(
                    cls(bot_id=bot_id, user_id=user_id, alias=alias, target=target)
                )
                await session.flush()
        except IntegrityError:
            # savepoint 已回滚；竞争者行存在 → 收敛为 UPDATE，否则如实上抛
            check = await session.execute(
                select(cls).where(
                    cls.bot_id == bot_id,
                    cls.user_id == user_id,
                    cls.alias == alias,
                )
            )
            if check.scalars().first() is None:
                raise
            await session.execute(stmt)
        await session.commit()

    @classmethod
    @with_session
    async def delete_alias(
        cls, session: AsyncSession, bot_id: str, user_id: str, alias: str
    ) -> bool:
        """删除别名；不存在返回 False。"""
        result = await session.execute(
            select(cls).where(
                cls.bot_id == bot_id,
                cls.user_id == user_id,
                cls.alias == alias,
            )
        )
        existing = result.scalars().first()
        if existing is None:
            return False
        await session.delete(existing)
        await session.commit()
        return True

    @classmethod
    @with_session
    async def list_aliases(
        cls, session: AsyncSession, bot_id: str, user_id: str
    ) -> list[ArkAlias]:
        """该用户的全部别名。"""
        result = await session.execute(
            select(cls).where(cls.bot_id == bot_id, cls.user_id == user_id)
        )
        return list(result.scalars().all())

    @classmethod
    @with_session
    async def resolve_alias(
        cls, session: AsyncSession, bot_id: str, user_id: str, alias: str
    ) -> str | None:
        """别名 → 干员中文名；无匹配返回 None（精确匹配，小写化）。"""
        result = await session.execute(
            select(cls).where(
                cls.bot_id == bot_id,
                cls.user_id == user_id,
                cls.alias == (alias or "").strip().lower(),
            )
        )
        existing = result.scalars().first()
        return existing.target if existing else None


class ArkCommandAlias(SQLModel, table=True):
    """主人自定义指令别名表（ark指令别名 招募=公招推荐）。

    全局生效（不分 bot/用户），alias 唯一。target 为目标命令词（不带
    ark/方舟 前缀）；target 为空串是 tombstone——用于停用内置别名
    （内置别名不落库，删除时写 tombstone，重启不复活）。
    """

    __tablename__ = "ArkCommandAlias"
    __table_args__ = (
        UniqueConstraint("alias", name="ux_ark_cmd_alias"),
        {"extend_existing": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    alias: str = Field(default="", title="触发词（小写）", index=True)
    target: str = Field(default="", title="目标命令词（空=停用内置别名）")

    @classmethod
    @with_session
    async def set_alias(cls, session: AsyncSession, alias: str, target: str) -> None:
        """设置别名（同键覆盖，原子 upsert）。"""
        stmt = (
            update(cls)
            .where(cls.alias == alias)
            .values(target=target)
            .execution_options(synchronize_session="fetch")
        )
        result = await session.execute(stmt)
        if result.rowcount:
            await session.commit()
            return
        session.add(cls(alias=alias, target=target))
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            await session.execute(stmt)
        await session.commit()

    @classmethod
    @with_session
    async def delete_alias(cls, session: AsyncSession, alias: str) -> bool:
        """删除别名行；不存在返回 False。"""
        result = await session.execute(select(cls).where(cls.alias == alias))
        existing = result.scalars().first()
        if existing is None:
            return False
        await session.delete(existing)
        await session.commit()
        return True

    @classmethod
    @with_session
    async def list_aliases(cls, session: AsyncSession) -> list[ArkCommandAlias]:
        """全部别名行（含 tombstone）。"""
        result = await session.execute(select(cls))
        return list(result.scalars().all())


@on_core_start
async def _ark_auto_migrate() -> None:
    """启动时自动补列（热重载安全）。

    `exec_list` 只在冷启动跑，热重载（reload_plugin）不重跑。
    本钩子通过对比"模型列 vs 实表列"补齐缺的列，幂等。
    随后为 ArkUser 补建 (bot_id, user_id, uid) 唯一索引（先清理旧版
    update_data 全行覆盖 bug 留下的重复行），幂等。
    """
    await auto_add_missing_columns(__name__, log_prefix="[ARK·补列]")
    await ensure_ark_user_unique_index(log_prefix="[ARK·唯一索引]")
    await ensure_ark_alias_unique_index()
    # 密钥首建/自愈的同步等待在线程内完成（避免落回事件循环）
    await crypto_warmup()
    # 存量明文凭据升级为密文（幂等；加密不可用时下轮启动再试）
    try:
        await ArkUser.reencrypt_plaintext_credentials()
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·加密] 存量明文重加密异常（不影响启动）: "
            f"{exc.__class__.__name__}"
        )
