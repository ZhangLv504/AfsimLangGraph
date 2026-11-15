"""文件标记解析与条件路由的单元测试。"""

from afsim_lg.graph.nodes import (
    parse_files,
    route_after_assess,
    route_after_fix,
    route_after_generate,
)


def test_parse_files_strips_fences_and_end_marker():
    """代码围栏与结束标记不应混入文件内容。"""
    text = (
        "===FILE: main.txt===\n"
        "```text\n"
        "end_time 10 sec\n"
        "```\n"
        "===FILE: setup.txt===\n"
        "===END===\n"
        "```"
    )
    assert parse_files(text) == {"main.txt": "end_time 10 sec\n"}


def test_parse_files_keeps_content_before_end_marker():
    """结束标记前的最后一个文件内容不能被丢弃。"""
    text = "===FILE: main.txt===\nend_time 5 sec\n===END==="
    assert parse_files(text) == {"main.txt": "end_time 5 sec\n"}


def test_routing():
    """生成失败、验收失败与重试上限的分支。"""
    assert route_after_generate({"files": {}}) == "fix"
    assert route_after_generate({"files": {"main.txt": "x"}, "generation_error": ""}) == "write"
    assert route_after_assess({"status": "passed"}) == "finalize"
    assert route_after_assess({"status": "retry"}) == "fix"
    assert route_after_fix({"attempt": 1, "max_attempts": 2}) == "generate"
    assert route_after_fix({"attempt": 2, "max_attempts": 2}) == "finalize"
