import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.opening import opening_policy
from app.vimax_engine import process


class V4PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_event_survives_generation_broadcast_review_and_immutable_archive(self):
        roles = ['坐席', '客户1', '客户2']
        state = {'id': 'd' * 32, 'template_id': 'source', 'status': 'running', 'cursor': 0,
                 'config': {'dialogue_flow_version': 4, 'opening_policy': opening_policy(9),
                            'target_messages': 9, 'customer_count': 2, 'language': 'zh-CN',
                            'persona_cards': {role: {'identity': '普通群友'} for role in roles},
                            'memory_enabled': False},
                 'agent_sessions': {role: {'turns': 0, 'memory_events': []} for role in roles},
                 'architecture': 'agent_team', 'schedule': [{'stage': '自然群聊'}] * 9,
                 'messages': [], 'errors': [], 'vimax_story': 'already planned',
                 'review_progress': {'phase': 'writing', 'repair_rounds': 0},
                 'version_record': {'version': 'V03', 'code': 'test-code'}}
        template = {'analysis': {'workflow_steps': []}, 'source': {'accounts': [], 'codes': [],
                    'messages': [{'sender': '坐席', 'source_row': 1,
                                  'content': '打开 https://example.invalid/test 点赞后截图先发群里，再发给经理。'}]}}
        reviews = []

        def get_run(_):
            return copy.deepcopy(state)

        def save_run(run):
            nonlocal state
            state = copy.deepcopy(run)

        async def review(_template, run):
            reviews.append(copy.deepcopy(run))
            return {'issues': [], 'goal_met': True, 'goal_reason': 'mock review',
                    'action_updates': [], 'decision_updates': [], 'memory_reflections': []}

        class Writer:
            def __init__(self, chat_model):
                pass

            async def write_script_based_on_story(self, story, request, system_prompt):
                self_request = request
                assert '八项格式' in self_request
                return ['\n'.join([
                    '001|坐席|0|4|寒暄暖场|文本|-|大家好',
                    '002|客户1|0|7|了解群用途|文本|-|这里做什么的？',
                    '003|坐席|0|8|自然群聊|文本|-|打开 https://example.invalid/test 点赞后截图先发群里，再发给经理。',
                    '004|客户1|0|32|自然群聊|文本|-|点好赞了',
                    '005|客户1|0|15|自然群聊|图片|SCREENSHOT_C1_01|',
                    '006|坐席|0|5|自然群聊|文本|-|收到了，再发给经理就可以。',
                    '007|客户1|0|42|自然群聊|文本|-|截图和代码都发给经理了',
                    '008|客户2|0|20|自然群聊|文本|-|我还没打开，先看看',
                    '009|客户1|0|6|自然群聊|文本|-|嗯。',
                ])]

        with tempfile.TemporaryDirectory() as directory:
            api = SimpleNamespace(get_run=get_run, save_run=save_run, get_template=lambda _: template,
                                  DATA_DIR=Path(directory),
                                  model_settings=lambda: ('https://example.invalid/v1', 'test-key', 'gpt-6-sol'),
                                  model_reasoning_effort=lambda _: 'high', review_dialogue=review)
            with patch('app.vimax_engine.ChatOpenAI'), patch('app.vimax_engine.screenwriter_class', return_value=Writer):
                await process(state['id'], api)
            self.assertEqual(state['status'], 'completed', state.get('errors'))
            self.assertEqual(len(state['messages']), 9)
            self.assertEqual(state['messages'][4]['type'], '图片')
            self.assertEqual(reviews[0]['messages'][4]['image_code'], 'SCREENSHOT_C1_01')
            self.assertEqual(state['messages'][4]['content'], '')
            self.assertEqual(len(state['candidate_versions']), 1)
            record = state['candidate_versions'][0]
            snapshot = Path(directory) / record['path']
            self.assertIn('待补素材码：SCREENSHOT_C1_01', (snapshot / 'messages.md').read_text(encoding='utf-8'))
            self.assertEqual(record['capture_status'], 'running')


if __name__ == '__main__':
    unittest.main()
