from afsim_lg.contract import ScenarioContract, fallback_contract, guess_duration


def test_guess_duration():
    assert guess_duration("仿真 60 秒后结束") == 60
    assert guess_duration("运行 2 分钟") == 120
    assert guess_duration("持续 1 小时") == 3600
    assert guess_duration("没有时长") is None


def test_contract_normalizes_chinese_fields():
    contract = ScenarioContract.model_validate(
        {
            "summary": "测试",
            "duration_sec": 60,
            "platforms": [{"side": "蓝方", "domain": "air", "count": 1}],
            "expectations": [
                {"kind": "探测", "actor_side": "红方", "target_side": "蓝方", "min_count": 1}
            ],
        }
    )
    assert contract.platforms[0].side == "blue"
    assert contract.expectations[0].kind == "detect"
    assert contract.expectations[0].actor_side == "red"
    assert contract.expected_sides == {"blue"}
    assert contract.needs_event_evidence is True


def test_fallback_contract_keeps_duration():
    contract = fallback_contract("蓝方飞机飞 90 秒", "bad json")
    assert contract.duration_sec == 90
    assert contract.platforms == []
    assert "降级契约" in contract.notes
