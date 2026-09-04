"""第三方资源字段 → 安全缓存文件名（2026-08 审计问题 #7）。

威胁模型：外部 GameData / Wiki 页名的 levelId、敌人 key、iconId 直接拼接
为缓存路径。数据源被投毒时（"../target"、"/abs/path"），裸拼接可逃出缓存
目录覆写进程权限内的任意 .json/.webp 文件。

两层防线：
1. ``safe_cache_stem``：白名单清洗（非法字符→"_"）；发生替换/截断的值
   追加内容哈希后缀，避免不同原始值清洗后互相碰撞。纯白名单的常规值
   **原样返回**，存量缓存文件名不受影响。
2. ``resolve_inside``：落盘前 resolve 并校验目标仍位于基准目录内
   （纵深防御，拦截任何残余的穿越构造）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# 字母/数字/下划线/连字符 + 中文（PRTS 敌人页名是中文，需保留）
# % 保留：PRTS 等源用 percent-encoding 命名文件（%E9%81%93…），它是
# 文件系统合法字面字符且不构成路径穿越（真分隔符已在 urlsplit 拆掉）
_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z_%\-一-鿿]")


def safe_cache_stem(value: str, *, max_len: int = 64) -> str:
    """外部字段 → 可直接拼进缓存路径的文件名主干（无分隔符/无 .. 前缀）。"""
    text = (value or "").strip()
    if not text:
        return "_blank"
    cleaned = _UNSAFE_CHARS.sub("_", text)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    if cleaned != text or not cleaned.strip("_"):
        # 清洗/截断发生过（或清洗后为空）：追加内容哈希区分不同原始值
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned[:max_len]}~{digest}"
    return cleaned


def resolve_inside(base: Path, name: str) -> Path:
    """base / name，并校验解析结果仍在 base 内；越界抛 ValueError。"""
    target = (base / name).resolve()
    base_resolved = base.resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise ValueError(f"cache path escapes base dir: {name!r}")
    return target


def safe_url_basename(url: str) -> str:
    """URL 最后一段 → 安全缓存文件名（保留扩展名，剥 query/fragment）。

    各图片缓存入口（image / render / bulk_download / itemicon）此前直接
    ``url.split('/')[-1]`` 拼缓存路径——异常 URL（``..``、空段、Windows
    反斜杠）可能逃出缓存目录。这里先 urlsplit 规范解析（query/fragment
    不进文件名），再对主干与扩展名分别过白名单清洗；正常 CDN 文件名
    原样返回（存量缓存兼容）。URL 无有效末段（如以 / 结尾）返回空串。
    """
    from urllib.parse import urlsplit

    last = urlsplit(url).path.replace("\\", "/").rsplit("/", 1)[-1]
    if not last or last in (".", ".."):
        return ""
    stem, dot, ext = last.rpartition(".")
    if not dot or not stem:
        return safe_cache_stem(last, max_len=128)
    # 128：percent-encoded 中文名天然较长（每汉字 9 字符），64 会把正常
    # PRTS 文件名截断出哈希后缀、白白失效存量缓存；仍在文件系统 255 限内
    return f"{safe_cache_stem(stem, max_len=128)}.{safe_cache_stem(ext)}"


def cache_names_for_url(url: str) -> tuple[str, str]:
    """图片缓存文件名：(新键, 旧键) —— 审计 #20 串图修复。

    旧键 = safe_url_basename(url)（纯 basename）：不同 URL 末段相同会
    共享同一缓存文件（不同公告图同名、带签名参数的 CDN 直链），互相
    覆盖串图。新键在旧键主干上追加**规范化 URL 短哈希**（sha1 的
    netloc+path+query 前 8 位，fragment 剔除），不同 URL 必不同键。

    读取方先查新键再回退旧键（存量缓存不失效、不重下）；写入方
    （惰性下载器 pic_download_from_url）只用新键。
    """
    from urllib.parse import urlsplit

    legacy = safe_url_basename(url)
    if not legacy:
        return "", ""
    parts = urlsplit(url)
    digest_source = f"{parts.netloc}|{parts.path}|{parts.query}"
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:8]
    stem, dot, ext = legacy.rpartition(".")
    if not dot or not stem:
        return f"{legacy}~{digest}", legacy
    return f"{stem}~{digest}.{ext}", legacy
