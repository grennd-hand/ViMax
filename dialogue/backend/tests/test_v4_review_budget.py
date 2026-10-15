import os
import copy
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_test_data = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'SCRIPT_GENERATOR_DATA_DIR': _test_data.name}):
    from app import main


def tearDownModule():
    _test_data.cleanup()


class ReviewBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_review_receives_its_own_style_without_mutating_it(self):
        template = {'source': {'messages': [], 'accounts': [], 'codes': []}, 'analysis': {}}
        run = {'config': {'dialogue_flow_version': 4, 'creation_mode': 'independent_random'},
               'creative_variation': {'style_label': '碎句接话', 'axes': {'rhythm': 'fragments'}}, 'messages': []}
        before = copy.deepcopy(run)
        model = AsyncMock(return_value={'issues': [], 'goal_met': True, 'goal_reason': 'test'})
        with patch.object(main, 'model_json', model):
            await main.review_dialogue(template, run)
        payload = model.call_args.args[1]
        self.assertEqual(payload['creative_variation'], run['creative_variation'])
        self.assertIn('不要求引出难题', payload['style_review_policy'])
        self.assertEqual(run, before)

    async def test_review_preserves_interleaved_reply_and_timing_evidence(self):
        template = {'source': {'messages': [], 'accounts': [], 'codes': []}, 'analysis': {}}
        run = {'config': {'dialogue_flow_version': 4}, 'messages': [
            {'seq': 1, 'sender': '客户1', 'stage': '闲聊', 'content': '椅面掉皮了', 'reply_to': None, 'delay': 8},
            {'seq': 2, 'sender': '客户2', 'stage': '闲聊', 'content': '泳镜怎么选', 'reply_to': None, 'delay': 11},
            {'seq': 3, 'sender': '客户3', 'stage': '闲聊', 'content': '我家的换过椅面', 'reply_to': 1, 'delay': 15},
        ]}
        before = copy.deepcopy(run)
        model = AsyncMock(return_value={'issues': [], 'goal_met': True, 'goal_reason': 'test'})
        with patch.object(main, 'model_json', model):
            await main.review_dialogue(template, run)
        self.assertEqual(model.await_count, 1)
        messages = model.call_args.args[1]['messages']
        self.assertEqual(messages[2]['reply_to'], 1)
        self.assertEqual([m['delay'] for m in messages], [8, 11, 15])
        self.assertEqual(run, before)

    async def test_v4_honors_configured_budget_and_legacy_keeps_its_limit(self):
        template = {'source': {'messages': [], 'accounts': [], 'codes': []}, 'analysis': {}}
        for version, expected in ((3, 180), (4, 360)):
            run = {'config': {'dialogue_flow_version': version}, 'messages': []}
            model = AsyncMock(return_value={'issues': [], 'goal_met': True, 'goal_reason': 'test'})
            with patch.dict(os.environ, {'MODEL_CALL_BUDGET_SECONDS': '360'}), patch.object(main, 'model_json', model):
                await main.review_dialogue(template, run)
            self.assertEqual(model.await_count, 1)
            self.assertEqual(model.call_args.kwargs['call_budget_seconds'], expected)
            self.assertEqual(model.call_args.kwargs['stream'], version >= 4)
