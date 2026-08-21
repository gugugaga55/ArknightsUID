"""用户级默认账号（ark切换）存储。

多账号用户切换默认查询账号：resolve_credential 在未显式指定 uid/序号时
优先取这里存的默认 UID。JSON 落盘（与 push_state.json 同模式），
键为 f"{bot_id}|{user_id}"，值为游戏 UID。

不经过数据库（ArkUser 行没有"默认"语义，EndUID 回退账号更是只读），
独立小状态文件最稳：删账号/清缓存都不影响其他用户。
"""
from __future__ import annotations

import json

from gsuid_core.logger import logger

try:
    from ArknightsUID.ArknightsUID.utils.path import MAIN_PATH
except ModuleNotFoundError:
    from ArknightsUID.utils.path import MAIN_PATH

_STATE_PATH = MAIN_PATH / "account_switch.json"


def _key(bot_id: str, user_id: str) -> str:
    return f"{bot_id}|{user_id}"


def _load() -> dict[str, str]:
    try:
        state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return {str(k): str(v) for k, v in state.items() if v}
    except Exception:
        pass
    return {}


def _save(state: dict[str, str]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·切换] 状态写入失败: {exc.__class__.__name__}: {exc}"
        )


def get_default_uid(bot_id: str, user_id: str) -> str:
    """该用户的默认账号 UID；未设置返回空串。"""
    return _load().get(_key(bot_id, user_id), "")


def set_default_uid(bot_id: str, user_id: str, uid: str) -> bool:
    """设置默认账号。uid 为空等价于清除。"""
    if not uid:
        return clear_default_uid(bot_id, user_id)
    state = _load()
    state[_key(bot_id, user_id)] = uid
    _save(state)
    return True


def clear_default_uid(bot_id: str, user_id: str, uid: str = "") -> bool:
    """清除默认账号。

    uid 非空时仅当当前默认恰好指向该 uid 才清除（删账号时防止误清
    指向其他账号的默认）。返回是否发生清除。
    """
    state = _load()
    key = _key(bot_id, user_id)
    current = state.get(key, "")
    if not current:
        return False
    if uid and current != uid:
        return False
    state.pop(key, None)
    _save(state)
    return True
