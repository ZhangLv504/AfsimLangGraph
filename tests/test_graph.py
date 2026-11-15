from afsim_lg.graph.nodes import (
    parse_files,
    route_after_assess,
    route_after_fix,
    route_after_generate,
)


def test_parse_files_strips_fences_and_end_marker():
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
    text = "===FILE: main.txt===\nend_time 5 sec\n===END==="
    assert parse_files(text) == {"main.txt": "end_time 5 sec\n"}


def test_routing():
    assert route_after_generate({"files": {}}) == "fix"
    assert route_after_generate({"files": {"main.txt": "x"}, "generation_error": ""}) == "write"
    assert route_after_assess({"status": "passed"}) == "finalize"
    assert route_after_assess({"status": "retry"}) == "fix"
    assert route_after_fix({"attempt": 1, "max_attempts": 2}) == "generate"
    assert route_after_fix({"attempt": 2, "max_attempts": 2}) == "finalize"
