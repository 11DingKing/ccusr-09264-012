"""命令行入口。

用法：
  python3 -m service_09252_006.cli serve   --db ./data/qe.db --host 127.0.0.1 --port 8080 \\
      --bootstrap-token <token>
  python3 -m service_09252_006.cli verify  --db ./data/qe.db [--json]

verify 为离线核验：不需要服务进程，只读打开数据库并重算全部指纹。
核验通过退出码 0；发现不一致退出码 2；数据库无法打开退出码 1。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from .application.container import ApplicationContext
from .application.verification import verify_database
from .api.http_api import HttpApiServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="service_09252_006")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="启动 HTTP API 服务")
    serve_p.add_argument("--db", required=True)
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8080)
    serve_p.add_argument("--bootstrap-token", default="")

    verify_p = sub.add_parser("verify", help="离线完整性核验")
    verify_p.add_argument("--db", required=True)
    verify_p.add_argument("--json", action="store_true", help="只输出 JSON 报告")

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    if args.command == "verify":
        return _verify(args)
    return 1


def _serve(args: argparse.Namespace) -> int:
    with ApplicationContext(args.db) as context:
        server = HttpApiServer(
            context,
            host=args.host,
            port=args.port,
            bootstrap_token=args.bootstrap_token,
        )
        host, port = server.address
        print(f"质量证据链服务监听在 http://{host}:{port}", flush=True)
        try:
            server.serve_foreground()
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
    return 0


def _verify(args: argparse.Namespace) -> int:
    try:
        report = verify_database(args.db)
    except (sqlite3.Error, OSError) as exc:
        print(f"无法打开数据库: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(report)
    return 0 if report.ok else 2


def _print_human(report) -> None:
    d = report.to_dict()
    print("完整性核验报告")
    print("==============")
    print(f"结论: {'通过' if d['ok'] else '发现问题'}")
    print(f"内容对象: {d['blob_count']}  评审包: {d['package_count']}"
          f"  已封存: {d['sealed_count']}  已签发: {d['decided_count']}")
    if d["withdrawn_in_sealed"]:
        print(f"提示: {len(d['withdrawn_in_sealed'])} 个封存清单条目引用的版本事后被撤回"
              "（历史指纹仍有效，需复审时应另建复审包）")
    for warning in d["warnings"]:
        print(f"  [警告] {warning['kind']}: {warning}")
    for failure in d["failures"]:
        print(f"  [失败] {failure['kind']}: {failure}")


if __name__ == "__main__":
    raise SystemExit(main())
