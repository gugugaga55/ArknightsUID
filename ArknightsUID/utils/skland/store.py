"""自有账号表（ArkUser / ArkBind）读写。

不依赖 ArkCredential（避免与 adapter 循环 import）——list_local_accounts 直接
返回 ArkUser ORM 对象，由 adapter.resolve_accounts 映射成 ArkCredential。
"""

from __future__ import annotations

import asyncio
from typing import Any

from gsuid_core.logger import logger

from ..crypto import EncryptionUnavailableError, encrypt_secret
from ..database.models import ArkBind, ArkUser
from .auth import list_ark_roles_by_cred


async def list_local_accounts(user_id: str, bot_id: str) -> list[ArkUser]:
    """读取该用户在自有表里的全部明日方舟（game_id==1）账号。"""
    users = await ArkUser.select_data_list(user_id=user_id, bot_id=bot_id) or []
    return [u for u in users if (u.game_id or 1) == 1]


# ArkBind.insert_uid 是"查列表 → 拼接 → 整行覆盖"的非原子读改写：
# 同一用户并发两次登录（扫码轮询期间又发 ark绑定）会互相整行覆盖、
# 丢 uid。按 (bot_id, user_id) 串行化账号与绑定的整个写入/删除过程，
# 防止并发解绑在账号写完、绑定未写时插入，留下没有账号的孤立绑定。
_bind_locks: dict[tuple[str, str], asyncio.Lock] = {}
_bind_locks_guard = asyncio.Lock()


async def _bind_write_lock(bot_id: str, user_id: str) -> asyncio.Lock:
    async with _bind_locks_guard:
        lock = _bind_locks.get((bot_id, user_id))
        if lock is None:
            lock = asyncio.Lock()
            _bind_locks[(bot_id, user_id)] = lock
        return lock


async def persist_ark_login(
    user_id: str,
    bot_id: str,
    group_id: Any,
    cred: str,
    hg_token: str = "",
    skland_user_id: str = "",
) -> tuple[list[dict[str, str]] | None, str | None]:
    """登录成功后拉绑定 → 写 ArkUser + ArkBind（幂等 upsert）。

    返回 (roles, None) 成功，或 (None, 错误文案) 失败。
    """
    roles = await list_ark_roles_by_cred(cred)
    if roles is None:
        return None, "拉取森空岛绑定信息失败，请检查凭据是否正确或稍后重试。"
    if not roles:
        return None, "未找到明日方舟账号（该森空岛账号下没有 gameId=1 的方舟角色）。"

    # uid 会作为磁盘缓存目录段（PLAYER_PATH / uid），入库前强制纯数字，
    # 阻断被污染的 API 响应构造路径逃逸
    valid_roles: list[dict[str, str]] = []
    for role in roles:
        if str(role.get("role_id") or "").isdigit():
            valid_roles.append(role)
        else:
            logger.warning(
                "[ArknightsUID·绑定] 跳过非法 role_id（非纯数字），"
                f"疑似响应被污染：{str(role.get('role_id'))[:12]!r}"
            )
    if not valid_roles:
        return None, "森空岛返回的账号数据异常，已拒绝写入。"

    # 加密在写库之前一次性完成：密钥不可用时拒绝登录落库（不留半写状态），
    # 错误文案引导修复密钥——静默明文降级会把 cred 直接写进数据库文件
    try:
        enc_cred = encrypt_secret(cred)
        enc_hg_token = encrypt_secret(hg_token)
    except EncryptionUnavailableError as exc:
        logger.warning(f"[ArknightsUID·绑定] 登录信息未保存: {exc}")
        return None, (
            "本地凭据加密初始化失败，为避免明文存储已取消保存登录信息。\n"
            "请检查插件数据目录下 ark_cred_key 密钥文件"
            "（或 ARK_UID_ENC_KEY 环境变量）后重试。"
        )

    async with await _bind_write_lock(bot_id, user_id):
        for role in valid_roles:
            # 按 (user_id, bot_id, uid) upsert：只更新当前账号行。
            # 不能用基类 insert_data——它的 update 分支只按 (user_id, bot_id)
            # 更新，会把该用户全部账号行改写成当前 uid（多账号凭据互相覆盖）。
            # cred / hg_token 是长期凭据，落库前加密（见 utils/crypto）。
            saved = await ArkUser.upsert_by_uid(
                user_id=user_id,
                bot_id=bot_id,
                uid=role["role_id"],
                nickname=role["nickname"],
                cookie=enc_cred,
                hg_token=enc_hg_token,
                skland_user_id=skland_user_id,
                record_id=role["record_uid"],
                server_id=role["server_id"],
                game_id=1,
                cookie_status="",
                platform="3",
            )
            if not saved:
                logger.warning(
                    f"[ArknightsUID·绑定] 账号 {role['role_id']} 写入未确认，"
                    "中止本轮保存（已写入的 role 保留，凭据未落库的账号请重试登录）"
                )
                return None, "登录信息保存失败（数据库写入异常），请稍后重试。"
            # insert_uid 自动去重（多 uid 用 _ 拼接），重复绑返回 -2 无害；
            # 与并发登录/退订互斥，防整行覆盖丢 uid
            await ArkBind.insert_uid(
                user_id, bot_id, role["role_id"], group_id=group_id
            )
    logger.info(
        f"[ArknightsUID·绑定] 用户 {user_id} 写入 {len(valid_roles)} 个方舟角色"
    )
    return valid_roles, None


async def remove_ark_login(user_id: str, bot_id: str, uid: str = "") -> int:
    """删除自有登录；确认账号删除成功后再清绑定，同用户登录/解绑互斥。"""
    async with await _bind_write_lock(bot_id, user_id):
        uids = [uid] if uid else [
            user.uid for user in await list_local_accounts(user_id, bot_id) if user.uid
        ]
        if uid:
            deleted = await ArkUser.delete_by_user(user_id, bot_id, uid=uid)
        else:
            deleted = await ArkUser.delete_by_user(user_id, bot_id)
        if deleted is None:
            logger.error(
                f"[ArknightsUID·绑定] 删除登录时数据库不可用 user={user_id} "
                f"bot={bot_id}，绑定关系保留"
            )
            return 0
        if deleted:
            for target_uid in uids:
                await ArkBind.delete_uid(user_id, bot_id, target_uid)
        return deleted
