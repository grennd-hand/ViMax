import json
import unittest
from app.narrative_plan import DialogueEvent, module, parse_plan


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
