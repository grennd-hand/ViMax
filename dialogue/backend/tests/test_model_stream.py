import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx


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
    async def call(self, handler, *, stream=True, budget=.4, effort="high", model="gpt-6-sol", payload=None):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token = api._run_model_client.set(client)
            effort_token = api._run_reasoning_effort.set(None)
            try:
                with patch.object(api, "model_settings", return_value=("https://example.invalid/v1", "test-key", model)), \
                        patch.object(api, "model_reasoning_effort", return_value=effort), \
                        patch.dict(os.environ, {"MODEL_FALLBACK_NAME": "", "MODEL_TIMEOUT_SECONDS": ".01"}):
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


if __name__ == "__main__":
    unittest.main()
