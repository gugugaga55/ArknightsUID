"""自有账号表（ArkUser / ArkBind）读写。

不依赖 ArkCredential（避免与 adapter 循环 import）——list_local_accounts 直接
返回 ArkUser ORM 对象，由 adapter.resolve_accounts 映射成 ArkCredential。
"""
from __future__ import annotations

from typing import Any

from gsuid_core.logger import logger

try:
    from ArknightsUID.ArknightsUID.utils.database.models import ArkBind, ArkUser
    from ArknightsUID.ArknightsUID.utils.skland.auth import list_ark_roles_by_cred
except ModuleNotFoundError:
    from ArknightsUID.utils.database.models import ArkBind, ArkUser
    from ArknightsUID.utils.skland.auth import list_ark_roles_by_cred


async def list_local_accounts(user_id: str, bot_id: str) -> list[ArkUser]:
    """读取该用户在自有表里的全部明日方舟（game_id==1）账号。"""
    users = await ArkUser.select_data_list(user_id=user_id, bot_id=bot_id) or []
    return [u for u in users if (u.game_id or 1) == 1]


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

    for role in roles:
        # insert_data 内部 upsert：存在（按 uid）则 update，否则 insert。重复登录幂等。
        await ArkUser.insert_data(
            user_id=user_id,
            bot_id=bot_id,
            uid=role["role_id"],
            nickname=role["nickname"],
            cookie=cred,
            hg_token=hg_token,
            skland_user_id=skland_user_id,
            record_id=role["record_uid"],
            server_id=role["server_id"],
            game_id=1,
            cookie_status="",
            platform="3",
        )
        # insert_uid 自动去重（多 uid 用 _ 拼接），重复绑返回 -2 无害
        await ArkBind.insert_uid(user_id, bot_id, role["role_id"], group_id=group_id)
    logger.info(
        f"[ArknightsUID·绑定] 用户 {user_id} 写入 {len(roles)} 个方舟角色"
    )
    return roles, None


async def remove_ark_login(user_id: str, bot_id: str, uid: str = "") -> int:
    """删除自有登录。uid 非空删指定 uid，否则删该用户全部。返回处理的账号数。"""
    if uid:
        await ArkUser.delete_by_user(user_id, bot_id, uid=uid)
        await ArkBind.delete_uid(user_id, bot_id, uid)
        return 1

    users = await list_local_accounts(user_id, bot_id)
    for u in users:
        if u.uid:
            await ArkBind.delete_uid(user_id, bot_id, u.uid)
    count = await ArkUser.delete_by_user(user_id, bot_id)
    return count
