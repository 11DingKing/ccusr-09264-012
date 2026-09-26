"""敏感材料最小披露策略。

规则（按机构隔离 + 按角色）：
- 非敏感材料：参与该机构评审流程的角色可见；
- 敏感企业反馈：仅本机构管理员、被正式分配到包含该材料评审包的评审人、
  质量权威机构、审计可见；本机构普通提交人不可见；
- 任何外机构用户一律不可见（审计除外，审计可跨机构只读）；
- 评审人若其请求已被取消（重新分配给他人），从取消时刻起失去该包
  敏感材料的访问权（权限变化即时生效）。
"""
from __future__ import annotations

from .enums import RequestStatus, Role, Sensitivity
from .models import PackageEntry, ReviewPackage, User


class DisclosureContext:
    """一次访问的权限上下文：用户当前有效的评审分配。

    active_request_package_ids: 该用户作为评审人、状态仍为
    pending/accepted/completed（即未 cancelled/declined）的请求所在包。
    declined 也不应保留访问权——评审人拒绝后即与该包无关。
    """

    ACTIVE_STATUSES = frozenset(
        {
            RequestStatus.PENDING.value,
            RequestStatus.ACCEPTED.value,
            RequestStatus.COMPLETED.value,
        }
    )

    def __init__(self, user: User, active_request_package_ids: set[str]) -> None:
        self.user = user
        self.active_package_ids = active_request_package_ids

    def can_see_entry(
        self,
        entry: PackageEntry,
        package: ReviewPackage | None = None,
    ) -> bool:
        user = self.user
        is_auditor = user.has_role(Role.AUDITOR)
        is_authority = user.has_role(Role.QUALITY_AUTHORITY)
        institution = package_institution(entry, package)
        same_institution = (
            user.institution_id is not None and user.institution_id == institution
        )

        # 全局只读角色
        if is_auditor or is_authority:
            return True

        is_sensitive = entry.sensitivity == Sensitivity.SENSITIVE.value

        # 本机构成员视角
        if same_institution:
            if user.has_role(Role.INSTITUTION_ADMIN):
                return True  # 管理员可见本机构全部材料
            if user.has_role(Role.INSTITUTION_SUBMITTER):
                return not is_sensitive  # 提交人不见敏感企业反馈
            return False

        # 跨机构：只有“仍被有效分配到该包”的评审人可见
        if user.has_role(Role.REVIEWER):
            return entry.package_id in self.active_package_ids
        return False


def package_institution(entry: PackageEntry, package: ReviewPackage | None) -> str | None:
    if package is not None:
        return package.institution_id
    return None


def redact_entry(entry: PackageEntry, visible: bool) -> dict:
    """不可见时给出稳定的占位结构，证明材料存在但不泄露内容指纹之外的信息。

    sha256 属于内容指纹，本身也可能泄露内容，故对无权限者一并遮蔽；
    仅暴露材料在清单中的存在与类别。
    """
    base = {
        "entry_id": entry.entry_id,
        "package_id": entry.package_id,
        "material_id": entry.material_id,
        "version_id": entry.version_id,
        "kind": entry.kind,
        "sensitivity": entry.sensitivity,
    }
    if not visible:
        base["redacted"] = True
        return base
    base.update(
        {
            "sha256": entry.sha256,
            "redacted": False,
        }
    )
    return base
