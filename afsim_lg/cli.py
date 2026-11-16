"""命令行入口：环境检查、建库、检索、单次校验、完整生成流程、流程图导出与回归集。"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from afsim_lg.afsim.runner import check_env, run_mission
from afsim_lg.config import get_settings
from afsim_lg.graph.build import build_graph, run as run_graph
from afsim_lg.rag.index import build_index, search
from afsim_lg.regression import CASES, run_all


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

    graph_parser = sub.add_parser("export-graph", help="导出 Mermaid 流程图到 Markdown")
    graph_parser.add_argument("output", nargs="?", default="graph.md", help="输出路径，相对路径按项目根解析")

    regress = sub.add_parser("regress", help="跑固定需求的端到端回归集")
    regress.add_argument("--attempts", type=int, default=3, help="每个用例的最大生成轮数")
    regress.add_argument("--case", action="append", dest="case_names", help="只跑指定用例，可重复")
    regress.add_argument("--report-dir", help="报告目录，相对路径按项目根解析")

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
            "generation_mode": state.get("generation_mode", ""),
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
    if args.command == "export-graph":
        source = build_graph().get_graph().draw_mermaid()
        if source.startswith("---"):
            # 去掉 Mermaid 的 YAML 前端配置，部分 Markdown 预览器不识别。
            source = source.split("---", 2)[2].lstrip()
        out = Path(args.output)
        if not out.is_absolute():
            out = get_settings().project_root / out
        out.write_text(
            "# 当前 LangGraph 流程图\n\n"
            "由 `build_graph().get_graph().draw_mermaid()` 生成，"
            "实线为固定边，虚线为条件边。\n\n"
            f"```mermaid\n{source.rstrip()}\n```\n",
            encoding="utf-8",
        )
        print(out)
        return 0
    if args.command == "regress":
        known = {case["name"] for case in CASES}
        unknown = set(args.case_names or []) - known
        if unknown:
            print("unknown case: " + ", ".join(sorted(unknown)), file=sys.stderr)
            return 2
        cases = [
            case
            for case in CASES
            if not args.case_names or case["name"] in set(args.case_names)
        ]
        report = run_all(cases, args.attempts, args.report_dir)
        print(
            json.dumps(
                {"summary": report["summary"], "report": report["report_dir"]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if report["summary"]["failed"] == 0 else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
