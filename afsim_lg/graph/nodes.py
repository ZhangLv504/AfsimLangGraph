"""LangGraph 节点：需求解析、生成、落盘、校验、验收、修复与收尾。"""

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
# 下列上限用于控制单次模型的上下文：文件数、每个检索片段、验收时读取的 main.txt
MAX_FILES = 12
CORPUS_CHARS = 1400
MAIN_CHARS = 4000

# 注入的事件块包含 PLATFORM_ADDED：没有它，evt 里就没有平台阵营，无法按阵营验收
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
    """当前运行的全部产物目录。"""
    return get_settings().runs_dir / state["run_id"]


def _attempt_no(state: MainState) -> int:
    """当前是第几轮生成（从 1 开始）。"""
    return state.get("attempt", 0) + 1


def _attempt_dir(state: MainState) -> Path:
    """当前轮次的留痕目录。"""
    return _run_dir(state) / "attempts" / f"attempt-{_attempt_no(state):02d}"


def _write_json(path: Path, payload) -> None:
    """以 UTF-8、不转义中文的方式写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )


def _corpus_block(hits: list[dict]) -> str:
    """把检索命中拼成给模型的参考片段。"""
    parts = []
    for i, hit in enumerate(hits, 1):
        meta = hit["metadata"]
        parts.append(
            f"[{i}] {meta['skill']}/{meta['path']} (kind={meta['kind']})\n"
            f"{hit['document'][:CORPUS_CHARS]}"
        )
    return "\n\n".join(parts)


def _clean(content: str) -> str:
    """去掉模型额外包上的 Markdown 代码围栏。"""
    lines = content.strip().splitlines()
    if len(lines) >= 2 and FENCE_RE.match(lines[0].strip()) and FENCE_RE.match(lines[-1].strip()):
        lines = lines[1:-1]
    return "\n".join(lines).strip()


def parse_files(text: str) -> dict[str, str]:
    """按 `===FILE: 名称===` 标记拆分模型输出。

    按行解析而不是整段正则：模型经常在末尾多写 `===END===` 或代码围栏，
    只靠非贪婪正则会把标记吞进最后一个文件。

    Args:
        text: 模型输出全文。

    Returns:
        文件名到内容的映射；空文件被丢弃。
    """
    files: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush():
        """把当前累积的文件内容提交到结果，并清空缓冲区。"""
        if current and buffer:
            body = _clean("\n".join(buffer))
            if body:
                files[current] = body + "\n"

    for raw in text.splitlines():
        line = raw.strip()
        mark = FILE_MARK_RE.match(line)
        if mark:
            flush()
            # 模型偶尔把文件名包在反引号里或写成 Windows 路径
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
    """把模型给出的文件名限制在运行目录内，只接受 .txt。

    模型可能输出绝对路径或目录穿越，这里返回 None 而不是抛异常，让单个
    非法文件被跳过而不中断整轮生成。
    """
    if name.startswith("/") or ":" in name or ".." in Path(name).parts:
        return None
    if Path(name).suffix.lower() != ".txt":
        return None
    target = (run_dir / name).resolve()
    if run_dir.resolve() not in target.parents:
        return None
    return target


def _ensure_event_output(files: dict[str, str], contract: ScenarioContract) -> tuple[dict, bool]:
    """契约要求事件但工程没写 event_output 时补一段标准配置。

    只在完全缺失时注入；模型已写 event_output 就不改，避免覆盖它自定义的
    事件选择。

    Args:
        files: 生成文件映射。
        contract: 当前需求契约。

    Returns:
        处理后的文件映射与是否发生注入。
    """
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
    """把自然语言需求解析为契约并落盘。"""
    run_dir = _run_dir(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    contract, error = parse_contract(state["requirement"])
    _write_json(run_dir / "contract.json", contract.model_dump())
    if error:
        (run_dir / "contract_error.txt").write_text(error, encoding="utf-8")
    return {"contract": contract.model_dump(), "contract_error": error}


def retrieve(state: MainState) -> dict:
    """按需求检索技能语料 Top-8。"""
    hits = search(state["requirement"], k=8)
    return {"retrieved": hits}


def generate(state: MainState) -> dict:
    """调用生成模型，要求按文件标记输出完整工程。

    第一次解析不到文件时追加一条格式提醒再试一次；仍失败则把错误交给
    修复分支，而不是让整次运行异常退出。
    """
    user_parts = [f"【需求】\n{state['requirement']}"]
    contract = state.get("contract")
    if contract:
        # 契约约束平台、时长与事件范围，减少模型按常识补装备
        user_parts.append(
            "【需求契约】\n" + json.dumps(contract, ensure_ascii=False, indent=1)
        )
    if state.get("feedback"):
        # 反馈来自上一轮的 mission 报错与验收缺口，定向修比整份重写省上下文
        user_parts.append(f"【上一轮失败反馈】\n{state['feedback']}")
    user_parts.append(f"【技能检索片段】\n{_corpus_block(state.get('retrieved', []))}")
    user_parts.append("请输出完整工程文件。")
    prompt = "\n\n".join(user_parts)

    files = parse_files(chat(GENERATOR_SYSTEM, prompt))
    if not files:
        # 7B 模型偶尔漏掉文件标记；先补一次格式要求，不占用修复轮次
        retry_prompt = prompt + "\n\n注意：你的输出没有可解析的文件标记。只输出文件标记格式。"
        files = parse_files(chat(GENERATOR_SYSTEM, retry_prompt))
    error = "" if files else "模型输出中没有可解析的 ===FILE: ... === 文件块"
    return {
        "files": files,
        "generation_error": error,
        # 生成次数单独计数；用 attempt + 1 推算会把修复轮次也算成一次生成
        "generation": state.get("generation", 0) + 1,
    }


def write_files(state: MainState) -> dict:
    """把生成文件写入运行目录，并复制一份到本轮的留痕目录。"""
    run_dir = _run_dir(state)
    run_dir.mkdir(parents=True, exist_ok=True)
    # event_output 默认写 output/，目录不存在时 AFSIM 会直接报错
    (run_dir / "output").mkdir(exist_ok=True)
    contract = ScenarioContract.model_validate(state["contract"]) if state.get("contract") else ScenarioContract()
    files, _ = _ensure_event_output(state.get("files", {}), contract)

    written = 0
    # 超出上限的文件静默跳过，避免模型吐出大量杂项文件占满工作区
    for name, content in list(files.items())[:MAX_FILES]:
        target = _safe_path(run_dir, name)
        if target is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        written += 1
    if not written:
        return {"generation_error": "没有可写入的文件"}

    # 先留证据再进入校验：下一轮会覆盖运行目录里的同名文件
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
    """在运行目录执行 mission.exe。

    完整输出只落盘，状态里仅保留摘要，避免 checkpoint 体积随日志增长。
    """
    run_dir = _run_dir(state)
    result = run_mission(run_dir)
    # 运行目录只保留最新一轮输出，attempt 目录保留历史，内容相同
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
    """让验收模型只处理确定性检查覆盖不到的需求。"""
    run_dir = _run_dir(state)
    main_txt = ""
    main_path = run_dir / "main.txt"
    if main_path.is_file():
        # 验收主要看契约、检查项与事件证据，main.txt 只取开头，避免上下文超限
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
    """先做确定性检查，再让模型补充判断，并保存本轮验收证据。"""
    mission = state.get("mission", {})
    attempt_dir = Path(state["attempt_dir"]) if state.get("attempt_dir") else _attempt_dir(state)

    if not mission.get("ok"):
        # 编译/运行失败时 evt 不可信，不跑确定性检查，直接把报错交给修复
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
        # 验收模型不可用按“未通过”处理，让流程走修复而不是抛异常中断
        judge_passed = False
        missing = ["验收模型输出无法解析"]
        fix_hint = "重新生成并确保满足需求。"

    failed = [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]
    # 确定性检查与模型判断是“与”关系：任何一项失败都不算通过
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
    """汇总本轮错误与验收缺口，作为下一轮生成的反馈。"""
    assessment = state.get("assessment", {})
    mission = state.get("mission", {})
    failed_dir = _attempt_dir(state)
    # 生成阶段就失败的轮次没有 summary，这里补一条；已存在的不要覆盖
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
    """汇总各轮留痕，写出 manifest 并给出最终状态。"""
    passed = state.get("assessment", {}).get("passed", False)
    status = "passed" if passed else "failed"
    run_dir = _run_dir(state)
    attempts = []
    attempts_dir = run_dir / "attempts"
    if attempts_dir.is_dir():
        # attempt-NN 的字典序就是轮次顺序；损坏的 summary 跳过，不阻断收尾
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
    """有可写入文件则继续落盘，否则进入修复分支。"""
    # 解析失败时 files 为空且 generation_error 非空，走 fix 汇总错误
    return "write" if state.get("files") and not state.get("generation_error") else "fix"


def route_after_fix(state: MainState) -> str:
    """未达生成上限时重试，否则收尾。"""
    if state.get("attempt", 0) >= state.get("max_attempts", 2):
        return "finalize"
    return "generate"


def route_after_assess(state: MainState) -> str:
    """验收通过直接收尾，否则回到修复。"""
    return "finalize" if state.get("status") == "passed" else "fix"
