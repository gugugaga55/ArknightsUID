"""绑定/登录模块的纯文本格式化（命令侧与网页登录侧共用）。"""

from __future__ import annotations

from ..utils.enduid.adapter import hide_uid


def format_bind_success(
    roles: list[dict], shown_uids: dict[str, str] | None = None
) -> str:
    lines = ["「明日方舟」登录成功，已写入账号："]
    for role in roles:
        raw_id = str(role.get("role_id") or "")
        uid_str = (
            shown_uids[raw_id]
            if shown_uids and raw_id in shown_uids
            else hide_uid(raw_id)
        )
        lines.append(f"- {role['nickname']}（UID {uid_str}）")
    lines.append("现在可使用 ark理智 / ark每日 / ark肉鸽 等命令。")
    return "\n".join(lines)
