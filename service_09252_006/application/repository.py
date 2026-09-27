"""仓库抽象端口。

应用服务只依赖此接口；SQLite 实现见 persistence 层。所有涉及多步
状态推进的用例必须在 transaction() 上下文中完成，以保证并发复审下
的可串行化与崩溃可恢复。
"""
from __future__ import annotations

import abc
from contextlib import AbstractContextManager

from ..domain.models import (
    AuditEntry,
    Blob,
    Material,
    MaterialVersion,
    Objection,
    PackageEntry,
    PackageMerge,
    ReviewPackage,
    ReviewRequest,
    User,
)


class Repository(abc.ABC):
    # ---- 事务/生命周期 ----
    @abc.abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """串行写事务（SQLite 实现为 BEGIN IMMEDIATE）。"""

    @abc.abstractmethod
    def close(self) -> None: ...

    # ---- 用户/角色 ----
    @abc.abstractmethod
    def upsert_user(self, user: User) -> None: ...

    @abc.abstractmethod
    def get_user(self, user_id: str) -> User | None: ...

    @abc.abstractmethod
    def put_token(self, token: str, user_id: str, at: str) -> None: ...

    @abc.abstractmethod
    def get_user_by_token(self, token: str) -> User | None: ...

    # ---- 幂等键 ----
    @abc.abstractmethod
    def get_idempotent_result(self, key: str) -> dict | None: ...

    @abc.abstractmethod
    def save_idempotent_result(self, key: str, result: dict) -> None: ...

    # ---- 材料与版本 ----
    @abc.abstractmethod
    def put_blob(self, blob: Blob) -> None: ...

    @abc.abstractmethod
    def get_blob(self, sha256: str) -> Blob | None: ...

    @abc.abstractmethod
    def insert_material(self, material: Material) -> None: ...

    @abc.abstractmethod
    def get_material(self, material_id: str) -> Material | None: ...

    @abc.abstractmethod
    def insert_version(self, version: MaterialVersion) -> None: ...

    @abc.abstractmethod
    def get_version(self, version_id: str) -> MaterialVersion | None: ...

    @abc.abstractmethod
    def find_version_by_digest(
        self, material_id: str, sha256: str
    ) -> MaterialVersion | None: ...

    @abc.abstractmethod
    def list_versions(self, material_id: str) -> list[MaterialVersion]: ...

    @abc.abstractmethod
    def mark_version_withdrawn(
        self, version_id: str, withdrawn: bool, at: str
    ) -> bool: ...

    @abc.abstractmethod
    def mark_material_withdrawn(
        self, material_id: str, withdrawn: bool
    ) -> bool: ...

    # ---- 评审包 ----
    @abc.abstractmethod
    def insert_package(self, package: ReviewPackage) -> None: ...

    @abc.abstractmethod
    def get_package(self, package_id: str) -> ReviewPackage | None: ...

    @abc.abstractmethod
    def list_packages(
        self, institution_id: str | None = None,
    ) -> list[ReviewPackage]: ...

    @abc.abstractmethod
    def insert_entry(self, entry: PackageEntry) -> None: ...

    @abc.abstractmethod
    def entry_exists(self, package_id: str, version_id: str) -> bool: ...

    @abc.abstractmethod
    def transition_package_status(
        self,
        package_id: str,
        expected_status: str,
        new_status: str,
        **fields,
    ) -> bool:
        """条件更新；状态不再是 expected_status 时返回 False（并发冲突）。"""

    # ---- 复核案件合并 ----
    @abc.abstractmethod
    def insert_merge(self, merge: PackageMerge) -> None:
        """追加合并事件与原案成员（原案行本身不动）。"""

    @abc.abstractmethod
    def find_merge_by_group_key(self, group_key: str) -> PackageMerge | None:
        """按原案集合的规范化键查找：重复合并请求据此回放既有主案。"""

    @abc.abstractmethod
    def find_merge_by_source(self, source_package_id: str) -> PackageMerge | None:
        """该原案是否已并入某个主案。"""

    @abc.abstractmethod
    def find_merge_by_primary(self, primary_package_id: str) -> PackageMerge | None:
        """该包是否为某次合并的主案。"""

    @abc.abstractmethod
    def get_merge(self, merge_id: str) -> PackageMerge | None: ...

    @abc.abstractmethod
    def resolve_package_id(self, package_id: str) -> str:
        """旧编号跳转：若该编号是已合并原案，返回主案编号；否则原样返回。"""

    # ---- 评审请求 ----
    @abc.abstractmethod
    def insert_request(self, request: ReviewRequest) -> None: ...

    @abc.abstractmethod
    def get_request(self, request_id: str) -> ReviewRequest | None: ...

    @abc.abstractmethod
    def list_requests_by_package(self, package_id: str) -> list[ReviewRequest]: ...

    @abc.abstractmethod
    def list_active_requests_by_reviewer(self, reviewer_id: str) -> list[ReviewRequest]:
        """状态属于 pending/accepted/completed 的请求（权限变化依据）。"""

    @abc.abstractmethod
    def update_request(self, request: ReviewRequest) -> None: ...

    # ---- 异议 ----
    @abc.abstractmethod
    def insert_objection(self, objection: Objection) -> None: ...

    @abc.abstractmethod
    def list_objections_by_package(self, package_id: str) -> list[Objection]: ...

    # ---- 审计 ----
    @abc.abstractmethod
    def insert_audit(self, entry: AuditEntry) -> None: ...

    @abc.abstractmethod
    def list_audit(
        self, package_id: str | None = None, limit: int = 200
    ) -> list[AuditEntry]: ...
