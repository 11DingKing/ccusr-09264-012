"""复核案件合并：把若干原案并入一个新主案。

法务要求的不变量：
- 原案编号与独立证据全部保留——合并只追加合并关系，绝不动原案行、
  条目、指纹或状态；
- 合并前的链接仍能跳转——旧编号经 resolve_package_id / 包视图上的
  merged_into 都能找到主案；
- 重复合并请求不得制造新主案——同一组原案的合并按规范化键回放既有
  主案（数据库另有 UNIQUE 约束兜底）；
- 一个原案只能并入一个主案，主案不能再作为原案，跳转永远单跳。
"""
from __future__ import annotations

from ..domain.enums import PackageStatus, Role
from ..domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..domain.fingerprint import canonical_json
from ..domain.models import PackageEntry, PackageMerge, ReviewPackage, User
from .base import Service, require_roles

DEFAULT_MERGE_REASON = "复核案件合并"


def canonical_merge_group_key(source_package_ids: list[str] | tuple[str, ...]) -> str:
    """原案集合的规范化键：排序后做确定性编码，集合相同则键相同。"""
    return canonical_json(sorted(set(source_package_ids))).decode("utf-8")


class MergeService(Service):
    def merge_packages(
        self,
        actor: User,
        *,
        source_package_ids: list[str],
        title: str | None = None,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.INSTITUTION_ADMIN)
        distinct = sorted({pid.strip() for pid in source_package_ids if pid and pid.strip()})
        if len(distinct) < 2:
            raise ValidationError("复核案件合并至少需要两个不同的原案")
        if title is not None and not title.strip():
            raise ValidationError("主案标题不能为空")
        reason = reason.strip() or DEFAULT_MERGE_REASON
        group_key = canonical_merge_group_key(distinct)

        def work() -> dict:
            # 重复合并请求：同一组原案 -> 回放既有主案，绝不新建
            existing = self.repo.find_merge_by_group_key(group_key)
            if existing is not None:
                return self._merge_dict(existing, replayed=True)

            sources: list[ReviewPackage] = []
            for pid in distinct:
                package = self.repo.get_package(pid)
                if package is None:
                    raise NotFoundError("原案不存在", details={"package_id": pid})
                sources.append(package)

            institution_ids = {p.institution_id for p in sources}
            if len(institution_ids) != 1:
                raise ValidationError("只能合并同一机构的案件")
            institution_id = institution_ids.pop()
            if (
                not actor.has_role(Role.QUALITY_AUTHORITY)
                and actor.institution_id != institution_id
            ):
                raise PermissionDeniedError("只能合并本机构的案件")

            for package in sources:
                if package.status == PackageStatus.DRAFT.value:
                    raise ConflictError(
                        "草稿状态的案件不能作为原案合并，请先封存",
                        details={"package_id": package.package_id},
                    )
                if self.repo.find_merge_by_primary(package.package_id) is not None:
                    raise ConflictError(
                        "主案不能再次作为原案并入他案",
                        details={"package_id": package.package_id},
                    )
                prior = self.repo.find_merge_by_source(package.package_id)
                if prior is not None:
                    raise ConflictError(
                        "原案已并入其他主案，不能重复合并",
                        details={
                            "package_id": package.package_id,
                            "primary_package_id": prior.primary_package_id,
                        },
                    )

            primary_id = self.ids.new_id("pkg")
            primary = ReviewPackage(
                package_id=primary_id,
                institution_id=institution_id,
                title=(title.strip() if title else f"复核案件合并主案（{len(sources)} 案）"),
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
            self.repo.insert_package(primary)
            copied = self._copy_live_entries(sources, primary_id)

            merge = PackageMerge(
                merge_id=self.ids.new_id("mrg"),
                group_key=group_key,
                primary_package_id=primary_id,
                institution_id=institution_id,
                reason=reason,
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
                source_package_ids=tuple(distinct),
            )
            self.repo.insert_merge(merge)
            self.audit(
                actor.user_id, "package.merged",
                package_id=primary_id, institution_id=institution_id,
                detail={
                    "merge_id": merge.merge_id,
                    "source_package_ids": distinct,
                    "reason": reason,
                    "copied_entries": copied,
                },
            )
            return self._merge_dict(merge, copied_entries=copied)

        return self.idempotent(idempotency_key, work)

    # -------------------------------------------------------------- 查询
    def resolve_package_id(self, package_id: str) -> str:
        """旧编号跳转：已合并的原案编号 -> 主案编号；未合并则原样返回。"""
        return self.repo.resolve_package_id(package_id)

    def get_merge(self, actor: User, merge_id: str) -> dict:
        merge = self.repo.get_merge(merge_id)
        if merge is None:
            raise NotFoundError("合并记录不存在")
        self._check_merge_visible(actor, merge)
        return self._merge_dict(merge)

    def build_merge_view(self, actor: User, package_id: str) -> dict:
        """某包的合并关系：是原案则给出主案跳转，是主案则给出原案清单。"""
        if self.repo.get_package(package_id) is None:
            raise NotFoundError("评审包不存在")
        as_source = self.repo.find_merge_by_source(package_id)
        as_primary = self.repo.find_merge_by_primary(package_id)
        merge = as_source or as_primary
        if merge is None:
            raise NotFoundError("该评审包未参与任何合并")
        self._check_merge_visible(actor, merge)
        view = self._merge_dict(merge)
        view["direction"] = "source" if as_source is not None else "primary"
        return view

    # -------------------------------------------------------------- 内部
    def _check_merge_visible(self, actor: User, merge: PackageMerge) -> None:
        if (
            actor.institution_id != merge.institution_id
            and not actor.has_role(Role.QUALITY_AUTHORITY)
            and not actor.has_role(Role.AUDITOR)
        ):
            raise PermissionDeniedError("不能查看其他机构的合并记录")

    def _copy_live_entries(self, sources: list[ReviewPackage], primary_id: str) -> int:
        """把各原案中【未撤回】的条目并入主案；同一版本只带一份。"""
        seen: set[str] = set()
        count = 0
        for package in sources:
            for entry in package.entries:
                if entry.version_id in seen:
                    continue
                version = self.repo.get_version(entry.version_id)
                if version is None or version.withdrawn:
                    continue
                seen.add(entry.version_id)
                self.repo.insert_entry(
                    PackageEntry(
                        entry_id=self.ids.new_id("ent"),
                        package_id=primary_id,
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

    @staticmethod
    def _merge_dict(
        merge: PackageMerge,
        *,
        replayed: bool = False,
        copied_entries: int | None = None,
    ) -> dict:
        result = {
            "merge_id": merge.merge_id,
            "primary_package_id": merge.primary_package_id,
            "source_package_ids": list(merge.source_package_ids),
            "institution_id": merge.institution_id,
            "reason": merge.reason,
            "created_by": merge.created_by,
            "created_at": merge.created_at,
            "replayed": replayed,
        }
        if copied_entries is not None:
            result["copied_entries"] = copied_entries
        return result
