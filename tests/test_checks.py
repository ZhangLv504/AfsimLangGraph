"""确定性检查的通过、时长不足与缺少 evt 三种情况。"""

from afsim_lg.afsim.evt import EvtSummary
from afsim_lg.contract import ScenarioContract
from afsim_lg.graph.checks import evaluate, filter_judge_missing


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
   sensor radar1 WSF_RADAR_SENSOR
      frame_time 1 sec
   end_sensor
end_platform_type

platform_type PLANE WSF_PLATFORM
   side blue
   spatial_domain air
   mover WSF_AIR_MOVER end_mover
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


def test_missing_domain_marker_fails():
    """要求 air 域但没有 spatial_domain 或 air mover 时失败。"""
    files = FILES.replace("   spatial_domain air\n", "").replace(
        "   mover WSF_AIR_MOVER end_mover\n", ""
    )
    checks, _ = evaluate(make_contract(), files, make_evt())
    domain = next(c for c in checks if c["name"] == "平台域")
    assert domain["ok"] is False


def test_missing_sensor_keyword_fails():
    """要求雷达但工程里只有几何传感器时失败。"""
    files = FILES.replace("WSF_RADAR_SENSOR", "WSF_GEOMETRIC_SENSOR")
    checks, _ = evaluate(make_contract(), files, make_evt())
    sensor = next(c for c in checks if c["name"] == "装备:传感器")
    assert sensor["ok"] is False


def test_equipment_outside_glossary_skipped():
    """词表外的装备描述不做确定性判断。"""
    contract = make_contract()
    contract.platforms[0].sensors = ["神秘装备"]
    checks, _ = evaluate(contract, FILES, make_evt())
    assert all(c["name"] != "装备:传感器" for c in checks)


def test_filter_judge_missing_drops_fabricated_items():
    """需求没写的交战、平台应被过滤，真实缺失保留。"""
    contract = ScenarioContract.model_validate(
        {"platforms": [{"side": "blue", "domain": "air", "count": 1, "main_entity": "飞机"}]}
    )
    kept, dropped = filter_judge_missing(
        ["缺少交战事件", "缺少航母", "缺少 end_time"],
        "蓝方一架飞机平飞 90 秒",
        contract,
    )
    assert "缺少交战事件" in dropped
    assert "缺少航母" in dropped
    assert kept == ["缺少 end_time"]


def test_filter_judge_missing_keeps_declared_equipment():
    """需求与契约里出现过的装备缺失项应保留。"""
    contract = ScenarioContract.model_validate(
        {"platforms": [{"side": "red", "domain": "ground", "count": 1, "sensors": ["雷达"]}]}
    )
    kept, dropped = filter_judge_missing(["缺少雷达"], "红方雷达探测蓝方飞机", contract)
    assert kept == ["缺少雷达"]
    assert dropped == []
