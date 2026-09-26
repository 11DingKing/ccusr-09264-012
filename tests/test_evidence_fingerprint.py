"""内容指纹：内容寻址、版本关系、幂等上传。"""
import unittest

from service_09252_006.domain.enums import MaterialKind, Role
from service_09252_006.domain.fingerprint import (
    canonical_json,
    digest_bytes,
    manifest_fingerprint,
)
from tests.support import Harness


class FingerprintTests(unittest.TestCase):
    def test_canonical_json_is_stable(self) -> None:
        a = canonical_json({"b": 1, "a": [1, 2, {"c": 3}]})
        b = canonical_json({"a": [1, 2, {"c": 3}], "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(digest_bytes(b"abc"), digest_bytes(b"abc"))
        self.assertNotEqual(digest_bytes(b"abc"), digest_bytes(b"abd"))

    def test_manifest_fingerprint_independent_of_entry_order(self) -> None:
        entries = [
            {"material_id": "m2", "version_id": "v2", "sha256": "x" * 64,
             "kind": "faculty", "sensitivity": "normal"},
            {"material_id": "m1", "version_id": "v1", "sha256": "y" * 64,
             "kind": "syllabus", "sensitivity": "normal"},
        ]
        f1 = manifest_fingerprint("p1", "inst-a", entries, "2026-09-25T01:00:00+00:00")
        f2 = manifest_fingerprint("p1", "inst-a", list(reversed(entries)),
                                  "2026-09-25T01:00:00+00:00")
        self.assertEqual(f1, f2)
        # 封存时间不同 -> 指纹不同（两次封存可区分）
        f3 = manifest_fingerprint("p1", "inst-a", entries, "2026-09-25T02:00:00+00:00")
        self.assertNotEqual(f1, f3)


class EvidenceVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)

    def tearDown(self) -> None:
        self.h.close()

    def test_upload_dedup_and_version_chain(self) -> None:
        m = self.h.ctx.evidence.register_material(
            self.admin, kind=MaterialKind.SYLLABUS.value, title="大纲"
        )
        v1 = self.h.ctx.evidence.upload_version(
            self.admin, material_id=m["material_id"], data=b"v1"
        )
        v1_again = self.h.ctx.evidence.upload_version(
            self.admin, material_id=m["material_id"], data=b"v1"
        )
        self.assertEqual(v1["version_id"], v1_again["version_id"])
        self.assertTrue(v1_again["replayed"])

        v2 = self.h.ctx.evidence.upload_version(
            self.admin, material_id=m["material_id"], data=b"v2"
        )
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["supersedes_version_id"], v1["version_id"])

        stored = self.h.ctx.evidence.get_material(self.admin, m["material_id"])
        self.assertEqual(stored["current_version_id"], v2["version_id"])

    def test_expected_digest_mismatch_rejected(self) -> None:
        from service_09252_006.domain.errors import ValidationError

        m = self.h.ctx.evidence.register_material(
            self.admin, kind=MaterialKind.SYLLABUS.value, title="大纲"
        )
        with self.assertRaises(ValidationError):
            self.h.ctx.evidence.upload_version(
                self.admin,
                material_id=m["material_id"],
                data=b"v1",
                expected_sha256="sha256:" + "0" * 64,
            )

    def test_cross_institution_upload_denied(self) -> None:
        from service_09252_006.domain.errors import PermissionDeniedError

        m = self.h.ctx.evidence.register_material(
            self.admin, kind=MaterialKind.SYLLABUS.value, title="大纲"
        )
        other = self.h.user("admin-b", Role.INSTITUTION_ADMIN, institution_id="inst-b")
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.evidence.upload_version(
                other, material_id=m["material_id"], data=b"x"
            )


if __name__ == "__main__":
    unittest.main()
