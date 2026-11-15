"""LangGraph 工作流状态定义。"""

from typing import TypedDict


class MainState(TypedDict, total=False):
    """单次运行在节点间传递的状态。

    工程正文始终落盘，状态里只放标识、路径和摘要，避免 checkpoint 膨胀。
    """

    # run_id 同时用作 LangGraph 的 thread_id，回放检查点时不需要额外映射
    run_id: str
    requirement: str
    max_attempts: int
    # attempt 由 fix 递增；generation 统计生成节点执行次数，后者才是实际生成轮数
    attempt: int
    generation: int
    # full / repair：本轮是整份生成还是只改部分文件
    generation_mode: str
    # 上一轮报错检索到的 ref/lesson 片段，供下一轮生成参考
    error_keywords: list[str]
    error_refs: list[dict]
    contract: dict
    contract_error: str
    retrieved: list[dict]
    # 文件正文也放进状态供验收拼接，但完整副本已落盘到 attempt_dir
    files: dict[str, str]
    generation_error: str
    feedback: str
    attempt_dir: str
    # 以下字段只存摘要，完整日志与明细在留痕目录，避免 checkpoint 持续膨胀
    mission: dict
    evidence: dict
    checks: list[dict]
    assessment: dict
    # 取值：running / passed / retry
    status: str
