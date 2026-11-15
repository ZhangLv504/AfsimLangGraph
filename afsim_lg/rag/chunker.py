import re
from dataclasses import dataclass, field
from pathlib import Path

MIN_CHARS = 400
MAX_CHARS = 1800

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
    text: str
    path: str
    skill: str
    kind: str
    heading: str = ""
    block: str = ""
    chunk_index: int = 0
    extra: dict = field(default_factory=dict)

    def embedding_text(self) -> str:
        parts = [f"[{self.skill}/{self.path}]", self.kind]
        if self.heading:
            parts.append(self.heading)
        if self.block:
            parts.append(self.block)
        header = " | ".join(parts)
        return f"{header}\n{self.text}"


def discover_skill_roots(base: Path) -> list[Path]:
    return sorted(
        p.parent
        for p in base.rglob("SKILL.md")
        if ".git" not in p.parts and "__pycache__" not in p.parts
    )


def iter_corpus_files(skill_root: Path) -> list[Path]:
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
    rel = path.relative_to(skill_root)
    if ".cursor" in rel.parts or ".trae" in rel.parts:
        return "command"
    if len(rel.parts) > 1 and rel.parts[0] in ROOT_KINDS:
        return ROOT_KINDS[rel.parts[0]]
    return "skill"


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
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
    chunks: list[Chunk] = []
    headings: list[str] = []
    buffer: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush():
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
    lines = text.splitlines()
    segments: list[tuple[str, str]] = []
    pending_comments: list[str] = []
    prose: list[str] = []

    def flush_prose():
        nonlocal prose
        body = "\n".join(prose).strip()
        prose = []
        if body:
            for part in _split_long(body, MAX_CHARS):
                segments.append((part, ""))

    def is_block_start(index: int) -> str | None:
        token_match = TOKEN_RE.match(lines[index].strip())
        if not token_match or lines[index].lstrip().startswith("#"):
            return None
        token = token_match.group(1)
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
    skill = skill_root.name
    kind = classify_kind(skill_root, path)
    rel = path.relative_to(skill_root).as_posix()
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
    chunks: list[Chunk] = []
    for skill_root in discover_skill_roots(base):
        for path in iter_corpus_files(skill_root):
            chunks.extend(chunk_file(skill_root, path))
    return chunks
