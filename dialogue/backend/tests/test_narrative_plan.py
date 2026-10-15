import json
import unittest
from app.narrative_plan import DialogueEvent, OrganicPlan, module, parse_plan, planning_requirement
from app.model_stream import ModelRefusalError


class NovelEventPlanTests(unittest.TestCase):
    def setUp(self):
        self.run={'config':{'target_messages':6},'agent_sessions':{'坐席':{},'客户1':{}}}
        self.events=[{'index':0,'is_last':False,'description':'了解活动','process_chain':['提出疑问后获得说明'],
                      'message_start':1,'message_end':3,'roles':['坐席','客户1'],'cause':'收到邀请',
                      'outcome':'了解活动时间','carry_forward':'人物已经知道时间'},
                     {'index':1,'is_last':True,'description':'作出选择','process_chain':['依据时间决定参与'],
                      'message_start':4,'message_end':6,'roles':['客户1','坐席'],'cause':'获得时间说明',
                      'outcome':'确认个人选择','carry_forward':'不能再把这个选择当作尚未作出'}]

    def test_upstream_event_and_valid_plan_are_reused(self):
        self.assertTrue(issubclass(DialogueEvent,module.Event))
        text=json.dumps({'events':self.events},ensure_ascii=False)
        self.assertEqual(parse_plan(text,self.run),self.events)

    def test_gaps_and_unknown_roles_are_rejected_before_writing(self):
        self.events[1]['message_start']=5
        with self.assertRaisesRegex(ValueError,'不连续'):
            parse_plan(json.dumps({'events':self.events}),self.run)
        self.events[1]['message_start']=4
        self.events[1]['roles']=['原母本账号']
        with self.assertRaisesRegex(ValueError,'未设置'):
            parse_plan(json.dumps({'events':self.events}),self.run)

    def test_incomplete_causal_chain_and_early_end_are_rejected(self):
        self.events[0]['process_chain']=[]
        with self.assertRaisesRegex(ValueError,'具体交流过程'):
            parse_plan(json.dumps({'events':self.events}),self.run)
        self.events[0]['process_chain']=['收到邀请后提出具体疑问']
        self.events[0]['is_last']=True
        with self.assertRaisesRegex(ValueError,'结束标记'):
            parse_plan(json.dumps({'events':self.events}),self.run)

    def test_local_trailing_comma_recovery_avoids_another_model_call(self):
        text=json.dumps({'events':self.events},ensure_ascii=False)[:-1]+',}'
        self.assertEqual(parse_plan(text,self.run),self.events)

    def organic_plan(self):
        self.run['config']['dialogue_flow_version'] = 3
        event = {k: v for k, v in self.events[0].items() if k not in {'message_start', 'message_end'}}
        event.update(is_last=True, trigger='有人询问群用途', stop_when='用途已经得到解答')
        people = [dict(role=role, personal_now='刚进群', cares_about='自己的安排', may_ignore='无关进度',
                       wants='判断是否适合自己', attention='具体做法', reply_when='有人问到自己经历过的事',
                       leave_unanswered='别人的材料进度', voice='简短直接', relationship='陌生群友')
                  for role in self.run['agent_sessions']]
        return {'events': [event], 'threads': [{'topic': '群用途', 'kind': '业务', 'roles': ['客户1'], 'seed': '刚进群'}],
                'people': people}

    def test_organic_plan_does_not_need_message_coverage_or_a_timetable(self):
        data = self.organic_plan()
        self.run['config']['target_messages'] = 150
        result = parse_plan(json.dumps(data), self.run)
        self.assertNotIn('message_start', result[0])
        self.assertEqual(result[0]['stop_when'], '用途已经得到解答')
        self.assertEqual(self.run['person_situations']['客户1']['voice'], '简短直接')

    def test_organic_plan_rejects_missing_stop_conditions_intents_and_unknown_roles(self):
        for missing in ('stop_when', 'attention', 'unknown_role'):
            data = self.organic_plan()
            if missing == 'stop_when':
                del data['events'][0]['stop_when']
            elif missing == 'attention':
                del data['people'][0]['attention']
            else:
                data['events'][0]['roles'] = ['未知群友']
            with self.assertRaises(ValueError):
                parse_plan(json.dumps(data), self.run)

    def test_independent_requirement_accepts_sharing_without_a_problem_to_solve(self):
        self.organic_plan()
        self.run['config'].update(creation_mode='independent_random', target_messages=150)
        requirement = planning_requirement(self.run)
        self.assertIn('分享见闻、随口玩笑、感受与兴趣本身都能成为话题', requirement)
        self.assertIn('不要求每人有问题', requirement)
        self.assertIn('数量随内容需要', requirement)
        self.assertNotIn('提出新话题的人须有当时的实际疑问', requirement)
        self.assertNotIn('长篇也只准备少数', requirement)
        schema = json.loads(requirement.rsplit('：\n', 1)[1])
        self.assertEqual(schema['required'], ['events', 'threads', 'people'])
        self.assertEqual(schema['properties']['events']['maxItems'], 18)
        self.assertIn('想被听见', schema['$defs']['PersonIntent']['properties']['wants']['description'])
        self.assertIn('日常交流不必解决问题', schema['$defs']['ConversationBeat']['properties']['outcome']['description'])

    def test_independent_sharing_plan_keeps_parser_media_and_private_context_contracts(self):
        data = self.organic_plan()
        self.run['config'].update(creation_mode='independent_random', target_messages=150)
        event = data['events'][0]
        event.update(description='看到一朵云，想说一句', process_chain=['分享形状', '对方可能接一句喜好'],
                     cause='刚好想被听见', outcome='分享过这个见闻', trigger='本人想分享',
                     stop_when='说够或没人接就停')
        data['threads'][0].update(topic='路上见闻', kind='日常', seed='一朵有趣的云')
        data['people'][1].update(wants='想分享而非求助', reply_when='听到有趣的形容就接一句')
        result = parse_plan(json.dumps(data), self.run)
        self.assertEqual(result[0]['outcome'], '分享过这个见闻')
        self.assertEqual(self.run['person_situations']['客户1']['wants'], '想分享而非求助')
        requirement = planning_requirement(self.run)
        for boundary in ('必要业务事实和原值只来自母本', '先公开说明，再操作和截图',
                         '母本要求截图发群时', '别人依据此前公开的话回应', '人物起初陌生'):
            self.assertIn(boundary, requirement)

    def test_independent_schema_descriptions_do_not_change_fixed_mode(self):
        self.organic_plan()
        self.run['config']['creation_mode'] = 'fixed'
        schema_before = OrganicPlan.model_json_schema()
        fixed_before = planning_requirement(self.run)
        self.run['config']['creation_mode'] = 'independent_random'
        planning_requirement(self.run)
        self.run['config']['creation_mode'] = 'fixed'
        self.assertEqual(planning_requirement(self.run), fixed_before)
        self.assertEqual(OrganicPlan.model_json_schema(), schema_before)
        self.assertIn('提出新话题的人须有当时的实际疑问', fixed_before)

    def test_independent_complete_object_with_actual_v09_shaped_chinese_preamble_is_local_only(self):
        data = self.organic_plan()
        self.run['config']['creation_mode'] = 'independent_random'
        prefix = ('我会按要求只返回规划 JSON：把前几条安排成招呼、暖场和群用途探问，再让坐席公开完整入口与步骤；'
                  '后续保留日常岔话、不同参与意愿，以及“点赞—截图—发群—联系提交”的实际因果。'
                  '业务奖励和付款只作为母本说法，不写成已验证结果。')
        before = json.loads(json.dumps(data, ensure_ascii=False))
        events = parse_plan(prefix + json.dumps(data, ensure_ascii=False), self.run)
        self.assertEqual(events, data['events'])
        self.assertEqual(self.run['conversation_threads'], data['threads'])
        self.assertEqual(self.run['person_situations']['客户1']['wants'], data['people'][1]['wants'])
        self.assertEqual(data, before)

    def test_prefixed_independent_object_still_requires_schema_roles_and_continuity(self):
        data = self.organic_plan()
        self.run['config']['creation_mode'] = 'independent_random'
        for change in ('missing_field', 'unknown_role', 'bad_index'):
            value = json.loads(json.dumps(data, ensure_ascii=False))
            if change == 'missing_field': del value['events'][0]['stop_when']
            if change == 'unknown_role': value['events'][0]['roles'] = ['未知人']
            if change == 'bad_index': value['events'][0]['index'] = 1
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_plan('以下是规划：' + json.dumps(value, ensure_ascii=False), self.run)

    def test_independent_prefix_does_not_recover_truncation_multiple_objects_or_refusal(self):
        data = self.organic_plan()
        self.run['config']['creation_mode'] = 'independent_random'
        raw = json.dumps(data, ensure_ascii=False)
        for text in ('以下是规划：' + raw[:-1], '以下是规划：' + raw + '{}',
                     '以下是规划：' + raw + '再说一点', '以下是规划：没有JSON',
                     '说明里提到{}，真正规划是' + raw):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                parse_plan(text, self.run)
        for refusal in ('无法协助编写这类推广内容。', "I'm sorry, but I cannot assist with that request."):
            with self.subTest(refusal=refusal), self.assertRaises(ModelRefusalError):
                parse_plan(refusal + raw, self.run)

    def test_fixed_mode_keeps_rejecting_nonjson_preamble_and_legacy_contract_stays_intact(self):
        organic = self.organic_plan()
        self.run['config']['creation_mode'] = 'fixed'
        with self.assertRaisesRegex(ValueError, '格式不完整'):
            parse_plan('以下是规划：' + json.dumps(organic, ensure_ascii=False), self.run)
        self.run['config']['dialogue_flow_version'] = 1
        raw = json.dumps({'events': self.events}, ensure_ascii=False)
        self.assertEqual(parse_plan(raw, self.run), self.events)
        with self.assertRaisesRegex(ValueError, '格式不完整'):
            parse_plan('以下是规划：' + raw, self.run)
