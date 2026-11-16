"""固定需求的端到端回归集。

每个用例都走完整的 生成→校验→验收→修复 流程，结果写入
`workspace/regressions/<时间>/report.json` 与 `report.md`。
"""

import json
import time
from datetime import datetime
from pathlib import Path

from afsim_lg.config import get_settings
from afsim_lg.graph.build import run as run_graph

CASES = [
    {
        "name": "simple-flight",
        "requirement": "生成一个最简单的 AFSIM 场景：蓝方一架飞机在 12000 米高度平飞 90 秒，不需要探测或交战",
    },
    {
        "name": "radar-detect",
        "requirement": "红方一部地面雷达探测蓝方一架飞机，仿真 90 秒",
    },
    {
        "name": "engagement-fire",
        "requirement": "红方战斗机向蓝方飞机发射一枚导弹，仿真 120 秒",
    },
]


def _manifest_attempts(run_dir: Path) -> list[dict]:
    """从 manifest 提取每轮的模式与结果，便于报告展示。"""
    path = run_dir / "manifest.json"
    if not path.is_file():
        return []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    attempts = []
    for item in manifest.get("attempts", []):
        mission = item.get("mission", {})
        assessment = item.get("assessment", {})
        attempts.append(
            {
                "attempt": item.get("attempt"),
                "generation_mode": item.get("generation_mode", ""),
                "mission_ok": bool(mission.get("ok")),
                "passed": bool(assessment.get("passed")),
            }
        )
    return attempts


def run_case(case: dict, max_attempts: int = 3) -> dict:
    """执行单个回归用例并汇总结果。

    单个用例抛异常时记录错误，不中断整个回归集。
    """
    settings = get_settings()
    run_id = f"regress-{datetime.now():%Y%m%d-%H%M%S}-{case['name']}"
    started = time.monotonic()
    try:
        state = run_graph(case["requirement"], max_attempts, run_id)
        error = ""
    except Exception as exc:  # noqa: BLE001 - 批量回归需要隔离单例崩溃
        state = {}
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    run_dir = settings.runs_dir / run_id
    assessment = state.get("assessment", {}) if state else {}
    return {
        "name": case["name"],
        "requirement": case["requirement"],
        "run_id": run_id,
        "status": state.get("status", "error") if state else "error",
        "passed": state.get("status") == "passed" if state else False,
        "generations": state.get("generation", 0) if state else 0,
        "elapsed_sec": round(elapsed, 1),
        "error": error,
        "missing": list(assessment.get("missing", [])),
        "checks": [
            {"name": check["name"], "ok": check["ok"], "detail": check["detail"]}
            for check in (state.get("checks", []) if state else [])
        ],
        "attempts": _manifest_attempts(run_dir),
        "run_dir": str(run_dir),
    }


def _markdown(report: dict) -> str:
    """把回归结果渲染成易读的 Markdown。"""
    summary = report["summary"]
    lines = [
        "# 端到端回归报告",
        "",
        f"- 开始：{report['started_at']}",
        f"- 结束：{report['finished_at']}",
        f"- max_attempts：{report['max_attempts']}",
        f"- 通过：{summary['passed']}/{summary['total']}（{summary['pass_rate']:.0%}）",
        f"- 总生成轮数：{summary['generations']}，平均 {summary['avg_generations']}",
        "",
        "| 用例 | 结果 | 生成轮数 | mission | 确定性检查 | 耗时(s) |",
        "|------|------|----------|---------|------------|---------|",
    ]
    for result in report["results"]:
        checks_ok = sum(1 for check in result["checks"] if check["ok"])
        last = result["attempts"][-1] if result["attempts"] else None
        mission = "-" if last is None else ("ok" if last["mission_ok"] else "fail")
        lines.append(
            f"| {result['name']} | {'PASS' if result['passed'] else 'FAIL'} | "
            f"{result['generations']} | {mission} | "
            f"{checks_ok}/{len(result['checks'])} | {result['elapsed_sec']} |"
        )

    failures = [result for result in report["results"] if not result["passed"]]
    if failures:
        lines += ["", "## 失败详情", ""]
        for result in failures:
            lines.append(f"### {result['name']}")
            lines.append(f"- run_dir：`{result['run_dir']}`")
            if result["error"]:
                lines.append(f"- 异常：{result['error']}")
            for item in result["missing"]:
                lines.append(f"- {item}")
            lines.append("")
    return "\n".join(lines)


def run_all(
    cases: list[dict] | None = None,
    max_attempts: int = 3,
    report_dir: str | None = None,
) -> dict:
    """依次执行回归用例并写出报告。

    Args:
        cases: 用例列表；None 时使用内置的三类需求。
        max_attempts: 每个用例的最大生成轮数。
        report_dir: 报告目录；相对路径按项目根解析。

    Returns:
        含 summary、results 与 report_dir 的报告字典。
    """
    selected = cases if cases is not None else CASES
    started = datetime.now()
    results = [run_case(case, max_attempts) for case in selected]
    finished = datetime.now()
    passed = sum(1 for result in results if result["passed"])
    generations = sum(result["generations"] for result in results)
    summary = {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results), 3) if results else 0.0,
        "generations": generations,
        "avg_generations": round(generations / len(results), 2) if results else 0.0,
        "elapsed_sec": round((finished - started).total_seconds(), 1),
    }
    report = {
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "max_attempts": max_attempts,
        "summary": summary,
        "results": results,
    }

    root = Path(report_dir) if report_dir else Path("workspace") / "regressions" / f"{started:%Y%m%d-%H%M%S}"
    if not root.is_absolute():
        root = get_settings().project_root / root
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "report.md").write_text(_markdown(report), encoding="utf-8")
    report["report_dir"] = str(root)
    return report