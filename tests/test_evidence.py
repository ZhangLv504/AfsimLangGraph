from pathlib import Path

import pytest

from afsim_lg.afsim.evt import find_evt, parse_evt
from afsim_lg.afsim.runner import run_mission
from afsim_lg.config import get_settings
from afsim_lg.contract import ScenarioContract
from afsim_lg.graph.checks import evaluate

SCENARIO = """platform_type BLUE_AIR WSF_PLATFORM
   side blue
   mover WSF_AIR_MOVER
   end_mover
end_platform_type

platform_type RED_RADAR WSF_PLATFORM
   side red
   sensor search WSF_GEOMETRIC_SENSOR
      frame_time 1 sec
      maximum_range 200 km
      reports_location
      ignore_same_side
      on
   end_sensor
end_platform_type

platform blue_jet BLUE_AIR
   route
      position 30:00:00n 120:00:00e altitude 10000 ft msl
         speed 300 kts
      position 30:20:00n 120:20:00e altitude 10000 ft msl
         speed 300 kts
   end_route
end_platform

platform red_radar RED_RADAR
   position 30:10:00n 120:10:00e altitude 0 ft msl
end_platform

event_output
   file output/events.evt
   enable PLATFORM_ADDED
   enable SENSOR_TRACK_INITIATED
   enable WEAPON_FIRED
   enable WEAPON_HIT
   enable PLATFORM_BROKEN
end_event_output

end_time 120 sec
"""


@pytest.mark.skipif(
    not get_settings().mission_exe.is_file(), reason="mission.exe not installed"
)
def test_detection_evidence_pipeline(tmp_path: Path):
    (tmp_path / "output").mkdir()
    (tmp_path / "main.txt").write_text(SCENARIO, encoding="utf-8")

    result = run_mission(tmp_path)
    assert result.ok, result.errors

    evt_path = find_evt(tmp_path)
    assert evt_path is not None
    evt = parse_evt(evt_path)
    assert evt.platform_sides == {"blue_jet": "blue", "red_radar": "red"}
    assert len(evt.detections) == 1

    contract = ScenarioContract.model_validate(
        {
            "duration_sec": 120,
            "platforms": [
                {"side": "red", "domain": "ground", "count": 1},
                {"side": "blue", "domain": "air", "count": 1},
            ],
            "expectations": [
                {"kind": "detect", "actor_side": "red", "target_side": "blue", "min_count": 1}
            ],
        }
    )
    checks, evidence = evaluate(contract, SCENARIO, evt)
    assert all(c["ok"] for c in checks), checks
    assert evidence["detections"] == 1
