from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any

from gsuid_core.logger import logger

try:
    from ArknightsUID.ArknightsUID.utils.config import ArkConfig
    from ArknightsUID.ArknightsUID.utils.crypto import decrypt_secret
    from ArknightsUID.ArknightsUID.utils.database.models import ArkUser
    from ArknightsUID.ArknightsUID.utils.skland import client as local_skland
    from ArknightsUID.ArknightsUID.utils.skland import store as ark_store
except ModuleNotFoundError:
    from ArknightsUID.utils.config import ArkConfig
    from ArknightsUID.utils.crypto import decrypt_secret
    from ArknightsUID.utils.database.models import ArkUser
    from ArknightsUID.utils.skland import client as local_skland
    from ArknightsUID.utils.skland import store as ark_store

# 复用 skland 模块定义的游戏 ID，避免双处维护导致的不一致。
ARKNIGHTS_GAME_ID = local_skland.ARKNIGHTS_GAME_ID
ENDUID_MISSING_HINT = (
    "未找到明日方舟账号。请使用 ark绑定 登录，或安装 EndUID 复用其森空岛登录。"
)


@dataclass(slots=True)
class ArkCredential:
    uid: str
    nickname: str
    server_id: str
    cookie_status: str
    cred: str
    token: str
    hg_token: str
    skland_user_id: str
    # 森空岛账号 uid（抽卡 OAuth 链 u8_token_by_uid 用，非游戏内 UID）
    record_id: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.cred) and self.cookie_status != "无效"


@dataclass(slots=True)
class AccountResolveResult:
    installed: bool
    reason: str
    uids: list[str]
    accounts: list[ArkCredential]


def hide_uid(uid: str, user_pref: str = "") -> str:
    """UID 脱敏。user_pref: "on" 强制隐藏 / "off" 强制不隐藏 / "" 跟随全局 HideUid。"""
    pref = user_pref or ""
    uid_str = str(uid) if uid is not None else ""
    if pref == "off":
        return uid_str
    if pref != "on":
        try:
            if not ArkConfig.get_config("HideUid").data:
                return uid_str
        except Exception:
            # 配置层异常不阻断消息输出，退回脱敏（隐私优先）
            pass
    if len(uid_str) <= 4:
        return uid_str
    if len(uid_str) <= 8:
        return f"{uid_str[:2]}***{uid_str[-2:]}"
    return f"{uid_str[:3]}***{uid_str[-3:]}"


async def get_hide_uid_pref(uid: str, user_id: str, bot_id: str) -> str:
    """读 ArkUser.hide_uid_self_value；无记录/异常回空（跟随全局）。"""
    try:
        user = await ArkUser.base_select_data(
            uid=uid, user_id=user_id, bot_id=bot_id
        )
        return (getattr(user, "hide_uid_self_value", "") or "") if user else ""
    except Exception:
        return ""


async def hide_uid_for(uid: str, user_id: str, bot_id: str) -> str:
    """带用户级覆盖的脱敏（「ark设置隐藏UID」写入的 hide_uid_self_value）。"""
    return hide_uid(uid, await get_hide_uid_pref(uid, user_id, bot_id))


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _as_game_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _split_uid_list(value: Any) -> list[str]:
    if not value:
        return []
    uids: list[str] = []
    for item in str(value).split("_"):
        uid = item.strip()
        if uid and uid not in uids:
            uids.append(uid)
    return uids


def _exception_type(exc: Exception) -> str:
    # import 失败的异常 message 可能携带环境路径/凭据（零泄漏测试背书），只打类名
    return exc.__class__.__name__


def _import_first(module_names: tuple[str, ...]) -> ModuleType | None:
    for module_name in module_names:
        try:
            return import_module(module_name)
        except ModuleNotFoundError as exc:
            logger.debug(
                "[ArknightsUID] 可选模块未找到: "
                f"{module_name} ({_exception_type(exc)})"
            )
        except Exception as exc:
            logger.warning(
                "[ArknightsUID] 导入可选模块失败: "
                f"{module_name}: {_exception_type(exc)}"
            )
    return None


def _enduid_models() -> ModuleType | None:
    return _import_first(
        (
            "EndUID.utils.database.models",
            "plugins.EndUID.EndUID.utils.database.models",
            "gsuid_core.plugins.EndUID.EndUID.utils.database.models",
        )
    )


async def resolve_accounts(user_id: str, bot_id: str) -> AccountResolveResult:
    # 自有 ArkUser/ArkBind 优先（脱离 EndUID 硬依赖）
    local_users = await ark_store.list_local_accounts(user_id, bot_id)
    if local_users:
        uids: list[str] = []
        accounts: list[ArkCredential] = []
        seen: set[str] = set()
        for row in local_users:
            uid = (getattr(row, "uid", "") or "").strip()
            if uid and uid not in uids:
                uids.append(uid)
            if not uid or uid in seen:
                continue
            seen.add(uid)
            accounts.append(
                ArkCredential(
                    uid=uid,
                    nickname=getattr(row, "nickname", "") or "",
                    server_id=getattr(row, "server_id", "") or "1",
                    cookie_status=getattr(row, "cookie_status", "") or "",
                    # cookie/token/hg_token 密文落库（enc:v1:），读取时解密；
                    # EndUID 回退行是明文，decrypt_secret 对无前缀值原样透传
                    cred=decrypt_secret(getattr(row, "cookie", "") or ""),
                    token=decrypt_secret(getattr(row, "token", None) or ""),
                    hg_token=decrypt_secret(getattr(row, "hg_token", "") or ""),
                    skland_user_id=getattr(row, "skland_user_id", "") or "",
                    record_id=str(getattr(row, "record_id", "") or ""),
                )
            )
        if accounts:
            return AccountResolveResult(True, "", uids, accounts)

    # EndUID 回退
    models = _enduid_models()
    if models is None:
        return AccountResolveResult(False, ENDUID_MISSING_HINT, [], [])

    EndBind = models.EndBind
    EndUser = models.EndUser

    bind = await EndBind.get_data_by_user_id(user_id, bot_id)
    uids: list[str] = []
    if bind is not None:
        uids = _split_uid_list(getattr(bind, "ark_uid", ""))

    users = await EndUser.select_data_list(user_id=user_id, bot_id=bot_id) or []
    accounts: list[ArkCredential] = []
    seen_accounts: set[str] = set()
    for user in users:
        if _as_game_id(getattr(user, "game_id", None)) != ARKNIGHTS_GAME_ID:
            continue
        uid = _as_text(getattr(user, "uid", "")).strip()
        if uid and uid not in uids:
            uids.append(uid)
        if not uid or uid in seen_accounts:
            continue
        seen_accounts.add(uid)
        accounts.append(
            ArkCredential(
                uid=uid,
                nickname=_as_text(getattr(user, "nickname", "")),
                server_id=_as_text(getattr(user, "server_id", "")) or "1",
                cookie_status=_as_text(getattr(user, "cookie_status", "")),
                cred=decrypt_secret(_as_text(getattr(user, "cookie", ""))),
                token=decrypt_secret(_as_text(getattr(user, "token", ""))),
                hg_token=decrypt_secret(_as_text(getattr(user, "hg_token", ""))),
                skland_user_id=_as_text(getattr(user, "skland_user_id", "")),
                record_id=_as_text(getattr(user, "record_id", "")),
            )
        )

    if not uids and not accounts:
        return AccountResolveResult(True, "未找到 EndUID 记录的明日方舟账号，请先使用 EndUID 登录森空岛。", [], [])

    order = {uid: index for index, uid in enumerate(uids)}
    accounts.sort(key=lambda item: order.get(item.uid, len(order)))
    return AccountResolveResult(True, "", uids, accounts)


async def resolve_credential(
    user_id: str,
    bot_id: str,
    uid: str = "",
    index: int = 0,
) -> ArkCredential | str:
    result = await resolve_accounts(user_id, bot_id)
    if not result.installed:
        return result.reason
    if not result.accounts:
        if result.uids:
            shown = "、".join(hide_uid(item) for item in result.uids)
            return f"已找到明日方舟 UID：{shown}，但没有找到对应的 EndUID 凭据记录，请重新使用 EndUID 登录。"
        return result.reason

    if index > 0:
        if index > len(result.accounts):
            return f"未找到第 {index} 个明日方舟账号，请使用 ark绑定列表 查看可用账号。"
        account = result.accounts[index - 1]
        if account.usable:
            return account
        return "该账号的 EndUID 凭据为空或已失效，请重新使用 EndUID 登录。"

    if uid:
        for account in result.accounts:
            if account.uid == uid:
                if account.usable:
                    return account
                return "该账号的 EndUID 凭据为空或已失效，请重新使用 EndUID 登录。"
        return f"未找到 UID {hide_uid(uid)} 的可用凭据。"

    # ark切换 设置的用户级默认账号（未显式指定 uid/序号时生效）
    from ..account_switch import get_default_uid

    default_uid = get_default_uid(bot_id, user_id)
    if default_uid:
        for account in result.accounts:
            if account.uid == default_uid:
                if account.usable:
                    return account
                return (
                    "默认账号的凭据为空或已失效，"
                    "请重新 ark绑定 登录，或用 ark切换 换一个账号。"
                )
        # 默认指向的账号已不存在（删除登录等）：落回第一个可用账号
        logger.info(
            f"[ArknightsUID] 默认账号 {hide_uid(default_uid)} 不存在，回退首个可用账号"
        )

    for account in result.accounts:
        if account.usable:
            return account

    return "已找到明日方舟账号，但 EndUID 凭据为空或已失效，请重新使用 EndUID 登录。"
async def fetch_player_info(credential: ArkCredential) -> dict[str, Any] | str:
    """玩家信息（核心富数据 dump）。本地 Skland 客户端直连。

    v2 起不再回退 EndUID 请求：本地实现已覆盖全部接口，"本地失败再试
    EndUID"会把凭据失效/网络故障也当成可回退场景，双倍延迟且掩盖问题。
    EndUID 仅剩账号数据（resolve_accounts）的读取回退。
    """
    response = await local_skland.get_player_info(credential)
    if response is None:
        return "森空岛玩家信息接口请求失败。"
    return response


async def ark_attendance(credential: ArkCredential) -> dict[str, Any] | str:
    """明日方舟每日签到。"""
    response = await local_skland.ark_attendance(credential)
    if response is None:
        return "森空岛签到接口请求失败。"
    return response


async def fetch_cultivate_character(
    credential: ArkCredential, character_id: str
) -> dict[str, Any] | str:
    """单干员养成材料（按 characterId）。"""
    response = await local_skland.get_cultivate_character(
        credential, character_id
    )
    if response is None:
        return "森空岛单干员养成材料接口请求失败。"
    return response


async def fetch_cultivate_player(credential: ArkCredential) -> dict[str, Any] | str:
    """玩家养成进度（按 uid）。"""
    response = await local_skland.get_cultivate_player(credential)
    if response is None:
        return "森空岛玩家养成进度接口请求失败。"
    return response
