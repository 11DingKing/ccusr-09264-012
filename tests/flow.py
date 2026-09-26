"""测试用端到端流程辅助：快速造出已封存/已决定的评审包。"""
from __future__ import annotations

from types import SimpleNamespace

from service_09252_006.domain.enums import MaterialKind, Sensitivity


def upload_material(
    h,
    actor,
    *,
    kind: str = MaterialKind.SYLLABUS.value,
    data: bytes = b"syllabus-v1",
    title: str = "材料",
    sensitivity: str = Sensitivity.NORMAL.value,
):
    m = h.ctx.evidence.register_material(
        actor, kind=kind, title=title, sensitivity=sensitivity
    )
    v = h.ctx.evidence.upload_version(actor, material_id=m["material_id"], data=data)
    return SimpleNamespace(material=m, version=v)


def seal_new_package(h, admin, *, items=None, title="2026 秋评审包"):
    """items: [(uploaded,)] 默认为一份大纲 + 一份敏感企业反馈。"""
    pkg = h.ctx.packages.create_package(admin, title=title)
    pid = pkg["package_id"]
    if items is None:
        syllabus = upload_material(h, admin, kind=MaterialKind.SYLLABUS.value, data="大纲 v1".encode("utf-8"))
        feedback = upload_material(
            h, admin,
            kind=MaterialKind.ENTERPRISE_FEEDBACK.value,
            data="敏感反馈：企业要求匿名".encode("utf-8"),
            title="企业反馈",
            sensitivity=Sensitivity.SENSITIVE.value,
        )
        items = [syllabus, feedback]
    for item in items:
        h.ctx.packages.add_entry(
            admin, package_id=pid, version_id=item.version["version_id"]
        )
    sealed = h.ctx.packages.seal_package(admin, package_id=pid)
    return SimpleNamespace(package_id=pid, items=items, sealed=sealed)


def complete_review(
    h, authority, reviewer, package_id, *, verdict="approve",
    objection=None, deadline_local_iso=None, deadline_timezone=None,
):
    req = h.ctx.reviews.assign_reviewer(
        authority,
        package_id=package_id,
        reviewer_id=reviewer.user_id,
        deadline_local_iso=deadline_local_iso,
        deadline_timezone=deadline_timezone,
    )
    rid = req["request_id"]
    h.ctx.reviews.respond_assignment(reviewer, request_id=rid, accept=True)
    if verdict == "object":
        obj = h.ctx.reviews.record_objection(
            reviewer,
            request_id=rid,
            category=(objection or {}).get("category", "材料不完整"),
            detail=(objection or {}).get("detail", "缺少考核评分依据"),
        )
    else:
        obj = None
    h.ctx.reviews.submit_verdict(reviewer, request_id=rid, verdict=verdict)
    return SimpleNamespace(request=req, request_id=rid, objection=obj)
