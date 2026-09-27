"""复核案件合并服务：多案并一案，原案编号与独立证据全部保留。

不变量：
- 合并只追加 case_merges / merge_members 关系记录，并把原案标记为
  merged（只读）；原案的条目、封存指纹、评审记录一律不改写；
- 同一组原案编号的合并请求（与顺序、重复、重试无关）永远返回同一个
  主案，由 case_merges.source_key 唯一约束兜底，绝不制造新主案；
- 主案是一个新的评审包（draft），复制各原案中未撤回的条目（多案引用
  同一版本时只保留一条），随后走正常封存/评审/签发流程；
- 原案编号通过 merge_members 索引可解析到主案（旧链接跳转），原案
  本身仍可直查，证据保持独立。
"""
from __future__ import annotations

from ..domain.enums import PackageStatus, Role
from ..domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..domain.merge import merge_source_key, normalize_source_ids
from ..domain.models import CaseMerge, PackageEntry, ReviewPackage, User
from .base import Service, require_roles


class CaseMergeService(Service):
    # ------------------------------------------------------------- 合并
    def merge_cases(
        self,
        actor: User,
        *,
        source_package_ids: list[str],
        note: str = "",
        title: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)
        source_ids = normalize_source_ids(source_package_ids)
        if len(source_ids) < 2:
            raise ValidationError("复核案件合并至少需要两个不同的原案编号")
        source_key = merge_source_key(source_ids)

        def work() -> dict:
            # 重复合并请求：回放既有主案，绝不新建
            existing = self.repo.get_merge_by_key(source_key)
            if existing is not None:
                return self._merge_dict(existing, replayed=True)

            packages = []
            for pid in source_ids:
                package = self.repo.get_package(pid)
                if package is None:
                    raise NotFoundError(
                        "原案不存在", details={"package_id": pid}
                    )
                packages.append(package)

            institution_ids = {p.institution_id for p in packages}
            if len(institution_ids) != 1:
                raise ValidationError("只能合并同一机构的复核案件")
            institution_id = institution_ids.pop()
            if (
                not actor.has_role(Role.QUALITY_AUTHORITY)
                and actor.institution_id != institution_id
            ):
                raise PermissionDeniedError("只能合并本机构的复核案件")

            # 任一原案已并入其他主案：拒绝，绝不另起新主案
            for pid in source_ids:
                master = self.repo.resolve_package_id(pid)
                if master != pid:
                    raise ConflictError(
                        "原案已并入其他主案，不能重复合并",
                        details={"package_id": pid, "master_package_id": master},
                    )

            master_id = self.ids.new_id("pkg")
            master = ReviewPackage(
                package_id=master_id,
                institution_id=institution_id,
                title=(title or f"复核案件合并（{len(source_ids)} 案）").strip(),
                status=PackageStatus.DRAFT.value,
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
                sealed_at=None,
                manifest_fingerprint=None,
                decided_at=None,
                decision=None,
                decision_note=None,
                review_fingerprint=None,
                supersedes_package_id=None,
            )
            self.repo.insert_package(master)
            copied = sum(
                self._copy_live_entries(source, master_id) for source in packages
            )

            merge = CaseMerge(
                merge_id=self.ids.new_id("mrg"),
                master_package_id=master_id,
                institution_id=institution_id,
                source_package_ids=source_ids,
                source_key=source_key,
                note=note.strip(),
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
            )
            self.repo.insert_merge(merge)
            self.repo.mark_packages_merged(master_id, list(source_ids))
            self.audit(
                actor.user_id, "复核案件合并",
                package_id=master_id, institution_id=institution_id,
                detail={
                    "merge_id": merge.merge_id,
                    "source_package_ids": list(source_ids),
                    "copied_entries": copied,
                },
            )
            return self._merge_dict(merge, replayed=False)

        return self.idempotent(idempotency_key, work)

    def _copy_live_entries(self, source: ReviewPackage, master_id: str) -> int:
        """把原案中未撤回的条目复制进主案；已撤回/重复版本不复制。"""
        count = 0
        for entry in source.entries:
            version = self.repo.get_version(entry.version_id)
            if version is None or version.withdrawn:
                continue
            if self.repo.entry_exists(master_id, entry.version_id):
                continue  # 多案引用同一版本时只保留一条
            self.repo.insert_entry(
                PackageEntry(
                    entry_id=self.ids.new_id("ent"),
                    package_id=master_id,
                    material_id=entry.material_id,
                    version_id=entry.version_id,
                    sha256=entry.sha256,
                    kind=entry.kind,
                    sensitivity=entry.sensitivity,
                    added_at=self.clock.now_iso(),
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------- 查询
    def resolve_case_number(self, actor: User, package_id: str) -> dict:
        """旧编号跳转：原案编号 -> 主案编号；未合并的编号原样返回。"""
        master_id = self.repo.resolve_package_id(package_id)
        return {
            "package_id": package_id,
            "master_package_id": master_id,
            "merged": master_id != package_id,
        }

    def get_merge(self, actor: User, merge_id: str) -> dict:
        merge = self.repo.get_merge(merge_id)
        if merge is None:
            raise NotFoundError("合并记录不存在")
        self._check_merge_visible(actor, merge)
        return self._merge_dict(merge, replayed=False)

    def list_merges(self, actor: User) -> list[dict]:
        if actor.has_role(Role.QUALITY_AUTHORITY) or actor.has_role(Role.AUDITOR):
            merges = self.repo.list_merges(None)
        else:
            merges = self.repo.list_merges(actor.institution_id)
        return [self._merge_dict(m, replayed=False) for m in merges]

    @staticmethod
    def _check_merge_visible(actor: User, merge: CaseMerge) -> None:
        if actor.has_role(Role.QUALITY_AUTHORITY) or actor.has_role(Role.AUDITOR):
            return
        if actor.institution_id != merge.institution_id:
            raise PermissionDeniedError("不能查看其他机构的合并记录")

    @staticmethod
    def _merge_dict(merge: CaseMerge, *, replayed: bool) -> dict:
        return {
            "merge_id": merge.merge_id,
            "master_package_id": merge.master_package_id,
            "institution_id": merge.institution_id,
            "source_package_ids": list(merge.source_package_ids),
            "note": merge.note,
            "created_by": merge.created_by,
            "created_at": merge.created_at,
            "replayed": replayed,
        }
