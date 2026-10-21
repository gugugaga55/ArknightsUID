"""游戏好友查询：接口边界解析、短时缓存和本地分页。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from gsuid_core.logger import logger

from ..api.request_util import envelope_ok
from ..dict_utils import path
from ..disk_cache import cache_read, cache_write, is_fresh
from ..path import PLAYER_PATH
from ..player_store import assert_uid_segment
from ..reply_safety import safe_external_message
from .client import get_game_friends

if TYPE_CHECKING:
    from ..enduid.adapter import ArkCredential

FRIEND_CACHE_TTL = 60
FRIEND_PAGE_SIZE = 10


@dataclass(frozen=True, slots=True)
class GameFriend:
    uid: str
    name: str
    level: int
    avatar_url: str
    last_online_ts: int
    skland_nickname: str


@dataclass(slots=True)
class FriendList:
    friends: list[GameFriend]
    complete: bool


def parse_friend_list(data: Any, *, envelope: Any = None) -> FriendList:
    """只接受列表或明确的 list 包装，不猜测其它字段名或分页参数。

    hasMore 出现在 data 字典或响应信封任一处都按不完整处理：接口形状
    未经真实账号确认，宁可不缓存，也不把部分结果当完整目录。
    """
    rows = data.get("list") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("好友列表结构不符")
    friends = []
    for row in rows:
        uid = str(row["uid"] or "")
        if not uid:
            raise ValueError("好友 UID 缺失")
        friends.append(
            GameFriend(
                uid=uid,
                name=str(row["name"] or "未命名好友"),
                level=int(row.get("level") or 0),
                avatar_url=str(path(row, "avatar", "url") or ""),
                last_online_ts=int(row.get("lastOnlineTs") or 0),
                skland_nickname=str(path(row, "sklandUser", "nickname") or ""),
            )
        )
    has_more = bool(path(data, "hasMore")) or bool(path(envelope, "hasMore"))
    return FriendList(friends, not has_more)


async def fetch_friends(
    credential: ArkCredential, *, force: bool = False
) -> FriendList | str:
    cache_path = PLAYER_PATH / assert_uid_segment(credential.uid) / "friends.json"
    if not force:
        cached, saved_ts = await cache_read(cache_path)
        if isinstance(cached, list) and is_fresh(saved_ts, FRIEND_CACHE_TTL):
            try:
                return FriendList([GameFriend(**row) for row in cached], True)
            except TypeError:
                # 历史缓存字段结构与当前模型不一致：当缓存缺失处理，走重取覆盖
                logger.debug("[ArknightsUID·好友] 缓存字段不匹配，重新拉取")

    response = await get_game_friends(credential)
    if response is None:
        return "森空岛好友查询失败，请稍后重试。"
    if not envelope_ok(response):
        message = safe_external_message(path(response, "message") or "请求未成功")
        return f"森空岛好友查询失败（code={path(response, 'code')}）：{message}"
    try:
        result = parse_friend_list(response.get("data"), envelope=response)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(f"[ArknightsUID·好友] 数据解析失败：{exc.__class__.__name__}")
        return "森空岛好友数据暂时无法解析，请稍后重试。"
    # 返回不完整的列表不能占用完整目录的缓存，也不伪造未知的下一页参数。
    if result.complete:
        await cache_write(cache_path, [asdict(friend) for friend in result.friends])
    return result
