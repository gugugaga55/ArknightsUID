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
# 字段边界用 ASCII 词边界断言而非 \b：Python re 的 \w 在 Unicode 模式下
# 包含汉字，字段名紧贴中文（"获取token=xxx失败"）时两侧都是 \w，\b 不
# 成立导致整条漏脱敏。这里要求字段名两侧不是 ASCII 字母数字/下划线，
# 汉字与标点都视为合法边界。
_FIELD_EDGE = r"(?<![A-Za-z0-9_])(?:{})(?![A-Za-z0-9_])"
# 赋值分隔符：字段名与分隔符之间允许一个闭合引号（JSON 键值
# `"token": "xxx"` 中间隔着 '"'，否则赋值分支整条不匹配、值原样泄漏）；
# 分隔符同时覆盖全角冒号/等号（中文报错消息常用）。
_ASSIGN_SEP = r"\s*['\"]?\s*[:：=＝]\s*"
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    rf"(?i)['\"]?{_FIELD_EDGE.format(_sensitive_fields)}{_ASSIGN_SEP}"
    rf"(?:\"[^\"]*\"|'[^']*'|[^,，;；\s]+)"
)
_SENSITIVE_FIELD_RE = re.compile(rf"(?i){_FIELD_EDGE.format(_sensitive_fields)}")
_SENTINEL_VALUE_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])SENTINEL_[A-Z0-9_]*(?![A-Za-z0-9_])")

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
