from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any

from gsuid_core.logger import logger

try:
    from ArknightsUID.ArknightsUID.utils.skland import client as local_skland
except ModuleNotFoundError:
    from ArknightsUID.utils.skland import client as local_skland

ARKNIGHTS_GAME_ID = 1
ENDUID_MISSING_HINT = "未检测到可用的 EndUID。请先将 EndUID 放入 gsuid_core/gsuid_core/plugins 并完成森空岛登录。"


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

    @property
    def usable(self) -> bool:
        return bool(self.cred) and self.cookie_status != "无效"


@dataclass(slots=True)
class AccountResolveResult:
    installed: bool
    reason: str
    uids: list[str]
    accounts: list[ArkCredential]


def hide_uid(uid: str) -> str:
    if len(uid) <= 4:
        return uid
    if len(uid) <= 8:
        return f"{uid[:2]}***{uid[-2:]}"
    return f"{uid[:3]}***{uid[-3:]}"


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


def _import_first(module_names: tuple[str, ...]) -> ModuleType | None:
    for module_name in module_names:
        try:
            return import_module(module_name)
        except ModuleNotFoundError as exc:
            logger.debug(f"[ArknightsUID] 可选模块未找到: {module_name} ({exc})")
        except Exception as exc:
            logger.warning(f"[ArknightsUID] 导入可选模块失败: {module_name}: {exc}")
    return None


def _enduid_models() -> ModuleType | None:
    return _import_first(
        (
            "EndUID.utils.database.models",
            "plugins.EndUID.EndUID.utils.database.models",
            "gsuid_core.plugins.EndUID.EndUID.utils.database.models",
        )
    )


def _enduid_requests() -> ModuleType | None:
    return _import_first(
        (
            "EndUID.utils.api.requests",
            "plugins.EndUID.EndUID.utils.api.requests",
            "gsuid_core.plugins.EndUID.EndUID.utils.api.requests",
        )
    )


async def resolve_accounts(user_id: str, bot_id: str) -> AccountResolveResult:
    models = _enduid_models()
    if models is None:
        return AccountResolveResult(False, ENDUID_MISSING_HINT, [], [])

    EndBind = models.EndBind
    EndUser = models.EndUser

    bind = await EndBind.get_data_by_user_id(user_id, bot_id)
    uids: list[str] = []
    if bind is not None:
        uids = _split_uid_list(getattr(bind, "ark_uid", ""))

    users = await EndUser.select_data_list(user_id=user_id, bot_id=bot_id)
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
                cred=_as_text(getattr(user, "cookie", "")),
                token=_as_text(getattr(user, "token", "")),
                hg_token=_as_text(getattr(user, "hg_token", "")),
                skland_user_id=_as_text(getattr(user, "skland_user_id", "")),
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

    for account in result.accounts:
        if account.usable:
            return account

    return "已找到明日方舟账号，但 EndUID 凭据为空或已失效，请重新使用 EndUID 登录。"


async def fetch_player_info(credential: ArkCredential) -> dict[str, Any] | str:
    response = await local_skland.get_player_info(credential)
    if response is not None:
        return response

    requests = _enduid_requests()
    if requests is None:
        return "森空岛玩家信息接口请求失败。"
    end_api = requests.end_api
    response = await end_api.get_player_info(
        credential.cred,
        credential.uid,
        game_id=ARKNIGHTS_GAME_ID,
    )
    if response is None:
        return "森空岛玩家信息接口请求失败。"
    return response


async def ark_attendance(credential: ArkCredential) -> dict[str, Any] | str:
    response = await local_skland.ark_attendance(credential)
    if response is not None:
        return response

    requests = _enduid_requests()
    if requests is None:
        return "森空岛签到接口请求失败。"
    end_api = requests.end_api
    if not hasattr(end_api, "ark_attendance"):
        return "当前 EndUID 版本缺少明日方舟签到接口，请更新 EndUID。"
    response = await end_api.ark_attendance(credential.cred, credential.uid)
    if response is None:
        return "森空岛签到接口请求失败。"
    return response
