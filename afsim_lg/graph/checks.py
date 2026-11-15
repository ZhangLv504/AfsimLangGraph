"""把契约与运行结果对照成可复现的检查项。"""

import re

from afsim_lg.afsim.evt import EvtSummary
from afsim_lg.contract import Expectation, ScenarioContract

# 数值和单位分开捕获，单位换算在 parse_end_time 里按表处理
END_TIME_RE = re.compile(r"(?mi)^\s*end_time\s+([\d.]+)\s*([A-Za-z]+)")
# 只匹配 platform 实例行（platform <名称> <类型>），不匹配 platform_type 定义
PLATFORM_INSTANCE_RE = re.compile(r"(?mi)^\s*platform\s+[A-Za-z_][A-Za-z0-9_]*")
# 只匹配行首 side 声明，不解析类型定义内部的选择器
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

# 只有非 land 域需要显式标记；land 是 AFSIM 默认值（文档：spatial_domain 缺省 land）
DOMAIN_HINTS = {
    "air": ("spatial_domain air", "wsf_air_mover", "wsf_p6dof_mover"),
    "surface": ("spatial_domain surface", "wsf_surface_mover"),
    "subsurface": ("spatial_domain subsurface", "wsf_subsurface_mover"),
    "space": (
        "spatial_domain space",
        "wsf_space_mover",
        "wsf_orbital_mover",
        "wsf_norad_space_mover",
    ),
}
# 中文装备描述 → 工程文本里应出现的关键字；词表外的描述不做判断
SENSOR_HINTS = {
    "雷达": ("radar",),
    "红外": ("infrared", "ir_sensor"),
    "光学": ("optical",),
    "声学": ("acoustic",),
    "传感器": ("sensor",),
}
WEAPON_HINTS = {
    "导弹": ("missile", "aam", "agm", "sam", "ssm", "weapon"),
    "火炮": ("gun", "cannon"),
    "炸弹": ("bomb",),
    "鱼雷": ("torpedo",),
    "武器": ("weapon",),
}
# 只看定义行里的类型名，避免平台/实例名（如 RedRadar）冒充装备
SENSOR_DEF_RE = re.compile(
    r"(?mi)^\s*(?:add\s+|edit\s+)?sensor\s+\S+\s+([A-Za-z_][A-Za-z0-9_]*)"
)
WEAPON_DEF_RE = re.compile(
    r"(?mi)^\s*(?:add\s+|edit\s+)?weapon\s+\S+\s+([A-Za-z_][A-Za-z0-9_]*)"
)

# 验收模型虚构缺失项时用来判断“需求/契约里根本没提”
EVENT_TERMS = {
    "detect": ("探测", "发现", "跟踪"),
    "fire": ("发射", "开火"),
    "hit": ("命中", "击中"),
    "kill": ("摧毁", "击毁", "击杀"),
}
ENTITY_TERMS = (
    "航母", "战斗机", "轰炸机", "运输机", "预警机", "直升机", "无人机",
    "坦克", "装甲车", "军舰", "舰艇", "潜艇", "卫星", "雷达车",
    "雷达", "传感器", "导弹", "火炮", "炸弹", "鱼雷",
)


def _check(name: str, ok: bool, detail: str) -> dict:
    """构造统一结构的检查项，便于汇总和留痕。"""
    return {"name": name, "ok": ok, "detail": detail}


def parse_end_time(files_text: str) -> tuple[float, str] | None:
    """从工程文本中读取 end_time 并换算成秒。

    Args:
        files_text: 生成文件的全部文本拼接。

    Returns:
        秒数与原始行；没有 end_time 时返回 None。
    """
    for match in END_TIME_RE.finditer(files_text):
        factor = TIME_FACTORS.get(match.group(2).lower())
        if factor:
            # 取第一处可识别单位的 end_time；多个文件里出现多次时以先出现的为准
            return float(match.group(1)) * factor, match.group(0).strip()
    return None


def _side_of(evt: EvtSummary, name: str) -> str:
    return evt.platform_sides.get(name, "").lower()


def _match_side(evt: EvtSummary, name: str, expected: str | None) -> bool:
    # 严格比较阵营：平台名只做精确匹配，未知阵营不会被视为“任意方”
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
        # PLATFORM_BROKEN 没有攻击者；killer 为空时只按受害者阵营匹配
        return sum(
            1
            for r in evt.kills
            if _match_side(evt, r["victim"], expectation.target_side)
            and (not r["killer"] or _match_side(evt, r["killer"], expectation.actor_side))
        )
    return 0


def _contains_marker(text: str, marker: str) -> bool:
    """按标识符边界匹配关键字，避免 RedRadar 里的 radar 冒充雷达传感器。"""
    pattern = rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _required_equipment(
    descriptions: list[str], hints: dict[str, tuple]
) -> dict[str, tuple]:
    """从中文装备描述中找出需要检查的关键词组。"""
    required: dict[str, tuple] = {}
    for description in descriptions:
        for term, markers in hints.items():
            if term in description:
                required[term] = markers
    return required


def filter_judge_missing(
    missing: list[str], requirement: str, contract: ScenarioContract
) -> tuple[list[str], list[str]]:
    """丢弃验收模型虚构的平台/装备/事件缺失项。

    只处理词表内能识别的对象；词表外或需求原文能对应的缺失项保留。
    返回（保留项，被丢弃项），后者写入留痕便于回溯。
    """
    parts = [requirement, contract.summary, contract.notes]
    for platform in contract.platforms:
        parts.extend([platform.main_entity, *platform.sensors, *platform.weapons])
    allowed_text = " ".join(parts)
    allowed_events = {expectation.kind for expectation in contract.expectations}

    kept: list[str] = []
    dropped: list[str] = []
    for item in missing:
        fabricated = False
        for kind, terms in EVENT_TERMS.items():
            if kind in allowed_events:
                continue
            if any(term in item and term not in requirement for term in terms):
                fabricated = True
                break
        # 契约没有事件要求时，“缺少交战”这类泛化结论也不应采纳
        if not fabricated and not allowed_events and "交战" in item and "交战" not in requirement:
            fabricated = True
        if not fabricated:
            fabricated = any(
                term in item and term not in allowed_text for term in ENTITY_TERMS
            )
        (dropped if fabricated else kept).append(item)
    return kept, dropped


def evaluate(
    contract: ScenarioContract,
    files_text: str,
    evt: EvtSummary | None,
) -> tuple[list[dict], dict]:
    """按契约生成确定性检查项并汇总运行证据。

    Args:
        contract: 需求契约。
        files_text: 生成文件全部文本，用于读 end_time 和声明。
        evt: 解析后的事件摘要；没有 evt 时传 None。

    Returns:
        检查项列表与精简后的证据字典。
    """
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
                    # 留 0.5 秒容差，吸收浮点输出与单位换算误差
                    actual + 0.5 >= contract.duration_sec,
                    f"要求 {contract.duration_sec:g}s，实际 {actual:g}s（{line}）",
                )
            )

    expected_sides = contract.expected_sides
    if expected_sides:
        # 阵营从工程文本检查而不是 evt：没有交战事件时 evt 里也可能没有平台记录
        declared = {m.group(1).lower() for m in SIDE_RE.finditer(files_text)}
        missing = sorted(expected_sides - declared)
        checks.append(
            _check("阵营覆盖", not missing, "缺少阵营：" + ", ".join(missing) if missing else "阵营齐全")
        )

    if contract.expected_platform_count:
        # platform_type 定义不算实例，这里只数 platform <名称> <类型> 行
        actual = len(PLATFORM_INSTANCE_RE.findall(files_text))
        checks.append(
            _check(
                "平台数量",
                actual >= contract.expected_platform_count,
                f"要求至少 {contract.expected_platform_count} 个 platform 实例，实际 {actual}",
            )
        )

    normalized = re.sub(r"\s+", " ", files_text.lower())
    required_domains = {
        platform.domain for platform in contract.platforms if platform.domain in DOMAIN_HINTS
    }
    if required_domains:
        missing_domains = sorted(
            domain
            for domain in required_domains
            if not any(_contains_marker(normalized, hint) for hint in DOMAIN_HINTS[domain])
        )
        checks.append(
            _check(
                "平台域",
                not missing_domains,
                "缺少平台域标记：" + ", ".join(missing_domains)
                if missing_domains
                else "平台域标记齐全",
            )
        )

    sensor_types = " ".join(SENSOR_DEF_RE.findall(files_text)).lower()
    weapon_types = " ".join(WEAPON_DEF_RE.findall(files_text)).lower()
    for label, hints, descriptions, types_text, any_type in (
        (
            "传感器",
            SENSOR_HINTS,
            [s for p in contract.platforms for s in p.sensors],
            sensor_types,
            sensor_types,
        ),
        (
            "武器",
            WEAPON_HINTS,
            [w for p in contract.platforms for w in p.weapons],
            weapon_types,
            weapon_types,
        ),
    ):
        required = _required_equipment(descriptions, hints)
        if not required:
            continue
        missing_terms = []
        for term, markers in required.items():
            # “传感器/武器”这种泛化描述，只要存在同类定义就算满足
            if term in {"传感器", "武器"} and any_type:
                continue
            if not any(_contains_marker(types_text, marker) for marker in markers):
                missing_terms.append(term)
        checks.append(
            _check(
                f"装备:{label}",
                not missing_terms,
                "缺少：" + "、".join(missing_terms)
                if missing_terms
                else "已包含 " + "、".join(required),
            )
        )

    if contract.expectations:
        if evt is None:
            # 契约有事件要求却没有任何 evt，先记一条总失败，再逐项记缺失
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

    # 只保留验收需要的字段与五类关键事件计数，避免把整份 evt 明细写进留痕
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
