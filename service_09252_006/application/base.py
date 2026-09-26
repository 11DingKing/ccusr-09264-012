"""应用服务共用工具：鉴权、幂等、审计。"""
from __future__ import annotations

import functools
from typing import Callable

from ..domain.errors import PermissionDeniedError
from ..domain.enums import Role
from ..domain.models import AuditEntry, User
from ..application.ports import Clock, IdGenerator
from ..application.repository import Repository


def require_user(actor: User | None) -> User:
    if actor is None:
        raise PermissionDeniedError("缺少操作身份")
    return actor


def require_roles(actor: User, *roles: Role) -> User:
    require_user(actor)
    if not any(actor.has_role(r) for r in roles):
        raise PermissionDeniedError(
            "当前角色无权执行该操作",
            details={"required_any": [r.value for r in roles]},
        )
    return actor


class Service:
    """应用服务基类：仓库、时钟、ID 端口与审计/幂等封装。"""

    def __init__(
        self,
        repo: Repository,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self.repo = repo
        self.clock = clock
        self.ids = ids

    def audit(
        self,
        actor_id: str,
        action: str,
        *,
        package_id: str | None = None,
        institution_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self.repo.insert_audit(
            AuditEntry(
                audit_id=self.ids.new_id("aud"),
                package_id=package_id,
                institution_id=institution_id,
                actor_id=actor_id,
                action=action,
                at=self.clock.now_iso(),
                detail=detail or {},
            )
        )

    def idempotent(self, key: str | None, work: Callable[[], dict]) -> dict:
        """在单个写事务内执行 work，并按 key 记录结果。

        - key 已存在：直接回放首次结果（不重复执行）；
        - key 为空：不做幂等记录；
        - work 抛错则整体回滚，调用方用同一 key 重试是安全的（可恢复）。
        """
        if key is None:
            with self.repo.transaction():
                return work()
        with self.repo.transaction():
            prior = self.repo.get_idempotent_result(key)
            if prior is not None:
                prior["replayed"] = True
                return prior
            result = work()
            result.setdefault("replayed", False)
            stored = dict(result)
            self.repo.save_idempotent_result(key, stored)
            return result
