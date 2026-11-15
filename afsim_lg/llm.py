import httpx

from afsim_lg.config import get_settings


class LLMError(RuntimeError):
    pass


def chat(
    system: str,
    user: str,
    *,
    json_mode: bool = False,
    temperature: float = 0.2,
    num_ctx: int = 8192,
    num_predict: int = 4096,
    timeout: float = 600,
) -> str:
    settings = get_settings()
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }
    if json_mode:
        payload["format"] = "json"
    with httpx.Client(base_url=settings.ollama_host, timeout=timeout, trust_env=False) as client:
        response = client.post("/api/chat", json=payload)
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
    if not content.strip():
        raise LLMError("empty response from model")
    return content
