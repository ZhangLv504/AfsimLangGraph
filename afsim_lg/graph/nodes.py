"""LangGraph 节点：需求解析、生成、落盘、校验、验收、修复与收尾。"""

import json
import re
from datetime import datetime
from pathlib import Path

from afsim_lg.afsim.evt import find_evt, parse_evt
from afsim_lg.afsim.runner import run_mission
from afsim_lg.afsim.statics import check_platform_types
from afsim_lg.config import get_settings
from afsim_lg.contract import ScenarioContract, parse_contract
from afsim_lg.graph.checks import evaluate
from afsim_lg.graph.state import MainState
from afsim_lg.llm import chat
from afsim_lg.rag.errors import (
    extract_error_keywords,
    search_error_refs,
    search_error_skill,
)
from afsim_lg.rag.index import search

FILE_MARK_RE = re.compile(r"^===FILE:\s*(.+?)\s*===$")
END_MARK_RE = re.compile(r"^===END===$")
FENCE_RE = re.compile(r"^(```|~~~)")
# 下列上限用于控制单次模型的上下文：文件数、每个检索片段、验收时读取的 main.txt
MAX_FILES = 12
CORPUS_CHARS = 1400
MAIN_CHARS = 4000
# Undefined X 只报名字，不报位置；单独转成“补定义块”的指令，7B 才会照做
UNDEFINED_ERROR_RE = re.compile(r"Undefined\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
# 定向修复时每个文件的展示上限：只看错误附近，避免整份工程撑爆上下文
REPAIR_FILE_CHARS = 3000
# 修复轮还要塞工程正文，重检索片段只取前几条、每条截得更短
REPAIR_CORPUS_HITS = 4
REPAIR_CORPUS_CHARS = 700

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


REPAIR_SYSTEM = """你是 AFSIM 2.9.0 场景修复器。根据错误反馈与当前工程，只输出需要修改的文件。

输出格式必须严格如下，不要使用 Markdown 代码围栏：
===FILE: main.txt===
<修改后的完整文件内容>
===END===

规则：
1. 只输出必须改动的文件；没有输出的文件，系统会保留原样。
2. 每个输出文件必须给出完整内容，不能只给片段或 diff。
3. 保留当前工程中已经正确的部分，把改动限制在错误相关的最小范围。
4. platform 引用的 platform_type，以及 platform_type 里的 sensor/weapon/mover/radar_signature 等引用，必须在文件集中有定义；报 Undefined X 时补 X 的定义块，不要只改无关参数。
5. 注释必须独立成行；不要使用 thread_count。
6. 错误信息不足以判断时，宁可原样输出当前文件，也不要新增未定义的类型或命令。"""


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


def _corpus_block(
    hits: list[dict], chars: int = CORPUS_CHARS, limit: int | None = None
) -> str:
    """把检索命中拼成给模型的参考片段。"""
    parts = []
    for i, hit in enumerate(hits[:limit], 1):
        meta = hit["metadata"]
        parts.append(
            f"[{i}] {meta['skill']}/{meta['path']} (kind={meta['kind']})\n"
            f"{hit['document'][:chars]}"
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


def _common_parts(state: MainState) -> list[str]:
    """组装需求、契约、反馈与报错参考，供整份生成和定向修复共用。"""
    parts = [f"【需求】\n{state['requirement']}"]
    contract = state.get("contract")
    if contract:
        # 契约约束平台、时长与事件范围，减少模型按常识补装备
        parts.append(
            "【需求契约】\n" + json.dumps(contract, ensure_ascii=False, indent=1)
        )
    if state.get("feedback"):
        # 反馈来自上一轮的 mission 报错与验收缺口，定向修比整份重写省上下文
        parts.append(f"【上一轮失败反馈】\n{state['feedback']}")
    if state.get("error_refs"):
        parts.append(f"【报错参考片段】\n{_corpus_block(state['error_refs'])}")
    return parts


def _full_generate(state: MainState) -> tuple[dict[str, str], str]:
    """整份重生成，返回文件映射与错误信息。"""
    prompt = "\n\n".join(
        _common_parts(state)
        + [
            f"【技能检索片段】\n{_corpus_block(state.get('retrieved', []))}",
            "请输出完整工程文件。",
        ]
    )
    files = parse_files(chat(GENERATOR_SYSTEM, prompt))
    if not files:
        # 7B 模型偶尔漏掉文件标记；先补一次格式要求，不占用修复轮次
        retry_prompt = prompt + "\n\n注意：你的输出没有可解析的文件标记。只输出文件标记格式。"
        files = parse_files(chat(GENERATOR_SYSTEM, retry_prompt))
    error = "" if files else "模型输出中没有可解析的 ===FILE: ... === 文件块"
    return files, error


def _repair_files(state: MainState) -> dict[str, str]:
    """让模型只输出需要修改的文件；解析不到时返回空字典。"""
    blocks = [
        f"===FILE: {name}===\n{content[:REPAIR_FILE_CHARS]}"
        for name, content in state.get("files", {}).items()
    ]
    parts = _common_parts(state) + ["【当前工程】\n" + "\n\n".join(blocks)]
    if state.get("retrieved"):
        # 修复轮优先看按错误重检索到的语法/模板，只取最相关的几条
        parts.append(
            "【报错重检索片段】\n"
            + _corpus_block(
                state["retrieved"],
                chars=REPAIR_CORPUS_CHARS,
                limit=REPAIR_CORPUS_HITS,
            )
        )
    parts.append("只输出需要修改的文件。")
    return parse_files(chat(REPAIR_SYSTEM, "\n\n".join(parts)))


def generate(state: MainState) -> dict:
    """生成工程；有上一轮文件时优先定向修复，否则整份生成。

    修复输出无法解析时退回整份生成；仍失败则把错误交给修复分支，而不是
    让整次运行异常退出。
    """
    files: dict[str, str] = {}
    mode = "full"
    if state.get("files") and state.get("feedback"):
        changed = _repair_files(state)
        if changed:
            # 只覆盖模型返回的文件，未返回的沿用上一轮
            files = dict(state["files"])
            files.update(changed)
            mode = "repair"
    if files:
        error = ""
    else:
        files, error = _full_generate(state)
    return {
        "files": files,
        "generation_error": error,
        # 生成次数单独计数；用 attempt + 1 推算会把修复轮次也算成一次生成
        "generation": state.get("generation", 0) + 1,
        "generation_mode": mode,
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
                "origin": hit.get("origin", "requirement"),
                "document": hit["document"],
            }
            for hit in state.get("retrieved", [])
        ],
    )
    return {"files": files, "attempt_dir": str(attempt_dir), "generation_error": ""}


def validate(state: MainState) -> dict:
    """先做静态引用检查，通过后再在运行目录执行 mission.exe。

    静态检查失败时跳过 mission.exe：省一次完整运行，错误文案也足够定位。
    完整输出只落盘，状态里仅保留摘要，避免 checkpoint 体积随日志增长。
    """
    run_dir = _run_dir(state)
    attempt_dir = Path(state["attempt_dir"]) if state.get("attempt_dir") else _attempt_dir(state)
    attempt_dir.mkdir(parents=True, exist_ok=True)

    static_errors = check_platform_types(state.get("files", {}))
    _write_json(attempt_dir / "static.json", {"ok": not static_errors, "errors": static_errors})
    if static_errors:
        output = "静态检查失败，未运行 mission.exe：\n" + "\n".join(static_errors)
        # 运行目录只保留最新一轮输出，attempt 目录保留历史，内容相同
        (run_dir / "mission_stdout.log").write_text(output, encoding="utf-8")
        (attempt_dir / "mission_stdout.log").write_text(output, encoding="utf-8")
        mission = {
            "ok": False,
            # 负数表示引擎没有真正启动，与超时的 -1 区分
            "exit_code": -2,
            "elapsed_sec": 0.0,
            "timed_out": False,
            "errors": static_errors,
            "static_failed": True,
        }
        _write_json(attempt_dir / "mission.json", mission)
        return {"mission": mission}

    result = run_mission(run_dir)
    # 运行目录只保留最新一轮输出，attempt 目录保留历史，内容相同
    (run_dir / "mission_stdout.log").write_text(result.output, encoding="utf-8")
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
            "fix_hint": "先修复静态检查错误。" if mission.get("static_failed") else "先修复 mission.exe 报错。",
        }
        _write_json(attempt_dir / "assessment.json", assessment)
        _write_json(
            attempt_dir / "summary.json",
            {
                "attempt": _attempt_no(state),
                "generation_mode": state.get("generation_mode", ""),
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
            "generation_mode": state.get("generation_mode", ""),
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


def _undefined_hint(errors: list[str]) -> str:
    """把 Undefined 报错转成“补定义块”的明确指令。"""
    names = sorted(
        {
            match.group(1)
            for error in errors
            if (match := UNDEFINED_ERROR_RE.search(error))
        }
    )
    if not names:
        return ""
    return (
        "缺少定义：" + "、".join(names) + "。引用处保留，补上对应定义块；例如签名：\n"
        "radar_signature <名字> WSF_RADAR_SIGNATURE\n"
        "   constant 10 m^2\n"
        "end_radar_signature"
    )


def _merge_retrieved(
    error_hits: list[dict], old_hits: list[dict], limit: int = 8
) -> list[dict]:
    """错误检索结果优先，再补首轮需求检索；按技能/路径/块去重。"""
    merged: list[dict] = []
    seen: set[tuple] = set()
    for hit in error_hits + old_hits:
        meta = hit["metadata"]
        key = (meta["skill"], meta["path"], meta.get("chunk_index"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(hit)
        if len(merged) >= limit:
            break
    return merged


def fix(state: MainState) -> dict:
    """汇总本轮错误与验收缺口，检索对应教训，作为下一轮生成的反馈。"""
    assessment = state.get("assessment", {})
    mission = state.get("mission", {})
    failed_dir = _attempt_dir(state)
    # 生成阶段就失败的轮次没有 summary，这里补一条；已存在的不要覆盖
    if not (failed_dir / "summary.json").exists():
        _write_json(
            failed_dir / "summary.json",
            {
                "attempt": _attempt_no(state),
                "generation_mode": state.get("generation_mode", ""),
                "generation_error": state.get("generation_error", ""),
                "files": [],
                "assessment": assessment,
            },
        )
    parts = []
    if mission.get("errors"):
        # 静态检查失败时引擎没有启动，标注清楚避免模型当成运行期错误去猜
        label = "静态检查错误" if mission.get("static_failed") else "mission.exe 报错"
        parts.append(f"{label}：\n" + "\n".join(mission["errors"]))
    undefined_hint = _undefined_hint(mission.get("errors", []))
    if undefined_hint:
        parts.append(undefined_hint)
    if assessment.get("missing"):
        parts.append("需求缺失：\n" + "\n".join(assessment["missing"]))
    if assessment.get("fix_hint"):
        parts.append("修改建议：\n" + assessment["fix_hint"])
    feedback = "\n\n".join(parts)

    keywords = extract_error_keywords(mission.get("errors", []))
    refs = search_error_refs(keywords)
    # 首轮只按需求检索；修复轮再按报错关键词查语法/模板，结果排在前面
    error_hits = [
        {**hit, "origin": "error"}
        for hit in search_error_skill(state.get("requirement", ""), keywords)
    ]
    retrieved = state.get("retrieved", [])
    if error_hits:
        old_hits = [
            {**hit, "origin": hit.get("origin", "requirement")} for hit in retrieved
        ]
        retrieved = _merge_retrieved(error_hits, old_hits)
    _write_json(
        failed_dir / "fix.json",
        {
            "feedback": feedback,
            "keywords": keywords,
            "hits": [
                {"keyword": hit["keyword"], **hit["metadata"], "document": hit["document"]}
                for hit in refs
            ],
            "retrieved": [
                {
                    "skill": hit["metadata"]["skill"],
                    "path": hit["metadata"]["path"],
                    "kind": hit["metadata"]["kind"],
                    "chunk_index": hit["metadata"].get("chunk_index", 0),
                    "origin": hit.get("origin", ""),
                    "distance": hit.get("distance"),
                }
                for hit in retrieved
            ],
        },
    )
    return {
        "attempt": state.get("attempt", 0) + 1,
        "feedback": feedback,
        "error_keywords": keywords,
        "error_refs": refs,
        "retrieved": retrieved,
    }


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
