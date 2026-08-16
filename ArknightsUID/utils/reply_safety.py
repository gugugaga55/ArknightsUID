"""回复脱敏：外部 API 报文回显给用户前的统一过滤。

account / gacha 等命令模块共用，防止上游错误消息（或中间人环境下被
污染的响应）夹带 cred/token 类键值经 bot 回复泄漏到群聊。
"""
from __future__ import annotations

import re
from typing import Any

_sensitive_fields = (
    r"cookie|cred|token|hg_token|skland_user_id"
    # 扩充常见高敏感字段，覆盖驼峰/下划线/空格变体，避免外部消息夹带泄漏。
    r"|access[_ ]?token|refresh[_ ]?token"
    r"|secret|password|passwd"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b(?:{_sensitive_fields})\b\s*[:=]\s*"
    rf"(?:\"[^\"]*\"|'[^']*'|[^,，;；\s]+)"
)
_SENSITIVE_FIELD_RE = re.compile(rf"(?i)\b(?:{_sensitive_fields})\b")
_SENTINEL_VALUE_RE = re.compile(r"(?i)\bSENTINEL_[A-Z0-9_]*\b")

_REDACTED = "已隐藏敏感内容"


def safe_external_message(value: Any) -> str:
    """抹去外部消息中的敏感字段赋值与字段名，返回可安全回显的文本。"""
    if value in (None, ""):
        return ""
    text = str(value)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(_REDACTED, text)
    text = _SENSITIVE_FIELD_RE.sub(_REDACTED, text)
    text = _SENTINEL_VALUE_RE.sub(_REDACTED, text)
    text = re.sub(rf"(?:{_REDACTED}[\s,，;；]*){{2,}}", _REDACTED, text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text or _REDACTED
