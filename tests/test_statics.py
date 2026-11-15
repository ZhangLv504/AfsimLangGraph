"""静态 platform_type 引用检查的单元测试。"""

from afsim_lg.afsim.statics import check_platform_types


def test_reports_missing_type():
    """引用未定义类型时给出文件名与行号。"""
    files = {"main.txt": "platform p1 MISSING\n   side blue\nend_platform\n"}
    assert check_platform_types(files) == [
        "Could not find platform_type MISSING @ 'main.txt', line 1"
    ]


def test_accepts_definition_after_use():
    """实测 AFSIM 允许后定义，检查只要求存在定义。"""
    files = {
        "main.txt": (
            "platform p1 LATE\n   side blue\nend_platform\n"
            "platform_type LATE WSF_PLATFORM\n   side blue\nend_platform_type\n"
        )
    }
    assert check_platform_types(files) == []


def test_accepts_builtin_and_cross_file_definition():
    """WSF_ 内置类型和另一文件中的定义都应通过。"""
    files = {
        "main.txt": (
            "platform p1 WSF_PLATFORM\n   side blue\nend_platform\n"
            "platform p2 CUSTOM\n   side blue\nend_platform\n"
        ),
        "setup.txt": "platform_type CUSTOM WSF_PLATFORM\n   side red\nend_platform_type\n",
    }
    assert check_platform_types(files) == []


def test_ignores_comments_scripts_and_references():
    """注释、script/execute 区域和块内 platform 引用不参与检查。"""
    files = {
        "main.txt": (
            "/* platform p1 GHOST */\n"
            "// platform p2 GHOST2\n"
            "script void Foo()\n"
            "   platform p3 GHOST3\n"
            "end_script\n"
            "platform real WSF_PLATFORM\n"
            "   processor proc\n"
            "      platform real\n"
            "   end_processor\n"
            "end_platform\n"
        )
    }
    assert check_platform_types(files) == []