"""命令行入口：环境检查、建库、检索、单次校验与完整生成流程。"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from afsim_lg.afsim.runner import check_env, run_mission
from afsim_lg.config import get_settings
from afsim_lg.graph.build import run as run_graph
from afsim_lg.rag.index import build_index, search


def _mission_dict(result) -> dict:
    """把 MissionResult 转成可打印的字典。"""
    return {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "workdir": str(result.workdir),
        "input_file": result.input_file,
        "elapsed_sec": round(result.elapsed_sec, 3),
        "timed_out": result.timed_out,
        "errors": result.errors,
    }


def main(argv: list[str] | None = None) -> int:
    """解析子命令并执行。

    Args:
        argv: 参数列表；None 时使用 sys.argv。

    Returns:
        进程退出码；run/validate 未通过时返回 1。
    """
    parser = argparse.ArgumentParser(prog="afsim-lg")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-afsim", help="检查 AFSIM 安装与 mission.exe 版本")

    validate = sub.add_parser("validate", help="在工程目录内运行 mission.exe")
    validate.add_argument("workdir")
    validate.add_argument("--input", default="main.txt")
    validate.add_argument("--timeout", type=float, default=600)

    index = sub.add_parser("index", help="切块两套技能语料并写入 Chroma")
    index.add_argument("--rebuild", action="store_true")

    find = sub.add_parser("search", help="用 bge-m3 检索技能语料")
    find.add_argument("query")
    find.add_argument("-k", type=int, default=8)
    find.add_argument("--skill", choices=["afsim-skill", "afsim-projectskill"])

    run_parser = sub.add_parser("run", help="自然语言需求 → 生成并校验 AFSIM 工程")
    run_parser.add_argument("requirement")
    run_parser.add_argument("--max-attempts", type=int, default=2)
    run_parser.add_argument("--run-id")

    args = parser.parse_args(argv)
    if args.command == "check-afsim":
        print(json.dumps(check_env(), indent=2, ensure_ascii=False))
        return 0
    if args.command == "validate":
        result = run_mission(args.workdir, args.input, timeout_sec=args.timeout)
        print(json.dumps(_mission_dict(result), indent=2, ensure_ascii=False))
        return 0 if result.ok else 1
    if args.command == "index":
        stats = build_index(get_settings().project_root / "afsim-tools", rebuild=args.rebuild)
        print(json.dumps(asdict(stats), indent=2, ensure_ascii=False))
        return 0
    if args.command == "search":
        where = {"skill": args.skill} if args.skill else None
        hits = search(args.query, k=args.k, where=where)
        for i, hit in enumerate(hits, 1):
            meta = hit["metadata"]
            print(
                f"[{i}] {meta['skill']}/{meta['path']} "
                f"(kind={meta['kind']}, dist={hit['distance']:.4f})"
            )
            print(hit["document"][:300].replace("\n", " "))
            print()
        return 0
    if args.command == "run":
        state = run_graph(args.requirement, args.max_attempts, args.run_id)
        summary = {
            "run_id": state["run_id"],
            "status": state.get("status"),
            "generations": state.get("generation", 1),
            "run_dir": str(get_settings().runs_dir / state["run_id"]),
            "files": sorted(state.get("files", {})),
            "contract": state.get("contract", {}),
            "contract_error": state.get("contract_error", ""),
            "mission": state.get("mission", {}),
            "checks": state.get("checks", []),
            "evidence": state.get("evidence", {}),
            "assessment": state.get("assessment", {}),
            "manifest": str(get_settings().runs_dir / state["run_id"] / "manifest.json"),
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if summary["status"] == "passed" else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
