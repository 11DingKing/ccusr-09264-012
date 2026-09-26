"""评审服务：分配、接受/拒绝、结论（含异议）、签发。

并发模型：
- 一个包可同时分配给多名评审人，彼此独立；同一评审人重复分配
  回放既有请求（幂等）；
- sealed -> under_review 的迁移用条件 UPDATE 抢占，先到者推进状态，
  后来者在 under_review 上继续追加请求，不冲突；
- 签发用条件 UPDATE 固定 decided，并发双签发只有一方成功；
- 评审结论与签发结论都固定到封存时的 manifest_fingerprint。
"""
from __future__ import annotations

from ..domain.enums import (
    Decision,
    PackageStatus,
    RequestStatus,
    Role,
    Verdict,
)
from ..domain.errors import (
    ConflictError,
    DeadlineExceededError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..domain.fingerprint import review_record_fingerprint
from ..domain.models import Objection, ReviewRequest, User
from ..application.timeutil import now_is_past, resolve_deadline
from .base import Service, require_roles


class ReviewService(Service):
    # ------------------------------------------------------------- 分配
    def assign_reviewer(
        self,
        actor: User,
        *,
        package_id: str,
        reviewer_id: str,
        deadline_local_iso: str | None = None,
        deadline_timezone: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.INSTITUTION_ADMIN)

        def work() -> dict:
            package = self.repo.get_package(package_id)
            if package is None:
                raise NotFoundError("评审包不存在")
            if (
                not actor.has_role(Role.QUALITY_AUTHORITY)
                and package.institution_id != actor.institution_id
            ):
                raise PermissionDeniedError("只能分配本机构评审包")
            if package.status not in (
                PackageStatus.SEALED.value,
                PackageStatus.UNDER_REVIEW.value,
            ):
                raise ConflictError(
                    "仅已封存的评审包可分配评审",
                    details={"status": package.status},
                )

            reviewer = self.repo.get_user(reviewer_id)
            if reviewer is None or not reviewer.has_role(Role.REVIEWER):
                raise ValidationError("被分配人不是评审人", details={"reviewer_id": reviewer_id})
            if reviewer.institution_id == package.institution_id:
                raise ValidationError("评审人必须独立于送审机构")

            # 同一评审人已有有效请求：幂等回放
            for req in self.repo.list_requests_by_package(package_id):
                if req.reviewer_id == reviewer_id and req.status in (
                    RequestStatus.PENDING.value,
                    RequestStatus.ACCEPTED.value,
                    RequestStatus.COMPLETED.value,
                ):
                    return self._request_dict(req, replayed=True)

            deadline_utc = None
            tz_name = None
            if deadline_local_iso is not None:
                tz_name = deadline_timezone or "UTC"
                try:
                    deadline_utc = resolve_deadline(deadline_local_iso, tz_name).at_utc_iso
                except ValueError as exc:
                    raise ValidationError(str(exc))

            request = ReviewRequest(
                request_id=self.ids.new_id("req"),
                package_id=package_id,
                institution_id=package.institution_id,
                reviewer_id=reviewer_id,
                status=RequestStatus.PENDING.value,
                assigned_by=actor.user_id,
                assigned_at=self.clock.now_iso(),
                responded_at=None,
                completed_at=None,
                verdict=None,
                comment=None,
                deadline_at_utc=deadline_utc,
                deadline_timezone=tz_name,
            )

            if package.status == PackageStatus.SEALED.value:
                moved = self.repo.transition_package_status(
                    package_id,
                    PackageStatus.SEALED.value,
                    PackageStatus.UNDER_REVIEW.value,
                )
                if not moved:
                    # 并发下别的分配已推进状态；under_review 同样允许追加
                    fresh = self.repo.get_package(package_id)
                    if fresh.status != PackageStatus.UNDER_REVIEW.value:
                        raise ConflictError("评审包状态已改变，分配失败")
            self.repo.insert_request(request)
            self.audit(
                actor.user_id, "review.assigned",
                package_id=package_id, institution_id=package.institution_id,
                detail={
                    "request_id": request.request_id,
                    "reviewer_id": reviewer_id,
                    "deadline_at_utc": deadline_utc,
                    "deadline_timezone": tz_name,
                },
            )
            return self._request_dict(request)

        return self.idempotent(idempotency_key, work)

    def cancel_request(
        self,
        actor: User,
        *,
        request_id: str,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        """取消分配（例如改派他人）。取消后该评审人立即失去敏感材料访问权。"""
        require_roles(actor, Role.QUALITY_AUTHORITY, Role.INSTITUTION_ADMIN)

        def work() -> dict:
            req = self.repo.get_request(request_id)
            if req is None:
                raise NotFoundError("评审请求不存在")
            if (
                not actor.has_role(Role.QUALITY_AUTHORITY)
                and req.institution_id != actor.institution_id
            ):
                raise PermissionDeniedError("只能取消本机构评审请求")
            if req.status in (RequestStatus.COMPLETED.value, RequestStatus.CANCELLED.value):
                return self._request_dict(req, replayed=True)
            req.status = RequestStatus.CANCELLED.value
            req.responded_at = self.clock.now_iso()
            self.repo.update_request(req)
            self.audit(
                actor.user_id, "review.cancelled",
                package_id=req.package_id, institution_id=req.institution_id,
                detail={"request_id": request_id, "reason": reason},
            )
            return self._request_dict(req)

        return self.idempotent(idempotency_key, work)

    # ------------------------------------------------------------- 响应
    def respond_assignment(
        self,
        actor: User,
        *,
        request_id: str,
        accept: bool,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.REVIEWER)

        def work() -> dict:
            req = self._require_reviewer_request(actor, request_id)
            if req.status in (
                RequestStatus.ACCEPTED.value,
                RequestStatus.DECLINED.value,
            ):
                return self._request_dict(req, replayed=True)
            if req.status != RequestStatus.PENDING.value:
                raise ConflictError("当前请求状态不能响应", details={"status": req.status})
            req.status = (
                RequestStatus.ACCEPTED.value if accept else RequestStatus.DECLINED.value
            )
            req.responded_at = self.clock.now_iso()
            self.repo.update_request(req)
            self.audit(
                actor.user_id,
                "review.accepted" if accept else "review.declined",
                package_id=req.package_id, institution_id=req.institution_id,
                detail={"request_id": request_id},
            )
            return self._request_dict(req)

        return self.idempotent(idempotency_key, work)

    def record_objection(
        self,
        actor: User,
        *,
        request_id: str,
        category: str,
        detail: str,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.REVIEWER)
        if not category.strip() or not detail.strip():
            raise ValidationError("异议类别与内容不能为空")

        def work() -> dict:
            req = self._require_reviewer_request(actor, request_id)
            if req.status not in (
                RequestStatus.ACCEPTED.value,
                RequestStatus.COMPLETED.value,
            ):
                raise ConflictError("只有已接受的评审可以登记异议")
            self._check_deadline(req)
            objection = Objection(
                objection_id=self.ids.new_id("obj"),
                request_id=request_id,
                package_id=req.package_id,
                institution_id=req.institution_id,
                reviewer_id=actor.user_id,
                category=category.strip(),
                detail=detail.strip(),
                created_at=self.clock.now_iso(),
            )
            self.repo.insert_objection(objection)
            self.audit(
                actor.user_id, "review.objection_recorded",
                package_id=req.package_id, institution_id=req.institution_id,
                detail={"objection_id": objection.objection_id, "category": category},
            )
            return {
                "objection_id": objection.objection_id,
                "request_id": request_id,
                "category": objection.category,
                "detail": objection.detail,
                "created_at": objection.created_at,
            }

        return self.idempotent(idempotency_key, work)

    def submit_verdict(
        self,
        actor: User,
        *,
        request_id: str,
        verdict: str,
        comment: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.REVIEWER)
        if verdict not in (Verdict.APPROVE.value, Verdict.OBJECT.value):
            raise ValidationError("结论必须是 approve 或 object")

        def work() -> dict:
            req = self._require_reviewer_request(actor, request_id)
            if req.status == RequestStatus.COMPLETED.value:
                return self._request_dict(req, replayed=True)
            if req.status != RequestStatus.ACCEPTED.value:
                raise ConflictError("只有已接受的评审可以提交结论")
            self._check_deadline(req)

            if verdict == Verdict.OBJECT.value:
                objections = self.repo.list_objections_by_package(req.package_id)
                mine = [o for o in objections if o.request_id == request_id]
                if not mine:
                    raise ValidationError(
                        "反对结论必须至少先登记一条异议",
                    )

            req.status = RequestStatus.COMPLETED.value
            req.verdict = verdict
            req.comment = comment.strip() or None
            req.completed_at = self.clock.now_iso()
            self.repo.update_request(req)
            self.audit(
                actor.user_id, "review.verdict_submitted",
                package_id=req.package_id, institution_id=req.institution_id,
                detail={"request_id": request_id, "verdict": verdict},
            )
            return self._request_dict(req)

        return self.idempotent(idempotency_key, work)

    # --------------------------------------------------------------- 签发
    def issue_decision(
        self,
        actor: User,
        *,
        package_id: str,
        decision: str,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.QUALITY_AUTHORITY)
        if decision not in {d.value for d in Decision}:
            raise ValidationError("未知签发结论", details={"decision": decision})

        def work() -> dict:
            package = self.repo.get_package(package_id)
            if package is None:
                raise NotFoundError("评审包不存在")
            if package.status == PackageStatus.DECIDED.value:
                return {
                    "package_id": package_id,
                    "decision": package.decision,
                    "manifest_fingerprint": package.manifest_fingerprint,
                    "review_fingerprint": package.review_fingerprint,
                    "replayed": True,
                }
            if package.status not in (
                PackageStatus.SEALED.value,
                PackageStatus.UNDER_REVIEW.value,
            ):
                raise ConflictError("当前评审包状态不能签发", details={"status": package.status})

            requests = self.repo.list_requests_by_package(package_id)
            completed = [r for r in requests if r.status == RequestStatus.COMPLETED.value]
            if not completed:
                raise ConflictError("尚无评审人完成评审，不能签发")
            if decision == Decision.APPROVED.value:
                if any(r.verdict == Verdict.OBJECT.value for r in completed):
                    raise ConflictError("存在反对结论，不能签发通过")
            objections = self.repo.list_objections_by_package(package_id)
            fingerprint = review_record_fingerprint(
                package_id,
                package.manifest_fingerprint,
                [
                    {
                        "request_id": r.request_id,
                        "reviewer_id": r.reviewer_id,
                        "status": r.status,
                        "verdict": r.verdict,
                        "comment": r.comment,
                        "assigned_at": r.assigned_at,
                        "completed_at": r.completed_at,
                    }
                    for r in requests
                ],
                [
                    {
                        "objection_id": o.objection_id,
                        "request_id": o.request_id,
                        "reviewer_id": o.reviewer_id,
                        "category": o.category,
                        "detail": o.detail,
                        "created_at": o.created_at,
                    }
                    for o in objections
                ],
            )
            decided_at = self.clock.now_iso()
            moved = self.repo.transition_package_status(
                package_id,
                package.status,  # 条件：当前仍是 sealed 或 under_review
                PackageStatus.DECIDED.value,
                decided_at=decided_at,
                decision=decision,
                decision_note=note.strip() or None,
                review_fingerprint=fingerprint,
            )
            if not moved:
                fresh = self.repo.get_package(package_id)
                if fresh.status == PackageStatus.DECIDED.value:
                    return {
                        "package_id": package_id,
                        "decision": fresh.decision,
                        "manifest_fingerprint": fresh.manifest_fingerprint,
                        "review_fingerprint": fresh.review_fingerprint,
                        "replayed": True,
                    }
                raise ConflictError("评审包状态已被其他操作改变，请重试")

            self.audit(
                actor.user_id, "decision.issued",
                package_id=package_id, institution_id=package.institution_id,
                detail={
                    "decision": decision,
                    "manifest_fingerprint": package.manifest_fingerprint,
                    "review_fingerprint": fingerprint,
                },
            )
            decided = self.repo.get_package(package_id)
            return {
                "package_id": package_id,
                "status": decided.status,
                "decision": decided.decision,
                "note": decided.decision_note,
                "decided_at": decided.decided_at,
                "manifest_fingerprint": decided.manifest_fingerprint,
                "review_fingerprint": decided.review_fingerprint,
            }

        return self.idempotent(idempotency_key, work)

    # --------------------------------------------------------------- 查询
    def list_requests(self, actor: User, package_id: str) -> list[dict]:
        package = self.repo.get_package(package_id)
        if package is None:
            raise NotFoundError("评审包不存在")
        if (
            actor.institution_id != package.institution_id
            and not actor.has_role(Role.QUALITY_AUTHORITY)
            and not actor.has_role(Role.AUDITOR)
            and not self._is_assigned_reviewer(actor, package_id)
        ):
            raise PermissionDeniedError("不能查看该评审包的分配")
        reqs = self.repo.list_requests_by_package(package_id)
        # 评审人只能看到自己的请求明细；他人存在与否以计数暴露
        if actor.has_role(Role.REVIEWER) and not actor.has_role(
            Role.QUALITY_AUTHORITY
        ) and not actor.has_role(Role.AUDITOR):
            reqs = [r for r in reqs if r.reviewer_id == actor.user_id]
        return [self._request_dict(r) for r in reqs]

    # --------------------------------------------------------------- 内部
    def _is_assigned_reviewer(self, actor: User, package_id: str) -> bool:
        return any(
            r.package_id == package_id
            for r in self.repo.list_active_requests_by_reviewer(actor.user_id)
        )

    def _require_reviewer_request(self, actor: User, request_id: str) -> ReviewRequest:
        req = self.repo.get_request(request_id)
        if req is None:
            raise NotFoundError("评审请求不存在")
        if req.reviewer_id != actor.user_id:
            raise PermissionDeniedError("这不是分配给当前评审人的请求")
        return req

    def _check_deadline(self, req: ReviewRequest) -> None:
        if req.deadline_at_utc is None:
            return
        if now_is_past(req.deadline_at_utc, self.clock.now_utc()):
            raise DeadlineExceededError(
                "评审已超过截止时间，需重新分配",
                details={
                    "request_id": req.request_id,
                    "deadline_at_utc": req.deadline_at_utc,
                    "deadline_timezone": req.deadline_timezone,
                },
            )

    @staticmethod
    def _request_dict(r: ReviewRequest, *, replayed: bool = False) -> dict:
        return {
            "request_id": r.request_id,
            "package_id": r.package_id,
            "reviewer_id": r.reviewer_id,
            "status": r.status,
            "assigned_by": r.assigned_by,
            "assigned_at": r.assigned_at,
            "responded_at": r.responded_at,
            "completed_at": r.completed_at,
            "verdict": r.verdict,
            "comment": r.comment,
            "deadline_at_utc": r.deadline_at_utc,
            "deadline_timezone": r.deadline_timezone,
            "replayed": replayed,
        }
