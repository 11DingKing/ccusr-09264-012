"""跨时区截止处理。

截止时间以“当地墙上时间 + IANA 时区”输入，统一换算为 UTC 绝对时刻
存储与比较；展示时再还原。这样不同时区的评审人看到同一个截止瞬间，
DST 等歧义由 zoneinfo 按其规则解析。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Deadline:
    at_utc_iso: str
    timezone: str


def resolve_deadline(local_iso: str, tz_name: str) -> Deadline:
    """把 '2026-09-30T17:00' 与 'Asia/Shanghai' 解析为 UTC 时刻。"""
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区: {tz_name}") from exc
    try:
        parsed = datetime.fromisoformat(local_iso)
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {local_iso}") from exc
    if parsed.tzinfo is not None:
        # 已带偏移量：以该绝对时刻为准，tz_name 仅作展示时区
        moment = parsed.astimezone(timezone.utc)
    else:
        moment = parsed.replace(tzinfo=tz).astimezone(timezone.utc)
    return Deadline(at_utc_iso=moment.isoformat(), timezone=tz_name)


def now_is_past(deadline_utc_iso: str, now_utc: datetime) -> bool:
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    deadline = datetime.fromisoformat(deadline_utc_iso)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return now_utc >= deadline.astimezone(timezone.utc)


def to_local(deadline_utc_iso: str, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return deadline_utc_iso
    moment = datetime.fromisoformat(deadline_utc_iso)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).isoformat()
