"""自有账号表（ArkUser / ArkBind）读写。

不依赖 ArkCredential（避免与 adapter 循环 import）——list_local_accounts 直接
返回 ArkUser ORM 对象，由 adapter.resolve_accounts 映射成 ArkCredential。
"""

from __future__ import annotations

import asyncio
from typing import Any

from gsuid_core.logger import logger

from ..crypto import EncryptionUnavailableError, encrypt_secret
from ..database.models import ArkBind as ArkBind, ArkUser
from .auth import list_ark_roles_by_cred


async def list_local_accounts(user_id: str, bot_id: str) -> list[ArkUser]:
    """读取该用户在自有表里的全部明日方舟（game_id==1）账号。"""
    users = await ArkUser.account_rows(user_id=user_id, bot_id=bot_id)
    if users is None:
        raise RuntimeError("local account source unavailable")
    return [
        u for u in users
        if (u.game_id or 1) == 1 and not getattr(u, "login_disabled", False)
    ]


async def disabled_account_uids(user_id: str, bot_id: str) -> set[str]:
    """读取失败不可放开 EndUID 回退。"""
    users = await ArkUser.account_rows(user_id=user_id, bot_id=bot_id)
    if users is None:
        raise RuntimeError("account source state unavailable")
    return {
        str(row.uid) for row in users
        if (row.game_id or 1) == 1 and getattr(row, "login_disabled", False)
    }


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
        saved = await ArkUser.save_login_roles(
            user_id=user_id, bot_id=bot_id, group_id=group_id,
            roles=valid_roles, cookie=enc_cred, hg_token=enc_hg_token,
            skland_user_id=skland_user_id,
        )
        if not saved:
            return None, "登录信息保存失败（数据库写入异常），请稍后重试。"
    logger.info(
        f"[ArknightsUID·绑定] 用户 {user_id} 写入 {len(valid_roles)} 个方舟角色"
    )
    return valid_roles, None


async def remove_ark_login(user_id: str, bot_id: str, uid: str = "") -> int | None:
    """解除本插件登录；不删除 EndUID 原记录。None 表示读取/写入失败。"""
    from ..enduid.adapter import resolve_accounts, resolve_enduid_accounts

    async with await _bind_write_lock(bot_id, user_id):
        if uid:
            resolved = await resolve_accounts(user_id, bot_id)
            if not resolved.installed:
                return None if resolved.reason.startswith("读取") else 0
            visible = {account.uid for account in resolved.accounts} | set(resolved.uids)
            if uid not in visible:
                return 0
            targets = [uid]
        else:
            # 也解除被本地账号遮蔽的 EndUID 角色，避免下一次查询复活。
            try:
                local = await list_local_accounts(user_id, bot_id)
                blocked = await disabled_account_uids(user_id, bot_id)
                external = await resolve_enduid_accounts(user_id, bot_id)
            except Exception as exc:
                logger.warning(
                    "[ArknightsUID·绑定] 读取账号来源失败，未解除: "
                    f"{exc.__class__.__name__}"
                )
                return None
            targets = list(dict.fromkeys([
                *(row.uid for row in local if row.uid),
                *(account.uid for account in external.accounts), *external.uids,
            ]))
            targets = [target for target in targets if target not in blocked]
        if not targets:
            return 0
        return await ArkUser.disable_logins(user_id, bot_id, targets)


async def remove_invalid_ark_login(user_id: str, bot_id: str, uid: str) -> int | None:
    """维护清理仅解除仍标为无效的方舟行；不覆盖并发重登。"""
    if not uid.isdigit() or not user_id or not bot_id:
        return 0
    async with await _bind_write_lock(bot_id, user_id):
        return await ArkUser.disable_logins(user_id, bot_id, [uid], only_invalid=True)
