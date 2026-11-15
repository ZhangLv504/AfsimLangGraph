"""event_output 注入逻辑的单元测试。"""

from afsim_lg.contract import ScenarioContract
from afsim_lg.graph.nodes import _ensure_event_output


def contract_with_events():
    """构造要求探测事件的契约。"""
    return ScenarioContract.model_validate(
        {"expectations": [{"kind": "detect", "actor_side": "red", "target_side": "blue"}]}
    )


def test_injects_event_output_when_missing():
    """缺少事件输出时应追加标准块。"""
    files = {"main.txt": "end_time 60 sec\n"}
    result, injected = _ensure_event_output(files, contract_with_events())
    assert injected is True
    assert "event_output" in result["main.txt"]
    assert "enable PLATFORM_ADDED" in result["main.txt"]
    assert "end_time 60 sec" in result["main.txt"]


def test_keeps_existing_event_output():
    """已有事件输出时不应覆盖模型的选择。"""
    files = {"main.txt": "event_output file output/x.evt end_event_output\nend_time 60 sec\n"}
    result, injected = _ensure_event_output(files, contract_with_events())
    assert injected is False
    assert result == files


def test_skips_injection_without_expectations():
    """无交战要求时不需要注入事件输出。"""
    files = {"main.txt": "end_time 60 sec\n"}
    result, injected = _ensure_event_output(files, ScenarioContract())
    assert injected is False
    assert result == files
