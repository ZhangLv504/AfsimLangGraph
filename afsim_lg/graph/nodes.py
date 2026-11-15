import json
import re
from datetime import datetime
from pathlib import Path

from afsim_lg.afsim.evt import find_evt, parse_evt
from afsim_lg.afsim.runner import run_mission
from afsim_lg.config import get_settings
from afsim_lg.contract import ScenarioContract, parse_contract
from afsim_lg.graph.checks import evaluate
from afsim_lg.graph.state import MainState
from afsim_lg.llm import chat
from afsim_lg.rag.index import search

FILE_MARK_RE = re.compile(r"^===FILE:\s*(.+?)\s*===$")
END_MARK_RE = re.compile(r"^===END===$")
FENCE_RE = re.compile(r"^(```|~~~)")
MAX_FILES = 12
CORPUS_CHARS = 1400
MAIN_CHARS = 4000

EVENT_OUTPUT_STANZA = """event_output
   file output/events.evt
   enable PLATFORM_ADDED
   enable SENSOR_TRACK_INITIATED
   enable WEAPON_FIRED
   enable WEAPON_HIT
   enable PLATFORM_BROKEN
end_event_output
"""

GENERATOR_SYSTEM = """你是 AFSIM 2.9.0 场景生成器。根据用户需求、需求契约与检索到的技能片段，输出可编译的 AFSIM 工程文件。

输出格式必须严格如下，不要使用 Markdown 代码围栏：
===FILE: main.txt===
<文件内容>
===FILE: setup.txt===
<文件内容>
===END===

硬性规则：
1. 必须包含 main.txt；路径只能是相对路径，扩展名 .txt。
2. platform_type 必须带基类型 WSF_PLATFORM；每个块都要有对应的 end_ 闭合。
2a. 先定义 platform_type，再写 platform 实例；`platform <实例名> <类型名>` 的类型名必须与已定义的 platform_type 名称逐字符一致。
3. 坐标写成 30:00:00n 120:00:00e；速度带单位 kts；时间带单位 sec；高度带 ft msl。
4. 注释必须独立成行，不要行尾注释。
5. 不要使用 thread_count（本机 2.9.0 构建不支持该命令）。
6. output/ 目录由系统预建，event_output 可写 output/xxx.evt。
7. 优先复用检索片段中已验证的写法，不要自造命令。
8. 必须显式写 end_time，且不小于契约要求的时长。
9. 检索片段只是参考，不要把片段里的类型名（platform_type / sensor / weapon / antenna_pattern 等）直接引用到工程，除非你在同一份输出中完整定义了它；所有引用必须先定义后使用。
10. 契约 expectations 非空时，必须包含事件输出块：
event_output
   file output/events.evt
   enable PLATFORM_ADDED
   enable SENSOR_TRACK_INITIATED
   enable WEAPON_FIRED
   enable WEAPON_HIT
   enable PLATFORM_BROKEN
end_event_output"""

JUDGE_SYSTEM = """你是 AFSIM 场景验收员。对照用户需求与需求契约检查生成的工程，只输出 JSON：
{"passed": true 或 false, "missing": ["缺失或错误的点"], "fix_hint": "下一轮如何修改"}

判定规则：
1. 确定性检查有失败项时，passed 必须为 false。
2. 只检查需求原文中明确写出的内容；需求没写的平台、装备、交战一律不算缺失，不要按常识补默认要求。
3. 契约 expectations 为空时，不得因为“没有探测/发射/命中”判失败。
4. missing 必须是需求原文能对应的具体缺失（平台、阵营、装备、时长、明确要求的事件），不能是推测。
5. 确定性检查全部通过且找不到具体缺失时，passed 应为 true。"""


def _run_dir(state: MainState) -> Path:
    return get_settings().runs_dir / state["run_id"]


def _attempt_no(state: MainState) -> int:
    return state.get("attempt", 0) + 1


def _attempt_dir(state: MainState) -> Path:
    return _run_dir(state) / "attempts" / f"attempt-{_attempt_no(state):02d}"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )


def _corpus_block(hits: list[dict]) -> str:
    parts = []
    for i, hit in enumerate(hits, 1):
        meta = hit["metadata"]
        parts.append(
            f"[{i}] {meta['skill']}/{meta['path']} (kind={meta['kind']})\n"
            f"{hit['document'][:CORPUS_CHARS]}"
        )
    return "\n\n".join(parts)


def _clean(content: str) -> str:
    lines = content.strip().splitlines()
    if len(lines) >= 2 and FENCE_RE.match(lines[0].strip()) and FENCE_RE.match(lines[-1].strip()):
        lines = lines[1:-1]
    return "\n".join(lines).strip()


def parse_files(text: str) -> dict[str, str]:
    files: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush():
        if current and buffer:
            body = _clean("\n".join(buffer))
            if body:
                files[current] = body + "\n"

    for raw in text.splitlines():
        line = raw.strip()
        mark = FILE_MARK_RE.match(line)
        if mark:
            flush()
            current = mark.group(1).strip().strip("`").replace("\\", "/")
            buffer = []
            continue
        if END_MARK_RE.match(line):
            flush()
            break
        if current is not None:
            buffer.append(raw)
    flush()
    return files


def _safe_path(run_dir: Path, name: str) -> Path | None:
    if name.startswith("/") or ":" in name or ".." in Path(name).parts:
        return None
    if Path(name).suffix.lower() != ".txt":
        return None
    target = (run_dir / name).resolve()
    if run_dir.resolve() not in target.parents:
        return None
    return target


def _ensure_event_output(files: dict[str, str], contract: ScenarioContract) -> tuple[dict, bool]:
    if not contract.needs_event_evidence:
        return files, False
    if any("event_output" in content for content in files.values()):
        return files, False
    if "main.txt" not in files:
        return files, False
    updated = dict(files)
    updated["main.txt"] = updated["main.txt"].rstrip() + "\n\n" + EVENT_OUTPUT_STANZA
    return updated, True


def parse_requirement(state: MainState) -> dict:
    run_dir = _run_dir(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    contract, error = parse_contract(state["requirement"])
    _write_json(run_dir / "contract.json", contract.model_dump())
    if error:
        (run_dir / "contract_error.txt").write_text(error, encoding="utf-8")
    return {"contract": contract.model_dump(), "contract_error": error}


def retrieve(state: MainState) -> dict:
    hits = search(state["requirement"], k=8)
    return {"retrieved": hits}


def generate(state: MainState) -> dict:
    user_parts = [f"【需求】\n{state['requirement']}"]
    contract = state.get("contract")
    if contract:
        user_parts.append(
            "【需求契约】\n" + json.dumps(contract, ensure_ascii=False, indent=1)
        )
    if state.get("feedback"):
        user_parts.append(f"【上一轮失败反馈】\n{state['feedback']}")
    user_parts.append(f"【技能检索片段】\n{_corpus_block(state.get('retrieved', []))}")
    user_parts.append("请输出完整工程文件。")
    prompt = "\n\n".join(user_parts)

    files = parse_files(chat(GENERATOR_SYSTEM, prompt))
    if not files:
        retry_prompt = prompt + "\n\n注意：你的输出没有可解析的文件标记。只输出文件标记格式。"
        files = parse_files(chat(GENERATOR_SYSTEM, retry_prompt))
    error = "" if files else "模型输出中没有可解析的 ===FILE: ... === 文件块"
    return {
        "files": files,
        "generation_error": error,
        "generation": state.get("generation", 0) + 1,
    }


def write_files(state: MainState) -> dict:
    run_dir = _run_dir(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "output").mkdir(exist_ok=True)
    contract = ScenarioContract.model_validate(state["contract"]) if state.get("contract") else ScenarioContract()
    files, _ = _ensure_event_output(state.get("files", {}), contract)

    written = 0
    for name, content in list(files.items())[:MAX_FILES]:
        target = _safe_path(run_dir, name)
        if target is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written += 1
    if not written:
        return {"generation_error": "没有可写入的文件"}

    attempt_dir = _attempt_dir(state)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for name, content in list(files.items())[:MAX_FILES]:
        target = _safe_path(attempt_dir, name)
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
    _write_json(
        attempt_dir / "retrieved.json",
        [
            {
                "skill": hit["metadata"]["skill"],
                "path": hit["metadata"]["path"],
                "kind": hit["metadata"]["kind"],
                "distance": hit["distance"],
                "document": hit["document"],
            }
            for hit in state.get("retrieved", [])
        ],
    )
    return {"files": files, "attempt_dir": str(attempt_dir), "generation_error": ""}


def validate(state: MainState) -> dict:
    run_dir = _run_dir(state)
    result = run_mission(run_dir)
    (run_dir / "mission_stdout.log").write_text(result.output, encoding="utf-8")
    attempt_dir = Path(state["attempt_dir"]) if state.get("attempt_dir") else _attempt_dir(state)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    (attempt_dir / "mission_stdout.log").write_text(result.output, encoding="utf-8")
    mission = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "elapsed_sec": round(result.elapsed_sec, 3),
        "timed_out": result.timed_out,
        "errors": result.errors,
    }
    _write_json(attempt_dir / "mission.json", mission)
    return {"mission": mission}


def _judge(state: MainState, checks: list[dict], evidence: dict) -> dict:
    run_dir = _run_dir(state)
    main_txt = ""
    main_path = run_dir / "main.txt"
    if main_path.is_file():
        main_txt = main_path.read_text(encoding="utf-8")[:MAIN_CHARS]
    check_lines = "\n".join(
        f"- {'PASS' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}" for c in checks
    ) or "- 无确定性检查"
    prompt = (
        f"【用户需求】\n{state['requirement']}\n\n"
        f"【需求契约】\n{json.dumps(state.get('contract', {}), ensure_ascii=False, indent=1)}\n\n"
        f"【确定性检查】\n{check_lines}\n\n"
        f"【运行证据】\n{json.dumps(evidence, ensure_ascii=False)}\n\n"
        f"【工程文件】\n{', '.join(sorted(state.get('files', {})))}\n\n"
        f"【main.txt】\n{main_txt}\n\n"
        "mission.exe 已成功运行。请判断需求是否被满足。"
    )
    return json.loads(chat(JUDGE_SYSTEM, prompt, json_mode=True))


def assess(state: MainState) -> dict:
    mission = state.get("mission", {})
    attempt_dir = Path(state["attempt_dir"]) if state.get("attempt_dir") else _attempt_dir(state)

    if not mission.get("ok"):
        assessment = {
            "passed": False,
            "missing": mission.get("errors", []),
            "fix_hint": "先修复 mission.exe 报错。",
        }
        _write_json(attempt_dir / "assessment.json", assessment)
        _write_json(
            attempt_dir / "summary.json",
            {
                "attempt": _attempt_no(state),
                "files": sorted(state.get("files", {})),
                "mission": mission,
                "checks": [],
                "assessment": assessment,
            },
        )
        return {"assessment": assessment, "status": "retry", "checks": [], "evidence": {}}

    contract = ScenarioContract.model_validate(state.get("contract", {}))
    files_text = "\n\n".join(state.get("files", {}).values())
    evt_path = find_evt(_run_dir(state))
    evt = parse_evt(evt_path) if evt_path else None
    checks, evidence = evaluate(contract, files_text, evt)

    try:
        verdict = _judge(state, checks, evidence)
        judge_passed = bool(verdict.get("passed"))
        missing = list(verdict.get("missing", []))
        fix_hint = verdict.get("fix_hint", "")
    except (json.JSONDecodeError, RuntimeError):
        judge_passed = False
        missing = ["验收模型输出无法解析"]
        fix_hint = "重新生成并确保满足需求。"

    failed = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
    passed = all(c["ok"] for c in checks) and judge_passed
    assessment = {
        "passed": passed,
        "missing": failed + missing,
        "fix_hint": fix_hint,
        "deterministic_passed": all(c["ok"] for c in checks),
    }
    _write_json(attempt_dir / "assessment.json", assessment)
    _write_json(attempt_dir / "checks.json", checks)
    _write_json(attempt_dir / "evidence.json", evidence)
    _write_json(
        attempt_dir / "summary.json",
        {
            "attempt": _attempt_no(state),
            "files": sorted(state.get("files", {})),
            "mission": mission,
            "checks": checks,
            "evidence": evidence,
            "assessment": assessment,
        },
    )
    return {
        "assessment": assessment,
        "evidence": evidence,
        "checks": checks,
        "status": "passed" if passed else "retry",
    }


def fix(state: MainState) -> dict:
    assessment = state.get("assessment", {})
    mission = state.get("mission", {})
    failed_dir = _attempt_dir(state)
    if not (failed_dir / "summary.json").exists():
        _write_json(
            failed_dir / "summary.json",
            {
                "attempt": _attempt_no(state),
                "generation_error": state.get("generation_error", ""),
                "files": [],
                "assessment": assessment,
            },
        )
    parts = []
    if mission.get("errors"):
        parts.append("mission.exe 报错：\n" + "\n".join(mission["errors"]))
    if assessment.get("missing"):
        parts.append("需求缺失：\n" + "\n".join(assessment["missing"]))
    if assessment.get("fix_hint"):
        parts.append("修改建议：\n" + assessment["fix_hint"])
    return {"attempt": state.get("attempt", 0) + 1, "feedback": "\n\n".join(parts)}


def finalize(state: MainState) -> dict:
    passed = state.get("assessment", {}).get("passed", False)
    status = "passed" if passed else "failed"
    run_dir = _run_dir(state)
    attempts = []
    attempts_dir = run_dir / "attempts"
    if attempts_dir.is_dir():
        for summary in sorted(attempts_dir.glob("attempt-*/summary.json")):
            try:
                attempts.append(json.loads(summary.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
    settings = get_settings()
    _write_json(
        run_dir / "manifest.json",
        {
            "run_id": state["run_id"],
            "requirement": state["requirement"],
            "contract": state.get("contract", {}),
            "contract_error": state.get("contract_error", ""),
            "status": status,
            "generations": state.get("generation", 1),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "models": {"llm": settings.llm_model, "embed": settings.embed_model},
            "attempts": attempts,
        },
    )
    return {"status": status}


def route_after_generate(state: MainState) -> str:
    return "write" if state.get("files") and not state.get("generation_error") else "fix"


def route_after_fix(state: MainState) -> str:
    if state.get("attempt", 0) >= state.get("max_attempts", 2):
        return "finalize"
    return "generate"


def route_after_assess(state: MainState) -> str:
    return "finalize" if state.get("status") == "passed" else "fix"
