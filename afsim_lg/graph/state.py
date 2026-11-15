"""LangGraph 工作流状态定义。"""

from typing import TypedDict


class MainState(TypedDict, total=False):
    """单次运行在节点间传递的状态。

    工程正文始终落盘，状态里只放标识、路径和摘要，避免 checkpoint 膨胀。
    """

    run_id: str
    requirement: str
    max_attempts: int
    attempt: int
    generation: int
    contract: dict
    contract_error: str
    retrieved: list[dict]
    files: dict[str, str]
    generation_error: str
    feedback: str
    attempt_dir: str
    mission: dict
    evidence: dict
    checks: list[dict]
    assessment: dict
    status: str
