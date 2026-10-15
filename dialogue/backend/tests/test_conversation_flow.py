import json
import unittest
from app.conversation_flow import rolling_context, naturalness_evidence, flow_errors, repeated_passages
from app.opening import opening_policy
from app.vimax_engine import decode_scenes
from app.narrative_plan import parse_plan


class FlowTests(unittest.TestCase):
    def fixture(self):
        return {'config': {'dialogue_flow_version': 2, 'target_messages': 3, 'language': 'zh-CN',
                           'opening_policy': opening_policy(3)},
                'agent_sessions': {'坐席': {}, '客户1': {}, '客户2': {}},
                'person_situations': {'客户1': {'personal_now': '手机快没电了'}}}

    def test_six_fields_keep_pacing_and_allow_a_participant_to_end(self):
        messages = decode_scenes(['001|坐席|0|4|寒暄暖场|大家好\n002|客户1|0|12|了解群用途|这是什么群？\n003|客户2|2|8|自然群聊|我也刚看到'], self.fixture())
        self.assertEqual([m['delay'] for m in messages], ['4', '12', '8'])
        self.assertIsNone(messages[1]['reply_to'])
        self.assertEqual(messages[-1]['sender'], '客户2')

    def test_continuation_uses_global_numbering_and_rejects_future_replies(self):
        run = self.fixture()
        messages = decode_scenes(['031|客户1|12|16|自然群聊|前面那句我刚看到'], run, 31, 1)
        self.assertEqual(messages[0]['seq'], 31)
        with self.assertRaisesRegex(ValueError, '尚未出现'):
            decode_scenes(['031|客户1|32|16|自然群聊|前面那句'], run, 31, 1)

    def test_invalid_delay_stage_and_missing_fields_rejected(self):
        for line in ['001|坐席|0|-1|寒暄暖场|大家好', '001|坐席|0|601|寒暄暖场|大家好',
                     '001|坐席|0|4|未知|大家好', '001|坐席|0|大家好']:
            with self.assertRaises(ValueError):
                decode_scenes([line], self.fixture(), 1, 1)

    def test_state_contains_only_owner_statements_and_prior_public_replies(self):
        history = [{'seq': 1, 'sender': '客户1', 'content': '手机快没电了', 'reply_to': None, 'stage': '寒暄暖场'},
                   {'seq': 2, 'sender': '客户2', 'content': '我这也是', 'reply_to': 1, 'stage': '寒暄暖场'}]
        context = rolling_context(self.fixture(), history)
        own = context['private_by_owner']['客户1']
        self.assertEqual([m['seq'] for m in own['own_recent']], [1])
        self.assertEqual([m['seq'] for m in own['received_replies']], [2])
        self.assertEqual(context['private_by_owner']['坐席']['own_recent'], [])

    def test_normal_chat_does_not_erase_an_explicit_refusal(self):
        history = [{'seq': 1, 'sender': '客户1', 'content': '我不参加', 'task_state': 'declined',
                    'reply_to': None, 'stage': '自然群聊'},
                   {'seq': 2, 'sender': '客户1', 'content': '我去吃饭', 'task_state': 'active',
                    'reply_to': None, 'stage': '自然群聊'}]
        state = rolling_context(self.fixture(), history)['private_by_owner']['客户1']
        self.assertEqual(state['last_nonactive_choice'], 'declined')
        self.assertEqual(state['explicit_choices'][0]['content'], '我不参加')

    def test_adaptive_stage_order_and_announced_values(self):
        messages = [{'seq': 1, 'content': '你好', 'stage': '寒暄暖场'},
                    {'seq': 2, 'content': '这里聊什么？', 'stage': '了解群用途'},
                    {'seq': 3, 'content': '入口 abc', 'stage': '自然群聊'}]
        self.assertEqual(flow_errors(messages, ['abc']), [])
        messages[0]['content'] = '入口 abc'
        self.assertTrue(any('提前公布' in e for e in flow_errors(messages, ['abc'])))
        messages[0]['content'] = '你好'
        messages[2]['stage'] = '寒暄暖场'
        self.assertTrue(any('不连贯' in e for e in flow_errors(messages, [])))

    def test_dense_reply_chain_and_uniform_timing_do_not_pass(self):
        messages = [{'seq': i, 'content': '普通消息', 'stage': ['寒暄暖场', '了解群用途'][i-1] if i < 3 else '自然群聊',
                     'delay': '1', 'reply_to': i-1 if i > 1 else None} for i in range(1, 31)]
        errors = flow_errors(messages, [])
        self.assertTrue(any('几乎每条' in e for e in errors))
        self.assertTrue(any('全部相同' in e for e in errors))
        for m in messages:
            m['reply_to'] = None
            m['delay'] = str(4 + m['seq'] % 5)
        self.assertEqual(flow_errors(messages, []), [])

    def test_self_explanations_are_evidence_not_automatic_errors(self):
        messages = [{'seq': 1, 'content': '不是别人劝的，是我自己想试', 'reply_to': None, 'delay': '4'}]
        self.assertEqual(naturalness_evidence(messages)['self_explanation_candidates'][0]['seq'], 1)

    def test_cross_segment_copied_reports_are_review_evidence(self):
        messages = [{'seq': 48, 'sender': '客户1', 'content': '我打开入口，给指定商品点了赞，也截好图了。'},
                    {'seq': 85, 'sender': '客户2', 'content': '我打开入口，给指定商品点了赞，也截好图了。'}]
        findings = repeated_passages(messages)
        self.assertEqual([(f['earlier_seq'], f['seq']) for f in findings], [(48, 85)])
        self.assertEqual(repeated_passages([{'seq': 1, 'content': 'https://example.com/very-long-path 【FHR888】'},
                                           {'seq': 2, 'content': 'https://example.com/very-long-path 【FHR888】'}]), [])

    def test_turn_selection_keeps_private_intents_scoped_and_history_unchanged(self):
        run = self.fixture()
        run['config']['dialogue_flow_version'] = 3
        run['person_situations']['客户1']['wants'] = '自己想问的问题，还没公开'
        history = [{'seq': 1, 'sender': '坐席', 'content': '大家好', 'reply_to': None, 'stage': '寒暄暖场'}]
        context = rolling_context(run, history)
        self.assertEqual(context['public_history'][0]['content'], '大家好')
        self.assertNotIn('wants', context['private_by_owner']['客户2']['personal_now'])
        self.assertEqual(context['private_by_owner']['客户1']['personal_now']['wants'], '自己想问的问题，还没公开')
        self.assertEqual(context['turn_selection']['recurrence_candidates'], [])

    def test_new_plan_requires_threads_and_private_situations(self):
        run = self.fixture()
        event = {'index': 0, 'is_last': True, 'description': '聊天', 'process_chain': ['寒暄后说明用途'],
                 'message_start': 1, 'message_end': 3, 'roles': ['坐席'], 'cause': '新人进群',
                 'outcome': '了解用途', 'carry_forward': '已知用途'}
        data = {'events': [event]}
        with self.assertRaisesRegex(ValueError, '当下处境'):
            parse_plan(json.dumps(data), run)
        data['threads'] = [{'topic': '手机电量', 'kind': '日常', 'roles': ['客户1'], 'seed': '有人手机快没电'}]
        data['people'] = [{'role': role, 'personal_now': '刚进群', 'cares_about': '自己的安排', 'may_ignore': '无关操作'}
                          for role in run['agent_sessions']]
        parse_plan(json.dumps(data), run)
        self.assertEqual(run['conversation_threads'][0]['topic'], '手机电量')
        self.assertEqual(set(run['person_situations']), set(run['agent_sessions']))
