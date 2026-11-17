"""
时间序列化工具

背景：MySQL 以 UTC 时区运行（func.now() 返回 UTC naive datetime），
若 API 直接 isoformat() 输出无时区标记的字符串（如 2026-08-06T01:28:13），
前端 new Date() 会按浏览器本地时区解析，导致展示时间比北京时间少 8 小时。

约定：
- 存储/内存：naive datetime（UTC 语义）
- API 对外输出：带 UTC 偏移的 ISO 字符串（+00:00），前端自动本地化显示
- 内部解析（游标、缓存回读）：统一归一化为 naive UTC
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """返回无时区标记的 UTC 当前时间，与数据库 DateTime 字段保持一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_iso(dt: datetime | None) -> str | None:
    """
    datetime → 带 UTC 偏移的 ISO 字符串（对外输出）

    naive datetime 视为 UTC（数据库存储语义），补上 +00:00 偏移，
    前端 new Date(iso) 即可按浏览器本地时区正确转换显示。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_db_time(s: str | None) -> datetime | None:
    """
    字符串 → naive UTC datetime（内部解析）

    兼容两种输入：
    - 无时区标记（旧缓存/游标）：直接视为 UTC
    - 带偏移（新格式，如 +00:00 或 Z）：先转 UTC 再去掉 tzinfo
    统一输出 naive UTC，避免与数据库值比较时 naive/aware 混用报错。
    """
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
