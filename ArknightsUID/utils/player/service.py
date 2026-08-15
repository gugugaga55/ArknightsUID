"""player_info 的带缓存获取（P1）。

game/player/info 返回 1w+ 行 JSON，直接打森空岛接口既慢又有风控压力。
落盘走 player_store（gzip 白名单已含 player_info.json），TTL 内直接读本地。

缓存文件存完整响应信封 {code, message, data}，mtime 作为 TTL 时钟
（不污染 JSON 结构，与 EndUID player_store 的用法一致）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gsuid_core.logger import logger

from ..path import PLAYER_PATH
from ..player_store import (
    player_json_exists,
    read_player_json,
    resolve_player_path,
    write_player_json,
)

if TYPE_CHECKING:
    from ..enduid.adapter import ArkCredential

# 缓存 TTL：300 秒（理智按 6 分钟回 1 点，5 分钟粒度足够）
DEFAULT_TTL = 300.0
_PLAYER_INFO_NAME = "player_info.json"


def _cache_path(uid: str) -> Path:
    return PLAYER_PATH / uid / _PLAYER_INFO_NAME


def _cache_age_seconds(uid: str) -> float | None:
    """缓存年龄；无缓存返回 None。"""
    resolved = resolve_player_path(_cache_path(uid))
    if resolved is None:
        return None
    try:
        return max(0.0, time.time() - resolved.stat().st_mtime)
    except OSError:
        return None


async def read_cached_player_info(uid: str, max_age: float = DEFAULT_TTL) -> dict[str, Any] | None:
    """读 TTL 内的缓存信封；不新鲜或不存在返回 None。"""
    age = _cache_age_seconds(uid)
    if age is None or age > max_age:
        return None
    data = await read_player_json(_cache_path(uid))
    if isinstance(data, dict) and data.get("code") == 0:
        return data
    return None


async def fetch_cached_player_info(
    credential: "ArkCredential", ttl: float = DEFAULT_TTL, force: bool = False
) -> dict[str, Any] | str:
    """带磁盘缓存的 player_info 获取。

    返回响应信封 dict；adapter 失败时返回中文错误文案（str）。
    force=True 跳过缓存直接请求（请求成功后仍会回写缓存）。
    """
    from ..enduid.adapter import fetch_player_info

    if not force:
        cached = await read_cached_player_info(credential.uid, ttl)
        if cached is not None:
            return cached

    response = await fetch_player_info(credential)
    if isinstance(response, str):
        return response

    # 信封 code != 0（token 失效等）不落盘，避免坏响应占用 TTL
    if isinstance(response, dict) and response.get("code") == 0:
        try:
            await write_player_json(_cache_path(credential.uid), response)
        except Exception as exc:
            logger.warning(
                f"[ArknightsUID·player] 缓存写入失败 uid={credential.uid}: "
                f"{exc.__class__.__name__}"
            )
    return response


async def drop_cached_player_info(uid: str) -> None:
    """清掉本地缓存（签到等会改变状态的命令之后可调用）。"""
    try:
        if player_json_exists(_cache_path(uid)):
            from ..player_store import remove_player_json

            remove_player_json(_cache_path(uid))
    except Exception as exc:
        logger.warning(
            f"[ArknightsUID·player] 缓存清理失败 uid={uid}: {exc.__class__.__name__}"
        )
