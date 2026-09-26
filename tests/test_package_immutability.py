"""评审包封存不变量、后补材料只能复审、材料撤回场景。"""
import unittest

from service_09252_006.domain.enums import (
    Decision,
    MaterialKind,
    PackageStatus,
    Role,
    Sensitivity,
)
from service_09252_006.domain.errors import ConflictError, ImmutabilityError
from tests.flow import complete_review, seal_new_package, upload_material
from tests.support import Harness


class PackageImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.reviewer = self.h.user(
            "rev-1", Role.REVIEWER, institution_id="inst-ext"
        )

    def tearDown(self) -> None:
        self.h.close()

    def test_sealed_package_pins_exact_versions(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        pkg = self.h.repo.get_package(sealed.package_id)
        self.assertEqual(pkg.status, PackageStatus.SEALED.value)
        self.assertIsNotNone(pkg.manifest_fingerprint)
        pinned = {(e.material_id, e.version_id) for e in pkg.entries}
        self.assertEqual(len(pinned), 2)

        # 上传新版本不改变封存包
        syllabus_item = sealed.items[0]
        v2 = self.h.ctx.evidence.upload_version(
            self.admin,
            material_id=syllabus_item.material["material_id"],
            data="大纲 v2 被覆盖式更新".encode("utf-8"),
        )
        pkg_after = self.h.repo.get_package(sealed.package_id)
        self.assertEqual(
            pkg_after.manifest_fingerprint, pkg.manifest_fingerprint
        )
        self.assertNotIn(
            v2["version_id"], [e.version_id for e in pkg_after.entries]
        )

    def test_late_file_cannot_enter_decided_package_but_creates_rereview(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        complete_review(
            self.h, self.authority, self.reviewer, sealed.package_id
        )
        decision = self.h.ctx.reviews.issue_decision(
            self.authority,
            package_id=sealed.package_id,
            decision=Decision.APPROVED.value,
        )
        self.assertEqual(decision["status"], PackageStatus.DECIDED.value)

        # 后补一份新材料
        late = upload_material(
            self.h, self.admin, kind=MaterialKind.ASSESSMENT.value,
            data="补充考核说明".encode("utf-8"), title="后补考核",
        )
        with self.assertRaises(ImmutabilityError):
            self.h.ctx.packages.add_entry(
                self.admin,
                package_id=sealed.package_id,
                version_id=late.version["version_id"],
            )

        # 只能发起复审请求（新包），并带上旧包未撤回条目
        re = self.h.ctx.packages.create_package(
            self.admin,
            title="复审包",
            supersedes_package_id=sealed.package_id,
        )
        self.assertEqual(re["supersedes_package_id"], sealed.package_id)
        re_pkg = self.h.repo.get_package(re["package_id"])
        self.assertEqual(re_pkg.status, PackageStatus.DRAFT.value)
        self.assertEqual(len(re_pkg.entries), 2)
        self.h.ctx.packages.add_entry(
            self.admin,
            package_id=re["package_id"],
            version_id=late.version["version_id"],
        )
        resealed = self.h.ctx.packages.seal_package(
            self.admin, package_id=re["package_id"]
        )
        # 新包指纹不同于旧包
        self.assertNotEqual(
            resealed["manifest_fingerprint"],
            sealed.sealed["manifest_fingerprint"],
        )

    def test_cannot_rereview_package_before_decision(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        with self.assertRaises(ConflictError):
            self.h.ctx.packages.create_package(
                self.admin,
                title="提前复审",
                supersedes_package_id=sealed.package_id,
            )

    def test_withdrawn_version_cannot_enter_package(self) -> None:
        item = upload_material(self.h, self.admin, data="将被撤回".encode("utf-8"))
        self.h.ctx.evidence.withdraw_version(
            self.admin, version_id=item.version["version_id"], reason="错误版本"
        )
        pkg = self.h.ctx.packages.create_package(self.admin, title="P")
        with self.assertRaises(ConflictError):
            self.h.ctx.packages.add_entry(
                self.admin,
                package_id=pkg["package_id"],
                version_id=item.version["version_id"],
            )

    def test_withdrawal_after_seal_preserves_history_and_flags_review(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        old_fingerprint = sealed.sealed["manifest_fingerprint"]
        target_version = sealed.items[0].version["version_id"]

        # 封存后撤回：历史包不变
        self.h.ctx.evidence.withdraw_version(
            self.admin, version_id=target_version, reason="发现错误"
        )
        pkg = self.h.repo.get_package(sealed.package_id)
        self.assertEqual(pkg.manifest_fingerprint, old_fingerprint)
        self.assertIn(target_version, [e.version_id for e in pkg.entries])

        # 以此为基础的复审包不会复制已撤回条目
        complete_review(
            self.h, self.authority, self.reviewer, sealed.package_id
        )
        self.h.ctx.reviews.issue_decision(
            self.authority,
            package_id=sealed.package_id,
            decision=Decision.NEEDS_REVISION.value,
        )
        re = self.h.ctx.packages.create_package(
            self.admin, title="复审",
            supersedes_package_id=sealed.package_id,
        )
        re_pkg = self.h.repo.get_package(re["package_id"])
        # 原 2 条，撤回 1 条，复制 1 条
        self.assertEqual(len(re_pkg.entries), 1)
        self.assertNotIn(target_version, [e.version_id for e in re_pkg.entries])

    def test_seal_with_withdrawn_entry_fails_even_under_race_like_check(self) -> None:
        item = upload_material(self.h, self.admin, data=b"x")
        pkg = self.h.ctx.packages.create_package(self.admin, title="P")
        self.h.ctx.packages.add_entry(
            self.admin, package_id=pkg["package_id"],
            version_id=item.version["version_id"],
        )
        self.h.ctx.evidence.withdraw_version(
            self.admin, version_id=item.version["version_id"], reason="事后撤回"
        )
        with self.assertRaises(ConflictError):
            self.h.ctx.packages.seal_package(
                self.admin, package_id=pkg["package_id"]
            )


if __name__ == "__main__":
    unittest.main()
