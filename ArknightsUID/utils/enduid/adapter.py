from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any

from gsuid_core.logger import logger

from ..config import ArkConfig
from ..crypto import decrypt_secret
from ..database.models import ArkUser
from ..skland import client as local_skland
from ..skland import store as ark_store

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
    source: str = "local"

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
        # 5-8 位只露首尾各 1 位：旧的"首2+尾2"对 5 位 UID 只遮 1 位，
        # 10 个候选即可复原（审计 #39）
        return f"{uid_str[:1]}***{uid_str[-1:]}"
    # 主流方舟 UID 9-10 位：首尾各 2 位（旧口径暴露 6/10 位，配合
    # 昵称/区服可枚举复原）
    return f"{uid_str[:2]}***{uid_str[-2:]}"


async def get_hide_uid_pref(uid: str, user_id: str, bot_id: str) -> str:
    """读 ArkUser.hide_uid_self_value；无记录/异常回空（跟随全局）。"""
    try:
        user = await ArkUser.base_select_data(uid=uid, user_id=user_id, bot_id=bot_id)
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
    unavailable = False
    for module_name in module_names:
        try:
            return import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name and not (
                module_name == exc.name or module_name.startswith(exc.name + ".")
            ):
                unavailable = True
            logger.debug(
                f"[ArknightsUID] 可选模块未找到: {module_name} ({_exception_type(exc)})"
            )
        except Exception as exc:
            unavailable = True
            logger.warning(
                "[ArknightsUID] 导入可选模块失败: "
                f"{module_name}: {_exception_type(exc)}"
            )
    if unavailable:
        raise RuntimeError("optional account source unavailable")
    return None


def _enduid_models() -> ModuleType | None:
    return _import_first(
        (
            "EndUID.utils.database.models",
            "plugins.EndUID.EndUID.utils.database.models",
            "gsuid_core.plugins.EndUID.EndUID.utils.database.models",
        )
    )


def _credential_from_row(row: Any, source: str) -> ArkCredential:
    return ArkCredential(
        uid=_as_text(getattr(row, "uid", "")).strip(),
        nickname=_as_text(getattr(row, "nickname", "")),
        server_id=_as_text(getattr(row, "server_id", "")) or "1",
        cookie_status=_as_text(getattr(row, "cookie_status", "")),
        cred=decrypt_secret(_as_text(getattr(row, "cookie", ""))),
        token=decrypt_secret(_as_text(getattr(row, "token", ""))),
        hg_token=decrypt_secret(_as_text(getattr(row, "hg_token", ""))),
        skland_user_id=_as_text(getattr(row, "skland_user_id", "")),
        record_id=_as_text(getattr(row, "record_id", "")), source=source,
    )


async def _read_enduid_rows(models, user_id: str, bot_id: str):
    """直接读同一 core ORM，会话错误上抛；不记录包含凭据的行 repr。"""
    from sqlmodel import select
    from gsuid_core.utils.database import base_models

    async with base_models.async_maker() as session:
        bound = await session.execute(select(models.EndBind).where(
            models.EndBind.user_id == user_id, models.EndBind.bot_id == bot_id,
        ))
        users = await session.execute(select(models.EndUser).where(
            models.EndUser.user_id == user_id, models.EndUser.bot_id == bot_id,
            models.EndUser.game_id == ARKNIGHTS_GAME_ID,
        ))
        return bound.scalars().first(), list(users.scalars().all())


async def resolve_enduid_accounts(user_id: str, bot_id: str) -> AccountResolveResult:
    """读取 EndUID 当前同身份的方舟账号；只读，不依赖其请求客户端。

    缺少可选插件与读取失败不同：后者抛出，删除全部时必须中止操作。
    """
    models = _enduid_models()
    if models is None:
        return AccountResolveResult(False, ENDUID_MISSING_HINT, [], [])
    bind, users = await _read_enduid_rows(models, user_id, bot_id)
    uids = _split_uid_list(getattr(bind, "ark_uid", "")) if bind else []
    accounts: list[ArkCredential] = []
    seen: set[str] = set()
    for row in users:
        if _as_game_id(getattr(row, "game_id", None)) != ARKNIGHTS_GAME_ID:
            continue
        # ORM 查询已带身份过滤；额外防止旧适配器返回其他身份的数据。
        if (str(getattr(row, "user_id", user_id)) != str(user_id)
                or str(getattr(row, "bot_id", bot_id)) != str(bot_id)):
            continue
        account = _credential_from_row(row, "enduid")
        if not account.uid or account.uid in seen:
            continue
        seen.add(account.uid)
        accounts.append(account)
        if account.uid not in uids:
            uids.append(account.uid)
    order = {uid: index for index, uid in enumerate(uids)}
    accounts.sort(key=lambda item: order.get(item.uid, len(order)))
    reason = "" if uids else "未找到明日方舟账号，请使用 ark绑定 登录。"
    return AccountResolveResult(True, reason, uids, accounts)


async def resolve_accounts(user_id: str, bot_id: str) -> AccountResolveResult:
    """本地列表优先；仅缺失/已确认无效凭据可复用同 UID 的 EndUID 行。"""
    try:
        local_users = await ark_store.list_local_accounts(user_id, bot_id)
    except Exception:
        return AccountResolveResult(False, "读取本地账号来源失败，请稍后重试。", [], [])
    accounts: list[ArkCredential] = []
    seen: set[str] = set()
    for row in local_users:
        account = _credential_from_row(row, "local")
        if account.uid and account.uid not in seen:
            accounts.append(account)
            seen.add(account.uid)
    if accounts and all(account.usable for account in accounts):
        return AccountResolveResult(True, "", [a.uid for a in accounts], accounts)

    try:
        blocked = await ark_store.disabled_account_uids(user_id, bot_id)
        external = await resolve_enduid_accounts(user_id, bot_id)
    except Exception as exc:
        logger.warning(
            "[ArknightsUID] 读取账号来源失败: " + _exception_type(exc)
        )
        if accounts:
            return AccountResolveResult(
                True, "读取 EndUID 账号来源失败，请稍后重试。",
                [a.uid for a in accounts], accounts,
            )
        return AccountResolveResult(False, "读取账号来源失败，请稍后重试。", [], [])

    if accounts:
        replacements = {a.uid: a for a in external.accounts if a.usable}
        accounts = [
            replacements.get(a.uid, a) if not a.usable and a.uid not in blocked else a
            for a in accounts
        ]
        return AccountResolveResult(True, "", [a.uid for a in accounts], accounts)
    external.uids = [uid for uid in external.uids if uid not in blocked]
    external.accounts = [a for a in external.accounts if a.uid not in blocked]
    if blocked and not external.uids and not external.accounts:
        external.installed = True
        external.reason = "本插件的登录已解除，请重新 ark绑定 登录以恢复。"
    return external


def _unusable_hint(account: ArkCredential) -> str:
    source = "EndUID 复用" if account.source == "enduid" else "本地"
    return f"该账号的{source}凭据为空或已失效，请重新 ark绑定 登录。"


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
            return f"已找到明日方舟 UID：{shown}，但没有找到可用凭据，请重新 ark绑定 登录。"
        return result.reason

    if index > 0:
        if index > len(result.accounts):
            return f"未找到第 {index} 个明日方舟账号，请使用 ark绑定列表 查看可用账号。"
        account = result.accounts[index - 1]
        if account.usable:
            return account
        return result.reason or _unusable_hint(account)

    if uid:
        for account in result.accounts:
            if account.uid == uid:
                if account.usable:
                    return account
                return result.reason or _unusable_hint(account)
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
        return "默认账号已不可用，请使用 ark切换 选择账号，或重新 ark绑定 登录。"

    for account in result.accounts:
        if account.usable:
            return account

    return "已找到明日方舟账号，但凭据为空或已失效，请重新 ark绑定 登录。"


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
    response = await local_skland.get_cultivate_character(credential, character_id)
    if response is None:
        return "森空岛单干员养成材料接口请求失败。"
    return response


async def fetch_cultivate_player(credential: ArkCredential) -> dict[str, Any] | str:
    """玩家养成进度（按 uid）。"""
    response = await local_skland.get_cultivate_player(credential)
    if response is None:
        return "森空岛玩家养成进度接口请求失败。"
    return response


async def fetch_rogue_records(
    credential: ArkCredential, topic_id: str = ""
) -> dict[str, Any] | str:
    """肉鸽战绩（森空岛 App 战绩查询页同源；topic_id 空串=服务端默认期）。"""
    response = await local_skland.get_rogue_records(credential, topic_id)
    if response is None:
        return "森空岛肉鸽战绩接口请求失败。"
    return response
