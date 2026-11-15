"""Ollama 对话接口的薄封装。"""

import httpx

from afsim_lg.config import get_settings


class LLMError(RuntimeError):
    """模型返回空内容时抛出。"""


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
    """调用 Ollama 对话接口并返回助手文本。

    Args:
        system: 系统提示词。
        user: 用户消息。
        json_mode: 是否让 Ollama 约束输出为 JSON。
        temperature: 采样温度，结构化任务保持较低值。
        num_ctx: 上下文窗口大小。
        num_predict: 单次生成的最大 token 数。
        timeout: 请求超时秒数。

    Returns:
        模型回复的纯文本。

    Raises:
        LLMError: 模型返回空内容。
    """
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
    # trust_env=False：本机环境注入的 NO_PROXY 含 [::1]，httpx 解析会直接抛错
    with httpx.Client(base_url=settings.ollama_host, timeout=timeout, trust_env=False) as client:
        response = client.post("/api/chat", json=payload)
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
    if not content.strip():
        raise LLMError("empty response from model")
    return content
