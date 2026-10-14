import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.vimax_engine import decode_scenes, process
from app.opening import opening_policy


class RollingResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_continues_after_checkpoint_and_preserves_recorded_calls(self):
        roles = ['坐席', '客户1', '客户2']
        run = {'id': 'local-test', 'template_id': 'source', 'status': 'running', 'cursor': 0,
               'config': {'dialogue_flow_version': 2, 'opening_policy': opening_policy(33),
                          'target_messages': 33, 'customer_count': 2, 'language': 'zh-CN',
                          'persona_cards': {r: {'identity': '普通群友'} for r in roles}, 'memory_enabled': False},
               'agent_sessions': {r: {'turns': 0, 'memory_events': []} for r in roles},
               'architecture': 'agent_team', 'schedule': [{'stage': '自然群聊'}] * 33,
               'messages': [], 'errors': [], 'vimax_story': 'already planned',
               'review_progress': {'phase': 'writing', 'repair_rounds': 0}}
        run['vimax_draft'] = decode_scenes(['001|坐席|0|4|寒暄暖场|大家好\n002|客户1|0|12|寒暄暖场|手机快没电了\n003|坐席|0|7|了解群用途|这里介绍一个小任务'], run, 1, 3)
        template = {'analysis': {'workflow_steps': []},
                    'source': {'accounts': [], 'codes': [], 'contacts': [],
                               'urls': ['https://example.invalid/task'],
                               'messages': [{'sender': '坐席', 'source_row': 1, 'content': '入口 https://example.invalid/task'}]}}
        state = copy.deepcopy(run)
        requests = []

        def get_run(_id):
            return copy.deepcopy(state)

        def save_run(value):
            nonlocal state
            state = copy.deepcopy(value)

        async def review(_template, _run):
            return {'issues': [], 'goal_met': True, 'goal_reason': 'mocked review',
                    'action_updates': [], 'decision_updates': [], 'memory_reflections': []}

        api = SimpleNamespace(get_run=get_run, save_run=save_run, get_template=lambda _: template,
                              model_settings=lambda: ('https://example.invalid/v1', 'test-key', 'gpt-6-sol'),
                              model_reasoning_effort=lambda _: 'high', review_dialogue=review)

        class Writer:
            def __init__(self, chat_model):
                self.chat = chat_model

            async def write_script_based_on_story(self, story, request, system_prompt):
                requests.append(request)
                current = get_run('local-test')
                current.setdefault('vimax_calls', []).append({'stage': self.chat.stage})
                save_run(current)
                lines = ['004|坐席|0|8|自然群聊|入口 https://example.invalid/task']
                lines += [f'{i:03d}|客户{1+i%2}|0|{5+i%7}|自然群聊|日常接话{i}' for i in range(5, 34)]
                return ['\n'.join(lines)]

        with patch('app.vimax_engine.ChatOpenAI'), patch('app.vimax_engine.screenwriter_class', return_value=Writer):
            await process('local-test', api)
        self.assertEqual(state['status'], 'completed', state.get('errors'))
        self.assertEqual(len(requests), 1)
        self.assertIn('从004至033', requests[0])
        self.assertIn('手机快没电了', requests[0])
        self.assertEqual(state['messages'][1]['content'], '手机快没电了')
        self.assertEqual(state['vimax_calls'], [{'stage': 'write_4'}])
        self.assertEqual(state['vimax_segments'], [{'start': 4, 'end': 33}])
        self.assertEqual([m['stage'] for m in state['messages']], [s['stage'] for s in state['schedule']])
        self.assertEqual(state['naturalness_report']['explicit_reply_ratio'], 0)
