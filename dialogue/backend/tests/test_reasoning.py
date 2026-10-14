import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx

_test_data = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'SCRIPT_GENERATOR_DATA_DIR':_test_data.name}):
    from app import main as api


def tearDownModule():
    _test_data.cleanup()


class FakeClient:
    def __init__(self, status=200): self.status=status;self.bodies=[]
    async def post(self,url,**kwargs):
        self.bodies.append(kwargs['json'])
        return httpx.Response(self.status,request=httpx.Request('POST',url),json=(
            {'choices':[{'message':{'content':'{"ok":true}'}}]} if self.status==200 else
            {'error':{'message':'reasoning_effort is unsupported'}}))


class ReasoningTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, client):
        token=api._run_model_client.set(client)
        try:
            with patch.object(api,'model_settings',return_value=('https://example.invalid/v1','test-key','gpt-6-sol')), patch.object(api,'model_reasoning_effort',return_value='high'):
                return await api.model_json('Check',{})
        finally:
            api._run_model_client.reset(token)

    async def test_high_is_sent_for_aliased_models(self):
        client=FakeClient()
        self.assertEqual(await self.call(client),{'ok':True})
        self.assertEqual(client.bodies[0]['reasoning_effort'],'high')
        self.assertEqual(client.bodies[0]['model'],'gpt-6-sol')

    async def test_explicit_high_is_not_silently_removed(self):
        client=FakeClient(400)
        with self.assertRaisesRegex(RuntimeError,'未自动降低'):
            await self.call(client)
        self.assertEqual(len(client.bodies),1)

    def test_run_config_accepts_gpt6_sol_high(self):
        config=api.SimulationConfig(template_id='test',customer_count=10,target_messages=150,dialogue_model='gpt-6-sol',reasoning_effort='high')
        self.assertEqual(config.reasoning_effort,'high')

    def test_model_default_can_clear_an_environment_effort(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'configs').mkdir()
            (root/'configs/agent.local.yaml').write_text("llm:\n  reasoning_effort: ''\n",encoding='utf-8')
            with patch.object(api,'APP_DIR',root/'dialogue'),patch.dict(os.environ,{'MODEL_REASONING_EFFORT':'high'}):
                self.assertIsNone(api.model_reasoning_effort())
