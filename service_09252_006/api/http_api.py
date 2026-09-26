"""HTTP API 边界（标准库 http.server，无第三方依赖）。

认证：Authorization: Bearer <api token>；管理端点（建用户、发 token）
使用启动时配置的 X-Bootstrap-Token。所有写操作支持 Idempotency-Key 头。

材料内容上传走 JSON（content_base64），下载为原始字节。
"""
from __future__ import annotations

import base64
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from ..application.container import ApplicationContext
from ..domain.enums import Role
from ..domain.errors import DomainError
from ..domain.models import User


class HttpApiServer:
    def __init__(
        self,
        context: ApplicationContext,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        bootstrap_token: str = "",
    ) -> None:
        self.context = context
        self.bootstrap_token = bootstrap_token

        class _Handler(ApiHandler):
            pass

        _Handler.app = self  # type: ignore[attr-defined]
        self._httpd = ThreadingHTTPServer((host, port), _Handler)

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2]  # type: ignore[return-value]

    def serve_foreground(self) -> None:
        self._httpd.serve_forever()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="qe-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class ApiHandler(BaseHTTPRequestHandler):
    app: HttpApiServer

    server_version = "QualityEvidence/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静
        return

    # ------------------------------------------------------------ 入口
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/healthz":
                self._send_json(200, {"ok": True})
                return
            name, kwargs = self._match(method, path)
            if name is None:
                self._send_json(404, {"error": {"code": "not_found", "message": "未知端点"}})
                return
            getattr(self, name)(**kwargs)
        except DomainError as exc:
            self._send_json(
                exc.http_status,
                {"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            self._send_json(
                400, {"error": {"code": "bad_request", "message": str(exc)}}
            )

    def _match(self, method: str, path: str) -> tuple[str | None, dict]:
        rules = ROUTES.get(method, [])
        parts = path.split("/")
        for pattern, name in rules:
            pparts = pattern.split("/")
            if len(pparts) != len(parts):
                continue
            kwargs: dict = {}
            for pp, actual in zip(pparts, parts):
                if pp.startswith("{") and pp.endswith("}"):
                    kwargs[pp[1:-1]] = actual
                elif pp != actual:
                    break
            else:
                return name, kwargs
        return None, {}

    # ------------------------------------------------------------ 工具
    @property
    def services(self) -> ApplicationContext:
        return self.app.context

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _idempotency_key(self) -> str | None:
        return self.headers.get("Idempotency-Key")

    def _actor(self) -> User:
        from ..domain.errors import PermissionDeniedError

        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise PermissionDeniedError("缺少 Bearer 令牌")
        user = self.services.repo.get_user_by_token(header[7:].strip())
        if user is None:
            raise PermissionDeniedError("令牌无效")
        return user

    def _require_bootstrap(self) -> None:
        from ..domain.errors import PermissionDeniedError

        token = self.app.bootstrap_token
        if token and self.headers.get("X-Bootstrap-Token") != token:
            raise PermissionDeniedError("管理端点需要引导令牌")

    def _send_json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, status: int, data: bytes, media_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    # ----------------------------------------------------- 管理（引导）
    def admin_create_user(self) -> None:
        self._require_bootstrap()
        body = self._read_json()
        roles = body.get("roles") or []
        valid = {r.value for r in Role}
        bad = [r for r in roles if r not in valid]
        if bad:
            from ..domain.errors import ValidationError

            raise ValidationError("未知角色", details={"roles": bad})
        user = User(
            user_id=body["user_id"],
            institution_id=body.get("institution_id"),
            roles=tuple(roles),
            display_name=body.get("display_name", ""),
        )
        with self.services.repo.transaction():
            self.services.repo.upsert_user(user)
        self._send_json(201, {"user_id": user.user_id, "roles": list(user.roles)})

    def admin_issue_token(self) -> None:
        self._require_bootstrap()
        body = self._read_json()
        user = self.services.repo.get_user(body["user_id"])
        if user is None:
            from ..domain.errors import NotFoundError

            raise NotFoundError("用户不存在")
        token = body.get("token") or "tok_" + uuid.uuid4().hex
        with self.services.repo.transaction():
            self.services.repo.put_token(token, user.user_id, self.services.clock.now_iso())
        self._send_json(201, {"user_id": user.user_id, "token": token})

    # --------------------------------------------------------- 证据
    def create_material(self) -> None:
        actor = self._actor()
        body = self._read_json()
        result = self.services.evidence.register_material(
            actor,
            kind=body["kind"],
            title=body["title"],
            sensitivity=body.get("sensitivity", "normal"),
            idempotency_key=self._idempotency_key(),
        )
        self._send_json(201, result)

    def get_material(self, material_id: str) -> None:
        actor = self._actor()
        self._send_json(200, self.services.evidence.get_material(actor, material_id))

    def upload_version(self, material_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        data = base64.b64decode(body["content_base64"], validate=True)
        result = self.services.evidence.upload_version(
            actor,
            material_id=material_id,
            data=data,
            media_type=body.get("media_type", "application/octet-stream"),
            expected_sha256=body.get("expected_sha256"),
            idempotency_key=self._idempotency_key(),
        )
        self._send_json(201, result)

    def get_version(self, version_id: str) -> None:
        actor = self._actor()
        self._send_json(200, self.services.evidence.get_version(actor, version_id))

    def withdraw_version(self, version_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        result = self.services.evidence.withdraw_version(
            actor,
            version_id=version_id,
            reason=body.get("reason", ""),
            idempotency_key=self._idempotency_key(),
        )
        self._send_json(200, result)

    def withdraw_material(self, material_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        result = self.services.evidence.withdraw_material(
            actor,
            material_id=material_id,
            reason=body.get("reason", ""),
            idempotency_key=self._idempotency_key(),
        )
        self._send_json(200, result)

    # --------------------------------------------------------- 评审包
    def create_package(self) -> None:
        actor = self._actor()
        body = self._read_json()
        result = self.services.packages.create_package(
            actor,
            title=body["title"],
            supersedes_package_id=body.get("supersedes_package_id"),
            idempotency_key=self._idempotency_key(),
        )
        self._send_json(201, result)

    def list_packages(self) -> None:
        actor = self._actor()
        self._send_json(200, {"packages": self.services.packages.list_packages(actor)})

    def get_package(self, package_id: str) -> None:
        actor = self._actor()
        self._send_json(200, self.services.packages.build_package_view(actor, package_id))

    def add_entry(self, package_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        result = self.services.packages.add_entry(
            actor,
            package_id=package_id,
            version_id=body["version_id"],
            idempotency_key=self._idempotency_key(),
        )
        self._send_json(201, result)

    def seal_package(self, package_id: str) -> None:
        actor = self._actor()
        result = self.services.packages.seal_package(
            actor,
            package_id=package_id,
            idempotency_key=self._idempotency_key(),
        )
        self._send_json(200, result)

    def download_entry(self, package_id: str, version_id: str) -> None:
        actor = self._actor()
        meta, data, media_type = self.services.packages.download_entry(
            actor, package_id=package_id, version_id=version_id
        )
        self._send_bytes(
            200,
            data,
            media_type,
            extra_headers={
                "X-Version-Id": meta["version_id"],
                "X-Content-Sha256": meta["sha256"],
            },
        )

    # ----------------------------------------------------------- 评审
    def assign(self, package_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        result = self.services.reviews.assign_reviewer(
            actor,
            package_id=package_id,
            reviewer_id=body["reviewer_id"],
            deadline_local_iso=body.get("deadline_local_iso"),
            deadline_timezone=body.get("deadline_timezone"),
            idempotency_key=self._idempotency_key(),
        )
        self._send_json(201, result)

    def list_requests(self, package_id: str) -> None:
        actor = self._actor()
        self._send_json(
            200, {"requests": self.services.reviews.list_requests(actor, package_id)}
        )

    def cancel_request(self, request_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        self._send_json(
            200,
            self.services.reviews.cancel_request(
                actor,
                request_id=request_id,
                reason=body.get("reason", ""),
                idempotency_key=self._idempotency_key(),
            ),
        )

    def respond_request(self, request_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        self._send_json(
            200,
            self.services.reviews.respond_assignment(
                actor,
                request_id=request_id,
                accept=bool(body["accept"]),
                idempotency_key=self._idempotency_key(),
            ),
        )

    def create_objection(self, request_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        self._send_json(
            201,
            self.services.reviews.record_objection(
                actor,
                request_id=request_id,
                category=body["category"],
                detail=body["detail"],
                idempotency_key=self._idempotency_key(),
            ),
        )

    def submit_verdict(self, request_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        self._send_json(
            200,
            self.services.reviews.submit_verdict(
                actor,
                request_id=request_id,
                verdict=body["verdict"],
                comment=body.get("comment", ""),
                idempotency_key=self._idempotency_key(),
            ),
        )

    def issue_decision(self, package_id: str) -> None:
        actor = self._actor()
        body = self._read_json()
        self._send_json(
            200,
            self.services.reviews.issue_decision(
                actor,
                package_id=package_id,
                decision=body["decision"],
                note=body.get("note", ""),
                idempotency_key=self._idempotency_key(),
            ),
        )


# 路由表：方法 -> [(路径模式, 处理方法名)]
def _routes() -> dict[str, list[tuple[str, str]]]:
    post = [
        ("/v1/admin/users", "admin_create_user"),
        ("/v1/admin/tokens", "admin_issue_token"),
        ("/v1/materials", "create_material"),
        ("/v1/materials/{material_id}/versions", "upload_version"),
        ("/v1/materials/{material_id}/withdraw", "withdraw_material"),
        ("/v1/versions/{version_id}/withdraw", "withdraw_version"),
        ("/v1/packages", "create_package"),
        ("/v1/packages/{package_id}/entries", "add_entry"),
        ("/v1/packages/{package_id}/seal", "seal_package"),
        ("/v1/packages/{package_id}/assignments", "assign"),
        ("/v1/packages/{package_id}/decision", "issue_decision"),
        ("/v1/requests/{request_id}/cancel", "cancel_request"),
        ("/v1/requests/{request_id}/respond", "respond_request"),
        ("/v1/requests/{request_id}/objections", "create_objection"),
        ("/v1/requests/{request_id}/verdict", "submit_verdict"),
    ]
    get = [
        ("/v1/materials/{material_id}", "get_material"),
        ("/v1/versions/{version_id}", "get_version"),
        ("/v1/packages", "list_packages"),
        ("/v1/packages/{package_id}", "get_package"),
        ("/v1/packages/{package_id}/requests", "list_requests"),
        (
            "/v1/packages/{package_id}/entries/{version_id}/content",
            "download_entry",
        ),
    ]
    return {"POST": post, "GET": get}


ROUTES = _routes()
