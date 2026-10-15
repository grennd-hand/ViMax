import copy
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.opening import opening_policy
from app.vimax_engine import ModelRefusalError, RecordedChat, decode_scenes, process, revise


class V4PipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_refusal_without_dialogue_is_not_a_repairable_line_count_error(self):
        run = {'config': {'target_messages': 150}, 'agent_sessions': {'坐席': {}}}
        with self.assertRaises(ModelRefusalError):
            decode_scenes(['无法协助编写该请求。'], run)

    async def test_failed_model_call_is_visible_without_archiving_private_error_text(self):
        state = {'status': 'running'}
        chat = SimpleNamespace(model_name='gpt-6-sol', reasoning_effort='high',
                               ainvoke=AsyncMock(side_effect=RuntimeError('private-provider-setting')))
        api = SimpleNamespace(get_run=lambda _: state, save_run=lambda run: None)
        with self.assertRaises(RuntimeError):
            await RecordedChat(chat, api, 'fixture', 'independent_full_draft').ainvoke([])
        self.assertEqual(state['vimax_failed_calls'][0]['error_type'], 'RuntimeError')
        self.assertFalse(state['vimax_failed_calls'][0]['response_received'])
        self.assertNotIn('private-provider-setting', str(state))

    async def run_case(self, invalid_first=False, independent=False, malformed_first=False, segmented=False):
        roles = ['坐席', '客户1', '客户2']
        target = 150 if segmented else 9
        state = {'id': 'd' * 32, 'template_id': 'source', 'status': 'running', 'cursor': 0,
                 'config': {'dialogue_flow_version': 4, 'opening_policy': opening_policy(target),
                            'target_messages': target, 'customer_count': 2, 'language': 'zh-CN',
                            'persona_cards': {role: {'identity': '普通群友'} for role in roles},
                            'memory_enabled': False},
                 'agent_sessions': {role: {'turns': 0, 'memory_events': []} for role in roles},
                 'architecture': 'agent_team', 'schedule': [{'stage': '自然群聊'}] * target,
                 'messages': [], 'errors': [], 'vimax_story': 'already planned',
                 'review_progress': {'phase': 'writing', 'repair_rounds': 0},
                 'version_record': {'version': 'V03', 'code': 'test-code'}}
        template = {'analysis': {'workflow_steps': []}, 'source': {'accounts': [], 'codes': [],
                    'messages': [{'sender': '坐席', 'source_row': 1,
                                  'content': '打开 https://example.invalid/test 点赞后截图先发群里，再发给经理。'}]}}
        if independent:
            from app.creative_variation import create_variation_plan
            from app.dialogue_corpus import parse_lccc, sample_reference
            state['config']['creation_mode'] = 'independent_random'
            state['creative_variation'] = create_variation_plan(roles, seed='fresh-pipeline')
            corpus = parse_lccc([['你 午饭 吃 了 没', '刚刚 吃 完 呀']],
                                {'name': 'fixture', 'url': 'https://example.invalid/corpus', 'license': 'fixture'})
            state['corpus_reference'] = sample_reference(corpus, 'fresh-pipeline')
            state['person_situations'] = {'客户1': {'attention': '只在听见公交换乘时开口'}}
        reviews = []
        requests = []

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
                requests.append(request)
                lines = [
                    '001|坐席|0|4|寒暄暖场|文本|-|大家好',
                    '002|客户1|0|7|了解群用途|文本|-|这里做什么的？',
                    '003|坐席|0|8|自然群聊|文本|-|打开 https://example.invalid/test 点赞后截图先发群里，再发给经理。',
                    '004|客户1|0|32|自然群聊|文本|-|点好赞了',
                    '005|客户1|0|15|自然群聊|图片|SCREENSHOT_C1_01|',
                    '006|坐席|0|5|自然群聊|文本|-|收到了，再发给经理就可以。',
                    '007|客户1|0|42|自然群聊|文本|-|截图和代码都发给经理了',
                    '008|客户2|0|20|自然群聊|文本|-|我还没打开，先看看',
                    '009|客户1|0|6|自然群聊|文本|-|嗯。',
                ]
                if invalid_first and len(requests) == 1:
                    lines[4] = '005|客户1|0|15|自然群聊|文本|-|截图已经发群里了'
                if segmented:
                    # Content quality is tested with real scripts; this fixture
                    # checks numbering/history across three independent chunks.
                    lines += [f'{i:03d}|客户2|0|10|自然群聊|文本|-|独立流程测试消息{i}' for i in range(10, 151)]
                    start = (len(requests) - 1) * 50
                    lines = lines[start:start + 50]
                return ['\n'.join(lines[:-1] if malformed_first and len(requests) == 1 else lines)]

        with tempfile.TemporaryDirectory() as directory:
            api = SimpleNamespace(get_run=get_run, save_run=save_run, get_template=lambda _: template,
                                  DATA_DIR=Path(directory),
                                  model_settings=lambda: ('https://example.invalid/v1', 'test-key', 'gpt-6-sol'),
                                  model_reasoning_effort=lambda _: 'high', review_dialogue=review)
            with patch('app.vimax_engine.ChatOpenAI') as model, patch('app.vimax_engine.screenwriter_class', return_value=Writer), \
                 patch.object(logging.getLogger('agent_framework._workflows._agent_executor'), 'disabled', True):
                await process(state['id'], api)
            self.assertEqual(state['status'], 'completed', state.get('errors'))
            self.assertEqual(len(state['messages']), target)
            self.assertEqual(state['messages'][4]['type'], '图片')
            self.assertEqual(reviews[0]['messages'][4]['image_code'], 'SCREENSHOT_C1_01')
            self.assertEqual(state['messages'][4]['content'], '')
            self.assertEqual(len(state['candidate_versions']), 2 if invalid_first else 1)
            record = state['candidate_versions'][-1]
            snapshot = Path(directory) / record['path']
            self.assertIn('待补素材码：SCREENSHOT_C1_01', (snapshot / 'messages.md').read_text(encoding='utf-8'))
            self.assertEqual(record['capture_status'], 'running')
            if independent:
                self.assertEqual(len(requests), 3 if segmented else 2 if malformed_first else 1)
                self.assertIn('你午饭吃了没', requests[0])
                self.assertIn('quoted_dialogue_examples', requests[0])
                self.assertIn('public_history' if segmented else '第一次完整创作', requests[0])
                self.assertIn('只在听见公交换乘时开口', requests[0])
                self.assertEqual(state['vimax_segments'],
                                 [{'start': i, 'end': i + 49, 'strategy': 'independent_fresh_segments'} for i in (1, 51, 101)]
                                 if segmented else [{'start': 1, 'end': 9, 'strategy': 'whole_fresh_draft'}])
                if segmented:
                    self.assertIn('大家好', requests[1])
                    self.assertIn('独立流程测试消息50', requests[1])
                    self.assertIn('独立流程测试消息100', requests[2])
                self.assertEqual(model.call_args.kwargs['max_tokens'], 32000)
            if invalid_first:
                self.assertEqual(len(requests), 2)
                rejected = Path(directory) / state['candidate_versions'][0]['path']
                self.assertIn('截图已经发群里了', (rejected / 'messages.md').read_text(encoding='utf-8'))

    async def test_image_event_survives_generation_broadcast_review_and_immutable_archive(self):
        await self.run_case()

    async def test_independent_full_draft_uses_own_style_and_real_reference_then_actual_review(self):
        await self.run_case(independent=True)

    async def test_independent_malformed_first_draft_gets_one_bounded_format_repair(self):
        await self.run_case(independent=True, malformed_first=True)

    async def test_independent_150_message_run_keeps_its_own_history_across_three_chunks(self):
        await self.run_case(independent=True, segmented=True)

    async def test_rejected_complete_first_draft_is_retained_before_retry(self):
        await self.run_case(invalid_first=True)

    async def test_revision_keeps_new_raw_response_when_working_run_is_saved(self):
        run = {'id': 'e' * 32, 'status': 'running', 'config': {
            'dialogue_flow_version': 4, 'target_messages': 3, 'language': 'zh-CN', 'customer_count': 1,
            'persona_cards': {'坐席': {}, '客户1': {}}}, 'agent_sessions': {'坐席': {}, '客户1': {}},
            'schedule': [{'stage': stage} for stage in ('寒暄暖场', '了解群用途', '自然群聊')],
            'messages': [], 'vimax_samples': []}
        lines = ['001|坐席|0|4|寒暄暖场|文本|-|大家好',
                 '002|客户1|0|8|了解群用途|文本|-|这里做什么的？',
                 '003|坐席|0|5|自然群聊|文本|-|这里交流日常问题。']
        run['messages'] = decode_scenes(['\n'.join(lines)], run)
        state = copy.deepcopy(run)

        def get_run(_):
            return copy.deepcopy(state)

        def save_run(value):
            nonlocal state
            state = copy.deepcopy(value)

        class Writer:
            def __init__(self, chat_model):
                pass

            async def write_script_based_on_story(self, story, request, system_prompt):
                latest = get_run(run['id'])
                latest['vimax_samples'].append({'stage': 'revision', 'content': 'preserved raw sample'})
                save_run(latest)
                return ['\n'.join(lines)]

        template = {'analysis': {}, 'source': {'accounts': [], 'codes': [],
                    'messages': [{'sender': '坐席', 'source_row': 1, 'content': '这里交流日常问题。'}]}}
        with tempfile.TemporaryDirectory() as directory:
            api = SimpleNamespace(get_run=get_run, save_run=save_run, DATA_DIR=Path(directory))
            await revise(Writer, None, api, run['id'], template, run, [], 'test revision')
            save_run(run)
        self.assertEqual(state['vimax_samples'], [{'stage': 'revision', 'content': 'preserved raw sample'}])


if __name__ == '__main__':
    unittest.main()
