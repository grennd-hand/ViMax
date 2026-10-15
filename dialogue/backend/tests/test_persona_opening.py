import json
import unittest

from app.persona_library import persona_library
from app.opening import opening_policy, opening_schedule, opening_stage
from app.narrative_plan import parse_plan
from app.vimax_engine import contract_errors, decode_scenes, writing_contract


class PersonaOpeningTests(unittest.TestCase):
    def run_fixture(self, total=30):
        policy = {'version': 1, 'warm_end': 2 if total == 6 else 3, 'orientation_end': 3 if total == 6 else 4, 'main_start': 4 if total == 6 else 5}
        return {'config': {'target_messages': total, 'customer_count': 1, 'language': 'zh-CN',
                           'persona_cards': {'客户1': {'identity': '慢热', 'reaction_cues': '新人→少说'}},
                           'opening_policy': policy},
                'agent_sessions': {'坐席': {}, '客户1': {}},
                'schedule': opening_schedule([{'stage': '自然群聊'}] * total, policy),
                'architecture': 'agent_team', 'status': 'completed'}

    def source_fixture(self):
        return {'accounts': [], 'codes': [], 'urls': ['https://example.invalid/task'], 'contacts': [],
                'messages': [{'sender': '坐席', 'source_row': 1, 'content': '打开 https://example.invalid/task'}]}

    def test_catalog_has_all_34_sources_and_complete_editable_cards(self):
        entries = [e for e in persona_library() if e['id'].startswith('inspired_')]
        self.assertEqual(len(entries), 34)
        self.assertEqual(len({e['id'] for e in entries}), 34)
        required = {'identity', 'priority', 'decision_rule', 'voice', 'social_style',
                    'speech_habits', 'reaction_cues', 'speech_avoid'}
        for entry in entries:
            self.assertEqual(set(entry['card']), required)
            self.assertTrue(all(entry['card'].values()))
            self.assertTrue(entry['source_url'].startswith('https://github.com/'))
        entries[0]['card']['identity'] = '用户修改'
        self.assertNotEqual(persona_library()[24]['card']['identity'], '用户修改')

    def test_translated_catalog_keeps_behavior_without_literal_chinese_catchphrases(self):
        entries = [e for e in persona_library('en-US') if e['id'].startswith('inspired_')]
        self.assertEqual(len(entries), 34)
        self.assertTrue(all('不直译中文口头禅' in e['card']['speech_habits'] for e in entries))

    def test_new_opening_has_no_fixed_transition_numbers(self):
        policy = opening_policy(150)
        self.assertEqual(policy['version'], 2)
        self.assertNotIn('warm_end', policy)
        self.assertNotIn('orientation_end', policy)
        self.assertEqual(opening_schedule([{'stage': '自然群聊'}], policy), [{'stage': '自然群聊'}])

    def test_premature_entry_is_rejected_and_regular_entry_is_allowed(self):
        run = self.run_fixture()
        source = self.source_fixture()
        def messages(entry_seq):
            return [{'seq': i, 'sender': '坐席', 'content': '入口 https://example.invalid/task' if i == entry_seq else '大家好',
                     'reply_to': None, 'type': '文本', 'image_code': None,
                     'stage': opening_stage(i, run['config'].get('opening_policy'))} for i in range(1, 31)]
        run['messages'] = messages(1)
        self.assertTrue(any('提前公布' in e for e in contract_errors(source, run)))
        run['messages'] = messages(run['config']['opening_policy']['main_start'])
        self.assertEqual(contract_errors(source, run), [])
        run['config'].pop('opening_policy')
        run['schedule'] = [{'stage': '自然群聊'}] * 30
        run['messages'] = messages(1)
        self.assertFalse(any('提前公布' in e for e in contract_errors(source, run)))

    def test_written_stages_and_zero_strength_are_used_by_vimax(self):
        run = self.run_fixture(6)
        scenes = ['\n'.join(f'{i:03d}|坐席|0|大家好' for i in range(1, 7))]
        stages = [m['stage'] for m in decode_scenes(scenes, run)]
        self.assertEqual(stages, ['寒暄暖场', '寒暄暖场', '了解群用途', '自然群聊', '自然群聊', '自然群聊'])
        self.assertEqual(stages, [slot['stage'] for slot in run['schedule']])

    def test_schedule_recovery_updates_opening_runs_and_preserves_old_runs(self):
        old = [{'stage': '自然群聊'}] * 6
        self.assertIs(opening_schedule(old, None), old)
        self.assertEqual(opening_schedule(old, self.run_fixture(6)['config']['opening_policy']), self.run_fixture(6)['schedule'])

    def test_zero_strength_preserves_the_cast_without_persona_cards(self):
        run = self.run_fixture(6)
        template = {'source': self.source_fixture(), 'analysis': {'primary_goal': '了解用途'}}
        run['config']['persona_strength'] = 0
        context = json.loads(writing_contract(template, run).split('\n')[-1])
        self.assertEqual(context['roles'], {})
        self.assertEqual(context['cast'], ['坐席', '客户1'])
        run['config']['persona_strength'] = .85
        context = json.loads(writing_contract(template, run).split('\n')[-1])
        self.assertEqual(context['roles'], run['config']['persona_cards'])

    def test_planning_rejects_merged_warmup_and_business_events(self):
        run = self.run_fixture(6)
        def event(index, start, end, last):
            return {'index': index, 'is_last': last, 'description': '交流', 'process_chain': ['接话后了解对方'],
                    'message_start': start, 'message_end': end, 'roles': ['坐席', '客户1'],
                    'cause': '刚进群', 'outcome': '愿意继续听', 'carry_forward': '保持已知信息'}
        good = [event(0, 1, 2, False), event(1, 3, 3, False), event(2, 4, 6, True)]
        self.assertEqual(parse_plan(json.dumps({'events': good}), run), good)
        with self.assertRaisesRegex(ValueError, '寒暄暖场'):
            parse_plan(json.dumps({'events': [event(0, 1, 6, True)]}), run)


if __name__ == '__main__':
    unittest.main()
