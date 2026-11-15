"""确定性检查的通过、时长不足与缺少 evt 三种情况。"""

from afsim_lg.afsim.evt import EvtSummary
from afsim_lg.contract import ScenarioContract
from afsim_lg.graph.checks import evaluate


def make_contract(duration=60.0):
    """构造“红方雷达探测蓝方飞机”的契约。"""
    return ScenarioContract.model_validate(
        {
            "summary": "红方雷达探测蓝方飞机",
            "duration_sec": duration,
            "platforms": [
                {"side": "red", "domain": "ground", "count": 1, "sensors": ["雷达"]},
                {"side": "blue", "domain": "air", "count": 1},
            ],
            "expectations": [
                {"kind": "detect", "actor_side": "red", "target_side": "blue", "min_count": 1}
            ],
        }
    )


def make_evt():
    """构造一次红方探测蓝方的事件摘要。"""
    evt = EvtSummary()
    evt.platform_sides = {"RedRadar": "red", "BlueJet": "blue"}
    evt.detections = [{"time": 130.0, "actor": "RedRadar", "target": "BlueJet"}]
    evt.last_time_sec = 130.0
    return evt


FILES = """
platform_type RADAR WSF_PLATFORM
   side red
end_platform_type

platform_type PLANE WSF_PLATFORM
   side blue
end_platform_type

platform RedRadar RADAR
   side red
end_platform

platform BlueJet PLANE
   side blue
end_platform

end_time 60 sec
"""


def test_all_checks_pass():
    """契约与运行结果一致时所有检查通过。"""
    checks, evidence = evaluate(make_contract(), FILES, make_evt())
    assert all(c["ok"] for c in checks), checks
    assert evidence["detections"] == 1
    assert evidence["evt_present"] is True


def test_duration_short_fails():
    """工程时长小于契约要求时应失败。"""
    checks, _ = evaluate(make_contract(duration=120), FILES, make_evt())
    duration = next(c for c in checks if c["name"] == "仿真时长")
    assert duration["ok"] is False


def test_missing_evt_fails_expectations():
    """契约要求事件但缺少 evt 时应失败。"""
    checks, evidence = evaluate(make_contract(), FILES, None)
    assert evidence["evt_present"] is False
    assert any(not c["ok"] and c["name"].startswith("事件") for c in checks)
