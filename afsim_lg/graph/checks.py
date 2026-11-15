import re

from afsim_lg.afsim.evt import EvtSummary
from afsim_lg.contract import Expectation, ScenarioContract

END_TIME_RE = re.compile(r"(?mi)^\s*end_time\s+([\d.]+)\s*([A-Za-z]+)")
PLATFORM_INSTANCE_RE = re.compile(r"(?mi)^\s*platform\s+[A-Za-z_][A-Za-z0-9_]*")
SIDE_RE = re.compile(r"(?mi)^\s*side\s+([A-Za-z_]+)")

TIME_FACTORS = {
    "sec": 1, "secs": 1, "s": 1, "second": 1, "seconds": 1,
    "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "h": 3600,
}
EVENT_KEYS = [
    "PLATFORM_ADDED",
    "SENSOR_TRACK_INITIATED",
    "WEAPON_FIRED",
    "WEAPON_HIT",
    "PLATFORM_BROKEN",
]


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def parse_end_time(files_text: str) -> tuple[float, str] | None:
    for match in END_TIME_RE.finditer(files_text):
        factor = TIME_FACTORS.get(match.group(2).lower())
        if factor:
            return float(match.group(1)) * factor, match.group(0).strip()
    return None


def _side_of(evt: EvtSummary, name: str) -> str:
    return evt.platform_sides.get(name, "").lower()


def _match_side(evt: EvtSummary, name: str, expected: str | None) -> bool:
    return expected is None or _side_of(evt, name) == expected.lower()


def _expectation_count(expectation: Expectation, evt: EvtSummary) -> int:
    kind = expectation.kind
    if kind == "detect":
        rows = evt.detections
        return sum(
            1
            for r in rows
            if _match_side(evt, r["actor"], expectation.actor_side)
            and _match_side(evt, r["target"], expectation.target_side)
        )
    if kind in {"fire", "hit"}:
        rows = evt.fires if kind == "fire" else evt.hits
        return sum(
            1
            for r in rows
            if _match_side(evt, r["actor"], expectation.actor_side)
            and _match_side(evt, r["target"], expectation.target_side)
        )
    if kind == "kill":
        return sum(
            1
            for r in evt.kills
            if _match_side(evt, r["victim"], expectation.target_side)
            and (not r["killer"] or _match_side(evt, r["killer"], expectation.actor_side))
        )
    return 0


def evaluate(
    contract: ScenarioContract,
    files_text: str,
    evt: EvtSummary | None,
) -> tuple[list[dict], dict]:
    checks: list[dict] = []

    if contract.duration_sec is not None:
        parsed = parse_end_time(files_text)
        if parsed is None:
            checks.append(_check("仿真时长", False, "工程中没有找到 end_time"))
        else:
            actual, line = parsed
            checks.append(
                _check(
                    "仿真时长",
                    actual + 0.5 >= contract.duration_sec,
                    f"要求 {contract.duration_sec:g}s，实际 {actual:g}s（{line}）",
                )
            )

    expected_sides = contract.expected_sides
    if expected_sides:
        declared = {m.group(1).lower() for m in SIDE_RE.finditer(files_text)}
        missing = sorted(expected_sides - declared)
        checks.append(
            _check("阵营覆盖", not missing, "缺少阵营：" + ", ".join(missing) if missing else "阵营齐全")
        )

    if contract.expected_platform_count:
        actual = len(PLATFORM_INSTANCE_RE.findall(files_text))
        checks.append(
            _check(
                "平台数量",
                actual >= contract.expected_platform_count,
                f"要求至少 {contract.expected_platform_count} 个 platform 实例，实际 {actual}",
            )
        )

    if contract.expectations:
        if evt is None:
            checks.append(_check("事件证据", False, "契约要求交战事件，但没有生成 output/*.evt 或文件为空"))
        for expectation in contract.expectations:
            if evt is None:
                checks.append(
                    _check(
                        f"事件:{expectation.kind}",
                        False,
                        f"缺少事件证据（要求至少 {expectation.min_count} 次）",
                    )
                )
                continue
            count = _expectation_count(expectation, evt)
            target = expectation.target_side or "任意"
            actor = expectation.actor_side or "任意"
            checks.append(
                _check(
                    f"事件:{expectation.kind}",
                    count >= expectation.min_count,
                    f"{actor}→{target} 要求≥{expectation.min_count}，evt 实际 {count}",
                )
            )

    evidence = {
        "evt_present": evt is not None,
        "evt_path": evt.path if evt else "",
        "last_time_sec": evt.last_time_sec if evt else 0.0,
        "counts": {k: evt.counts.get(k, 0) for k in EVENT_KEYS} if evt else {},
        "detections": len(evt.detections) if evt else 0,
        "fires": len(evt.fires) if evt else 0,
        "hits": len(evt.hits) if evt else 0,
        "kills": len(evt.kills) if evt else 0,
    }
    return checks, evidence
