import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from app.model_stream import ModelRefusalError


_test_data = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"SCRIPT_GENERATOR_DATA_DIR": _test_data.name}):
    from app import main as api


def tearDownModule():
    _test_data.cleanup()


def event(value):
    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


def delta(content=None, finish=None, **extra):
    return event({"choices": [{"index": 0, "delta": {"content": content, **extra}, "finish_reason": finish}]})


def complete(content):
    return delta(content) + delta(finish="stop") + "data: [DONE]\n\n"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


class ModelStreamTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, handler, *, stream=True, budget=.4, effort="high", model="gpt-6-sol", payload=None, fallback=""):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token = api._run_model_client.set(client)
            effort_token = api._run_reasoning_effort.set(None)
            try:
                with patch.object(api, "model_settings", return_value=("https://example.invalid/v1", "test-key", model)), \
                        patch.object(api, "model_reasoning_effort", return_value=effort), \
                        patch.dict(os.environ, {"MODEL_FALLBACK_NAME": fallback, "MODEL_TIMEOUT_SECONDS": ".01"}):
                    return await api.model_json("Review", payload or {}, stream=stream, call_budget_seconds=budget)
            finally:
                api._run_reasoning_effort.reset(effort_token)
                api._run_model_client.reset(token)

    async def test_fragmented_chinese_json_and_terminal_usage_are_joined_once(self):
        bodies = []
        text = delta(extra="ignored") + delta(reasoning_content="内部推理")
        text += delta('{"issues":[],"goal_met":true,"goal_reason":"')
        text += delta("截图事件完整") + delta('"}') + delta(finish="stop")
        text += event({"choices": [], "usage": {"total_tokens": 20}}) + "data: [DONE]"
        raw = text.encode("utf-8")

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, stream=Chunks([raw[i:i + 3] for i in range(0, len(raw), 3)]))

        self.assertEqual(await self.call(handler), {"issues": [], "goal_met": True, "goal_reason": "截图事件完整"})
        self.assertEqual(len(bodies), 1)
        self.assertTrue(bodies[0]["stream"])
        self.assertEqual(bodies[0]["reasoning_effort"], "high")
        self.assertEqual(bodies[0]["model"], "gpt-6-sol")

    async def test_error_in_stream_never_accepts_a_preceding_valid_json(self):
        for failure in (event({"error": {"message": "upstream failed"}}),
                        'event: error\ndata: {"message":"failed"}\n\n'):
            with self.subTest(failure=failure):
                bodies = []

                def handler(request):
                    bodies.append(json.loads(request.content))
                    return httpx.Response(200, stream=Chunks([(delta('{"ok":true}') + failure).encode()]))

                with self.assertRaisesRegex(RuntimeError, "连续失败"):
                    await self.call(handler)
                self.assertEqual(len(bodies), 2)  # Existing retry count, no additional calls.

    async def test_incomplete_truncated_and_malformed_streams_are_rejected(self):
        variants = [delta('{"ok":true}') + delta(finish="stop"),
                    delta('{"ok":true}') + "data: [DONE]\n\n",
                    delta('{"ok":true}') + delta(finish="length") + "data: [DONE]\n\n",
                    delta('{"ok":true}') + delta(finish="content_filter") + "data: [DONE]\n\n",
                    "data: {invalid}\n\n", complete('{"ok":'),
                    delta('{"ok":true}') + delta(finish="stop") + delta("extra") + "data: [DONE]\n\n"]
        for text in variants:
            with self.subTest(text=text):
                with self.assertRaises(RuntimeError):
                    await self.call(lambda request: httpx.Response(200, stream=Chunks([text.encode()])))

    async def test_total_deadline_cancels_a_stream_that_keeps_sending_data(self):
        streams, requests = [], []

        def handler(request):
            requests.append(request)
            stream = Chunks([b": heartbeat\n\n"] * 20, delay=.015)
            streams.append(stream)
            return httpx.Response(200, stream=stream)

        began = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "总等待上限"):
            await self.call(handler, budget=.05)
        self.assertLess(time.monotonic() - began, .3)
        self.assertEqual(len(requests), 1)
        self.assertTrue(streams[0].closed)

    async def test_stream_uses_remaining_budget_instead_of_short_read_timeout(self):
        raw = complete('{"ok":true}').encode()
        result = await self.call(lambda request: httpx.Response(200, stream=Chunks([raw], delay=.04)), budget=.3)
        self.assertEqual(result, {"ok": True})

    async def test_legacy_default_keeps_non_streaming_body_and_json_shape(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

        # Omit stream entirely to check the public default, not merely stream=False.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token = api._run_model_client.set(client)
            try:
                with patch.object(api, "model_settings", return_value=("https://example.invalid/v1", "", "gpt-6-sol")), \
                        patch.object(api, "model_reasoning_effort", return_value="high"):
                    result = await api.model_json("Check", {}, call_budget_seconds=.4)
            finally:
                api._run_model_client.reset(token)
        self.assertEqual(result, {"ok": True})
        self.assertNotIn("stream", bodies[0])
        self.assertEqual(bodies[0]["reasoning_effort"], "high")

    async def test_explicit_high_unsupported_does_not_downgrade_or_retry(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(400, json={"error": {"message": "reasoning_effort is unsupported"}})

        with self.assertRaisesRegex(RuntimeError, "未自动降低"):
            await self.call(handler)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["reasoning_effort"], "high")

    async def test_implicit_unsupported_effort_keeps_existing_removal_policy(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            if len(bodies) == 1:
                return httpx.Response(400, json={"error": {"message": "reasoning_effort is unsupported"}})
            return httpx.Response(200, stream=Chunks([complete('{"ok":true}').encode()]))

        with patch.object(api, "_reasoning_unsupported", set()), patch.dict(os.environ, {"MODEL_SCENE_REASONING_EFFORT": "low"}):
            self.assertEqual(await self.call(handler, effort=None, model="gpt-6", payload={"scene_draft": True}), {"ok": True})
            self.assertIn(("https://example.invalid/v1", "gpt-6"), api._reasoning_unsupported)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0]["reasoning_effort"], "low")
        self.assertNotIn("reasoning_effort", bodies[1])
        self.assertTrue(all(body["stream"] for body in bodies))

    async def test_stream_refusal_markers_stop_once_even_after_valid_json_without_fallback(self):
        cases = [delta('{"ok":true}') + delta(refusal="I cannot assist with that request."),
                 delta(content=[{"type": "refusal", "refusal": "抱歉，我无法协助处理这个请求。"}]),
                 delta(finish="content_filter"),
                 delta(finish="content_filter", refusal="服务端明确的过滤原因"),
                 event({"error": {"code": "bad_request", "type": "content_policy_violation", "message": "过滤说明"}}),
                 'event: error\ndata: {"code":"content_filter","message":"过滤说明"}\n\n']
        for text in cases:
            with self.subTest(text=text):
                bodies, streams = [], []
                def handler(request):
                    bodies.append(json.loads(request.content))
                    stream = Chunks([(text + delta(finish="stop") + "data: [DONE]\n\n").encode()])
                    streams.append(stream)
                    return httpx.Response(200, stream=stream)
                with self.assertRaisesRegex(ModelRefusalError, "拒绝") as caught:
                    await self.call(handler, fallback="other-model")
                self.assertNotIn("连接", str(caught.exception))
                self.assertEqual(len(bodies), 1)
                self.assertEqual(bodies[0]["model"], "gpt-6-sol")
                self.assertEqual(bodies[0]["reasoning_effort"], "high")
                self.assertTrue(streams[0].closed)
                if "content_filter" in text or "content_policy_violation" in text:
                    self.assertEqual(caught.exception.kind, "content_filter")

    async def test_nonstream_refusal_and_content_filter_are_terminal(self):
        choices = [{"message": {"refusal": "I cannot help with this request.", "content": None}},
                   {"finish_reason": "content_filter", "message": {"content": '{"ok":true}', "refusal": "过滤原因"}},
                   {"message": {"content": [{"type": "refusal", "refusal": "服务端拒绝"}]}}]
        for choice in choices:
            with self.subTest(choice=choice):
                requests = []
                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json={"choices": [choice]})
                with self.assertRaisesRegex(ModelRefusalError, "拒绝"):
                    await self.call(handler, stream=False, fallback="other-model")
                self.assertEqual(len(requests), 1)

    async def test_plain_text_explicit_refusal_is_terminal_in_both_modes(self):
        for stream in (True, False):
            for refusal in ("I'm sorry, but I can't assist with that request.", "抱歉，我无法帮助完成这个请求。",
                            "无法协助编写这类推广话术。可以改写为说明用途的内容。",
                            "不能帮助完成这个请求。", "无法编写这些话术。", "不能生成这类内容。", "无法提供该内容。"):
                with self.subTest(stream=stream, refusal=refusal):
                    requests = []
                    def handler(request):
                        requests.append(request)
                        return (httpx.Response(200, stream=Chunks([complete(refusal).encode()])) if stream else
                                httpx.Response(200, json={"choices": [{"message": {"content": refusal}}]}))
                    with self.assertRaisesRegex(ModelRefusalError, "拒绝"):
                        await self.call(handler, stream=stream, fallback="other-model")
                    self.assertEqual(len(requests), 1)

    async def test_valid_json_containing_dialogue_refusal_is_not_a_model_refusal(self):
        value = {"turns": [{"content": "抱歉，我不能帮助完成这个请求。"},
                           {"content": "无法协助编写这类推广话术。可以改写为其他内容。"}],
                 "goal_reason": "I cannot guarantee payment."}
        content = json.dumps(value, ensure_ascii=False)
        self.assertEqual(await self.call(lambda request: httpx.Response(200, stream=Chunks([complete(content).encode()]))), value)

    async def test_bare_nonrefusal_statement_keeps_json_failure_retries(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "不能保证到账。"}}]})
        with self.assertRaisesRegex(RuntimeError, "JSON 无效") as caught:
            await self.call(handler, stream=False)
        self.assertNotIsInstance(caught.exception, ModelRefusalError)
        self.assertEqual(len(requests), 2)

    async def test_http_policy_refusal_does_not_downgrade_or_try_fallback(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(400, json={"error": {"code": "content_filter", "message": "请求被安全过滤"}})
        with self.assertRaisesRegex(ModelRefusalError, "内容过滤"):
            await self.call(handler, stream=False, fallback="other-model")
        self.assertEqual(len(requests), 1)

    async def test_policy_refusal_after_existing_implicit_effort_retry_is_still_terminal(self):
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            error = ({"message": "reasoning_effort is unsupported"} if len(bodies) == 1 else
                     {"code": "content_filter", "message": "请求被过滤"})
            return httpx.Response(400, json={"error": error})
        with patch.object(api, "_reasoning_unsupported", set()), patch.dict(os.environ, {"MODEL_SCENE_REASONING_EFFORT": "low"}):
            with self.assertRaisesRegex(ModelRefusalError, "内容过滤"):
                await self.call(handler, stream=False, effort=None, model="gpt-6", payload={"scene_draft": True}, fallback="other-model")
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all(body["model"] == "gpt-6" for body in bodies))

    async def test_nonrefusal_errors_keep_both_attempts_and_existing_fallback_order(self):
        models = []
        def handler(request):
            models.append(json.loads(request.content)["model"])
            raise httpx.ReadError("offline transport failed", request=request)
        with self.assertRaisesRegex(RuntimeError, "传输失败"):
            await self.call(handler, fallback="other-model")
        self.assertEqual(models, ["gpt-6-sol", "other-model", "gpt-6-sol", "other-model"])

    async def test_refusal_details_redact_key_private_endpoint_and_authorization(self):
        reason = "拒绝原因 test-key https://example.invalid/v1 example.invalid Bearer test-key api_key=test-key"
        for stream in (True, False):
            with self.subTest(stream=stream):
                def handler(request):
                    return (httpx.Response(200, stream=Chunks([delta(refusal=reason).encode()])) if stream else
                            httpx.Response(200, json={"choices": [{"message": {"content": None, "refusal": reason}}]}))
                with self.assertRaises(ModelRefusalError) as caught:
                    await self.call(handler, stream=stream)
                rendered = str(caught.exception)
                self.assertIn("拒绝原因", rendered)
                self.assertNotIn("test-key", rendered)
                self.assertNotIn("example.invalid", rendered)
                self.assertIsNone(caught.exception.__cause__)

    async def test_transport_json_and_response_shape_failures_keep_existing_retries_and_distinct_labels(self):
        for kind, expected in (("transport", "传输失败"), ("json", "JSON 无效"), ("shape", "响应结构")):
            with self.subTest(kind=kind):
                requests = []
                def handler(request):
                    requests.append(request)
                    if kind == "transport":
                        raise httpx.ReadError("private details must not be shown", request=request)
                    if kind == "json":
                        return httpx.Response(200, json={"choices": [{"message": {"content": "not valid json"}}]})
                    return httpx.Response(200, json={"choices": []})
                with self.assertRaisesRegex(RuntimeError, expected) as caught:
                    await self.call(handler, stream=False)
                self.assertEqual(len(requests), 2)
                self.assertNotIn("private details", str(caught.exception))

    async def test_format_diagnostics_preserve_internal_reason_and_json_position_without_extra_requests(self):
        cases = [
            (False, {}, ("响应结构", "KeyError", "choices")),
            (False, {"choices": []}, ("响应结构", "IndexError", "list index out of range")),
            (True, event({"usage": {}}), ("流式格式", "ValueError", "缺少choices")),
            (True, delta('{"ok":true}') + "data: [DONE]\n\n", ("流式格式", "未正常结束即返回DONE")),
            (True, delta('{"ok":true}') + delta(finish="stop"), ("流式格式", "未收到完整stop与DONE")),
            (True, delta('{"ok":true}') + delta(finish="length"), ("流式格式", "未完整结束：length")),
            (True, "data: {invalid}\n\n", ("JSON 无效", "JSONDecodeError", "line 1 column 2 (char 1)")),
            (True, complete('{"ok":'), ("JSON 无效", "JSONDecodeError", "line 1 column 7 (char 6)")),
        ]
        for stream, value, expected in cases:
            with self.subTest(stream=stream, expected=expected):
                requests = []
                def handler(request):
                    requests.append(request)
                    return (httpx.Response(200, stream=Chunks([value.encode()])) if stream else
                            httpx.Response(200, json=value))
                with self.assertRaises(RuntimeError) as caught:
                    await self.call(handler, stream=stream)
                for part in expected:
                    self.assertIn(part, str(caught.exception))
                self.assertEqual(len(requests), 2)

    async def test_format_diagnostics_redact_bound_and_remove_controls_from_upstream_reason(self):
        reason = ("unexpected finish test-key https://example.invalid/v1 example.invalid "
                  "Bearer test-key api_key=test-key\n\x00" + "x" * 600)
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, stream=Chunks([delta(finish=reason).encode()]))
        with self.assertRaises(RuntimeError) as caught:
            await self.call(handler)
        rendered = str(caught.exception)
        self.assertIn("ValueError: 模型流未完整结束", rendered)
        self.assertIn("unexpected finish", rendered)
        self.assertNotIn("test-key", rendered)
        self.assertNotIn("example.invalid", rendered)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\x00", rendered)
        self.assertLess(len(rendered), 500)
        self.assertEqual(len(requests), 2)


if __name__ == "__main__":
    unittest.main()
