"""Chroma 索引的建库、写入与检索。"""

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import chromadb

from afsim_lg.config import get_settings
from afsim_lg.rag.chunker import Chunk, chunk_skills
from afsim_lg.rag.embed import embed_query, embed_texts

WRITE_BATCH = 64


@dataclass
class IndexStats:
    """一次建库的结果摘要。"""

    chunks: int
    files: int
    seconds: float
    collection: str
    path: str


def _chunk_id(chunk: Chunk) -> str:
    """用“技能:路径:序号”生成稳定 ID，重复建库时覆盖同一块而不是追加。"""
    key = f"{chunk.skill}:{chunk.path}:{chunk.chunk_index}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _metadata(chunk: Chunk) -> dict:
    return {
        "skill": chunk.skill,
        "path": chunk.path,
        "kind": chunk.kind,
        "heading": chunk.heading,
        "block": chunk.block,
        "chunk_index": chunk.chunk_index,
    }


def get_collection(create: bool = True):
    """打开语料集合。

    embedding_function=None 是刻意的：Chroma 默认会自动下载 ONNX 模型，
    这里统一由 bge-m3 生成向量并显式传入，避免联网和模型不一致。

    Args:
        create: 集合不存在时是否创建。

    Returns:
        Chroma 集合对象。
    """
    settings = get_settings()
    client = chromadb.PersistentClient(path=str(settings.chroma_dir))
    if create:
        return client.get_or_create_collection(
            name=settings.corpus_collection,
            embedding_function=None,
            metadata={"hnsw:space": "cosine"},
        )
    return client.get_collection(name=settings.corpus_collection, embedding_function=None)


def build_index(base: Path, rebuild: bool = False) -> IndexStats:
    """切块、向量化并写入 Chroma。

    Args:
        base: 技能语料根目录。
        rebuild: 是否先删除已有集合。

    Returns:
        块数、文件数、耗时等统计。
    """
    settings = get_settings()
    started = time.monotonic()
    client = chromadb.PersistentClient(path=str(settings.chroma_dir))
    if rebuild and settings.corpus_collection in [
        c.name for c in client.list_collections()
    ]:
        client.delete_collection(settings.corpus_collection)
    collection = client.get_or_create_collection(
        name=settings.corpus_collection,
        embedding_function=None,
        metadata={"hnsw:space": "cosine"},
    )

    chunks = chunk_skills(base)
    for start in range(0, len(chunks), WRITE_BATCH):
        batch = chunks[start : start + WRITE_BATCH]
        collection.add(
            ids=[_chunk_id(c) for c in batch],
            documents=[c.text for c in batch],
            metadatas=[_metadata(c) for c in batch],
            embeddings=embed_texts([c.embedding_text() for c in batch]),
        )
    elapsed = time.monotonic() - started
    return IndexStats(
        chunks=collection.count(),
        files=len({f"{c.skill}/{c.path}" for c in chunks}),
        seconds=elapsed,
        collection=settings.corpus_collection,
        path=str(settings.chroma_dir),
    )


def search(query: str, k: int = 8, where: dict | None = None) -> list[dict]:
    """按自然语言查询检索语料块。

    Args:
        query: 查询文本。
        k: 返回条数。
        where: Chroma 元数据过滤条件。

    Returns:
        命中列表，包含正文、元数据与 cosine 距离。
    """
    collection = get_collection(create=False)
    result = collection.query(
        query_embeddings=[embed_query(query)],
        n_results=k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    hits = []
    for doc, meta, distance in zip(
        result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        hits.append({"document": doc, "metadata": meta, "distance": distance})
    return hits
