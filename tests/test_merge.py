"""复核案件合并：原案保留、旧编号跳转、重复合并不生成新主案。"""
import concurrent.futures
import json
import sqlite3
import unittest
import urllib.error
import urllib.request

from service_09252_006.api.http_api import HttpApiServer
from service_09252_006.application.container import ApplicationContext
from service_09252_006.application.ports import SystemClock, Uuid4IdGenerator
from service_09252_006.application.verification import verify_database
from service_09252_006.domain.enums import PackageStatus, Role
from service_09252_006.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from tests.flow import seal_new_package, upload_material
from tests.support import Harness


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.authority = self.h.user(
            "auth", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.case_a = seal_new_package(self.h, self.admin, title="案件 A")
        self.case_b = seal_new_package(self.h, self.admin, title="案件 B")

    def tearDown(self) -> None:
        self.h.close()

    def _merge(self, **kwargs):
        params = {"source_package_ids": [self.case_a.package_id, self.case_b.package_id]}
        params.update(kwargs)
        return self.h.ctx.merges.merge_packages(self.admin, **params)

    def test_merge_creates_primary_and_preserves_sources(self) -> None:
        before_a = self.h.repo.get_package(self.case_a.package_id)
        before_b = self.h.repo.get_package(self.case_b.package_id)

        result = self._merge(reason="法务要求并案复核")
        primary_id = result["primary_package_id"]
        self.assertNotIn(primary_id, (self.case_a.package_id, self.case_b.package_id))
        self.assertEqual(
            sorted(result["source_package_ids"]),
            sorted([self.case_a.package_id, self.case_b.package_id]),
        )
        self.assertEqual(result["reason"], "法务要求并案复核")

        # 主案是草稿，汇集两个原案的全部未撤回条目
        primary = self.h.repo.get_package(primary_id)
        self.assertEqual(primary.status, PackageStatus.DRAFT.value)
        expected_versions = {
            e.version_id for e in before_a.entries + before_b.entries
        }
        self.assertEqual({e.version_id for e in primary.entries}, expected_versions)
        self.assertEqual(result["copied_entries"], len(expected_versions))

        # 原案编号与独立证据原样保留：状态、条目、封存指纹全部不变
        for before in (before_a, before_b):
            after = self.h.repo.get_package(before.package_id)
            self.assertEqual(after.status, before.status)
            self.assertEqual(after.manifest_fingerprint, before.manifest_fingerprint)
            self.assertEqual(
                [e.entry_id for e in after.entries],
                [e.entry_id for e in before.entries],
            )

        # 合并后离线核验仍然通过（原案指纹未被触碰）
        self.assertTrue(verify_database(self.h.db_path).ok)

    def test_merge_relationship_persisted_in_sqlite(self) -> None:
        result = self._merge()
        primary_id = result["primary_package_id"]

        conn = sqlite3.connect(f"file:{self.h.db_path}?mode=ro", uri=True)
        try:
            merges = conn.execute("SELECT * FROM package_merges").fetchall()
            self.assertEqual(len(merges), 1)
            self.assertEqual(merges[0][2], primary_id)  # primary_package_id
            members = conn.execute(
                "SELECT source_package_id FROM package_merge_members"
            ).fetchall()
            self.assertEqual(
                sorted(r[0] for r in members),
                sorted([self.case_a.package_id, self.case_b.package_id]),
            )
            # 原案索引保留：两个原案在 packages 表中仍可独立查询
            kept = conn.execute(
                "SELECT package_id FROM packages WHERE package_id IN (?, ?)",
                (self.case_a.package_id, self.case_b.package_id),
            ).fetchall()
            self.assertEqual(len(kept), 2)
        finally:
            conn.close()

    def test_duplicate_merge_does_not_create_new_primary(self) -> None:
        first = self._merge(idempotency_key="merge-1")
        package_count = len(self.h.repo.list_packages())

        # 同一幂等键重发
        again_same_key = self._merge(idempotency_key="merge-1")
        # 不同幂等键、参数顺序不同的重复合并请求
        again_other_key = self._merge(
            source_package_ids=[self.case_b.package_id, self.case_a.package_id],
            idempotency_key="merge-2",
        )
        # 不带幂等键的重复合并请求
        again_no_key = self._merge()

        for dup in (again_same_key, again_other_key, again_no_key):
            self.assertEqual(dup["primary_package_id"], first["primary_package_id"])
            self.assertEqual(dup["merge_id"], first["merge_id"])
            self.assertTrue(dup["replayed"])

        # 没有制造新主案：包总数与合并事件数都不变
        self.assertEqual(len(self.h.repo.list_packages()), package_count)
        conn = sqlite3.connect(f"file:{self.h.db_path}?mode=ro", uri=True)
        try:
            (merge_rows,) = conn.execute(
                "SELECT COUNT(*) FROM package_merges"
            ).fetchone()
            (pkg_rows,) = conn.execute("SELECT COUNT(*) FROM packages").fetchone()
        finally:
            conn.close()
        self.assertEqual(merge_rows, 1)
        self.assertEqual(pkg_rows, package_count)

    def test_python_query_jumps_from_old_number(self) -> None:
        result = self._merge()
        primary_id = result["primary_package_id"]

        # 仓库与服务两层都能从旧编号跳转到主案
        for old in (self.case_a.package_id, self.case_b.package_id):
            self.assertEqual(self.h.repo.resolve_package_id(old), primary_id)
            self.assertEqual(self.h.ctx.merges.resolve_package_id(old), primary_id)
        # 未合并的编号原样返回
        self.assertEqual(self.h.repo.resolve_package_id(primary_id), primary_id)
        self.assertEqual(self.h.repo.resolve_package_id("pkg_不存在"), "pkg_不存在")

    def test_old_links_still_resolve_with_jump_target(self) -> None:
        result = self._merge()
        primary_id = result["primary_package_id"]

        # 合并前的链接（旧编号视图）仍能打开，且携带跳转目标
        view = self.h.ctx.packages.build_package_view(
            self.admin, self.case_a.package_id
        )
        self.assertEqual(view["package_id"], self.case_a.package_id)
        self.assertEqual(view["status"], PackageStatus.SEALED.value)
        self.assertTrue(view["entries"])  # 原案独立证据仍在
        self.assertEqual(
            view["merged_into"],
            {
                "merge_id": result["merge_id"],
                "primary_package_id": primary_id,
                "primary_url": f"/v1/packages/{primary_id}",
            },
        )

        # 主案视图给出原案清单
        primary_view = self.h.ctx.packages.build_package_view(self.admin, primary_id)
        self.assertEqual(
            sorted(primary_view["merged_from"]),
            sorted([self.case_a.package_id, self.case_b.package_id]),
        )
        self.assertEqual(primary_view["merge_id"], result["merge_id"])

        # 合并关系查询：原案 -> source 方向，主案 -> primary 方向
        as_source = self.h.ctx.merges.build_merge_view(
            self.admin, self.case_b.package_id
        )
        self.assertEqual(as_source["direction"], "source")
        self.assertEqual(as_source["primary_package_id"], primary_id)
        as_primary = self.h.ctx.merges.build_merge_view(self.admin, primary_id)
        self.assertEqual(as_primary["direction"], "primary")

    def test_merge_view_requires_permission(self) -> None:
        result = self._merge()
        outsider = self.h.user(
            "admin-b", Role.INSTITUTION_ADMIN, institution_id="inst-b"
        )
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.merges.get_merge(outsider, result["merge_id"])
        auditor = self.h.user("aud", Role.AUDITOR, institution_id=None)
        view = self.h.ctx.merges.get_merge(auditor, result["merge_id"])
        self.assertEqual(view["merge_id"], result["merge_id"])

    def test_source_cannot_join_two_merges(self) -> None:
        self._merge()
        case_c = seal_new_package(self.h, self.admin, title="案件 C")
        with self.assertRaises(ConflictError):
            self.h.ctx.merges.merge_packages(
                self.admin,
                source_package_ids=[self.case_a.package_id, case_c.package_id],
            )

    def test_primary_cannot_be_merged_again_as_source(self) -> None:
        result = self._merge()
        case_c = seal_new_package(self.h, self.admin, title="案件 C")
        with self.assertRaises(ConflictError):
            self.h.ctx.merges.merge_packages(
                self.admin,
                source_package_ids=[result["primary_package_id"], case_c.package_id],
            )

    def test_draft_package_cannot_be_merged(self) -> None:
        draft = self.h.ctx.packages.create_package(self.admin, title="草稿")
        with self.assertRaises(ConflictError):
            self.h.ctx.merges.merge_packages(
                self.admin,
                source_package_ids=[self.case_a.package_id, draft["package_id"]],
            )

    def test_merge_requires_two_distinct_sources(self) -> None:
        with self.assertRaises(ValidationError):
            self.h.ctx.merges.merge_packages(
                self.admin, source_package_ids=[self.case_a.package_id]
            )
        with self.assertRaises(ValidationError):
            self.h.ctx.merges.merge_packages(
                self.admin,
                source_package_ids=[self.case_a.package_id, self.case_a.package_id],
            )

    def test_merge_requires_existing_packages(self) -> None:
        with self.assertRaises(NotFoundError):
            self.h.ctx.merges.merge_packages(
                self.admin,
                source_package_ids=[self.case_a.package_id, "pkg_不存在"],
            )

    def test_merge_requires_same_institution(self) -> None:
        other_admin = self.h.user(
            "admin-b", Role.INSTITUTION_ADMIN, institution_id="inst-b"
        )
        case_x = seal_new_package(self.h, other_admin, title="他机构案件")
        with self.assertRaises(ValidationError):
            self.h.ctx.merges.merge_packages(
                self.authority,
                source_package_ids=[self.case_a.package_id, case_x.package_id],
            )

    def test_other_institution_admin_cannot_merge(self) -> None:
        outsider = self.h.user(
            "admin-b", Role.INSTITUTION_ADMIN, institution_id="inst-b"
        )
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.merges.merge_packages(
                outsider,
                source_package_ids=[self.case_a.package_id, self.case_b.package_id],
            )

    def test_submitter_cannot_merge(self) -> None:
        submitter = self.h.user("sub-a", Role.INSTITUTION_SUBMITTER)
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.merges.merge_packages(
                submitter,
                source_package_ids=[self.case_a.package_id, self.case_b.package_id],
            )

    def test_withdrawn_entries_not_copied_and_shared_version_once(self) -> None:
        # 案件 C：两份材料，封存后撤回其中一份
        m1 = upload_material(self.h, self.admin, data=b"c-1", title="C1")
        m2 = upload_material(self.h, self.admin, data=b"c-2", title="C2")
        case_c = seal_new_package(self.h, self.admin, items=[m1, m2], title="案件 C")
        self.h.ctx.evidence.withdraw_version(
            self.admin, version_id=m2.version["version_id"], reason="作废"
        )

        # 案件 D：引用了与 C 相同的版本（同一版本进入两个原案）
        case_d = self.h.ctx.packages.create_package(self.admin, title="案件 D")
        self.h.ctx.packages.add_entry(
            self.admin,
            package_id=case_d["package_id"],
            version_id=m1.version["version_id"],
        )
        extra = upload_material(self.h, self.admin, data=b"d-1", title="D1")
        self.h.ctx.packages.add_entry(
            self.admin,
            package_id=case_d["package_id"],
            version_id=extra.version["version_id"],
        )
        self.h.ctx.packages.seal_package(self.admin, package_id=case_d["package_id"])

        result = self.h.ctx.merges.merge_packages(
            self.admin,
            source_package_ids=[case_c.package_id, case_d["package_id"]],
        )
        primary = self.h.repo.get_package(result["primary_package_id"])
        copied = {e.version_id for e in primary.entries}
        # 撤回的版本不带入；共享版本只带一份
        self.assertNotIn(m2.version["version_id"], copied)
        self.assertEqual(
            copied, {m1.version["version_id"], extra.version["version_id"]}
        )
        self.assertEqual(result["copied_entries"], 2)

    def test_three_way_merge_group_key(self) -> None:
        case_c = seal_new_package(self.h, self.admin, title="案件 C")
        sources = [self.case_a.package_id, self.case_b.package_id, case_c.package_id]
        first = self.h.ctx.merges.merge_packages(
            self.admin, source_package_ids=sources
        )
        # 打乱顺序的重复请求仍回放同一主案
        dup = self.h.ctx.merges.merge_packages(
            self.admin, source_package_ids=list(reversed(sources))
        )
        self.assertEqual(dup["primary_package_id"], first["primary_package_id"])
        self.assertTrue(dup["replayed"])
        self.assertEqual(len(self.h.repo.list_packages()), 4)


class MergeConcurrencyTests(unittest.TestCase):
    """并发重复合并：多连接同时请求同一组原案，只生成一个主案。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.case_a = seal_new_package(self.h, self.admin, title="案件 A")
        self.case_b = seal_new_package(self.h, self.admin, title="案件 B")

    def tearDown(self) -> None:
        self.h.close()

    def test_concurrent_duplicate_merges_create_single_primary(self) -> None:
        sources = [self.case_a.package_id, self.case_b.package_id]
        results: list[dict] = []
        errors: list[Exception] = []

        def merge() -> None:
            ctx = ApplicationContext(
                self.h.db_path, clock=SystemClock(), ids=Uuid4IdGenerator()
            )
            try:
                admin = ctx.repo.get_user("admin-a")
                results.append(
                    ctx.merges.merge_packages(admin, source_package_ids=sources)
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                ctx.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: merge(), range(4)))

        self.assertEqual(errors, [])
        primaries = {r["primary_package_id"] for r in results}
        self.assertEqual(len(primaries), 1)
        self.assertEqual(len(self.h.repo.list_packages()), 3)
        self.assertEqual(
            self.h.repo.find_merge_by_source(self.case_a.package_id).primary_package_id,
            primaries.pop(),
        )


class MergeHttpTests(unittest.TestCase):
    """HTTP 端到端：合并端点、重复合并不造新主案、旧链接带跳转目标。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user(
            "admin-a", Role.INSTITUTION_ADMIN, token="tok-admin"
        )
        self.case_a = seal_new_package(self.h, self.admin, title="案件 A")
        self.case_b = seal_new_package(self.h, self.admin, title="案件 B")
        self.server = HttpApiServer(self.h.ctx, host="127.0.0.1", port=0)
        self.server.start()
        host, port = self.server.address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.stop()
        self.h.close()

    def _request(self, method: str, path: str, body=None, idempotency_key=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": "Bearer tok-admin"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_merge_over_http_and_old_links_jump(self) -> None:
        sources = [self.case_a.package_id, self.case_b.package_id]
        status, created = self._request(
            "POST", "/v1/merges",
            {"source_package_ids": sources, "reason": "法务并案"},
            idempotency_key="m-1",
        )
        self.assertEqual(status, 201, created)
        primary_id = created["primary_package_id"]
        self.assertEqual(created["reason"], "法务并案")

        # 重复合并请求（换键、换顺序）不制造新主案
        status, dup = self._request(
            "POST", "/v1/merges",
            {"source_package_ids": list(reversed(sources))},
            idempotency_key="m-2",
        )
        self.assertEqual(status, 201, dup)
        self.assertEqual(dup["primary_package_id"], primary_id)
        self.assertTrue(dup["replayed"])

        status, listing = self._request("GET", "/v1/packages")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["packages"]), 3)

        # 合并前的链接仍能打开，并给出跳转目标
        status, view = self._request("GET", f"/v1/packages/{self.case_a.package_id}")
        self.assertEqual(status, 200, view)
        self.assertEqual(view["merged_into"]["primary_package_id"], primary_id)
        self.assertEqual(
            view["merged_into"]["primary_url"], f"/v1/packages/{primary_id}"
        )
        self.assertTrue(view["entries"])

        # 主案视图列出原案；合并记录可按编号查询
        status, primary_view = self._request("GET", f"/v1/packages/{primary_id}")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(primary_view["merged_from"]), sorted(sources))

        status, merge = self._request("GET", f"/v1/merges/{created['merge_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(merge["primary_package_id"], primary_id)

        status, rel = self._request(
            "GET", f"/v1/packages/{self.case_b.package_id}/merge"
        )
        self.assertEqual(status, 200)
        self.assertEqual(rel["direction"], "source")
        self.assertEqual(rel["primary_package_id"], primary_id)


if __name__ == "__main__":
    unittest.main()
