"""状态卡背景图（对齐 EndUID「体力背景」的产品思路，本地实现）。

优先级：用户上传 → 用户干员立绘偏好 → 管理员全局 → 随机持有干员立绘 → 模板渐变兜底。
- 上传：`ark设置背景` + 图片；`ark设置全局背景` + 图片（superuser）。
- 干员立绘：`ark设置背景 阿米娅`（跟随游戏内当前穿戴皮肤，未持有回退
  精二/初始立绘）/ `ark设置背景 阿米娅 报童`（钉死指定时装皮肤）。
  与上传图共用用户槽位——后写覆盖，「重置」两者同清。
- 随机立绘：玩家当前持有干员按当前皮肤取 hycdn portrait（与头像同源
  CDN），无持有数据时直接落到渐变兜底。

存储：resource/backgrounds/ 下 webp（上传原图经 PIL 重编码，长边 ≤1600、
质量 82——上游表情包截图常见 3MB+，不压会拖慢每次出卡）；干员偏好是
JSON 标记（char_id/skin_id/name/rarity），渲染期解析成 hycdn 立绘。

关键素材事实（2026-09 实测）：char_skin/portrait/ 的立绘是 180×360
不透明半身像（无 alpha、自带画底），模板层按「右侧人物栏」使用，
不能整幅放大当全幅背景。
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4
import random
from pathlib import Path

from gsuid_core.logger import logger

__all__ = [
    "BG_DIR",
    "user_bg_path",
    "admin_bg_path",
    "user_op_bg_path",
    "save_bg_from_url",
    "save_op_bg",
    "read_op_bg",
    "clear_user_bg",
    "delete_bg",
    "resolve_status_bg",
]

from .resource.RESOURCE_PATH import RESOURCE_PATH

BG_DIR = RESOURCE_PATH / "backgrounds"

# 重编码上限：长边 1600（卡宽 560 的 2.8 倍，再大只是浪费）
_BG_MAX_EDGE = 1600
_BG_QUALITY = 82


def _safe_stem(*parts: str) -> str:
    """路径片段拼文件名：只留字母数字，防注入穿越。"""
    return "_".join("".join(ch for ch in p if ch.isalnum()) for p in parts)


def user_bg_path(bot_id: str, user_id: str) -> Path:
    return BG_DIR / f"user_{_safe_stem(bot_id, user_id)}.webp"


def admin_bg_path(bot_id: str) -> Path:
    return BG_DIR / f"admin_{_safe_stem(bot_id)}.webp"


def user_op_bg_path(bot_id: str, user_id: str) -> Path:
    """用户级「干员立绘背景」偏好文件（JSON 标记，与上传图共用用户槽位）。"""
    return BG_DIR / f"user_{_safe_stem(bot_id, user_id)}_op.json"


def read_op_bg(bot_id: str, user_id: str) -> dict[str, str | int]:
    """读干员立绘偏好；坏数据一律降级为空 dict（调用方落回下一级背景）。"""
    try:
        data = json.loads(user_op_bg_path(bot_id, user_id).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("char_id"), str):
        return {}
    return {
        "char_id": data["char_id"],
        "skin_id": str(data.get("skin_id") or ""),
        "name": str(data.get("name") or ""),
        "rarity": int(data.get("rarity") or 0),
    }


def save_op_bg(
    bot_id: str, user_id: str, *, char_id: str, skin_id: str, name: str, rarity: int
) -> None:
    """写干员立绘偏好；同时清掉用户上传图（同一用户槽位，后写生效）。"""
    delete_bg(user_bg_path(bot_id, user_id))
    path = user_op_bg_path(bot_id, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "char_id": char_id,
        "skin_id": skin_id,
        "name": name,
        "rarity": rarity,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def clear_user_bg(bot_id: str, user_id: str) -> bool:
    """重置用户背景槽：上传图与干员立绘偏好一并清。返回是否清到了内容。"""
    cleared_upload = delete_bg(user_bg_path(bot_id, user_id))
    cleared_op = delete_bg(user_op_bg_path(bot_id, user_id))
    return cleared_upload or cleared_op


async def save_bg_from_url(url: str, path: Path) -> bool:
    """下载用户上传的背景图 → 重编码落盘；失败返回 False（调用方提示）。

    不做内容审核（与头像/皮肤图同信任级别）；仅约束为可解码图片。
    """
    if not url.startswith(("http://", "https://")):
        return False
    from .http import safe_download_bytes

    raw = await safe_download_bytes(url, timeout=20.0)
    if raw is None:
        return False

    try:
        await asyncio.to_thread(_save_background, raw, path)
        return True
    except Exception as exc:
        logger.warning(f"[ArknightsUID·背景] 图片保存失败: {exc.__class__.__name__}")
        return False


def _save_background(raw: bytes, path: Path) -> None:
    """图片解码/缩放/编码与磁盘写入在工作线程完成，替换成功前保留旧图。"""
    from io import BytesIO
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        with Image.open(BytesIO(raw)) as source:
            image = source.convert("RGBA" if "A" in source.getbands() or "transparency" in source.info else "RGB")
            image.thumbnail((_BG_MAX_EDGE, _BG_MAX_EDGE))
            image.save(temp, "WEBP", quality=_BG_QUALITY)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def delete_bg(path: Path) -> bool:
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def _file_b64(path: Path) -> str:
    """本地背景文件 → data URI（读一次即可，卡片每次渲染都要用）。"""
    try:
        import base64

        raw = path.read_bytes()
        return f"data:image/webp;base64,{base64.b64encode(raw).decode()}"
    except OSError:
        return ""


async def _resolve_op_bg(op: dict, chars: list | None) -> str:
    """干员立绘偏好 → b64；任何一步失败返回空串（调用方落回下一级）。

    未钉皮肤（skin_id 空）时跟随玩家游戏内当前穿戴皮肤；未持有该干员
    回退 char_portrait_b64 的精二→精一→初始逐档链（自带 404 负缓存）。
    """
    from .player.context import char_portrait_b64, skin_portrait_url
    from .render import get_image_b64_with_cache
    from .resource.RESOURCE_PATH import CACHE_PATH

    skin_id = str(op.get("skin_id") or "")
    if not skin_id and chars:
        for char in chars:
            if getattr(char, "char_id", "") == op.get("char_id"):
                skin_id = getattr(char, "skin_id", "") or ""
                break
    try:
        if skin_id:
            url = skin_portrait_url(skin_id)
            if not url:
                return ""
            return await get_image_b64_with_cache(url, CACHE_PATH / "portraits")
        rarity = op.get("rarity") or 0
        return await char_portrait_b64(str(op["char_id"]), int(rarity))
    except Exception:
        logger.debug("[ArknightsUID·背景] 干员立绘获取失败", exc_info=True)
        return ""


async def resolve_status_bg(
    bot_id: str, user_id: str, chars: list | None = None
) -> tuple[str, str]:
    """状态卡背景 → (b64 data URI, 来源标签)。空串 = 模板渐变兜底。

    chars 为玩家持有干员（CharEntry，带 skin_id）；随机立绘用当前皮肤。
    """
    upload = user_bg_path(bot_id, user_id)
    if upload.exists():
        b64 = _file_b64(upload)
        if b64:
            return b64, "自定义"

    op = read_op_bg(bot_id, user_id)
    if op:
        b64 = await _resolve_op_bg(op, chars)
        if b64:
            return b64, op["name"] or "干员立绘"

    admin = admin_bg_path(bot_id)
    if admin.exists():
        b64 = _file_b64(admin)
        if b64:
            return b64, "全局"

    if chars:
        candidates = [c for c in chars if getattr(c, "char_id", "")]
        if candidates:
            char = random.choice(candidates)
            try:
                from .player.context import skin_portrait_url
                from .render import get_image_b64_with_cache
                from .resource.RESOURCE_PATH import CACHE_PATH

                url = skin_portrait_url(
                    getattr(char, "skin_id", "") or char.char_id
                )
                if url:
                    b64 = await get_image_b64_with_cache(
                        url, CACHE_PATH / "portraits"
                    )
                    if b64:
                        return b64, getattr(char, "name", "") or "干员立绘"
            except Exception:
                logger.debug("[ArknightsUID·背景] 立绘获取失败", exc_info=True)
    return "", ""
