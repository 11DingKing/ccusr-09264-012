"""测试共用构造：临时数据库、固定时钟、预置用户。"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from service_09252_006.application.container import ApplicationContext
from service_09252_006.application.ports import FixedClock, SequentialIdGenerator
from service_09252_006.domain.enums import Role
from service_09252_006.domain.models import User

START = datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc)  # 09:00 上海


class Harness:
    def __init__(self, moment: datetime | None = None) -> None:
        fd, self.db_path = tempfile.mkstemp(prefix="qe-test-", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)  # 让仓储自行建库
        self.clock = FixedClock(moment or START)
        self.ids = SequentialIdGenerator()
        self.ctx = ApplicationContext(
            self.db_path, clock=self.clock, ids=self.ids
        )
        self.repo = self.ctx.repo

    def close(self) -> None:
        self.ctx.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def __enter__(self) -> "Harness":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def user(
        self,
        user_id: str,
        roles,
        *,
        institution_id: str | None = "inst-a",
        display_name: str = "",
        token: str | None = None,
    ) -> User:
        if isinstance(roles, Role):
            roles = (roles,)
        roles = tuple(r.value if isinstance(r, Role) else r for r in roles)
        u = User(
            user_id=user_id,
            institution_id=institution_id,
            roles=roles,
            display_name=display_name or user_id,
        )
        self.repo.upsert_user(u)
        if token:
            self.repo.put_token(token, user_id, self.clock.now_iso())
        return u


def shanghai(hour: int, minute: int = 0, day: int = 25) -> str:
    return (
        datetime(2026, 9, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
        .isoformat()
    )
