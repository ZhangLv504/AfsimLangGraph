"""LangGraph 图装配与运行入口。"""

import uuid
from datetime import datetime

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from afsim_lg.config import get_settings
from afsim_lg.graph import nodes
from afsim_lg.graph.state import MainState


def build_graph(checkpointer=None):
    """装配生成、校验、验收与修复的条件边。

    Args:
        checkpointer: LangGraph 检查点存储；传 None 时不持久化。

    Returns:
        编译后的状态图。
    """
    graph = StateGraph(MainState)
    graph.add_node("parse_requirement", nodes.parse_requirement)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("generate", nodes.generate)
    graph.add_node("write", nodes.write_files)
    graph.add_node("validate", nodes.validate)
    graph.add_node("assess", nodes.assess)
    graph.add_node("fix", nodes.fix)
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "parse_requirement")
    graph.add_edge("parse_requirement", "retrieve")
    graph.add_edge("retrieve", "generate")
    # 生成阶段解析不出文件时无法落盘，直接进 fix 汇总错误信息
    graph.add_conditional_edges(
        "generate",
        nodes.route_after_generate,
        {"write": "write", "fix": "fix"},
    )
    graph.add_edge("write", "validate")
    graph.add_edge("validate", "assess")
    # 验收通过就收尾，否则带着本轮的检查与事件证据进入修复
    graph.add_conditional_edges(
        "assess",
        nodes.route_after_assess,
        {"fix": "fix", "finalize": "finalize"},
    )
    # 还有修复轮次就重新生成，达到 max_attempts 直接收尾
    graph.add_conditional_edges(
        "fix",
        nodes.route_after_fix,
        {"generate": "generate", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def new_run_id() -> str:
    """生成按时间排序且不重复的运行标识。"""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def run(requirement: str, max_attempts: int = 2, run_id: str | None = None) -> dict:
    """执行一次完整的生成-校验-验收流程。

    Args:
        requirement: 用户自然语言需求。
        max_attempts: 最多生成几轮（含首轮）。
        run_id: 指定运行标识；不传时自动生成。

    Returns:
        图执行结束时的状态。
    """
    settings = get_settings()
    settings.checkpoints_db.parent.mkdir(parents=True, exist_ok=True)
    run_id = run_id or new_run_id()
    initial: MainState = {
        "run_id": run_id,
        "requirement": requirement,
        "max_attempts": max_attempts,
        "attempt": 0,
        "status": "running",
    }
    with SqliteSaver.from_conn_string(str(settings.checkpoints_db)) as saver:
        graph = build_graph(saver)
        # thread_id 直接用 run_id，便于按运行目录回放对应的检查点
        result = graph.invoke(
            initial, config={"configurable": {"thread_id": run_id}}
        )
    # 连接随 with 关闭，检查点已落盘，可按 thread_id 回放
    return result
