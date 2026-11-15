from pathlib import Path

from afsim_lg.afsim.evt import parse_evt, time_to_seconds

BS = chr(92)
FIXTURE = "\n".join(
    [
        f"0.00000 PLATFORM_ADDED RedRadar Type: SAM Side: red {BS}",
        " LLA: 40:00:00.00n 116:00:00.00e 0 m",
        "0.00000 PLATFORM_ADDED BlueJet Type: AIR Side: blue",
        f"00:02:10.0 SENSOR_TRACK_INITIATED RedRadar BlueJet Sensor: radar {BS}",
        " TrackId: RedRadar.1 Quality: 0.9",
        f"300.00000 WEAPON_FIRED RedRadar BlueJet Missile_1 Engagement: 1 {BS}",
        " Launch: LLA: 40:00:00.00n 116:00:00.00e 0 m",
        f"381.70000 WEAPON_HIT RedRadar BlueJet Missile_1 Engagement: 1 {BS}",
        " Pk: 0.5 Result: KILLED",
    ]
)


def test_time_to_seconds():
    assert time_to_seconds("381.7") == 381.7
    assert time_to_seconds("00:02:10.0") == 130.0
    assert time_to_seconds("01:00:00") == 3600.0


def test_parse_evt(tmp_path: Path):
    path = tmp_path / "events.evt"
    path.write_text(FIXTURE, encoding="utf-8")
    summary = parse_evt(path)

    assert summary.platform_sides == {"RedRadar": "red", "BlueJet": "blue"}
    assert len(summary.detections) == 1
    assert summary.detections[0]["actor"] == "RedRadar"
    assert summary.detections[0]["target"] == "BlueJet"
    assert summary.fires[0]["weapon"] == "Missile_1"
    assert summary.hits[0]["result"] == "KILLED"
    assert summary.kills == [{"time": 381.7, "victim": "BlueJet", "killer": "RedRadar"}]
    assert summary.last_time_sec == 381.7
    assert summary.counts["WEAPON_FIRED"] == 1
