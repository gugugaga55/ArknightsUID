"""QQ 头像获取（状态卡用户头像条）。

渠道对齐 EndUID：onebot → q1.qlogo.cn 个人号大图，qqgroup → q.qlogo.cn
官机头像；其他平台无来源，返回空串。两次实测坑位都已建模：
- qlogo 对无效号/无头像返回 QQ 企鹅占位图（HTTP 200 不报错），按 md5
  识别并视为无头像（名单来自 EndUID 实测，见 at_help.py）；
- 头像不持久缓存——企鹅占位判断要吃原始字节（get_image_b64_with_cache
  会重编码 webp，md5 对不上），qlogo 响应小（~20KB），按次拉取换
  「换头像即时生效」。

一切失败返回空串，调用方/模板走 SVG 菱形标兜底。
"""

from __future__ import annotations

import base64
import hashlib

from gsuid_core.logger import logger

__all__ = ["user_avatar_b64"]

# qlogo 企鹅占位图 md5（EndUID at_help.py 实测名单）
_QQ_DEFAULT_AVATAR_MD5 = frozenset(
    {
        "bad9cbb852b22fe58e62f3f23c7d63d2",  # q1.qlogo 个人号占位 (s>=140)
        "4700116072a9eba9330a81fbbe49b7d5",  # q1.qlogo 个人号占位 (s=100)
        "bb7257abd317126fc3fd3e29ea118958",  # q.qlogo 官机(qqgroup)占位
    }
)


def _avatar_url(bot_id: str, user_id: str, bot_self_id: str = "") -> str:
    """按平台组头像 URL；无来源返回空串。"""
    if not user_id or not user_id.isdigit():
        return ""
    if bot_id == "onebot":
        return f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    if bot_id == "qqgroup":
        return f"https://q.qlogo.cn/qqapp/{bot_self_id}/{user_id}/100"
    return ""


def _sniff_mime(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG"):
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def user_avatar_b64(bot_id: str, user_id: str, bot_self_id: str = "") -> str:
    """查询者 QQ 头像 → data URI；无来源/占位图/失败一律空串（模板兜底）。"""
    url = _avatar_url(bot_id, user_id, bot_self_id)
    if not url:
        return ""
    try:
        from .http import get_client

        client = await get_client()
        response = await client.get(url, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        raw = response.content
        # 企鹅占位（无头像号）与异常小响应都视为无头像
        if len(raw) < 400 or hashlib.md5(raw).hexdigest() in _QQ_DEFAULT_AVATAR_MD5:
            return ""
        return f"data:{_sniff_mime(raw)};base64,{base64.b64encode(raw).decode()}"
    except Exception:
        logger.debug("[ArknightsUID·头像] 获取失败", exc_info=True)
        return ""
