"""可替换端口：时钟与标识生成。

应用服务只依赖这些抽象，测试可注入固定时钟/可预测 ID，
从而稳定复现状态变化与跨时区场景。
"""
from __future__ import annotations

import abc
import uuid
from datetime import datetime, timezone


class Clock(abc.ABC):
    @abc.abstractmethod
    def now_utc(self) -> datetime:
        """返回带 tzinfo 的当前 UTC 时刻。"""

    def now_iso(self) -> str:
        return self.now_utc().isoformat()


class SystemClock(Clock):
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """测试用：固定在某个时刻，可手动推进。"""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FixedClock 需要带时区的时刻")
        self._moment = moment.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        return self._moment

    def advance(self, seconds: float = 0, **kwargs) -> None:
        from datetime import timedelta

        delta = timedelta(seconds=seconds, **{k: v for k, v in kwargs.items() if k != "seconds"})
        self._moment = self._moment + delta

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("需要带时区的时刻")
        self._moment = moment.astimezone(timezone.utc)


class IdGenerator(abc.ABC):
    @abc.abstractmethod
    def new_id(self, prefix: str) -> str:
        """生成一个带前缀的新标识。"""


class Uuid4IdGenerator(IdGenerator):
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"


class SequentialIdGenerator(IdGenerator):
    """测试用：prefix_1、prefix_2 …… 便于断言。"""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def new_id(self, prefix: str) -> str:
        self._counts[prefix] = self._counts.get(prefix, 0) + 1
        return f"{prefix}_{self._counts[prefix]}"
