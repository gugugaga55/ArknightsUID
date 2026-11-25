"""公共命令参数解析工具（单一事实源）。

arknightsuid_account 的 _extract_uid/_extract_index 为本模块别名
（保留旧名供既有调用/测试引用）。
"""

from __future__ import annotations

import re


def extract_uid(text: str) -> str:
    """提取首个 5 位及以上数字作为 UID，无则返回空串。"""
    match = re.search(r"\d{5,}", text)
    if match is None:
        return ""
    return match.group(0)


def extract_index(text: str) -> int:
    """提取账号序号（如「肉鸽 2」→ 2、「第3号」→ 3）。

    若文本含 UID（5 位以上数字），返回 0（UID 优先，序号无意义）。
    """
    if extract_uid(text):
        return 0
    normalized = text.strip().replace("第", "").replace("号", "")
    match = re.search(r"(?:^|\s|#)(\d{1,2})(?:\s*)$", normalized)
    if match is None:
        return 0
    return int(match.group(1))


_EXPLICIT_ACCOUNT = re.compile(
    r"(?<!\S)(?:uid\s*[:：]\s*([0-9]{5,})|第\s*([0-9]{1,2})\s*号|([0-9]{1,2})\s*号)(?!\S)",
    re.IGNORECASE,
)
_LEGACY_ACCOUNT_UID = re.compile(r"(?<!\S)[0-9]{5,}(?!\S)")


def split_account(
    text: str, *, legacy_uid: bool = True, legacy_index: bool = True
) -> tuple[str, str, int]:
    """拆出账号参数，保留业务文本；不吞掉名称中的数字。"""
    text = text.strip()
    matches = list(_EXPLICIT_ACCOUNT.finditer(text))
    if matches:
        if len(matches) != 1:
            raise ValueError("一次只能指定一个账号。")
        match = matches[0]
        uid, first, second = match.groups()
        index = int(first or second or 0)
        if not uid and index == 0:
            raise ValueError("账号序号从 1 开始，可用账号见 ark账号。")
        return (text[:match.start()] + text[match.end():]).strip(), uid or "", index
    if re.search(r"(?<!\S)uid\s*[:：]", text, re.IGNORECASE):
        raise ValueError("UID 格式有误，请使用 UID:数字。")
    if legacy_uid and (match := _LEGACY_ACCOUNT_UID.search(text)):
        return (text[:match.start()] + text[match.end():]).strip(), match[0], 0
    if legacy_index and (match := re.search(r"(?<!\S)(?:第|#)?([0-9]{1,2})$", text)):
        index = int(match[1])
        if index == 0:
            raise ValueError("账号序号从 1 开始，可用账号见 ark账号。")
        return text[:match.start()].strip(), "", index
    return text, "", 0
