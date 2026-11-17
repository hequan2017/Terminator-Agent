"""OpenAI 兼容的 Chat Completions 客户端（流式 + 工具调用）。

适配 OpenAI / Claude(兼容端点) / DeepSeek / 智谱 GLM / 通义千问 / Ollama / vLLM 等。
"""
import json
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

import httpx

from .config import settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 字符串

    def parsed_args(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None


class LLMClient(Protocol):
    def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[dict]:
        """产出事件：{"type": "delta", "text": ...} 以及最后一个 {"type": "final", "response": LLMResponse}"""
        ...


class LLMError(RuntimeError):
    pass


class OpenAICompatClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model or settings.llm_model

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[dict]:
        if not self.api_key:
            raise LLMError("未配置 LLM_API_KEY，请在 .env 中设置大模型接口密钥")
        body = {"model": self.model, "messages": messages, "stream": True, "temperature": 0.2}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        content_parts: list[str] = []
        calls: dict[int, dict] = {}
        finish_reason = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.llm_timeout, connect=15)) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions", json=body,
                                     headers=headers) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise LLMError(f"大模型接口返回 {resp.status_code}：{text[:500]}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise LLMError(f"大模型接口错误：{chunk['error']}")
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            content_parts.append(text)
                            yield {"type": "delta", "text": text}
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]

        tool_calls = [
            ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=c["arguments"])
            for i, c in sorted(calls.items()) if c["name"]
        ]
        yield {"type": "final",
               "response": LLMResponse("".join(content_parts), tool_calls, finish_reason)}
