"""报错关键字提取与 ref/lesson 词法检索的单元测试。"""

import pytest

from afsim_lg.rag.errors import (
    extract_error_keywords,
    search_error_refs,
    search_error_skill,
)
from afsim_lg.rag.index import get_collection


def test_extracts_known_patterns():
    """常见 mission/静态报错应提取出可检索的短语。"""
    errors = [
        "Unknown command: end_platform_type @ 'main.txt', line 5",
        "Mover not closed @ 'main.txt', line 3",
        "Could not find platform_type FOO @ 'main.txt', line 1",
        "Unable to open event_output file @ 'main.txt', line 2",
    ]
    keywords = extract_error_keywords(errors)
    assert "Unknown command" in keywords
    assert "Mover not closed" in keywords
    assert "platform_type" in keywords
    assert "Unable to open" in keywords


def test_extracts_undefined_object():
    """真实运行出现过的 Undefined radar_signature 应可检索。"""
    keywords = extract_error_keywords(["ERROR: Undefined radar_signature."])
    assert "Undefined radar_signature" in keywords
    assert "radar_signature" in keywords


def test_empty_errors_return_empty():
    """没有报错时不应产生关键词。"""
    assert extract_error_keywords([]) == []


@pytest.fixture
def require_index():
    """未建库时跳过依赖真实语料的测试。"""
    try:
        collection = get_collection(create=False)
    except Exception:
        pytest.skip("corpus index not built")
    if collection.count() == 0:
        pytest.skip("corpus index is empty")


def test_search_returns_ref_or_lesson(require_index):
    """检索结果只应来自 ref/lesson，并带上命中关键词。"""
    hits = search_error_refs(["Mover not closed"])
    assert hits
    assert all(h["metadata"]["kind"] in {"ref", "lesson"} for h in hits)
    assert all(h["keyword"] == "Mover not closed" for h in hits)


def test_search_error_skill_returns_syntax_blocks(require_index):
    """按错误重检索应返回语法/模板类块，并保留距离字段。"""
    hits = search_error_skill("飞机平飞", ["Undefined radar_signature"])
    assert hits
    assert all(
        h["metadata"]["kind"] in {"block", "template", "command", "reference"}
        for h in hits
    )
    assert all("distance" in h for h in hits)