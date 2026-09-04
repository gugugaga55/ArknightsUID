"""公共安全取值工具。

从 arknightsuid_account 提炼（account 保留私有副本 _value/_path/_first_path，
迁移留待后续；本模块是新模块的单一事实源，避免重复实现与 regex 分叉）。
"""

from __future__ import annotations

from typing import Any


def value(data: Any, key: str) -> Any:
    """单层安全取值，非 dict 或缺键返回 None（不抛 KeyError）。"""
    if isinstance(data, dict) and key in data:
        return data[key]
    return None


def path(data: Any, *keys: str) -> Any:
    """多层路径取值，任一层非 dict 或缺键返回 None。"""
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def first_path(data: Any, paths: tuple[tuple[str, ...], ...]) -> Any:
    """多候选路径取第一个非空值，全空返回 None。"""
    for p in paths:
        v = path(data, *p)
        if v is not None and v != "":
            return v
    return None
