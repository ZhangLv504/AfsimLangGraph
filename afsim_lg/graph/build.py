import uuid
from datetime import datetime

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from afsim_lg.config import get_settings
from afsim_lg.graph import nodes
from afsim_lg.graph.state import MainState


def build_graph(checkpointer=None):
    graph = StateGraph(MainState)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("generate", nodes.generate)
    graph.add_node("write", nodes.write_files)
    graph.add_node("validate", nodes.validate)
    graph.add_node("assess", nodes.assess)
    graph.add_node("fix", nodes.fix)
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_conditional_edges(
        "generate",
        nodes.route_after_generate,
        {"write": "write", "fix": "fix"},
    )
    graph.add_edge("write", "validate")
    graph.add_edge("validate", "assess")
    graph.add_conditional_edges(
        "assess",
        nodes.route_after_assess,
        {"fix": "fix", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "fix",
        nodes.route_after_fix,
        {"generate": "generate", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def run(requirement: str, max_attempts: int = 2, run_id: str | None = None) -> dict:
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
        result = graph.invoke(
            initial, config={"configurable": {"thread_id": run_id}}
        )
    return result
