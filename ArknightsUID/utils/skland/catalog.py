"""公开头像目录；复用公共 Web 签名和磁盘缓存，无需账号登录。"""

from __future__ import annotations

from typing import Any

from ..disk_cache import cache_read, cache_write, is_fresh
from ..path import CACHE_BASE
from ..reply_safety import safe_external_message
from .web import web_public_get

CATALOG_CACHE_DIR = CACHE_BASE / "skland_public"
CATALOG_TTL = 3600
AVATAR_PAGE_SIZE = 16
ARK_AVATAR_CATEGORY_ID = 2


async def load_avatar_catalog(
    category_id: int | None = None, *, force: bool = False
) -> list[dict[str, Any]] | str:
    if category_id is None:
        api_path = "/web/v1/user/avatar/category/list"
        params = {}
        filename = "categories.json"
        field = "title"
    else:
        api_path = "/web/v1/user/avatars"
        params = {"categoryId": category_id}
        filename = f"avatars_{category_id}.json"
        field = "url"
    cache_path = CATALOG_CACHE_DIR / filename
    if not force:
        cached, saved_ts = await cache_read(cache_path)
        if isinstance(cached, list) and is_fresh(saved_ts, CATALOG_TTL):
            return cached

    response = await web_public_get(api_path, params)
    if response is None or response.get("code") != 0:
        message = safe_external_message(
            (response or {}).get("message") or "网络请求失败"
        )
        return f"森空岛头像目录获取失败：{message}"
    data = response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        return "森空岛头像目录格式不符，请稍后重试。"
    if data.get("hasMore"):
        return "森空岛头像目录尚未返回完整数据，请稍后重试。"
    try:
        rows = [{"id": int(row["id"]), field: row[field]} for row in data["list"]]
        if any(not isinstance(row[field], str) or not row[field] for row in rows):
            raise ValueError("目录内容缺失")
    except (KeyError, TypeError, ValueError):
        return "森空岛头像目录内容不符，请稍后重试。"
    await cache_write(cache_path, rows)
    return rows
