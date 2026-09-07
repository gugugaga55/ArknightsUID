"""绑定/登录模块的纯文本格式化（命令侧与网页登录侧共用）。"""

from __future__ import annotations

from ..utils.enduid.adapter import hide_uid


def format_bind_success(roles: list[dict]) -> str:
    lines = ["「明日方舟」登录成功，已写入账号："]
    for role in roles:
        lines.append(f"- {role['nickname']}（UID {hide_uid(role['role_id'])}）")
    lines.append("现在可使用 ark理智 / ark每日 / ark肉鸽 等命令。")
    return "\n".join(lines)
