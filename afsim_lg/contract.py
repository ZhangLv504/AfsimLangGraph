import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from afsim_lg.llm import chat

SIDE_MAP = {
    "蓝": "blue",
    "蓝方": "blue",
    "blue": "blue",
    "红": "red",
    "红方": "red",
    "red": "red",
    "绿": "green",
    "绿方": "green",
    "green": "green",
    "白": "white",
    "白方": "white",
    "white": "white",
}

KIND_MAP = {
    "探测": "detect",
    "发现": "detect",
    "detect": "detect",
    "detection": "detect",
    "发射": "fire",
    "开火": "fire",
    "fire": "fire",
    "命中": "hit",
    "击中": "hit",
    "hit": "hit",
    "摧毁": "kill",
    "击毁": "kill",
    "kill": "kill",
    "destroy": "kill",
}

CONTRACT_SYSTEM = """你是 AFSIM 需求分析器。把用户想定拆成 JSON 契约，只输出 JSON，不要解释。

结构：
{
  "summary": "一句话概括",
  "duration_sec": 数字或 null,
  "platforms": [
    {"side": "blue|red|green|white", "domain": "air|ground|surface|subsurface|space|unknown",
     "count": 1, "main_entity": "中文描述", "sensors": ["传感器描述"], "weapons": ["武器描述"]}
  ],
  "expectations": [
    {"kind": "detect|fire|hit|kill", "actor_side": "red|blue|null",
     "target_side": "red|blue|null", "min_count": 1}
  ],
  "notes": ""
}

规则：
- duration_sec 一律换算成秒；需求没写就填 null。
- expectations 只描述需求里明确要求的可观测事件：探测到目标、发射武器、命中、摧毁。
- 不要虚构需求中没有的平台或事件。
- platforms 的 sensors / weapons 只填需求中明确提到的装备；没提到就留空数组，不要按平台类型补默认装备。
- 需求是“最小场景/只飞行”这类没有交战要求时，expectations 必须为空。"""


class PlatformSpec(BaseModel):
    side: str | None = None
    domain: Literal["air", "ground", "surface", "subsurface", "space", "unknown"] = "unknown"
    count: int = Field(default=1, ge=1)
    main_entity: str = ""
    sensors: list[str] = Field(default_factory=list)
    weapons: list[str] = Field(default_factory=list)

    @field_validator("side", mode="before")
    @classmethod
    def normalize_side(cls, value):
        if value is None:
            return None
        return SIDE_MAP.get(str(value).strip().lower(), str(value).strip().lower())


class Expectation(BaseModel):
    kind: Literal["detect", "fire", "hit", "kill"]
    actor_side: str | None = None
    target_side: str | None = None
    min_count: int = Field(default=1, ge=1)

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_kind(cls, value):
        return KIND_MAP.get(str(value).strip().lower(), str(value).strip().lower())

    @field_validator("actor_side", "target_side", mode="before")
    @classmethod
    def normalize_side(cls, value):
        if value is None:
            return None
        return SIDE_MAP.get(str(value).strip().lower(), str(value).strip().lower())


class ScenarioContract(BaseModel):
    summary: str = ""
    duration_sec: float | None = Field(default=None, ge=0)
    platforms: list[PlatformSpec] = Field(default_factory=list)
    expectations: list[Expectation] = Field(default_factory=list)
    notes: str = ""

    @property
    def expected_sides(self) -> set[str]:
        return {p.side for p in self.platforms if p.side}

    @property
    def expected_platform_count(self) -> int:
        return sum(p.count for p in self.platforms)

    @property
    def needs_event_evidence(self) -> bool:
        return bool(self.expectations)


DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(秒|sec|secs|s|分钟|min|mins|分钟|小时|hour|hours|hr|h)(?![a-z])", re.I)
DURATION_FACTOR = {
    "秒": 1, "s": 1, "sec": 1, "secs": 1,
    "分钟": 60, "min": 60, "mins": 60,
    "小时": 3600, "hour": 3600, "hours": 3600, "hr": 3600, "h": 3600,
}


def guess_duration(requirement: str) -> float | None:
    match = DURATION_RE.search(requirement)
    if not match:
        return None
    return float(match.group(1)) * DURATION_FACTOR[match.group(2).lower()]


def fallback_contract(requirement: str, error: str) -> ScenarioContract:
    return ScenarioContract(
        summary=requirement.strip()[:200],
        duration_sec=guess_duration(requirement),
        notes=f"契约解析失败，使用降级契约：{error}",
    )


def parse_contract(requirement: str) -> tuple[ScenarioContract, str]:
    prompt = f"【用户想定】\n{requirement}"
    error = ""
    for _ in range(2):
        try:
            raw = chat(CONTRACT_SYSTEM + (f"\n上一次校验失败：{error}" if error else ""), prompt, json_mode=True)
            return ScenarioContract.model_validate(json.loads(raw)), ""
        except (json.JSONDecodeError, ValueError) as exc:
            error = str(exc)[:500]
    return fallback_contract(requirement, error), error
