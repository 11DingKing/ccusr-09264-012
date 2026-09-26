"""评审包服务：组包、封存、后补材料触发复审。

核心不变量：
- draft 包只能引用“当前未撤回”的版本；封存后清单指纹固定，
  此后材料撤回/新版本都不改变历史包——“某次评审看到了什么”可证；
- 后补（新上传/恢复）的材料不能塞进已封存或已决定的包，
  只能基于旧包创建新的复审包（supersedes 链）；
- 封存是幂等的：重复封存返回同一指纹。
"""
from __future__ import annotations

from ..domain.disclosure import DisclosureContext, redact_entry
from ..domain.enums import PackageStatus, Role
from ..domain.errors import (
    ConflictError,
    ImmutabilityError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..domain.fingerprint import manifest_fingerprint
from ..domain.models import PackageEntry, ReviewPackage, User
from .base import Service, require_roles


class PackageService(Service):
    def create_package(
        self,
        actor: User,
        *,
        title: str,
        supersedes_package_id: str | None = None,
        package_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)
        if not title.strip():
            raise ValidationError("评审包标题不能为空")

        def work() -> dict:
            pid = package_id or self.ids.new_id("pkg")
            if self.repo.get_package(pid) is not None:
                return self._package_dict(self.repo.get_package(pid))

            predecessor: ReviewPackage | None = None
            if supersedes_package_id is not None:
                predecessor = self.repo.get_package(supersedes_package_id)
                if predecessor is None:
                    raise NotFoundError(
                        "被复审的原评审包不存在",
                        details={"supersedes_package_id": supersedes_package_id},
                    )
                if predecessor.institution_id != actor.institution_id and not actor.has_role(
                    Role.QUALITY_AUTHORITY
                ):
                    raise PermissionDeniedError("不能为其他机构创建复审包")
                if predecessor.status != PackageStatus.DECIDED.value:
                    raise ConflictError(
                        "仅已签发结论的评审包可发起复审",
                        details={"predecessor_status": predecessor.status},
                    )

            package = ReviewPackage(
                package_id=pid,
                institution_id=actor.institution_id or "",
                title=title.strip(),
                status=PackageStatus.DRAFT.value,
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
                sealed_at=None,
                manifest_fingerprint=None,
                decided_at=None,
                decision=None,
                decision_note=None,
                review_fingerprint=None,
                supersedes_package_id=supersedes_package_id,
            )
            self.repo.insert_package(package)
            detail = {}
            if predecessor is not None:
                # 复审包默认带上原包中【未撤回】的条目，撤回的条目不复制
                detail["copied_entries"] = self._copy_live_entries(actor, predecessor, pid)
                detail["supersedes_package_id"] = supersedes_package_id
            self.audit(
                actor.user_id, "package.created",
                package_id=pid, institution_id=package.institution_id, detail=detail,
            )
            return self._package_dict(self.repo.get_package(pid))

        return self.idempotent(idempotency_key, work)

    def _copy_live_entries(self, actor: User, predecessor: ReviewPackage, new_pid: str) -> int:
        count = 0
        for entry in predecessor.entries:
            version = self.repo.get_version(entry.version_id)
            if version is None or version.withdrawn:
                continue
            new_entry = PackageEntry(
                entry_id=self.ids.new_id("ent"),
                package_id=new_pid,
                material_id=entry.material_id,
                version_id=entry.version_id,
                sha256=entry.sha256,
                kind=entry.kind,
                sensitivity=entry.sensitivity,
                added_at=self.clock.now_iso(),
            )
            self.repo.insert_entry(new_entry)
            count += 1
        return count

    def add_entry(
        self,
        actor: User,
        *,
        package_id: str,
        version_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.INSTITUTION_SUBMITTER)

        def work() -> dict:
            package = self.repo.get_package(package_id)
            if package is None:
                raise NotFoundError("评审包不存在")
            if package.institution_id != actor.institution_id:
                raise PermissionDeniedError("只能向本机构评审包添加材料")
            if not package.is_mutable():
                raise ImmutabilityError(
                    "评审包已封存，后补材料只能发起新的复审请求",
                    details={"package_id": package_id, "status": package.status},
                )
            version = self.repo.get_version(version_id)
            if version is None:
                raise NotFoundError("材料版本不存在")
            if version.institution_id != actor.institution_id:
                raise PermissionDeniedError("不能把其他机构材料加入评审包")
            if version.withdrawn:
                raise ConflictError("该版本已撤回，不能进入评审包")

            if self.repo.entry_exists(package_id, version_id):
                return {"package_id": package_id, "version_id": version_id, "replayed": True}

            entry = PackageEntry(
                entry_id=self.ids.new_id("ent"),
                package_id=package_id,
                material_id=version.material_id,
                version_id=version.version_id,
                sha256=version.sha256,
                kind=self.repo.get_material(version.material_id).kind,
                sensitivity=self.repo.get_material(version.material_id).sensitivity,
                added_at=self.clock.now_iso(),
            )
            self.repo.insert_entry(entry)
            self.audit(
                actor.user_id, "package.entry_added",
                package_id=package_id, institution_id=package.institution_id,
                detail={"version_id": version_id, "entry_id": entry.entry_id},
            )
            return {"package_id": package_id, "version_id": version_id, "entry_id": entry.entry_id}

        return self.idempotent(idempotency_key, work)

    def seal_package(
        self,
        actor: User,
        *,
        package_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)

        def work() -> dict:
            package = self.repo.get_package(package_id)
            if package is None:
                raise NotFoundError("评审包不存在")
            if package.institution_id != actor.institution_id and not actor.has_role(
                Role.QUALITY_AUTHORITY
            ):
                raise PermissionDeniedError("只能封存本机构评审包")

            if package.status == PackageStatus.SEALED.value:
                return self._package_dict(package, replayed=True)
            if package.status != PackageStatus.DRAFT.value:
                raise ImmutabilityError(
                    "评审包当前状态不能封存",
                    details={"status": package.status},
                )
            if not package.entries:
                raise ValidationError("评审包没有任何材料，不能封存")

            # 封存前最后一次撤回拦截（与 add_entry 构成双重检查）
            for entry in package.entries:
                version = self.repo.get_version(entry.version_id)
                if version is None or version.withdrawn:
                    raise ConflictError(
                        "清单中存在已撤回版本，请移除后再封存",
                        details={"version_id": entry.version_id},
                    )

            sealed_at = self.clock.now_iso()
            fingerprint = manifest_fingerprint(
                package.package_id,
                package.institution_id,
                [
                    {
                        "material_id": e.material_id,
                        "version_id": e.version_id,
                        "sha256": e.sha256,
                        "kind": e.kind,
                        "sensitivity": e.sensitivity,
                    }
                    for e in package.entries
                ],
                sealed_at,
            )
            ok = self.repo.transition_package_status(
                package_id,
                PackageStatus.DRAFT.value,
                PackageStatus.SEALED.value,
                sealed_at=sealed_at,
                manifest_fingerprint=fingerprint,
            )
            if not ok:
                # 并发：另一事务已推进状态
                fresh = self.repo.get_package(package_id)
                if fresh.status == PackageStatus.SEALED.value:
                    return self._package_dict(fresh, replayed=True)
                raise ConflictError("评审包状态已被其他操作改变，请重试")

            sealed = self.repo.get_package(package_id)
            self.audit(
                actor.user_id, "package.sealed",
                package_id=package_id, institution_id=package.institution_id,
                detail={"manifest_fingerprint": fingerprint,
                        "entries": len(package.entries)},
            )
            return self._package_dict(sealed)

        return self.idempotent(idempotency_key, work)

    # -------------------------------------------------------------- 视图
    def build_package_view(self, actor: User, package_id: str) -> dict:
        """按最小披露返回包视图；敏感条目对无权用户做遮蔽。

        曾被分配到该包的评审人（即使请求已取消/拒绝）可打开视图看到
        非敏感条目与“存在敏感条目”的事实，但敏感内容按当前有效分配遮蔽；
        与该包毫无关系的外部机构用户直接拒绝。
        """
        package = self.repo.get_package(package_id)
        if package is None:
            raise NotFoundError("评审包不存在")
        is_assigned = (
            actor.has_role(Role.REVIEWER)
            and any(
                r.reviewer_id == actor.user_id
                for r in self.repo.list_requests_by_package(package_id)
            )
        )
        if (
            actor.institution_id != package.institution_id
            and not actor.has_role(Role.QUALITY_AUTHORITY)
            and not actor.has_role(Role.AUDITOR)
            and not is_assigned
        ):
            raise PermissionDeniedError("不能查看其他机构评审包")

        active = {
            r.package_id
            for r in self.repo.list_active_requests_by_reviewer(actor.user_id)
        }
        ctx = DisclosureContext(actor, active)

        visible_entries = []
        hidden_count = 0
        for entry in package.entries:
            can_see = ctx.can_see_entry(entry, package)
            if not can_see:
                hidden_count += 1
            visible_entries.append(redact_entry(entry, can_see))

        view = self._package_dict(package)
        view["entries"] = visible_entries
        view["redacted_entries"] = hidden_count
        view["viewer"] = actor.user_id
        return view

    def download_entry(
        self, actor: User, *, package_id: str, version_id: str
    ) -> tuple[dict, bytes, str]:
        """通过评审包条目下载内容字节，强制走最小披露授权。

        返回 (版本描述, 字节, media_type)。评审人只可下载其仍有效分配
        所在包的敏感反馈；请求一旦取消，授权即时消失。
        """
        package = self.repo.get_package(package_id)
        if package is None:
            raise NotFoundError("评审包不存在")
        entry = next(
            (e for e in package.entries if e.version_id == version_id), None
        )
        if entry is None:
            raise NotFoundError("该材料版本不在评审包中")
        active = {
            r.package_id
            for r in self.repo.list_active_requests_by_reviewer(actor.user_id)
        }
        ctx = DisclosureContext(actor, active)
        if not ctx.can_see_entry(entry, package):
            raise PermissionDeniedError("无权下载该材料（最小披露限制）")
        version = self.repo.get_version(version_id)
        blob = self.repo.get_blob(entry.sha256)
        if version is None or blob is None:
            raise NotFoundError("内容缺失，无法提供")
        return {
            "version_id": version.version_id,
            "material_id": version.material_id,
            "sha256": "sha256:" + version.sha256,
            "media_type": version.media_type,
            "size": version.size,
        }, blob.data, version.media_type

    def list_packages(self, actor: User) -> list[dict]:
        if actor.has_role(Role.AUDITOR) or actor.has_role(Role.QUALITY_AUTHORITY):
            packages = self.repo.list_packages(None)
        else:
            packages = self.repo.list_packages(actor.institution_id)
        return [self._package_dict(p) for p in packages]

    @staticmethod
    def _package_dict(p: ReviewPackage, *, replayed: bool = False) -> dict:
        return {
            "package_id": p.package_id,
            "institution_id": p.institution_id,
            "title": p.title,
            "status": p.status,
            "created_by": p.created_by,
            "created_at": p.created_at,
            "sealed_at": p.sealed_at,
            "manifest_fingerprint": p.manifest_fingerprint,
            "decided_at": p.decided_at,
            "decision": p.decision,
            "decision_note": p.decision_note,
            "review_fingerprint": p.review_fingerprint,
            "supersedes_package_id": p.supersedes_package_id,
            "entry_count": len(p.entries),
            "replayed": replayed,
        }
