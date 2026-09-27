"""复核案件合并：原案保留、旧编号跳转、重复合并不生成新主案。"""
import unittest

from service_09252_006.application.verification import verify_database
from service_09252_006.domain.enums import Decision, PackageStatus, Role
from service_09252_006.domain.errors import (
    ConflictError,
    ImmutabilityError,
    PermissionDeniedError,
    ValidationError,
)
from tests.flow import complete_review, seal_new_package, upload_material
from tests.support import Harness


class CaseMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.case1 = seal_new_package(self.h, self.admin, title="案件一")
        self.case2 = seal_new_package(self.h, self.admin, title="案件二")

    def tearDown(self) -> None:
        self.h.close()

    def _merge(self, **kwargs):
        kwargs.setdefault(
            "source_package_ids", [self.case1.package_id, self.case2.package_id]
        )
        return self.h.ctx.merges.merge_cases(self.admin, **kwargs)

    def test_merge_creates_master_and_keeps_sources(self) -> None:
        result = self._merge(note="法务要求并案")
        master_id = result["master_package_id"]
        self.assertFalse(result["replayed"])

        # 主案是新评审包（draft），复制两案未撤回条目（默认每案 2 份）
        master = self.h.repo.get_package(master_id)
        self.assertEqual(master.status, PackageStatus.DRAFT.value)
        self.assertEqual(len(master.entries), 4)

        # 原案保留：编号不回收、状态 merged、独立证据与封存指纹原样
        for case in (self.case1, self.case2):
            source = self.h.repo.get_package(case.package_id)
            self.assertIsNotNone(source)
            self.assertEqual(source.status, PackageStatus.MERGED.value)
            self.assertEqual(len(source.entries), 2)
            self.assertEqual(
                source.manifest_fingerprint,
                case.sealed["manifest_fingerprint"],
            )

        # 合并关系落库：成员索引完整，审计动作名为“复核案件合并”
        members = self.h.repo.list_merge_members(master_id)
        self.assertEqual(
            {m.package_id for m in members},
            {self.case1.package_id, self.case2.package_id},
        )
        actions = [a.action for a in self.h.repo.list_audit(master_id)]
        self.assertIn("复核案件合并", actions)

    def test_duplicate_merge_request_replays_same_master(self) -> None:
        first = self._merge()
        # 顺序不同 + 重复编号 + 不同备注：仍是同一个合并请求
        second = self._merge(
            source_package_ids=[
                self.case2.package_id,
                self.case1.package_id,
                self.case2.package_id,
            ],
            note="重复提交",
        )
        self.assertTrue(second["replayed"])
        self.assertEqual(second["master_package_id"], first["master_package_id"])
        self.assertEqual(second["merge_id"], first["merge_id"])

        # 没有制造新主案：全库一条合并记录，包总数 = 两原案 + 一主案
        self.assertEqual(len(self.h.repo.list_merges()), 1)
        self.assertEqual(len(self.h.repo.list_packages("inst-a")), 3)

    def test_duplicate_merge_with_idempotency_key(self) -> None:
        first = self._merge(idempotency_key="merge-1")
        replay = self._merge(idempotency_key="merge-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["master_package_id"], first["master_package_id"])
        self.assertEqual(len(self.h.repo.list_merges()), 1)

    def test_overlapping_merge_is_rejected_not_new_master(self) -> None:
        first = self._merge()
        case3 = seal_new_package(self.h, self.admin, title="案件三")
        with self.assertRaises(ConflictError):
            self._merge(
                source_package_ids=[self.case2.package_id, case3.package_id]
            )
        # 没有制造新主案
        self.assertEqual(len(self.h.repo.list_merges()), 1)
        active = {
            p.package_id
            for p in self.h.repo.list_packages("inst-a")
            if p.status != PackageStatus.MERGED.value
        }
        self.assertEqual(active, {first["master_package_id"], case3.package_id})

    def test_old_case_number_resolves_to_master(self) -> None:
        master_id = self._merge()["master_package_id"]

        # Python 查询：旧编号 -> 主案
        resolved = self.h.ctx.merges.resolve_case_number(
            self.admin, self.case1.package_id
        )
        self.assertTrue(resolved["merged"])
        self.assertEqual(resolved["master_package_id"], master_id)
        # 仓储层同样可跳转
        self.assertEqual(
            self.h.repo.resolve_package_id(self.case2.package_id), master_id
        )
        effective = self.h.repo.get_package_effective(self.case1.package_id)
        self.assertEqual(effective.package_id, master_id)
        # 未合并的编号原样返回
        self.assertEqual(self.h.repo.resolve_package_id(master_id), master_id)
        self.assertFalse(
            self.h.ctx.merges.resolve_case_number(self.admin, master_id)["merged"]
        )

    def test_old_package_view_keeps_evidence_and_points_to_master(self) -> None:
        master_id = self._merge()["master_package_id"]
        view = self.h.ctx.packages.build_package_view(
            self.admin, self.case1.package_id
        )
        # 原案视图仍在，独立证据保留，并给出主案跳转
        self.assertEqual(view["package_id"], self.case1.package_id)
        self.assertEqual(view["status"], PackageStatus.MERGED.value)
        self.assertEqual(len(view["entries"]), 2)
        self.assertEqual(view["merged_into"], master_id)

        master_view = self.h.ctx.packages.build_package_view(self.admin, master_id)
        self.assertIsNone(master_view["merged_into"])
        self.assertEqual(
            master_view["merge"]["source_package_ids"],
            sorted([self.case1.package_id, self.case2.package_id]),
        )

    def test_merge_requires_two_distinct_cases(self) -> None:
        with self.assertRaises(ValidationError):
            self._merge(source_package_ids=[self.case1.package_id])
        with self.assertRaises(ValidationError):
            self._merge(
                source_package_ids=[self.case1.package_id, self.case1.package_id]
            )

    def test_merge_permission_and_institution_scope(self) -> None:
        other_admin = self.h.user(
            "admin-b", Role.INSTITUTION_ADMIN, institution_id="inst-b"
        )
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.merges.merge_cases(
                other_admin,
                source_package_ids=[self.case1.package_id, self.case2.package_id],
            )
        # 质量机构可跨机构合并
        result = self.h.ctx.merges.merge_cases(
            self.authority,
            source_package_ids=[self.case1.package_id, self.case2.package_id],
        )
        self.assertFalse(result["replayed"])

    def test_merged_source_is_read_only(self) -> None:
        self._merge()
        uploaded = upload_material(self.h, self.admin, data=b"late")
        with self.assertRaises(ImmutabilityError):
            self.h.ctx.packages.add_entry(
                self.admin,
                package_id=self.case1.package_id,
                version_id=uploaded.version["version_id"],
            )

    def test_decided_case_merge_keeps_fingerprints_verifiable(self) -> None:
        # 一案走完评审签发，再与另一案合并；离线核验仍全部通过
        reviewer = self.h.user("rev-1", Role.REVIEWER, institution_id="inst-ext")
        complete_review(self.h, self.authority, reviewer, self.case1.package_id)
        self.h.ctx.reviews.issue_decision(
            self.authority,
            package_id=self.case1.package_id,
            decision=Decision.APPROVED.value,
        )
        self._merge()
        report = verify_database(self.h.db_path)
        self.assertTrue(report.ok, report.failures)


if __name__ == "__main__":
    unittest.main()
