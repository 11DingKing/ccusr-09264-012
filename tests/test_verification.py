"""离线完整性核验命令：正常通过、篡改检出、撤回标注、CLI 退出码。"""
import json
import subprocess
import sys
import unittest

from service_09252_006.application.verification import verify_database
from service_09252_006.domain.enums import Decision, Role
from service_09252_006.domain.fingerprint import digest_bytes
from tests.flow import complete_review, seal_new_package
from tests.support import Harness


class VerificationTests(unittest.TestCase):
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

    def test_clean_database_passes(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        complete_review(self.h, self.authority, self.reviewer, sealed.package_id)
        self.h.ctx.reviews.issue_decision(
            self.authority, package_id=sealed.package_id,
            decision=Decision.APPROVED.value,
        )
        self.h.ctx.close()  # 落盘
        report = verify_database(self.h.db_path)
        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.blob_count, 2)
        self.assertEqual(report.decided_count, 1)

    def test_tampered_blob_detected(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        target_sha = sealed.items[0].version["sha256"].split(":", 1)[1]
        self.h.ctx.close()

        import sqlite3

        conn = sqlite3.connect(self.h.db_path)
        conn.execute(
            "UPDATE blobs SET data = ? WHERE sha256 = ?",
            ("被篡改的内容".encode("utf-8"), target_sha),
        )
        conn.commit()
        conn.close()

        report = verify_database(self.h.db_path)
        self.assertFalse(report.ok)
        kinds = {f["kind"] for f in report.failures}
        self.assertIn("blob_digest_mismatch", kinds)

    def test_manifest_tampering_detected(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        self.h.ctx.close()

        import sqlite3

        conn = sqlite3.connect(self.h.db_path)
        conn.execute(
            "UPDATE packages SET manifest_fingerprint = 'sha256:" + "f" * 64 + "'"
        )
        conn.commit()
        conn.close()

        report = verify_database(self.h.db_path)
        self.assertFalse(report.ok)
        self.assertTrue(
            any(f["kind"] == "manifest_fingerprint_mismatch" for f in report.failures)
        )

    def test_withdrawn_in_sealed_reported_as_warning_not_failure(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        self.h.ctx.evidence.withdraw_version(
            self.admin,
            version_id=sealed.items[0].version["version_id"],
            reason="错版",
        )
        self.h.ctx.close()
        report = verify_database(self.h.db_path)
        self.assertTrue(report.ok)  # 历史证据仍然完整
        self.assertEqual(len(report.withdrawn_in_sealed), 1)
        self.assertTrue(
            any(w["kind"] == "sealed_entry_withdrawn" for w in report.warnings)
        )

    def test_review_fingerprint_tampering_detected(self) -> None:
        sealed = seal_new_package(self.h, self.admin)
        # 走有异议的反对流程，制造 objections 留痕供篡改
        complete_review(
            self.h, self.authority, self.reviewer, sealed.package_id,
            verdict="object",
            objection={"category": "考核依据", "detail": "缺少评分标准"},
        )
        self.h.ctx.reviews.issue_decision(
            self.authority, package_id=sealed.package_id,
            decision=Decision.NEEDS_REVISION.value,
        )
        # 直接篡改异议内容（模拟绕过服务改库）
        import sqlite3

        self.h.ctx.close()
        conn = sqlite3.connect(self.h.db_path)
        conn.execute("UPDATE objections SET detail = '事后改写异议'")
        conn.commit()
        conn.close()

        report = verify_database(self.h.db_path)
        self.assertFalse(report.ok)
        self.assertTrue(
            any(f["kind"] == "review_fingerprint_mismatch" for f in report.failures)
        )


class CliVerifyTests(unittest.TestCase):
    def test_cli_exit_codes(self) -> None:
        from service_09252_006.cli import main

        with Harness() as h:
            admin = h.user("admin-a", Role.INSTITUTION_ADMIN)
            sealed = seal_new_package(h, admin)
            h.ctx.close()

            # 干净库：退出码 0
            rc = main(["verify", "--db", h.db_path, "--json"])
            self.assertEqual(rc, 0)

            # 篡改
            import sqlite3

            conn = sqlite3.connect(h.db_path)
            conn.execute(
                "UPDATE packages SET manifest_fingerprint = 'sha256:" + "0" * 64 + "'"
            )
            conn.commit()
            conn.close()

            rc = main(["verify", "--db", h.db_path, "--json"])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
