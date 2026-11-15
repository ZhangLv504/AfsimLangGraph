from pathlib import Path

import pytest

from afsim_lg.afsim.runner import run_mission
from afsim_lg.config import get_settings


@pytest.fixture(scope="module", autouse=True)
def require_afsim():
    if not get_settings().mission_exe.is_file():
        pytest.skip("mission.exe not installed")


def test_minimal_scenario_succeeds(tmp_path: Path):
    (tmp_path / "main.txt").write_text("end_time 2 sec\n", encoding="utf-8")
    result = run_mission(tmp_path)
    assert result.ok, result.errors
    assert "Simulation complete" in result.output


def test_unknown_command_reports_error(tmp_path: Path):
    (tmp_path / "main.txt").write_text("bogus_command 1\nend_time 2 sec\n", encoding="utf-8")
    result = run_mission(tmp_path)
    assert not result.ok
    assert any("Unknown command: bogus_command" in e for e in result.errors)
