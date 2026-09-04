"""鹰角官网 OAuth 链（抽卡/兑换码）客户端。"""

from __future__ import annotations

from .client import (
    ArkGachaAuth,
    exchange_gift_code,
    get_ak_cookie,
    get_gacha_categories,
    get_gacha_history_page,
    get_grant_token,
    get_u8_token,
    login_gacha_chain,
    resolve_record_uid,
)

__all__ = [
    "ArkGachaAuth",
    "exchange_gift_code",
    "get_ak_cookie",
    "get_gacha_categories",
    "get_gacha_history_page",
    "get_grant_token",
    "get_u8_token",
    "login_gacha_chain",
    "resolve_record_uid",
]
