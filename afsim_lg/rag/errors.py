"""报错关键字提取与 errors-ref/lesson 词法检索。

报错文案和语料里的 `[Exxx]`/`[Lxxx]` 条目是字面重合，用词法命中比向量
更准，也省掉一次 embedding 调用。检索只扫 `ref` 与 `lesson` 两类块。
"""

import re
from typing import Callable

from afsim_lg.rag.index import get_collection, search

ERROR_KINDS = ["ref", "lesson"]
# 重检索只取语法/模板类块；命令文档与参考也可能有用，但排在后面
SKILL_KINDS = ["block", "template", "reference", "command"]
KIND_PRIORITY = {"block": 0, "template": 0, "reference": 1, "command": 2}
MAX_KEYWORDS = 8
MAX_REFS = 4
# 每个关键词在每个类别下最多取几条，让 ref 与 lesson 都有机会进入结果
PER_KEYWORD_KIND = 2
ERROR_REF_CHARS = 800

# 每条规则返回候选关键词，按优先级排列；第一个是完整报错短语
_RULES: list[tuple[re.Pattern, Callable[[re.Match], list[str]]]] = [
    (
        re.compile(r"Unknown command:\s*([A-Za-z_][A-Za-z0-9_]*)", re.I),
        lambda m: [f"Unknown command: {m.group(1)}", "Unknown command", m.group(1)],
    ),
    (
        re.compile(r"Could not find platform_type\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
        lambda m: ["Could not find platform_type", "platform_type"],
    ),
    (
        re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s+not closed", re.I),
        lambda m: [f"{m.group(1)} not closed", "not closed"],
    ),
    (
        re.compile(r"Undefined\s+([A-Za-z_][A-Za-z0-9_]*)", re.I),
        lambda m: [f"Undefined {m.group(1)}", m.group(1)],
    ),
    (
        re.compile(r"Unable to open[^'\n]*", re.I),
        lambda m: ["Unable to open"],
    ),
    (
        re.compile(r"Unknown (?:weapon|sensor|platform|type)[^'\n]*", re.I),
        lambda m: [m.group(0).strip()],
    ),
    (
        re.compile(r"Invalid[^'\n]*format", re.I),
        lambda m: [m.group(0).strip()],
    ),
]


def extract_error_keywords(errors: list[str]) -> list[str]:
    """从 mission/静态错误中提取可用于词法检索的短语。

    Args:
        errors: 结构化错误列表，可能带 `@ 'file', line N` 位置后缀。

    Returns:
        去重后的关键词，最多 `MAX_KEYWORDS` 个。
    """
    keywords: list[str] = []

    def add(value: str) -> None:
        """按原顺序去重追加，避免大小写变体重复。"""
        value = value.strip()
        if value and value.lower() not in {k.lower() for k in keywords}:
            keywords.append(value)

    for raw in errors:
        # 位置后缀不参与检索
        message = raw.split(" @ '")[0].lstrip("* ").strip()
        for pattern, to_keywords in _RULES:
            match = pattern.search(message)
            if not match:
                continue
            for keyword in to_keywords(match):
                add(keyword)
            break
        if len(keywords) >= MAX_KEYWORDS:
            break
    return keywords[:MAX_KEYWORDS]


def search_error_skill(
    requirement: str, keywords: list[str], k: int = 6
) -> list[dict]:
    """用需求与报错关键词重查技能语料。

    首轮只按需求检索，修复轮需要的是报错对象的语法/模板；两者拼成一条
    查询，既保留需求上下文，又让错误词决定命中方向。

    Args:
        requirement: 用户原始需求。
        keywords: `extract_error_keywords` 的结果。
        k: 返回条数。

    Returns:
        命中列表，结构与 `index.search` 一致，含 distance。
    """
    if not keywords:
        return []
    query = "\n".join([requirement, " ".join(keywords)]).strip()
    # 多取一些再按类别重排：修复最需要 block/template 里的可复用语法，
    # 命令类文档（如 .cursor/commands）距离可能更近，但对改代码帮助小
    hits = search(query, k=k * 2, where={"kind": {"$in": SKILL_KINDS}})
    hits.sort(key=lambda hit: (KIND_PRIORITY.get(hit["metadata"]["kind"], 9), hit["distance"]))
    return hits[:k]


def search_error_refs(keywords: list[str], k: int = MAX_REFS) -> list[dict]:
    """按关键词检索 ref/lesson 块。

    Args:
        keywords: `extract_error_keywords` 的结果。
        k: 返回的最大条数。

    Returns:
        命中列表，含正文、元数据与命中的关键词。
    """
    if not keywords:
        return []
    collection = get_collection(create=False)
    hits: list[dict] = []
    seen: set[tuple[str, int]] = set()

    for keyword in keywords:
        for kind in ERROR_KINDS:
            result = collection.get(
                where={"kind": kind},
                where_document={"$contains": keyword},
                include=["documents", "metadatas"],
                limit=PER_KEYWORD_KIND,
            )
            documents = result.get("documents") or []
            metadatas = result.get("metadatas") or []
            for document, metadata in zip(documents, metadatas):
                key = (metadata["path"], metadata.get("chunk_index", 0))
                if key in seen:
                    continue
                seen.add(key)
                hits.append(
                    {
                        "document": document[:ERROR_REF_CHARS],
                        "metadata": metadata,
                        "keyword": keyword,
                    }
                )
                if len(hits) >= k:
                    return hits
    return hits