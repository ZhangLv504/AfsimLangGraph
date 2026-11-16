"""回归集报告与单例异常隔离的单元测试。"""

from types import SimpleNamespace

from afsim_lg import regression


def make_state(passed: bool) -> dict:
    """构造一个最小运行状态。"""
    return {
        "status": "passed" if passed else "failed",
        "generation": 2,
        "assessment": {"missing": [] if passed else ["事件:detect: 0"]},
        "checks": [{"name": "仿真时长", "ok": passed, "detail": "x"}],
    }


def test_run_all_writes_reports(tmp_path, monkeypatch):
    """报告应统计通过率并写出 JSON 与 Markdown。"""
    monkeypatch.setattr(
        regression,
        "get_settings",
        lambda: SimpleNamespace(project_root=tmp_path, runs_dir=tmp_path / "runs"),
    )
    states = iter([make_state(True), make_state(False)])
    monkeypatch.setattr(regression, "run_graph", lambda *args, **kwargs: next(states))

    report = regression.run_all(
        [
            {"name": "a", "requirement": "r1"},
            {"name": "b", "requirement": "r2"},
        ],
        max_attempts=2,
        report_dir="reports",
    )
    assert report["summary"] == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "pass_rate": 0.5,
        "generations": 4,
        "avg_generations": 2.0,
        "elapsed_sec": report["summary"]["elapsed_sec"],
    }
    root = tmp_path / "reports"
    assert (root / "report.json").is_file()
    markdown = (root / "report.md").read_text(encoding="utf-8")
    assert "| a | PASS" in markdown
    assert "| b | FAIL" in markdown
    assert "事件:detect: 0" in markdown


def test_run_case_records_exception(tmp_path, monkeypatch):
    """单个用例异常应被记录而不是中断回归集。"""
    monkeypatch.setattr(
        regression,
        "get_settings",
        lambda: SimpleNamespace(project_root=tmp_path, runs_dir=tmp_path / "runs"),
    )

    def boom(*args, **kwargs):
        """模拟运行崩溃。"""
        raise RuntimeError("model down")

    monkeypatch.setattr(regression, "run_graph", boom)
    result = regression.run_case({"name": "x", "requirement": "r"}, 1)
    assert result["passed"] is False
    assert result["status"] == "error"
    assert "RuntimeError: model down" in result["error"]