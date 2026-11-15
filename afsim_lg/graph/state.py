from typing import TypedDict


class MainState(TypedDict, total=False):
    run_id: str
    requirement: str
    max_attempts: int
    attempt: int
    retrieved: list[dict]
    files: dict[str, str]
    generation_error: str
    feedback: str
    mission: dict
    assessment: dict
    status: str
