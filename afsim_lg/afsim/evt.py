import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

RECORD_RE = re.compile(r"^(\d+(?::\d+){0,2}(?:\.\d+)?)\s+([A-Z][A-Z0-9_]*)\b\s*(.*)$")
SIDE_RE = re.compile(r"Side:\s*([A-Za-z_]+)")
RESULT_RE = re.compile(r"Result:\s*([A-Za-z_]+)")


@dataclass
class EvtSummary:
    path: str = ""
    last_time_sec: float = 0.0
    counts: Counter = field(default_factory=Counter)
    platform_sides: dict[str, str] = field(default_factory=dict)
    detections: list[dict] = field(default_factory=list)
    fires: list[dict] = field(default_factory=list)
    hits: list[dict] = field(default_factory=list)
    kills: list[dict] = field(default_factory=list)
    parse_errors: int = 0

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "last_time_sec": self.last_time_sec,
            "counts": dict(self.counts),
            "platform_sides": self.platform_sides,
            "detections": self.detections,
            "fires": self.fires,
            "hits": self.hits,
            "kills": self.kills,
            "parse_errors": self.parse_errors,
        }


def time_to_seconds(value: str) -> float:
    if ":" in value:
        parts = [float(p) for p in value.split(":")]
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    return float(value)


def read_records(text: str) -> tuple[list[str], int]:
    records: list[str] = []
    buffer = ""
    parse_errors = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        if not buffer and not line.strip():
            continue
        is_continuation = line.endswith("\\")
        chunk = line[:-1].rstrip() if is_continuation else line.strip()
        buffer = f"{buffer} {chunk}".strip() if buffer else chunk
        if is_continuation:
            continue
        if buffer:
            records.append(buffer)
            buffer = ""
    if buffer:
        records.append(buffer)
    return records, parse_errors


def parse_evt(path: Path) -> EvtSummary:
    summary = EvtSummary(path=str(path))
    text = path.read_text(encoding="utf-8", errors="replace")
    records, errors = read_records(text)
    summary.parse_errors = errors
    seen_detections: set[tuple[str, str]] = set()
    killed: set[str] = set()

    for record in records:
        match = RECORD_RE.match(record)
        if not match:
            summary.parse_errors += 1
            continue
        time_text, event, rest = match.groups()
        timestamp = time_to_seconds(time_text)
        summary.last_time_sec = max(summary.last_time_sec, timestamp)
        summary.counts[event] += 1
        tokens = rest.split()
        if not tokens:
            continue

        if event == "PLATFORM_ADDED":
            side = SIDE_RE.search(rest)
            if side:
                summary.platform_sides[tokens[0]] = side.group(1).lower()
        elif event == "SENSOR_TRACK_INITIATED" and len(tokens) >= 2:
            pair = (tokens[0], tokens[1])
            if pair not in seen_detections:
                seen_detections.add(pair)
                summary.detections.append(
                    {"time": timestamp, "actor": tokens[0], "target": tokens[1]}
                )
        elif event == "WEAPON_FIRED" and len(tokens) >= 2:
            summary.fires.append(
                {
                    "time": timestamp,
                    "actor": tokens[0],
                    "target": tokens[1],
                    "weapon": tokens[2] if len(tokens) > 2 else "",
                }
            )
        elif event == "WEAPON_HIT" and len(tokens) >= 2:
            result = RESULT_RE.search(rest)
            hit = {
                "time": timestamp,
                "actor": tokens[0],
                "target": tokens[1],
                "result": result.group(1) if result else "",
            }
            summary.hits.append(hit)
            if hit["result"].upper() in {"KILLED", "DESTROYED"} and tokens[1] not in killed:
                killed.add(tokens[1])
                summary.kills.append(
                    {"time": timestamp, "victim": tokens[1], "killer": tokens[0]}
                )
        elif event == "PLATFORM_BROKEN" and tokens[0] not in killed:
            killed.add(tokens[0])
            summary.kills.append({"time": timestamp, "victim": tokens[0], "killer": ""})
    return summary


def find_evt(run_dir: Path) -> Path | None:
    candidates = sorted(
        (p for p in (run_dir / "output").glob("*.evt") if p.stat().st_size > 0),
        key=lambda p: p.stat().st_mtime,
    )
    return candidates[-1] if candidates else None
