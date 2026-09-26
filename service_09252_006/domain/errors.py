"""领域错误层级。

应用服务抛出这些错误，接口层负责翻译成状态码；领域与持久化层
不感知 HTTP。
"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class PermissionDeniedError(DomainError):
    code = "permission_denied"
    http_status = 403


class ConflictError(DomainError):
    """状态机冲突，例如重复签发、并发下状态已被对方推进。"""

    code = "conflict"
    http_status = 409


class ImmutabilityError(ConflictError):
    """试图改动已封存/已决定的证据。"""

    code = "immutability_violation"


class DeadlineExceededError(ConflictError):
    code = "deadline_exceeded"
    http_status = 409


class IntegrityError(DomainError):
    """离线核验或写入时发现指纹不一致（疑似篡改）。"""

    code = "integrity_failure"
    http_status = 409
