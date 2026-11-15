"""技能语料切块：Markdown 按标题，AFSIM 文本按块结构。"""

import re
from dataclasses import dataclass, field
from pathlib import Path

# 块太小检索噪声大，块太大挤占生成上下文；AFSIM 块允许放宽到两倍
MIN_CHARS = 400
MAX_CHARS = 1800

# 目录名 → kind，用于检索结果展示和按类别过滤
ROOT_KINDS = {
    "refs": "ref",
    "memory": "lesson",
    "commands": "command",
    "templates": "template",
    "blocks": "block",
    "references": "reference",
    "output": "output",
}

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BLOCK_END_RE = re.compile(r"^end_([A-Za-z_][A-Za-z0-9_]*)\s*$")
TOKEN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\b")


@dataclass
class Chunk:
    """一个可独立检索的语料片段。"""

    text: str
    path: str
    skill: str
    kind: str
    heading: str = ""
    block: str = ""
    chunk_index: int = 0
    extra: dict = field(default_factory=dict)

    def embedding_text(self) -> str:
        """拼出用于向量化的文本。

        正文前加一行路径与类别头，让“同名但用途不同”的块（如模板与参考）
        在向量空间里更容易区分。
        """
        parts = [f"[{self.skill}/{self.path}]", self.kind]
        if self.heading:
            parts.append(self.heading)
        if self.block:
            parts.append(self.block)
        header = " | ".join(parts)
        return f"{header}\n{self.text}"


def discover_skill_roots(base: Path) -> list[Path]:
    """查找所有含 SKILL.md 的技能根目录。

    Args:
        base: 语料根目录。

    Returns:
        技能根目录列表。
    """
    return sorted(
        p.parent
        for p in base.rglob("SKILL.md")
        if ".git" not in p.parts and "__pycache__" not in p.parts
    )


def iter_corpus_files(skill_root: Path) -> list[Path]:
    """枚举技能下参与建库的文本文件。

    英文 README 与许可证不包含 AFSIM 知识，排除以减少检索噪声。
    """
    files = []
    for p in skill_root.rglob("*"):
        if not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
            continue
        if p.suffix.lower() not in {".md", ".txt"}:
            continue
        if p.name in {"README_en.md", "LICENSE.md"}:
            continue
        files.append(p)
    return sorted(files)


def classify_kind(skill_root: Path, path: Path) -> str:
    """按相对路径判断片段类别，作为检索元数据。"""
    rel = path.relative_to(skill_root)
    if ".cursor" in rel.parts or ".trae" in rel.parts:
        return "command"
    if len(rel.parts) > 1 and rel.parts[0] in ROOT_KINDS:
        return ROOT_KINDS[rel.parts[0]]
    return "skill"


def _split_long(text: str, limit: int) -> list[str]:
    """按行把超长文本切到 limit 以内，尽量不破坏语句。"""
    if len(text) <= limit:
        return [text]
    # 按行切而不是按字符切：尽量不把一条命令或一句说明劈成两半
    lines = text.splitlines()
    parts, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts


def chunk_markdown(text: str, path: str, skill: str, kind: str) -> list[Chunk]:
    """按标题层级切分 Markdown。

    代码围栏内不识别标题也不按长度切分，否则会把一段示例拆成两半，
    检索命中后无法直接复用。

    Args:
        text: Markdown 全文。
        path: 相对技能根的路径。
        skill: 技能名。
        kind: 片段类别。

    Returns:
        带标题路径的片段列表。
    """
    chunks: list[Chunk] = []
    headings: list[str] = []
    buffer: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush():
        """把当前缓冲区按标题路径输出为片段。"""
        nonlocal buffer
        body = "\n".join(buffer).strip()
        buffer = []
        if not body:
            return
        heading = " > ".join(headings)
        for part in _split_long(body, MAX_CHARS):
            chunks.append(
                Chunk(text=part, path=path, skill=skill, kind=kind, heading=heading)
            )

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            # 围栏内原样保留：示例代码不参与标题识别和长度切分
            marker = stripped[:3]
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            buffer.append(line)
            continue
        heading_match = None if in_fence else HEADING_RE.match(stripped)
        if heading_match:
            level = len(heading_match.group(1))
            # 只有攒够最小长度才切，避免每个标题都产生一个碎块
            if buffer and sum(len(x) + 1 for x in buffer) >= MIN_CHARS:
                flush()
            headings = headings[: level - 1]
            headings.append(heading_match.group(2).strip())
            buffer.append(line)
            continue
        buffer.append(line)
        if not in_fence and sum(len(x) + 1 for x in buffer) >= MAX_CHARS:
            flush()
    flush()
    return chunks


def chunk_afsim_text(text: str, path: str, skill: str, kind: str) -> list[Chunk]:
    """按 `命令 ... end_命令` 的完整块切分 AFSIM 文本。

    块必须整体保留：平台、传感器、武器定义一旦被截断，检索到也无法
    直接粘贴使用；块外散文再按长度合并。

    Args:
        text: AFSIM 文本全文。
        path: 相对技能根的路径。
        skill: 技能名。
        kind: 片段类别。

    Returns:
        片段列表，块片段带块名。
    """
    lines = text.splitlines()
    segments: list[tuple[str, str]] = []
    # 块前的注释行随块保留，块名和用途通常写在注释里
    pending_comments: list[str] = []
    prose: list[str] = []

    def flush_prose():
        """把块外散文按长度切分并输出。"""
        nonlocal prose
        body = "\n".join(prose).strip()
        prose = []
        if body:
            for part in _split_long(body, MAX_CHARS):
                segments.append((part, ""))

    def is_block_start(index: int) -> str | None:
        """判断当前行是否为带匹配 end_ 的块起始行。"""
        token_match = TOKEN_RE.match(lines[index].strip())
        if not token_match or lines[index].lstrip().startswith("#"):
            return None
        token = token_match.group(1)
        # 必须能找到同名 end_：没有闭合的命令（如 simulation）不当作独立块
        for later in lines[index + 1 :]:
            end_match = BLOCK_END_RE.match(later.strip())
            if end_match and end_match.group(1).lower() == token.lower():
                return token
        return None

    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            if prose:
                prose.append(line)
            index += 1
            continue
        if line.lstrip().startswith("#"):
            pending_comments.append(line)
            index += 1
            continue
        token = is_block_start(index)
        if token:
            flush_prose()
            end_token = f"end_{token.lower()}"
            block_lines = list(pending_comments)
            pending_comments = []
            while index < len(lines):
                block_lines.append(lines[index])
                if lines[index].strip().lower() == end_token:
                    index += 1
                    break
                index += 1
            body = "\n".join(block_lines).strip()
            # 块上限放宽一倍：宁可整块略大，也不要拆开定义
            for part in _split_long(body, MAX_CHARS * 2):
                segments.append((part, token))
            continue
        if pending_comments:
            prose.extend(pending_comments)
            pending_comments = []
        prose.append(line)
        index += 1
    prose.extend(pending_comments)
    flush_prose()

    merged: list[tuple[str, str]] = []
    for body, block in segments:
        # 相邻短散文合并，避免碎块；块片段保持独立，不与散文混合
        if merged and len(merged[-1][0]) < MIN_CHARS and merged[-1][1] == block == "":
            merged[-1] = (merged[-1][0] + "\n\n" + body, "")
        else:
            merged.append((body, block))
    return [
        Chunk(text=body, path=path, skill=skill, kind=kind, block=block)
        for body, block in merged
        if body
    ]


def chunk_file(skill_root: Path, path: Path) -> list[Chunk]:
    """按扩展名选择策略切分单个文件。"""
    skill = skill_root.name
    kind = classify_kind(skill_root, path)
    rel = path.relative_to(skill_root).as_posix()
    # utf-8-sig：语料里有带 BOM 的文件，不去掉会在首块开头留下不可见字符
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    chunks = (
        chunk_markdown(text, rel, skill, kind)
        if path.suffix.lower() == ".md"
        else chunk_afsim_text(text, rel, skill, kind)
    )
    for i, chunk in enumerate(chunks):
        chunk.chunk_index = i
    return chunks


def chunk_skills(base: Path) -> list[Chunk]:
    """切分 base 下所有技能语料。"""
    chunks: list[Chunk] = []
    for skill_root in discover_skill_roots(base):
        for path in iter_corpus_files(skill_root):
            chunks.extend(chunk_file(skill_root, path))
    return chunks
