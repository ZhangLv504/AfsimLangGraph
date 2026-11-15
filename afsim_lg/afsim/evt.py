"""解析 AFSIM 事件输出（.evt）文件。

实测格式要点：
- 时间戳有两种写法：`300.00000`（秒）和 `00:02:10.0`（h:m:s）。
- 长消息用行尾反斜杠续行，按行解析前必须先合并。
- 平台阵营只在 `PLATFORM_ADDED` 事件里出现，按阵营过滤事件依赖它。
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# 行首时间戳（秒或 h:m:s）+ 全大写事件名 + 其余消息
RECORD_RE = re.compile(r"^(\d+(?::\d+){0,2}(?:\.\d+)?)\s+([A-Z][A-Z0-9_]*)\b\s*(.*)$")
SIDE_RE = re.compile(r"Side:\s*([A-Za-z_]+)")
RESULT_RE = re.compile(r"Result:\s*([A-Za-z_]+)")


@dataclass
class EvtSummary:
    """一个 evt 文件的聚合结果。"""

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
        """转换为可 JSON 序列化的字典。"""
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
    """把 `h:m:s` 或纯秒数转换成秒。

    Args:
        value: evt 行首的时间戳文本。

    Returns:
        对应的秒数。
    """
    if ":" in value:
        parts = [float(p) for p in value.split(":")]
        # 从高位向低位折叠：h:m:s → h*3600 + m*60 + s，纯秒数直接返回
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    return float(value)


def read_records(text: str) -> tuple[list[str], int]:
    """把续行合并成完整记录。

    反斜杠结尾表示消息未结束；空行只用于分隔记录，不参与拼接。

    Args:
        text: evt 文件全文。

    Returns:
        记录列表与解析失败计数。
    """
    records: list[str] = []
    buffer = ""
    # 这里只负责合并续行；无法解析的记录由 parse_evt 按正则失败计数
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
    """解析 evt 文件并统计关键事件。

    探测按 (传感器平台, 目标) 去重，毁伤按被毁平台去重，避免同一交战的
    重复上报把计数抬高。

    Args:
        path: evt 文件路径。

    Returns:
        事件计数、平台阵营映射与交战明细。
    """
    summary = EvtSummary(path=str(path))
    text = path.read_text(encoding="utf-8", errors="replace")
    records, errors = read_records(text)
    summary.parse_errors = errors
    # 去重键：探测按 (平台, 目标)，毁伤按被毁平台；同一交战会被反复上报
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
            # 消息形如 "<平台名> ... Side: blue"，阵营只在这里出现
            side = SIDE_RE.search(rest)
            if side:
                summary.platform_sides[tokens[0]] = side.group(1).lower()
        elif event == "SENSOR_TRACK_INITIATED" and len(tokens) >= 2:
            # 前两个 token 依次是传感器平台与目标平台
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
                    # 第三个 token 才是武器名，部分记录可能没有
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
            # 命中不等于毁伤：只有 Result 为 KILLED/DESTROYED 才计入 kills
            if hit["result"].upper() in {"KILLED", "DESTROYED"} and tokens[1] not in killed:
                killed.add(tokens[1])
                summary.kills.append(
                    {"time": timestamp, "victim": tokens[1], "killer": tokens[0]}
                )
        elif event == "PLATFORM_BROKEN" and tokens[0] not in killed:
            killed.add(tokens[0])
            # 平台损坏事件不带攻击者，killer 留空，检查时只按受害者阵营匹配
            summary.kills.append({"time": timestamp, "victim": tokens[0], "killer": ""})
    return summary


def find_evt(run_dir: Path) -> Path | None:
    """在运行目录的 output 下取最新的非空 evt。

    Args:
        run_dir: 运行工程目录。

    Returns:
        evt 路径；没有可用文件时返回 None。
    """
    # 同一 run 目录跨轮次复用，旧 evt 可能残留，按 mtime 取最新
    candidates = sorted(
        (p for p in (run_dir / "output").glob("*.evt") if p.stat().st_size > 0),
        key=lambda p: p.stat().st_mtime,
    )
    return candidates[-1] if candidates else None
