"""Read a complete chat-completions SSE response within one shared deadline."""
from __future__ import annotations

import asyncio
import json
import time


async def stream_model_content(client, url: str, *, headers: dict, body: dict,
                               deadline: float):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("模型流式响应超过总等待上限")
    async with asyncio.timeout(remaining):
        async with client.stream("POST", url, headers=headers, json={**body, "stream": True},
                                 timeout=remaining) as response:
            if not response.is_success:
                # Retain the error body for the existing unsupported-effort policy.
                await response.aread()
                return response, None
            fragments: list[str] = []
            stopped = False
            data: list[str] = []
            event = ""

            def consume() -> bool:
                nonlocal stopped
                if event == "error":
                    raise ValueError("模型流内返回错误")
                if not data:
                    return False
                payload = "\n".join(data)
                if payload.strip() == "[DONE]":
                    if not stopped:
                        raise ValueError("模型流未正常结束即返回DONE")
                    return True
                chunk = json.loads(payload)
                if not isinstance(chunk, dict) or "error" in chunk:
                    raise ValueError("模型流内返回错误或格式无效")
                choices = chunk.get("choices")
                if not isinstance(choices, list):
                    raise ValueError("模型流缺少choices")
                for choice in choices:
                    if not isinstance(choice, dict):
                        raise ValueError("模型流choice格式无效")
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    if not isinstance(delta, dict):
                        raise ValueError("模型流delta格式无效")
                    content = delta.get("content")
                    if isinstance(content, list):
                        if any(not isinstance(part, dict) or not isinstance(part.get("text"), str)
                               for part in content):
                            raise ValueError("模型流文字分片格式无效")
                        content = "".join(part["text"] for part in content)
                    if content is not None:
                        if not isinstance(content, str) or (stopped and content):
                            raise ValueError("模型流文字分片格式无效")
                        fragments.append(content)
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if reason != "stop":
                            raise ValueError(f"模型流未完整结束：{reason}")
                        stopped = True
                return False

            async for line in response.aiter_lines():
                if not line:
                    if consume():
                        return response, "".join(fragments)
                    data, event = [], ""
                elif line.startswith("data:"):
                    value = line[5:]
                    data.append(value[1:] if value.startswith(" ") else value)
                elif line.startswith("event:"):
                    event = line[6:].strip()
            # Some providers omit the final blank line after [DONE].
            if consume():
                return response, "".join(fragments)
            raise ValueError("模型流连接结束，但未收到完整stop与DONE结束标记")
