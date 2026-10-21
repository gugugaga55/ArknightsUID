"""国服显示与日历统计统一使用北京时间，不依赖服务器本地时区。"""

from datetime import datetime, timedelta, timezone
import time

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_datetime(timestamp: float | None = None) -> datetime:
    return datetime.fromtimestamp(
        time.time() if timestamp is None else timestamp, tz=BEIJING_TZ
    )
