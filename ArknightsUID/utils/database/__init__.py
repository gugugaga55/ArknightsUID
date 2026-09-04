"""ArknightsUID 数据库层。

P0 阶段建两张最小表：
- `ArkUser`: 明日方舟用户（uid, cred, token, game_id 等）
- `ArkBind`: QQ-UID 绑定关系

后续随功能迭代再扩充（签到记录、抽卡记录、别名等）。
"""

from .models import ArkBind, ArkUser  # noqa: F401
