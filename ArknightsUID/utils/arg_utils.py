"""公共命令参数解析工具。

与 arknightsuid_account 的私有 _extract_uid/_extract_index 同源（regex 一致）。
接受短期双份：account 保留私有副本不动（降风险），本模块为 rogue 等新模块的
单一事实源。两处 regex 必须保持一致——若改动，同步更新 arknightsuid_account.__init__。
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
