"""VAF v0.1 command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from vaf.application.local_workflow import LocalWorkflow, WorkflowError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaf", description="规格驱动的 AI 软件研发工作流")
    parser.add_argument("--path", default=".", help="目标 Git 仓库路径，默认当前目录")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="初始化 .vaf 项目目录")
    run = subparsers.add_parser("run", help="创建 Change 并生成 PRD 草稿")
    run.add_argument("--change", required=True)
    run.add_argument("--title")
    run.add_argument("--objective")
    run.add_argument("--source", default="cli")
    run.add_argument("--implementation-spec", help="包含 implementation.changes 的 YAML 文件")

    for name in ("status", "review", "resume", "implement", "verify", "trace"):
        command = subparsers.add_parser(name)
        command.add_argument("--run", required=True)

    approve = subparsers.add_parser("approve")
    approve.add_argument("--run", required=True)
    approve.add_argument("--actor", default="user")
    approve.add_argument("--target-hash", required=True)
    approve.add_argument("--comment", default="")

    reject = subparsers.add_parser("reject")
    reject.add_argument("--run", required=True)
    reject.add_argument("--actor", default="user")
    reject.add_argument("--target-hash", required=True)
    reject.add_argument("--comment", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workflow = LocalWorkflow(Path(args.path))
    try:
        if args.command == "init":
            created = workflow.init_project()
            print(json.dumps({"project_root": str(workflow.project_root), "created": [str(path) for path in created]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            state = workflow.run(
                args.change,
                args.title or "",
                args.objective or "",
                args.source,
                args.implementation_spec,
            )
            print(json.dumps(state.__dict__, ensure_ascii=False, indent=2))
            return 0
        if args.command == "status":
            print(json.dumps(workflow.state(args.run).__dict__, ensure_ascii=False, indent=2))
            return 0
        if args.command == "review":
            print(json.dumps(workflow.review(args.run), ensure_ascii=False, indent=2))
            return 0
        if args.command == "approve":
            print(json.dumps(workflow.approve(args.run, args.actor, args.target_hash, args.comment).__dict__, ensure_ascii=False, indent=2))
            return 0
        if args.command == "reject":
            print(json.dumps(workflow.reject(args.run, args.actor, args.target_hash, args.comment).__dict__, ensure_ascii=False, indent=2))
            return 0
        if args.command == "resume":
            print(json.dumps(workflow.resume(args.run).__dict__, ensure_ascii=False, indent=2))
            return 0
        if args.command == "implement":
            print(json.dumps(workflow.implement(args.run).__dict__, ensure_ascii=False, indent=2))
            return 0
        if args.command == "verify":
            print(json.dumps(workflow.verify(args.run).__dict__, ensure_ascii=False, indent=2))
            return 0
        if args.command == "trace":
            print(json.dumps(workflow.trace(args.run), ensure_ascii=False, indent=2))
            return 0
    except WorkflowError as exc:
        print(f"VAF 错误：{exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
