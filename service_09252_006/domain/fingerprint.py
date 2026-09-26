"""内容指纹与清单指纹。

- 材料字节以 SHA-256 内容寻址；
- 评审包封存时对“明确材料集合”做规范化 JSON 哈希，评审决定固定到该指纹；
- 结论再对评审记录（请求、异议）做一次指纹，形成证据链。

所有函数均为纯函数，便于离线核验命令在没有服务进程时复用。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

ALGORITHM = "sha256"
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    """键排序、无空白、不转义非 ASCII 的确定性编码。"""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_json(value))


def manifest_fingerprint(
    package_id: str,
    institution_id: str,
    entries: Iterable[Mapping[str, Any]],
    sealed_at: str,
) -> str:
    """封存清单指纹。

    entries 必须是包含 material_id / version_id / sha256 / kind /
    sensitivity 的映射；函数内部排序，调用方顺序不影响指纹。
    """
    normalized_entries = sorted(
        (
            {
                "material_id": str(e["material_id"]),
                "version_id": str(e["version_id"]),
                "sha256": str(e["sha256"]),
                "kind": str(e["kind"]),
                "sensitivity": str(e["sensitivity"]),
            }
            for e in entries
        ),
        key=lambda e: (e["material_id"], e["version_id"]),
    )
    payload = {
        "schema": "quality-evidence-manifest/v1",
        "package_id": package_id,
        "institution_id": institution_id,
        "sealed_at": sealed_at,
        "entries": normalized_entries,
    }
    return ALGORITHM + ":" + digest_json(payload)


def review_record_fingerprint(
    package_id: str,
    manifest_fingerprint_value: str,
    requests: Iterable[Mapping[str, Any]],
    objections: Iterable[Mapping[str, Any]],
) -> str:
    """签发结论时对评审过程留痕的指纹（链到清单指纹）。"""
    norm_requests = sorted(
        (
            {
                "request_id": str(r["request_id"]),
                "reviewer_id": str(r["reviewer_id"]),
                "status": str(r["status"]),
                "verdict": None if r.get("verdict") is None else str(r["verdict"]),
                "comment": r.get("comment"),
                "assigned_at": r.get("assigned_at"),
                "completed_at": r.get("completed_at"),
            }
            for r in requests
        ),
        key=lambda r: r["request_id"],
    )
    norm_objections = sorted(
        (
            {
                "objection_id": str(o["objection_id"]),
                "request_id": str(o["request_id"]),
                "reviewer_id": str(o["reviewer_id"]),
                "category": str(o["category"]),
                "detail": str(o["detail"]),
                "created_at": str(o["created_at"]),
            }
            for o in objections
        ),
        key=lambda o: o["objection_id"],
    )
    payload = {
        "schema": "quality-evidence-review/v1",
        "package_id": package_id,
        "manifest_fingerprint": manifest_fingerprint_value,
        "requests": norm_requests,
        "objections": norm_objections,
    }
    return ALGORITHM + ":" + digest_json(payload)


def is_valid_digest(value: str) -> bool:
    try:
        algo, hexdigest = value.split(":", 1)
    except ValueError:
        return False
    if algo != ALGORITHM:
        return False
    return len(hexdigest) == 64 and all(c in "0123456789abcdef" for c in hexdigest)
