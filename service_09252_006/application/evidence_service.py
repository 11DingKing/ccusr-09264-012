"""证据接收服务：材料登记、版本上传（内容指纹去重）、撤回。

关键规则：
- 上传字节先算 sha256，按摘要内容寻址；同一材料重复上传相同字节
  返回既有版本（幂等）；
- 新版本只追加、不可改；旧版本可“撤回”，撤回不删除任何已封存清单
  里的引用——历史评审看到了什么永远可证；
- 撤回是对【新版本再入包】的拦截信号，并被离线核验标出。
"""
from __future__ import annotations

from ..domain.enums import MaterialKind, Role, Sensitivity
from ..domain.errors import NotFoundError, PermissionDeniedError, ValidationError
from ..domain.fingerprint import digest_bytes
from ..domain.models import Blob, Material, MaterialVersion, User
from .base import Service, require_roles


class EvidenceService(Service):
    # ---------------------------------------------------------- 材料登记
    def register_material(
        self,
        actor: User,
        *,
        kind: str,
        title: str,
        sensitivity: str = Sensitivity.NORMAL.value,
        material_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(
            actor,
            Role.INSTITUTION_ADMIN,
            Role.INSTITUTION_SUBMITTER,
        )
        self._validate_kind_sensitivity(kind, sensitivity)
        if not title.strip():
            raise ValidationError("材料标题不能为空")

        def work() -> dict:
            mid = material_id or self.ids.new_id("mat")
            existing = self.repo.get_material(mid)
            if existing is not None:
                # 客户端指定了 id 的重复提交：回放，不报错
                return self._material_dict(existing)
            material = Material(
                material_id=mid,
                institution_id=actor.institution_id or "",
                kind=kind,
                sensitivity=sensitivity,
                title=title.strip(),
                current_version_id=None,
                withdrawn=False,
                created_at=self.clock.now_iso(),
            )
            self.repo.insert_material(material)
            self.audit(
                actor.user_id, "material.registered",
                package_id=None, institution_id=material.institution_id,
                detail={"material_id": mid, "kind": kind},
            )
            return self._material_dict(material)

        return self.idempotent(idempotency_key, work)

    # ------------------------------------------------------------ 上传版本
    def upload_version(
        self,
        actor: User,
        *,
        material_id: str,
        data: bytes,
        media_type: str = "application/octet-stream",
        expected_sha256: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(
            actor,
            Role.INSTITUTION_ADMIN,
            Role.INSTITUTION_SUBMITTER,
        )
        if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
            raise ValidationError("材料内容不能为空")
        sha = digest_bytes(bytes(data))
        if expected_sha256 is not None:
            bare = expected_sha256.split(":", 1)[-1]
            if bare != sha:
                raise ValidationError(
                    "客户端提供的摘要与实际内容不一致",
                    details={"expected": expected_sha256, "actual": "sha256:" + sha},
                )

        def work() -> dict:
            material = self.repo.get_material(material_id)
            if material is None:
                raise NotFoundError("材料不存在", details={"material_id": material_id})
            if material.institution_id != actor.institution_id:
                raise PermissionDeniedError("只能为本机构材料上传新版本")
            if material.withdrawn:
                raise PermissionDeniedError("材料已整体撤回，不能再上传版本")

            # 内容相同：幂等回放既有版本
            existing = self.repo.find_version_by_digest(material_id, sha)
            if existing is not None:
                return self._version_dict(existing, replayed=True)

            prior_versions = self.repo.list_versions(material_id)
            version_no = len(prior_versions) + 1
            previous = prior_versions[-1] if prior_versions else None
            version_id = self.ids.new_id("ver")
            blob = Blob(
                sha256=sha,
                data=bytes(data),
                media_type=media_type,
                created_at=self.clock.now_iso(),
            )
            self.repo.put_blob(blob)
            version = MaterialVersion(
                version_id=version_id,
                material_id=material_id,
                institution_id=material.institution_id,
                sha256=sha,
                size=len(data),
                media_type=media_type,
                version_no=version_no,
                supersedes_version_id=previous.version_id if previous else None,
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
                withdrawn=False,
            )
            self.repo.insert_version(version)
            self.audit(
                actor.user_id, "version.uploaded",
                institution_id=material.institution_id,
                detail={
                    "material_id": material_id,
                    "version_id": version_id,
                    "version_no": version_no,
                    "sha256": sha,
                },
            )
            return self._version_dict(version)

        return self.idempotent(idempotency_key, work)

    # -------------------------------------------------------------- 撤回
    def withdraw_version(
        self,
        actor: User,
        *,
        version_id: str,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)

        def work() -> dict:
            version = self.repo.get_version(version_id)
            if version is None:
                raise NotFoundError("版本不存在", details={"version_id": version_id})
            if (
                not actor.has_role(Role.QUALITY_AUTHORITY)
                and version.institution_id != actor.institution_id
            ):
                raise PermissionDeniedError("只能撤回本机构材料")
            if version.withdrawn:
                return {"version_id": version_id, "withdrawn": True, "replayed": True}
            self.repo.mark_version_withdrawn(
                version_id, True, self.clock.now_iso()
            )
            self.audit(
                actor.user_id, "version.withdrawn",
                institution_id=version.institution_id,
                detail={"version_id": version_id, "reason": reason},
            )
            return {"version_id": version_id, "withdrawn": True, "reason": reason}

        return self.idempotent(idempotency_key, work)

    def withdraw_material(
        self,
        actor: User,
        *,
        material_id: str,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.QUALITY_AUTHORITY)

        def work() -> dict:
            material = self.repo.get_material(material_id)
            if material is None:
                raise NotFoundError("材料不存在", details={"material_id": material_id})
            if (
                not actor.has_role(Role.QUALITY_AUTHORITY)
                and material.institution_id != actor.institution_id
            ):
                raise PermissionDeniedError("只能撤回本机构材料")
            if not material.withdrawn:
                self.repo.mark_material_withdrawn(material_id, True)
                self.audit(
                    actor.user_id, "material.withdrawn",
                    institution_id=material.institution_id,
                    detail={"material_id": material_id, "reason": reason},
                )
            return {
                "material_id": material_id,
                "withdrawn": True,
                "reason": reason,
                "replayed": material.withdrawn,
            }

        return self.idempotent(idempotency_key, work)

    # ------------------------------------------------------------- 查询
    def get_material(self, actor: User, material_id: str) -> dict:
        material = self.repo.get_material(material_id)
        if material is None:
            raise NotFoundError("材料不存在")
        if actor.institution_id != material.institution_id and not actor.has_role(
            Role.QUALITY_AUTHORITY
        ) and not actor.has_role(Role.AUDITOR):
            raise PermissionDeniedError("不能查看其他机构材料")
        return self._material_dict(material)

    def get_version(self, actor: User, version_id: str) -> dict:
        version = self.repo.get_version(version_id)
        if version is None:
            raise NotFoundError("版本不存在")
        if actor.institution_id != version.institution_id and not actor.has_role(
            Role.QUALITY_AUTHORITY
        ) and not actor.has_role(Role.AUDITOR):
            raise PermissionDeniedError("不能查看其他机构材料")
        return self._version_dict(version)

    def download_blob(self, actor: User, version_id: str) -> tuple[dict, bytes]:
        """带权限的内容下载；敏感材料下载权限由调用方（包视图）决定。

        机构成员可取本机构材料；评审人只能通过包条目访问，由
        PackageService.build_package_view 授权后调用本方法的内部版本。
        """
        version = self.repo.get_version(version_id)
        if version is None:
            raise NotFoundError("版本不存在")
        if actor.institution_id != version.institution_id and not actor.has_role(
            Role.QUALITY_AUTHORITY
        ) and not actor.has_role(Role.AUDITOR):
            raise PermissionDeniedError("不能下载其他机构材料")
        blob = self.repo.get_blob(version.sha256)
        if blob is None:
            raise NotFoundError("内容字节缺失，无法核验完整性")
        return self._version_dict(version), blob.data

    # ------------------------------------------------------------- 辅助
    @staticmethod
    def _validate_kind_sensitivity(kind: str, sensitivity: str) -> None:
        kinds = {k.value for k in MaterialKind}
        if kind not in kinds:
            raise ValidationError("未知材料类型", details={"kind": kind})
        sens = {s.value for s in Sensitivity}
        if sensitivity not in sens:
            raise ValidationError("未知敏感度", details={"sensitivity": sensitivity})
        if (
            kind != MaterialKind.ENTERPRISE_FEEDBACK.value
            and sensitivity == Sensitivity.SENSITIVE.value
        ):
            raise ValidationError("仅企业反馈可标记为敏感")

    @staticmethod
    def _material_dict(m: Material) -> dict:
        return {
            "material_id": m.material_id,
            "institution_id": m.institution_id,
            "kind": m.kind,
            "sensitivity": m.sensitivity,
            "title": m.title,
            "current_version_id": m.current_version_id,
            "withdrawn": m.withdrawn,
            "created_at": m.created_at,
        }

    @staticmethod
    def _version_dict(v: MaterialVersion, *, replayed: bool = False) -> dict:
        return {
            "version_id": v.version_id,
            "material_id": v.material_id,
            "institution_id": v.institution_id,
            "version_no": v.version_no,
            "sha256": "sha256:" + v.sha256,
            "size": v.size,
            "media_type": v.media_type,
            "supersedes_version_id": v.supersedes_version_id,
            "withdrawn": v.withdrawn,
            "created_by": v.created_by,
            "created_at": v.created_at,
            "replayed": replayed,
        }
