"""输出块补全、定向修复与静态检查短路的单元测试。"""

from types import SimpleNamespace

from afsim_lg.graph import nodes


def test_injects_both_output_blocks_by_default():
    """没有交战要求时也默认补齐 event_pipe 与 event_output。"""
    files = {"main.txt": "end_time 60 sec\n"}
    result, changed = nodes._ensure_outputs(files)
    text = result["main.txt"]
    assert changed == ["event_pipe", "event_output"]
    assert "file output/events.aer" in text
    assert "file output/events.evt" in text
    for enable in (
        "AIRCOMBAT",
        "TRACK_UPDATE",
        "BEHAVIOR_TOOL",
        "PLATFORM_ADDED",
        "SENSOR_TRACK_INITIATED",
    ):
        assert f"enable {enable}" in text
    assert "end_time 60 sec" in text


def test_completes_missing_enables_without_duplicates():
    """已有块只补缺失 enable，保留自定义 file，不重复 enable。"""
    files = {
        "main.txt": (
            "event_output\n"
            "   file output/custom.evt\n"
            "   enable WEAPON_HIT\n"
            "end_event_output\n"
        )
    }
    result, changed = nodes._ensure_outputs(files)
    text = result["main.txt"]
    assert "event_output" in changed
    assert text.count("enable WEAPON_HIT") == 1
    assert "enable PLATFORM_ADDED" in text
    assert "file output/custom.evt" in text


def test_expands_single_line_block_and_replaces_null():
    """单行块展开为多行，file NULL 改成真实输出文件。"""
    files = {"main.txt": "event_output file NULL end_event_output\n"}
    result, _ = nodes._ensure_outputs(files)
    text = result["main.txt"]
    assert "file NULL" not in text
    assert "file output/events.evt" in text
    assert "enable PLATFORM_ADDED" in text
    assert text.count("end_event_output") == 1


def test_keeps_complete_blocks_untouched():
    """两个块都完整时不改写文本。"""
    text = (
        "event_pipe\n   file output/events.aer\n"
        "   enable AIRCOMBAT\n   enable TRACK_UPDATE\n   enable BEHAVIOR_TOOL\n"
        "end_event_pipe\n\n"
        "event_output\n   file output/events.evt\n"
        "   enable PLATFORM_ADDED\n   enable SENSOR_TRACK_INITIATED\n"
        "   enable WEAPON_FIRED\n   enable WEAPON_HIT\n   enable PLATFORM_BROKEN\n"
        "end_event_output\n"
    )
    result, changed = nodes._ensure_outputs({"main.txt": text})
    assert changed == []
    assert result["main.txt"] == text


def test_completes_block_in_included_file():
    """块写在被 include 的 setup.txt 时就地补全，不在 main.txt 重复添加。"""
    files = {
        "main.txt": "include setup.txt\nend_time 10 sec\n",
        "setup.txt": "event_output file NULL end_event_output\n",
    }
    result, changed = nodes._ensure_outputs(files)
    assert "event_output" in changed
    assert "event_output" in result["setup.txt"]
    assert "event_output" not in result["main.txt"]
    assert "event_pipe" in result["main.txt"]


def test_moves_outputs_to_main_when_setup_not_included():
    """输出块在不被 include 的文件里时不生效，应补到 main.txt。"""
    files = {
        "main.txt": "end_time 10 sec\n",
        "setup.txt": "event_output file NULL end_event_output\n",
    }
    result, changed = nodes._ensure_outputs(files)
    assert set(changed) == {"event_pipe", "event_output"}
    assert "event_output" in result["main.txt"]
    assert "file output/events.evt" in result["main.txt"]


def test_generate_repairs_only_returned_files(monkeypatch):
    """有上一轮文件与反馈时应走修复提示词，只覆盖模型返回的文件。"""
    calls = []
    reply = "===FILE: main.txt===\nplatform_type FOO WSF_PLATFORM\nend_platform_type\n===END==="

    def fake_chat(system, user, **kwargs):
        """记录调用并返回单文件修复结果。"""
        calls.append((system, user))
        return reply

    monkeypatch.setattr(nodes, "chat", fake_chat)
    state = {
        "requirement": "测试需求",
        "files": {"main.txt": "old main", "setup.txt": "keep setup"},
        "feedback": "mission.exe 报错：Unknown command",
        "retrieved": [
            {
                "document": "radar_signature example",
                "distance": 0.1,
                "metadata": {
                    "skill": "afsim-skill",
                    "path": "templates/signature.txt",
                    "kind": "template",
                    "chunk_index": 0,
                },
            }
        ],
    }
    result = nodes.generate(state)
    assert result["generation_mode"] == "repair"
    assert result["files"]["setup.txt"] == "keep setup"
    assert "platform_type FOO" in result["files"]["main.txt"]
    assert nodes.REPAIR_SYSTEM in calls[0][0]
    assert "【报错重检索片段】" in calls[0][1]
    assert "radar_signature example" in calls[0][1]


def test_generate_falls_back_to_full_when_repair_unparsable(monkeypatch):
    """修复输出无法解析时应在同一节点内退回整份生成。"""
    replies = iter(
        [
            "没有文件标记",
            "没有文件标记",
            "===FILE: main.txt===\nend_time 10 sec\n===END===",
        ]
    )
    monkeypatch.setattr(nodes, "chat", lambda *args, **kwargs: next(replies))
    state = {
        "requirement": "测试需求",
        "files": {"main.txt": "old main"},
        "feedback": "报错",
        "retrieved": [],
    }
    result = nodes.generate(state)
    assert result["generation_mode"] == "full"
    assert result["files"]["main.txt"].strip() == "end_time 10 sec"


def test_undefined_hint_names_missing_object():
    """Undefined 报错应转成补定义块的具体指令。"""
    hint = nodes._undefined_hint(
        ["ERROR: Undefined radar_signature.", "ERROR: Undefined optical_signature."]
    )
    assert "radar_signature" in hint
    assert "optical_signature" in hint
    assert "WSF_RADAR_SIGNATURE" in hint
    assert nodes._undefined_hint(["Unknown command: foo"]) == ""


def test_merge_retrieved_prefers_error_hits():
    """合并时错误检索在前，重复块只保留一份。"""
    def hit(path, index, origin):
        """构造一条最小检索命中。"""
        return {
            "document": "x",
            "distance": 0.1,
            "origin": origin,
            "metadata": {
                "skill": "afsim-skill",
                "path": path,
                "kind": "block",
                "chunk_index": index,
            },
        }

    merged = nodes._merge_retrieved(
        [hit("same.txt", 1, "error")],
        [hit("same.txt", 1, "requirement"), hit("other.txt", 2, "requirement")],
    )
    assert [h["metadata"]["path"] for h in merged] == ["same.txt", "other.txt"]
    assert merged[0]["origin"] == "error"
    assert merged[1]["origin"] == "requirement"


def test_validate_skips_mission_on_static_error(tmp_path, monkeypatch):
    """静态检查失败时不应启动 mission.exe。"""
    monkeypatch.setattr(nodes, "get_settings", lambda: SimpleNamespace(runs_dir=tmp_path))

    def fail_mission(*args, **kwargs):
        """mission 被调用即说明短路失效。"""
        raise AssertionError("mission.exe should not run")

    monkeypatch.setattr(nodes, "run_mission", fail_mission)
    state = {
        "run_id": "static-test",
        "files": {"main.txt": "platform p1 MISSING\n   side blue\nend_platform\n"},
    }
    result = nodes.validate(state)
    assert result["mission"]["static_failed"] is True
    assert result["mission"]["exit_code"] == -2
    assert result["mission"]["errors"]
