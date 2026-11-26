"""时间口径：校准区间左闭右开，时刻按分钟比较。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Union

CST = timezone(timedelta(hours=8))
EXERCISE_DATE = "2026-12-08"

TimeLike = Union[str, int, None]


def as_minutes(value: TimeLike) -> Optional[int]:
    """把 ``HH:MM`` 或 ISO 时间转换为当天分钟数；``None`` 原样返回。

    已经是整数时视为分钟数。ISO 字符串会按其自带日期/时区换算到
    演练日 (UTC+08:00) 的分钟数，保证跨日报文也能比较。
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = value.strip()
    if not text:
        return None
    if "T" in text or "-" in text and len(text) > 5:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone(CST)
        elif EXERCISE_DATE not in text:
            # 无时区的完整时间戳按演练时区解释
            dt = dt.replace(tzinfo=CST)
        return dt.hour * 60 + dt.minute
    hour, minute = text.split(":", 1)
    return int(hour) * 60 + int(minute)


def HM(value: TimeLike) -> Optional[str]:
    """把分钟数还原成 ``HH:MM``；无法转换时原样返回。"""
    m = as_minutes(value)
    if m is None:
        return None
    return f"{m // 60:02d}:{m % 60:02d}"


def in_half_open(moment: TimeLike, start: TimeLike, end: TimeLike) -> bool:
    """判断 moment 是否落在左闭右开区间 ``[start, end)``。"""
    m, lo, hi = as_minutes(moment), as_minutes(start), as_minutes(end)
    if m is None or lo is None or hi is None:
        return False
    return lo <= m < hi


def overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    """两个左闭右开时间区间是否重叠。"""
    return max(start_a, start_b) < min(end_a, end_b)
