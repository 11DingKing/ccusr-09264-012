"""评审流程、异议约束、签发规则与幂等性。"""
import unittest

from service_09252_006.domain.enums import (
    Decision,
    PackageStatus,
    RequestStatus,
    Role,
    Verdict,
)
from service_09252_006.domain.errors import (
    ConflictError,
    DeadlineExceededError,
    ValidationError,
)
from tests.flow import complete_review, seal_new_package
from tests.support import Harness, shanghai


class ReviewFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.reviewer = self.h.user(
            "rev-1", Role.REVIEWER, institution_id="inst-ext"
        )
        self.reviewer2 = self.h.user(
            "rev-2", Role.REVIEWER, institution_id="inst-ext2"
        )
        self.sealed = seal_new_package(self.h, self.admin)
        self.pid = self.sealed.package_id

    def tearDown(self) -> None:
        self.h.close()

    def test_full_approve_flow_fingers_decision_to_manifest(self) -> None:
        review = complete_review(
            self.h, self.authority, self.reviewer, self.pid
        )
        decision = self.h.ctx.reviews.issue_decision(
            self.authority,
            package_id=self.pid,
            decision=Decision.APPROVED.value,
            note="通过",
        )
        pkg = self.h.repo.get_package(self.pid)
        self.assertEqual(pkg.status, PackageStatus.DECIDED.value)
        self.assertEqual(decision["manifest_fingerprint"], pkg.manifest_fingerprint)
        self.assertIsNotNone(decision["review_fingerprint"])
        # 评审指纹在密码学意义上链到清单指纹：清单指纹变，评审指纹必变
        from service_09252_006.domain.fingerprint import review_record_fingerprint

        reqs = self.h.repo.list_requests_by_package(self.pid)
        objs = self.h.repo.list_objections_by_package(self.pid)
        request_payload = [
            {
                "request_id": r.request_id, "reviewer_id": r.reviewer_id,
                "status": r.status, "verdict": r.verdict, "comment": r.comment,
                "assigned_at": r.assigned_at, "completed_at": r.completed_at,
            }
            for r in reqs
        ]
        objection_payload = [
            {
                "objection_id": o.objection_id, "request_id": o.request_id,
                "reviewer_id": o.reviewer_id, "category": o.category,
                "detail": o.detail, "created_at": o.created_at,
            }
            for o in objs
        ]
        self.assertEqual(
            review_record_fingerprint(
                self.pid, pkg.manifest_fingerprint,
                request_payload, objection_payload,
            ),
            decision["review_fingerprint"],
        )
        other_manifest = "sha256:" + ("b" * 64)
        self.assertNotEqual(
            review_record_fingerprint(
                self.pid, other_manifest,
                request_payload, objection_payload,
            ),
            decision["review_fingerprint"],
        )

    def test_object_requires_objection(self) -> None:
        req = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.reviewer.user_id,
        )
        rid = req["request_id"]
        self.h.ctx.reviews.respond_assignment(
            self.reviewer, request_id=rid, accept=True
        )
        with self.assertRaises(ValidationError):
            self.h.ctx.reviews.submit_verdict(
                self.reviewer, request_id=rid, verdict=Verdict.OBJECT.value
            )
        self.h.ctx.reviews.record_objection(
            self.reviewer, request_id=rid,
            category="考核依据", detail="缺少评分标准",
        )
        result = self.h.ctx.reviews.submit_verdict(
            self.reviewer, request_id=rid, verdict=Verdict.OBJECT.value
        )
        self.assertEqual(result["status"], RequestStatus.COMPLETED.value)

        # 有反对结论时不能签发通过，但可签发需整改
        with self.assertRaises(ConflictError):
            self.h.ctx.reviews.issue_decision(
                self.authority, package_id=self.pid,
                decision=Decision.APPROVED.value,
            )
        decision = self.h.ctx.reviews.issue_decision(
            self.authority, package_id=self.pid,
            decision=Decision.NEEDS_REVISION.value,
        )
        self.assertEqual(decision["decision"], Decision.NEEDS_REVISION.value)

    def test_cannot_issue_without_completed_review(self) -> None:
        self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.reviewer.user_id,
        )
        with self.assertRaises(ConflictError):
            self.h.ctx.reviews.issue_decision(
                self.authority, package_id=self.pid,
                decision=Decision.APPROVED.value,
            )

    def test_reviewer_must_be_independent(self) -> None:
        internal = self.h.user(
            "rev-internal", Role.REVIEWER, institution_id="inst-a"
        )
        with self.assertRaises(ValidationError):
            self.h.ctx.reviews.assign_reviewer(
                self.authority, package_id=self.pid,
                reviewer_id=internal.user_id,
            )

    def test_multi_reviewer_concurrent_assignments(self) -> None:
        r1 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.reviewer.user_id,
        )
        r2 = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.reviewer2.user_id,
        )
        self.assertNotEqual(r1["request_id"], r2["request_id"])
        pkg = self.h.repo.get_package(self.pid)
        self.assertEqual(pkg.status, PackageStatus.UNDER_REVIEW.value)

        # 同一评审人重复分配 -> 回放
        r1_again = self.h.ctx.reviews.assign_reviewer(
            self.authority, package_id=self.pid,
            reviewer_id=self.reviewer.user_id,
        )
        self.assertEqual(r1_again["request_id"], r1["request_id"])
        self.assertTrue(r1_again["replayed"])


class DeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        # 固定在 2026-09-25 09:00 上海时间
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.reviewer = self.h.user(
            "rev-1", Role.REVIEWER, institution_id="inst-ext"
        )
        self.sealed = seal_new_package(self.h, self.admin)
        self.pid = self.sealed.package_id

    def tearDown(self) -> None:
        self.h.close()

    def _assign(self, local_iso, tz_name):
        return self.h.ctx.reviews.assign_reviewer(
            self.authority,
            package_id=self.pid,
            reviewer_id=self.reviewer.user_id,
            deadline_local_iso=local_iso,
            deadline_timezone=tz_name,
        )

    def test_same_instant_different_timezone_representation(self) -> None:
        # 上海 17:00 == 伦敦 10:00（9 月英国 BST, UTC+1）== UTC 09:00
        a = self._assign(shanghai(17, day=25), "Asia/Shanghai")
        b = self._assign("2026-09-25T10:00", "Europe/London")
        self.assertEqual(a["deadline_at_utc"], b["deadline_at_utc"])
        self.assertEqual(a["deadline_at_utc"], "2026-09-25T09:00:00+00:00")

    def test_action_before_deadline_allowed(self) -> None:
        req = self._assign(shanghai(18, day=25), "Asia/Shanghai")  # 还有 9 小时
        self.h.ctx.reviews.respond_assignment(
            self.reviewer, request_id=req["request_id"], accept=True
        )
        self.h.ctx.reviews.record_objection(
            self.reviewer, request_id=req["request_id"],
            category="x", detail="y",
        )

    def test_action_after_deadline_rejected(self) -> None:
        req = self._assign(shanghai(9, day=25), "Asia/Shanghai")  # 恰好到期
        self.h.ctx.reviews.respond_assignment(
            self.reviewer, request_id=req["request_id"], accept=True
        )
        with self.assertRaises(DeadlineExceededError):
            self.h.ctx.reviews.record_objection(
                self.reviewer, request_id=req["request_id"],
                category="x", detail="y",
            )
        with self.assertRaises(DeadlineExceededError):
            self.h.ctx.reviews.submit_verdict(
                self.reviewer, request_id=req["request_id"],
                verdict=Verdict.APPROVE.value,
            )

    def test_clock_crosses_timezone_day_boundary(self) -> None:
        # 洛杉矶 9/24 18:00（PDT, UTC-7）== UTC 9/25 01:00 —— 恰好当前时刻到期
        req = self._assign("2026-09-24T18:00", "America/Los_Angeles")
        self.assertEqual(req["deadline_at_utc"], "2026-09-25T01:00:00+00:00")
        self.h.ctx.reviews.respond_assignment(
            self.reviewer, request_id=req["request_id"], accept=True
        )
        with self.assertRaises(DeadlineExceededError):
            self.h.ctx.reviews.submit_verdict(
                self.reviewer, request_id=req["request_id"],
                verdict=Verdict.APPROVE.value,
            )


class IdempotencyTests(unittest.TestCase):
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

    def test_repeated_calls_with_same_key_replay(self) -> None:
        m1 = self.h.ctx.evidence.register_material(
            self.admin, kind="syllabus", title="M",
            idempotency_key="key-mat-1",
        )
        m2 = self.h.ctx.evidence.register_material(
            self.admin, kind="syllabus", title="M（重试，标题不同也忽略）",
            idempotency_key="key-mat-1",
        )
        self.assertEqual(m1["material_id"], m2["material_id"])
        self.assertTrue(m2["replayed"])

    def test_retry_after_failure_is_safe(self) -> None:
        # 用错误内容 + 客户端摘要校验，调用失败；换正确参数同流程可继续
        m = self.h.ctx.evidence.register_material(
            self.admin, kind="syllabus", title="M"
        )
        from service_09252_006.domain.errors import ValidationError

        with self.assertRaises(ValidationError):
            self.h.ctx.evidence.upload_version(
                self.admin, material_id=m["material_id"], data=b"v1",
                expected_sha256="sha256:" + "0" * 64,
                idempotency_key="up-1",
            )
        v = self.h.ctx.evidence.upload_version(
            self.admin, material_id=m["material_id"], data=b"v1",
            idempotency_key="up-1",
        )
        self.assertFalse(v.get("replayed"))


if __name__ == "__main__":
    unittest.main()
