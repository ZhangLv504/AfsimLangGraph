"""bge-m3 向量化：直接调用 Ollama 的 /api/embed。"""

import httpx

from afsim_lg.config import get_settings

# 分批请求 Ollama：一次提交过多文本会拉长单次响应，也更容易触发超时
BATCH_SIZE = 16


def _client() -> httpx.Client:
    # trust_env=False：系统 NO_PROXY 的 [::1] 写法会让 httpx 解析失败
    return httpx.Client(base_url=get_settings().ollama_host, timeout=300, trust_env=False)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量生成文本向量。

    Args:
        texts: 待向量化的文本列表。

    Returns:
        与输入顺序一致的 1024 维向量列表。
    """
    settings = get_settings()
    vectors: list[list[float]] = []
    with _client() as client:
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            response = client.post(
                "/api/embed", json={"model": settings.embed_model, "input": batch}
            )
            response.raise_for_status()
            vectors.extend(response.json()["embeddings"])
    return vectors


def embed_query(text: str) -> list[float]:
    """生成单条查询向量。

    Args:
        text: 查询文本。

    Returns:
        查询文本的 1024 维向量。
    """
    return embed_texts([text])[0]
