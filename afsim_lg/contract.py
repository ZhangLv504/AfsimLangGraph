"""需求契约：把自然语言想定解析成可校验的结构。

契约同时约束生成和验收：生成器按契约写工程，验收费按契约逐项检查。
"""

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from afsim_lg.llm import chat

# 模型输出中文、英文、大小写混用都有可能，统一在契约入口归一化，后续检查只认英文值
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

# 事件类型同样做中文/英文归一化，后续只按 detect/fire/hit/kill 统计
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
    """需求中一方平台的描述。"""

    side: str | None = None
    domain: Literal["air", "ground", "surface", "subsurface", "space", "unknown"] = "unknown"
    count: int = Field(default=1, ge=1)
    main_entity: str = ""
    sensors: list[str] = Field(default_factory=list)
    weapons: list[str] = Field(default_factory=list)

    @field_validator("side", mode="before")
    @classmethod
    def normalize_side(cls, value):
        """把中文或大小写混用的阵营归一化为英文枚举值。"""
        if value is None:
            return None
        return SIDE_MAP.get(str(value).strip().lower(), str(value).strip().lower())


class Expectation(BaseModel):
    """需求中明确要求的可观测事件。"""

    kind: Literal["detect", "fire", "hit", "kill"]
    actor_side: str | None = None
    target_side: str | None = None
    min_count: int = Field(default=1, ge=1)

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_kind(cls, value):
        """把“探测/发射/命中/摧毁”等写法归一化为事件枚举。"""
        return KIND_MAP.get(str(value).strip().lower(), str(value).strip().lower())

    @field_validator("actor_side", "target_side", mode="before")
    @classmethod
    def normalize_side(cls, value):
        """把事件参与方的中文阵营归一化为英文枚举值。"""
        if value is None:
            return None
        return SIDE_MAP.get(str(value).strip().lower(), str(value).strip().lower())


class ScenarioContract(BaseModel):
    """一次需求对应的完整验收契约。"""

    summary: str = ""
    duration_sec: float | None = Field(default=None, ge=0)
    platforms: list[PlatformSpec] = Field(default_factory=list)
    expectations: list[Expectation] = Field(default_factory=list)
    notes: str = ""

    @property
    def expected_sides(self) -> set[str]:
        """契约要求出现的所有阵营。"""
        return {p.side for p in self.platforms if p.side}

    @property
    def expected_platform_count(self) -> int:
        """契约要求的最少平台实例数。"""
        return sum(p.count for p in self.platforms)

    @property
    def needs_event_evidence(self) -> bool:
        """是否需要读取 evt 验证交战事件。"""
        return bool(self.expectations)


# 单位后禁止再跟字母，避免从 "minimum" 里截出 min、从 "hours" 里截出 h
DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(秒|sec|secs|s|分钟|min|mins|分钟|小时|hour|hours|hr|h)(?![a-z])", re.I)
DURATION_FACTOR = {
    "秒": 1, "s": 1, "sec": 1, "secs": 1,
    "分钟": 60, "min": 60, "mins": 60,
    "小时": 3600, "hour": 3600, "hours": 3600, "hr": 3600, "h": 3600,
}


def guess_duration(requirement: str) -> float | None:
    """从需求文本中猜测仿真时长，作为契约解析失败时的兜底。

    Args:
        requirement: 用户原始需求。

    Returns:
        换算成秒的时长；没有可识别的时长时返回 None。
    """
    match = DURATION_RE.search(requirement)
    if not match:
        return None
    # 只取第一处时长：想定通常只写一个总时长，多处时以先出现的为准
    return float(match.group(1)) * DURATION_FACTOR[match.group(2).lower()]


def fallback_contract(requirement: str, error: str) -> ScenarioContract:
    """构造降级契约：只保留可正则提取的时长。

    Args:
        requirement: 用户原始需求。
        error: 触发降级的原因，写入 notes 便于回溯。

    Returns:
        平台与事件为空的契约。
    """
    return ScenarioContract(
        summary=requirement.strip()[:200],
        duration_sec=guess_duration(requirement),
        # 平台与事件留空：降级契约只保证时长，不能把猜出来的需求当成必须满足
        notes=f"契约解析失败，使用降级契约：{error}",
    )


def parse_contract(requirement: str) -> tuple[ScenarioContract, str]:
    """调用模型把需求解析成契约，失败时重试一次再降级。

    Args:
        requirement: 用户原始需求。

    Returns:
        契约对象与错误信息；成功时错误为空字符串。
    """
    prompt = f"【用户想定】\n{requirement}"
    error = ""
    for _ in range(2):
        try:
            # 第二次把校验错误回传给模型，比原样重问更容易一次改对
            raw = chat(CONTRACT_SYSTEM + (f"\n上一次校验失败：{error}" if error else ""), prompt, json_mode=True)
            return ScenarioContract.model_validate(json.loads(raw)), ""
        except (json.JSONDecodeError, ValueError) as exc:
            # pydantic 的 ValidationError 是 ValueError 子类，一并按解析失败处理
            error = str(exc)[:500]
    return fallback_contract(requirement, error), error
