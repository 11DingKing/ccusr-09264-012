"""敏感企业反馈最小披露：机构隔离、角色限制、权限变化即时生效。"""
import unittest

from service_09252_006.domain.enums import (
    Decision,
    MaterialKind,
    Role,
    Sensitivity,
)
from service_09252_006.domain.errors import PermissionDeniedError
from tests.flow import complete_review, seal_new_package, upload_material
from tests.support import Harness


class DisclosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.submitter = self.h.user(
            "sub-a", Role.INSTITUTION_SUBMITTER
        )
        self.admin_b = self.h.user(
            "admin-b", Role.INSTITUTION_ADMIN, institution_id="inst-b"
        )
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.auditor = self.h.user("aud", Role.AUDITOR, institution_id=None)
        self.reviewer = self.h.user(
            "rev-1", Role.REVIEWER, institution_id="inst-ext"
        )
        self.reviewer2 = self.h.user(
            "rev-2", Role.REVIEWER, institution_id="inst-ext2"
        )
        self.sealed = seal_new_package(self.h, self.admin)
        self.pid = self.sealed.package_id
        # 敏感反馈条目是 items[1]
        self.sensitive_version = self.sealed.items[1].version["version_id"]
        self.normal_version = self.sealed.items[0].version["version_id"]

    def tearDown(self) -> None:
        self.h.close()

    def _find(self, view, version_id):
        return next(e for e in view["entries"] if e["version_id"] == version_id)

    def test_submitter_of_same_institution_cannot_see_sensitive_feedback(self) -> None:
        view = self.h.ctx.packages.build_package_view(self.submitter, self.pid)
        sensitive = self._find(view, self.sensitive_version)
        self.assertTrue(sensitive["redacted"])
        normal = self._find(view, self.normal_version)
        self.assertFalse(normal["redacted"])

    def test_other_institution_member_sees_nothing(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.packages.build_package_view(self.admin_b, self.pid)

    def test_unassigned_reviewer_cannot_download_sensitive(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.packages.download_entry(
                self.reviewer,
                package_id=self.pid,
                version_id=self.sensitive_version,
            )

    def test_assigned_reviewer_gets_then_loses_access_after_cancel(self) -> None:
        req = self.h.ctx.reviews.assign_reviewer(
            self.authority,
            package_id=self.pid,
            reviewer_id=self.reviewer.user_id,
        )
        # 分配后（pending）即可见
        view = self.h.ctx.packages.build_package_view(self.reviewer, self.pid)
        self.assertFalse(self._find(view, self.sensitive_version)["redacted"])

        meta, data, _ = self.h.ctx.packages.download_entry(
            self.reviewer,
            package_id=self.pid,
            version_id=self.sensitive_version,
        )
        self.assertEqual(data, "敏感反馈：企业要求匿名".encode("utf-8"))

        # 取消分配 -> 权限即时收回
        self.h.ctx.reviews.cancel_request(
            self.authority, request_id=req["request_id"], reason="改派"
        )
        view2 = self.h.ctx.packages.build_package_view(self.reviewer, self.pid)
        self.assertTrue(self._find(view2, self.sensitive_version)["redacted"])
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.packages.download_entry(
                self.reviewer,
                package_id=self.pid,
                version_id=self.sensitive_version,
            )

    def test_declined_reviewer_loses_access(self) -> None:
        req = self.h.ctx.reviews.assign_reviewer(
            self.authority,
            package_id=self.pid,
            reviewer_id=self.reviewer.user_id,
        )
        self.h.ctx.reviews.respond_assignment(
            self.reviewer, request_id=req["request_id"], accept=False
        )
        view = self.h.ctx.packages.build_package_view(self.reviewer, self.pid)
        self.assertTrue(self._find(view, self.sensitive_version)["redacted"])

    def test_reviewer_from_other_assignment_cannot_see(self) -> None:
        # reviewer2 被分配到另一个包，不能看本包敏感内容
        other_item = upload_material(
            self.h, self.admin,
            kind=MaterialKind.ENTERPRISE_FEEDBACK.value,
            data="另一份敏感反馈".encode("utf-8"),
            title="反馈2",
            sensitivity=Sensitivity.SENSITIVE.value,
        )
        other = seal_new_package(
            self.h, self.admin, items=[other_item], title="另一个包"
        )
        self.h.ctx.reviews.assign_reviewer(
            self.authority,
            package_id=other.package_id,
            reviewer_id=self.reviewer2.user_id,
        )
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.packages.build_package_view(self.reviewer2, self.pid)

    def test_authority_and_auditor_see_sensitive(self) -> None:
        for actor in (self.authority, self.auditor):
            view = self.h.ctx.packages.build_package_view(actor, self.pid)
            self.assertFalse(self._find(view, self.sensitive_version)["redacted"])

    def test_role_change_revokes_access_immediately(self) -> None:
        """用户角色被调整（例如评审人资格取消）后，访问立即按新角色判定。"""
        self.h.ctx.reviews.assign_reviewer(
            self.authority,
            package_id=self.pid,
            reviewer_id=self.reviewer.user_id,
        )
        # 管理员把该用户角色清空
        from service_09252_006.domain.models import User

        self.h.repo.upsert_user(
            User(
                user_id=self.reviewer.user_id,
                institution_id=self.reviewer.institution_id,
                roles=(),
                display_name=self.reviewer.display_name,
            )
        )
        # 新请求按身份重新加载用户（API 每次均如此），权限立即失效
        fresh_reviewer = self.h.repo.get_user(self.reviewer.user_id)
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.packages.build_package_view(fresh_reviewer, self.pid)
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.packages.download_entry(
                fresh_reviewer,
                package_id=self.pid,
                version_id=self.sensitive_version,
            )


if __name__ == "__main__":
    unittest.main()
