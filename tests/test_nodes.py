from afsim_lg.contract import ScenarioContract
from afsim_lg.graph.nodes import _ensure_event_output


def contract_with_events():
    return ScenarioContract.model_validate(
        {"expectations": [{"kind": "detect", "actor_side": "red", "target_side": "blue"}]}
    )


def test_injects_event_output_when_missing():
    files = {"main.txt": "end_time 60 sec\n"}
    result, injected = _ensure_event_output(files, contract_with_events())
    assert injected is True
    assert "event_output" in result["main.txt"]
    assert "enable PLATFORM_ADDED" in result["main.txt"]
    assert "end_time 60 sec" in result["main.txt"]


def test_keeps_existing_event_output():
    files = {"main.txt": "event_output file output/x.evt end_event_output\nend_time 60 sec\n"}
    result, injected = _ensure_event_output(files, contract_with_events())
    assert injected is False
    assert result == files


def test_skips_injection_without_expectations():
    files = {"main.txt": "end_time 60 sec\n"}
    result, injected = _ensure_event_output(files, ScenarioContract())
    assert injected is False
    assert result == files
