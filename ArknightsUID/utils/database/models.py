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

from typing import List, Optional

from sqlmodel import Field, select, col
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import Index, or_

from gsuid_core.utils.database.base_models import Bind, User, with_session
from gsuid_core.server import on_core_start

from ..api.request_util import RespCode  # noqa: F401  # 保留 RespCode 引用便于迁移
from .auto_migrate import auto_add_missing_columns

# 明日方舟官服 channelMasterId（与 skland/client.py 的 ARKNIGHTS_CHANNEL_OFFICIAL 一致）
ARKNIGHTS_CHANNEL_OFFICIAL = 1


class ArkUser(User, table=True):
    """明日方舟用户表（每个 (uid, game_id) 一行）。"""

    __tablename__ = "ArkUser"
    __table_args__ = (
        Index("idx_ark_uid_user_bot", "uid", "user_id", "bot_id"),
        Index("idx_ark_last_used", "last_used_time"),
        Index("idx_ark_cookie_status", "cookie_status"),
        {"extend_existing": True},
    )

    # 游戏 UID
    uid: str = Field(default="", title="明日方舟 UID")
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
    token: Optional[str] = Field(default=None, title="Skland API Token（短期）")
    hg_token: str = Field(default="", title="HyperGryph Login Token（抽卡用）")
    platform: str = Field(default="3", title="平台 ID")

    # 森空岛账号 ID（用于抽卡 OAuth 链换 u8_token）
    record_id: Optional[str] = Field(default=None, title="森空岛账号 ID")
    server_id: str = Field(default="1", title="服务器 ID")
    skland_user_id: str = Field(default="", title="森空岛用户 ID")

    # Cookie 状态（""=正常, "无效"=失效）
    cookie_status: str = Field(default="", title="Cookie 状态")

    # 用户偏好
    hide_uid_self_value: str = Field(default="", title="隐藏 UID（用户级，覆盖全局）")

    # 时间戳
    created_time: Optional[int] = Field(default=None, title="创建时间（秒）")
    last_used_time: Optional[int] = Field(default=None, title="最后使用时间（秒）")
    last_cred_request_time: Optional[int] = Field(
        default=None, title="上次请求 cred 时间（秒）"
    )

    @classmethod
    @with_session
    async def select_data_by_cred(
        cls,
        session: AsyncSession,
        cred: str,
        game_id: Optional[int] = ARKNIGHTS_CHANNEL_OFFICIAL,
    ) -> Optional["ArkUser"]:
        """通过 cred 查询用户（用于 token 缓存索引）。"""
        filters = [cls.cookie == cred]
        if game_id is not None:
            filters.append(cls.game_id == game_id)
        result = await session.execute(select(cls).where(*filters))
        data = result.scalars().all()
        return data[0] if data else None


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
    uid: Optional[str] = Field(default=None, title="明日方舟 UID")

    @classmethod
    @with_session
    async def get_bound_uid(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
    ) -> Optional[str]:
        """取该用户绑定的第一个明日方舟 UID。"""
        result = await session.execute(
            select(cls).where(cls.user_id == user_id, cls.bot_id == bot_id)
        )
        data = result.scalars().all()
        return data[0].uid if data and data[0].uid else None


@on_core_start
async def _ark_auto_migrate() -> None:
    """启动时自动补列（热重载安全）。

    `exec_list` 只在冷启动跑，热重载（reload_plugin）不重跑。
    本钩子通过对比"模型列 vs 实表列"补齐缺的列，幂等。
    """
    await auto_add_missing_columns(__name__, log_prefix="[ARK·补列]")
