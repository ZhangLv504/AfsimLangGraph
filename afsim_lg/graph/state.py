from typing import TypedDict


class MainState(TypedDict, total=False):
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
