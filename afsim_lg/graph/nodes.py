import json
import re
from pathlib import Path

from afsim_lg.afsim.runner import run_mission
from afsim_lg.config import get_settings
from afsim_lg.graph.state import MainState
from afsim_lg.llm import chat
from afsim_lg.rag.index import search

FILE_MARK_RE = re.compile(r"^===FILE:\s*(.+?)\s*===$")
END_MARK_RE = re.compile(r"^===END===$")
FENCE_RE = re.compile(r"^(```|~~~)")
MAX_FILES = 12
CORPUS_CHARS = 1400
MAIN_CHARS = 4000

GENERATOR_SYSTEM = """你是 AFSIM 2.9.0 场景生成器。根据用户需求与检索到的技能片段，输出可编译的 AFSIM 工程文件。

输出格式必须严格如下，不要使用 Markdown 代码围栏：
===FILE: main.txt===
<文件内容>
===FILE: setup.txt===
<文件内容>
===END===

硬性规则：
1. 必须包含 main.txt；路径只能是相对路径，扩展名 .txt。
2. platform_type 必须带基类型 WSF_PLATFORM；每个块都要有对应的 end_ 闭合。
3. 坐标写成 30:00:00n 120:00:00e；速度带单位 kts；时间带单位 sec；高度带 ft msl。
4. 注释必须独立成行，不要行尾注释。
5. 不要使用 thread_count（本机 2.9.0 构建不支持该命令）。
6. output/ 目录由系统预建，event_output 可写 output/xxx.evt。
7. 优先复用检索片段中已验证的写法，不要自造命令。
8. 仿真时长要覆盖需求中的时长。"""

JUDGE_SYSTEM = """你是 AFSIM 场景验收员。对照用户需求检查生成的工程，只输出 JSON：
{"passed": true 或 false, "missing": ["缺失或错误的点"], "fix_hint": "下一轮如何修改"}
只判断需求是否满足（平台、阵营、传感器、武器、时长等），不评价代码风格。"""


def _run_dir(state: MainState) -> Path:
    return get_settings().runs_dir / state["run_id"]


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


def retrieve(state: MainState) -> dict:
    hits = search(state["requirement"], k=8)
    return {"retrieved": hits}


def generate(state: MainState) -> dict:
    user_parts = [f"【需求】\n{state['requirement']}"]
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
    return {"files": files, "generation_error": error}


def write_files(state: MainState) -> dict:
    run_dir = _run_dir(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "output").mkdir(exist_ok=True)
    written = 0
    for name, content in list(state.get("files", {}).items())[:MAX_FILES]:
        target = _safe_path(run_dir, name)
        if target is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written += 1
    return {"generation_error": "" if written else "没有可写入的文件"}


def validate(state: MainState) -> dict:
    run_dir = _run_dir(state)
    result = run_mission(run_dir)
    (run_dir / "mission_stdout.log").write_text(result.output, encoding="utf-8")
    mission = {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "elapsed_sec": round(result.elapsed_sec, 3),
        "timed_out": result.timed_out,
        "errors": result.errors,
    }
    return {"mission": mission}


def assess(state: MainState) -> dict:
    mission = state.get("mission", {})
    if not mission.get("ok"):
        return {
            "assessment": {
                "passed": False,
                "missing": mission.get("errors", []),
                "fix_hint": "先修复 mission.exe 报错。",
            },
            "status": "retry",
        }

    run_dir = _run_dir(state)
    main_txt = ""
    main_path = run_dir / "main.txt"
    if main_path.is_file():
        main_txt = main_path.read_text(encoding="utf-8")[:MAIN_CHARS]
    prompt = (
        f"【用户需求】\n{state['requirement']}\n\n"
        f"【工程文件】\n{', '.join(sorted(state.get('files', {})))}\n\n"
        f"【main.txt】\n{main_txt}\n\n"
        "mission.exe 已编译并运行成功。请判断需求是否被满足。"
    )
    try:
        verdict = json.loads(chat(JUDGE_SYSTEM, prompt, json_mode=True))
        passed = bool(verdict.get("passed"))
        assessment = {
            "passed": passed,
            "missing": verdict.get("missing", []),
            "fix_hint": verdict.get("fix_hint", ""),
        }
    except (json.JSONDecodeError, RuntimeError):
        assessment = {
            "passed": False,
            "missing": ["验收模型输出无法解析"],
            "fix_hint": "重新生成并确保满足需求。",
        }
    return {"assessment": assessment, "status": "passed" if assessment["passed"] else "retry"}


def fix(state: MainState) -> dict:
    assessment = state.get("assessment", {})
    mission = state.get("mission", {})
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
    return {"status": "passed" if passed else "failed"}


def route_after_generate(state: MainState) -> str:
    return "write" if state.get("files") and not state.get("generation_error") else "fix"


def route_after_fix(state: MainState) -> str:
    if state.get("attempt", 0) >= state.get("max_attempts", 2):
        return "finalize"
    return "generate"


def route_after_assess(state: MainState) -> str:
    return "finalize" if state.get("status") == "passed" else "fix"
