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
