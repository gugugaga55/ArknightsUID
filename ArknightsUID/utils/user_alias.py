"""用户自定义别名的查询侧应用（见 database.models.ArkAlias）。

处理器在解析干员名前调用 apply_user_alias：用户别名优先级高于
内建别名（用户显式设置的表达意图更强）。仅做整词精确替换——
别名只替换整个 query，不做子串替换（避免误伤）。
"""

from __future__ import annotations


async def apply_user_alias(bot_id: str, user_id: str, query: str) -> str:
    """query 若命中该用户别名 → 返回指向的干员中文名，否则原样返回。"""
    text = (query or "").strip()
    if not text:
        return query
    try:
        from .database.models import ArkAlias

        target: str | None = await ArkAlias.resolve_alias(
            bot_id=bot_id, user_id=user_id, alias=text
        )
        if target and not str(target).startswith("rogue:"):
            return target
        return query
    except Exception:
        # 别名是锦上添花：任何 DB 异常都不阻断原查询
        return query


def parse_alias_pair(text: str) -> tuple[str, str] | None:
    """「别名 目标」「别名=目标」「别名 → 目标」→ (alias, target)。"""
    raw = (text or "").strip()
    if not raw:
        return None
    for sep in ("=", "＝", "→", "->"):
        if sep in raw:
            left, _, right = raw.partition(sep)
            alias, target = left.strip(), right.strip()
            if alias and target:
                return alias, target
    parts = raw.split()
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return None
