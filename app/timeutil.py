"""UTC 时刻解析与格式化。

题目要求填写 UTC 时刻：所有输入必须显式带时区（结尾 Z 或 +00:00 这类偏移），
内部统一存为 Unix 纪元秒整数，展示时再格式化为带 Z 的 UTC ISO-8601。
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


class TimeParseError(ValueError):
    pass


def parse_utc(value) -> int:
    """把带时区的时间字符串解析为 UTC 纪元秒（整数）。

    接受 ``2026-11-20T00:00:00Z``、``2026-11-20 01:30:00+00:00`` 等。
    不带时区的“朴素时间”一律拒绝，避免按服务器本地时区误解。
    """
    if value is None:
        raise TimeParseError("时间不能为空")
    text = str(value).strip()
    if not text:
        raise TimeParseError("时间不能为空")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise TimeParseError(f"无法解析时刻 {value!r}，应为 ISO-8601 且带时区，如 2026-11-20T00:00:00Z")
    if dt.tzinfo is None:
        raise TimeParseError(f"时刻 {value!r} 缺少时区信息，请用 Z 或 +00:00 明确标注 UTC")
    dt_utc = dt.astimezone(timezone.utc)
    if dt_utc.microsecond:
        raise TimeParseError("时刻精度只能到秒，不接受小数秒")
    return int(dt_utc.timestamp())


def format_utc(epoch) -> str:
    """纪元秒 -> ``2026-11-20T00:00:00Z``；None -> 空串。"""
    if epoch is None:
        return ""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
