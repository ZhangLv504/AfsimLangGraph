import httpx

from afsim_lg.config import get_settings

BATCH_SIZE = 16


def _client() -> httpx.Client:
    return httpx.Client(base_url=get_settings().ollama_host, timeout=300, trust_env=False)


def embed_texts(texts: list[str]) -> list[list[float]]:
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
    return embed_texts([text])[0]
