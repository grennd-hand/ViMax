"""Read a complete chat-completions SSE response within one shared deadline."""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlsplit


def safe_model_reason(detail, secrets=()):
    """Display a bounded refusal reason without credentials or endpoint URLs."""
    text = detail if isinstance(detail, str) else ""
    hidden = set(value for value in secrets if isinstance(value, str) and value)
    for value in list(hidden):
        if value.startswith(('http://', 'https://')):
            parsed = urlsplit(value)
            hidden.update(item for item in (parsed.netloc, parsed.hostname) if item)
    for value in sorted(hidden, key=len, reverse=True):
        text = text.replace(value, '[已隐藏私有配置]')
    text = re.sub(r'https?://[^\s<>\"\']+', '[已隐藏链接]', text, flags=re.I)
    text = re.sub(r'\bBearer\s+[^\s,;]+', '[已隐藏授权信息]', text, flags=re.I)
    text = re.sub(r'\b(?:api[_-]?key|authorization)\s*[:=]\s*[\"\']?[^\s,;\"\']+',
                  '[已隐藏授权信息]', text, flags=re.I)
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', text).strip()[:400]


class ModelRefusalError(RuntimeError):
    """An explicit refusal is terminal for this request, never a retry signal."""
    def __init__(self, kind='refusal', detail='', *, secrets=()):
        self.kind = kind
        self.detail = safe_model_reason(detail, secrets)
        label = '模型内容过滤，拒绝本次请求' if kind == 'content_filter' else '模型明确拒绝本次请求'
        super().__init__(label + ('：' + self.detail if self.detail else '；服务端未提供具体理由'))


def check_model_refusal(value, *, secrets=()):
    """Inspect explicit completion/error markers, not arbitrary dialogue text."""
    if not isinstance(value, dict):
        return
    if value.get('finish_reason') == 'content_filter':
        detail = value.get('refusal')
        for key in ('message', 'delta'):
            if not detail and isinstance(value.get(key), dict):
                detail = value[key].get('refusal')
        raise ModelRefusalError('content_filter', detail or '', secrets=secrets)
    refusal = value.get('refusal')
    if refusal:
        raise ModelRefusalError('refusal', refusal if isinstance(refusal, str) else '', secrets=secrets)
    if value.get('type') == 'refusal':
        raise ModelRefusalError('refusal', value.get('text', ''), secrets=secrets)
    for key in ('message', 'delta'):
        if isinstance(value.get(key), dict):
            check_model_refusal(value[key], secrets=secrets)
    for part in value.get('content', []) if isinstance(value.get('content'), list) else []:
        check_model_refusal(part, secrets=secrets)
    error = value.get('error')
    if isinstance(error, dict):
        markers = {str(error.get(key) or '').lower() for key in ('code', 'type')}
        matches = markers & {'content_filter', 'content_policy_violation', 'safety_violation',
                              'responsibleaipolicyviolation', 'refusal', 'model_refusal'}
        if matches:
            marker = sorted(matches)[0]
            kind = 'refusal' if marker in {'refusal', 'model_refusal'} else 'content_filter'
            raise ModelRefusalError(kind, error.get('message', ''), secrets=secrets)


def plain_text_refusal(content):
    """Recognize explicit non-JSON refusal language, not a character declining."""
    if not isinstance(content, str):
        return False
    text = content.strip()
    if text.startswith(('{', '[')):
        return False
    english = (r"^(?:(?:i(?:['’]m| am) sorry|sorry)[\s,!.:;]*(?:but\s+)?)?"
               r"i\s+(?:cannot|can['’]t|won['’]t|am unable to)\s+(?:help|assist|comply|provide|fulfill|generate|process|answer)\b")
    chinese = (r'^(?:(?:很|非常)?抱歉|对不起)[\s，。！,:：]*(?:但)?(?:我)?(?:无法|不能|不会)'
               r'(?:帮助|协助|满足|处理|回答|提供|生成|执行|完成|回应)')
    direct_chinese = r'^我(?:无法|不能|不会)(?:帮助|协助|满足|处理|回答|提供|生成|执行|完成|回应).{0,40}(?:请求|要求|内容|问题)'
    design_refusal = r'^我(?:无法|不能|不会)(?:帮助|协助)设计'
    bare_chinese = r'^(?:无法|不能)(?:协助|帮助|编写|生成|提供|满足|处理|回答|完成|执行|回应)'
    return bool(re.search(english, text, re.I) or re.search(chinese, text)
                or re.search(direct_chinese, text) or re.search(design_refusal, text)
                or re.search(bare_chinese, text))


def _strict_done_json(content, *, secrets):
    """Validate the entire body before accepting a gateway's missing stop marker."""
    text = content.strip()
    fenced = text.startswith('```')
    if fenced:
        match = re.fullmatch(r'```(?:json)?[ \t]*\r?\n(.*?)\r?\n```', text, re.I | re.S)
        if not match:
            raise ValueError('缺stop兼容模式要求单层完整JSON代码块')
        text = match[1].strip()
    if plain_text_refusal(text):
        raise ModelRefusalError('refusal', text, secrets=secrets)

    def reject_constant(value):
        raise ValueError('缺stop兼容模式不接受非标准JSON常量')

    value = json.loads(text, parse_constant=reject_constant)
    if not isinstance(value, (dict, list)):
        raise ValueError('缺stop兼容模式要求完整JSON对象或数组')
    check_model_refusal(value, secrets=secrets)
    return {'json_type': 'object' if isinstance(value, dict) else 'array', 'json_fence': fenced}


async def stream_model_content(client, url: str, *, headers: dict, body: dict,
                               deadline: float, allow_done_without_stop: bool = False):
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
            secrets = (url, headers.get('Authorization', ''), headers.get('Authorization', '').removeprefix('Bearer '))

            def consume() -> bool:
                nonlocal stopped
                if not data:
                    if event == "error":
                        raise ValueError("模型流内返回错误")
                    return False
                payload = "\n".join(data)
                if payload.strip() == "[DONE]":
                    if not stopped and not allow_done_without_stop:
                        raise ValueError("模型流未正常结束即返回DONE")
                    validation = _strict_done_json(''.join(fragments), secrets=secrets) if not stopped else {}
                    response.extensions['vimax_stream_protocol'] = {
                        'stop_received': stopped, 'done_received': True,
                        'allow_done_without_stop': allow_done_without_stop,
                        'compatibility_used': not stopped,
                        'strict_json_validated': not stopped, **validation}
                    return True
                chunk = json.loads(payload)
                check_model_refusal(chunk, secrets=secrets)
                if event == 'error':
                    # Some gateways send code/message directly on an error event.
                    check_model_refusal({'error': chunk}, secrets=secrets)
                    raise ValueError("模型流内返回错误")
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
                    check_model_refusal(choice, secrets=secrets)
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
