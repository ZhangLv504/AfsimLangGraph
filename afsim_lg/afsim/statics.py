"""不运行 mission.exe 的静态引用检查。

只做高置信规则：`platform` 实例引用的 `platform_type` 必须在文件集中定义。
实测 AFSIM 2.9 允许先使用后定义（两遍解析），所以只查“是否定义”，不查顺序。

不检查 `end_` 闭合：demos 里 platform/sensor/processor 既可能是定义块，也
可能是块内引用，仅靠词法栈会大量误报；闭合错误等 mission.exe 报出后，再走
错误关键字检索取对应教训。
"""

import re

FIRST_TOKEN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\b")
PLATFORM_TYPE_RE = re.compile(r"^platform_type\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.I)
# 只匹配“实例名 + 类型名”两个参数；块内引用（platform <name>）只有一个参数
PLATFORM_INSTANCE_RE = re.compile(
    r"^platform\s+[A-Za-z_][A-Za-z0-9_.-]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", re.I
)
MAX_ERRORS = 8


def _iter_code_lines(files: dict[str, str]):
    """逐行产出 (文件名, 行号, 去注释后的内容)。

    跳过 `/* */` 块注释、整行 `#`/`//` 注释，以及 script/execute 区域：
    这些区域里的文本不是 AFSIM 命令，参与匹配会产生误报。
    """
    for name, text in files.items():
        in_comment = False
        skip_until = ""
        for lineno, raw in enumerate(text.splitlines(), 1):
            line, pos = "", 0
            while pos < len(raw):
                if in_comment:
                    end = raw.find("*/", pos)
                    if end == -1:
                        pos = len(raw)
                    else:
                        in_comment = False
                        pos = end + 2
                    continue
                start = raw.find("/*", pos)
                if start == -1:
                    line += raw[pos:]
                    break
                line += raw[pos:start]
                in_comment = True
                pos = start + 2
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//")):
                continue
            first = FIRST_TOKEN_RE.match(stripped)
            token = first.group(1).lower() if first else ""
            if skip_until:
                if token == skip_until:
                    skip_until = ""
                continue
            if token in {"script", "execute"} and f"end_{token}" not in stripped.lower():
                skip_until = f"end_{token}"
                continue
            yield name, lineno, stripped


def check_platform_types(files: dict[str, str]) -> list[str]:
    """检查 platform 实例引用的类型是否有定义。

    Args:
        files: 文件名到内容的映射，按写入顺序扫描。

    Returns:
        错误列表；类型都有定义（或为 WSF_ 内置类型）时为空。
    """
    lines = list(_iter_code_lines(files))
    defined = {
        match.group(1).lower()
        for _, _, text in lines
        if (match := PLATFORM_TYPE_RE.match(text))
    }

    errors: list[str] = []
    for name, lineno, text in lines:
        match = PLATFORM_INSTANCE_RE.match(text)
        if not match:
            continue
        ptype = match.group(1)
        # WSF_ 前缀是 AFSIM 内置类型，不需要本地定义
        if ptype.lower() in defined or ptype.upper().startswith("WSF_"):
            continue
        errors.append(f"Could not find platform_type {ptype} @ '{name}', line {lineno}")
        if len(errors) >= MAX_ERRORS:
            break
    return errors