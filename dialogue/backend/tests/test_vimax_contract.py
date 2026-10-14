import unittest
from app.vimax_engine import decode_scenes, contract_errors, rebuild_memories

def run():
    return {'config':{'customer_count':1,'target_messages':3,'language':'zh-CN'},'architecture':'agent_team',
            'status':'completed','schedule':[{'stage':'自然群聊'}]*3,
            'agent_sessions':{'坐席':{'turns':0},'客户1':{'turns':0}}}

def source():
    return {'accounts':['原账号甲','原账号乙'],'codes':[],'urls':['https://example.invalid/task'],
            'contacts':[],'messages':[{'sender':'坐席','content':'打开 https://example.invalid/task 查看任务'}]}

class ContractTests(unittest.TestCase):
    def test_future_reply_rejected(self):
        with self.assertRaisesRegex(ValueError,'尚未出现'):
            decode_scenes(['001|坐席|0|大家好\n002|客户1|3|问题\n003|坐席|0|今天先到这里'],run())

    def test_unknown_actor_rejected(self):
        with self.assertRaisesRegex(ValueError,'未设置的人物'):
            decode_scenes(['001|坐席|0|大家好\n002|原账号甲|1|问题\n003|坐席|0|今天先到这里'],run())

    def test_count_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError,'实际生成2'):
            decode_scenes(['001|坐席|0|大家好\n002|客户1|1|问题'],run())

    def test_original_actor_coverage_not_required(self):
        r=run(); r['messages']=decode_scenes(['001|坐席|0|任务入口是 https://example.invalid/task\n002|客户1|1|知道了\n003|坐席|0|今天就到这里'],r)
        self.assertFalse(any('没有发言' in e for e in contract_errors(source(),r)))

    def test_new_link_rejected(self):
        r=run();r['messages']=decode_scenes(['001|坐席|0|任务入口是 https://other.invalid/task\n002|客户1|1|知道了\n003|坐席|0|今天就到这里'],r)
        self.assertTrue(any('母本之外' in e for e in contract_errors(source(),r)))

    def test_private_state_belongs_to_speaker(self):
        r=run();r['messages']=decode_scenes(['001|坐席|0|大家好\n002|客户1|1|我周六没有空\n003|坐席|2|那按你的安排来'],r)
        rebuild_memories(r)
        own=r['agent_sessions']['客户1']['memory_events']
        self.assertTrue(any(e['kind']=='own_statement' and e['seq']==2 for e in own))
        self.assertFalse(any(e['kind']=='own_statement' and e['seq']==1 for e in own))

    def test_unused_new_participant_is_not_a_goal_failure(self):
        r=run(); r['config']['customer_count']=2
        r['messages']=decode_scenes(['001|坐席|0|任务入口是 https://example.invalid/task\n002|客户1|1|知道了\n003|坐席|0|今天就到这里'],r)
        self.assertFalse(any('客户2' in e for e in contract_errors(source(),r)))

    def test_explicit_refusal_is_saved_as_personal_choice(self):
        r=run();r['messages']=decode_scenes(['001|坐席|0|大家好\n002|客户1|1|我不参加了。\n003|坐席|0|今天就到这里'],r)
        self.assertEqual(r['messages'][1]['task_state'],'declined')

    def test_departed_speaker_cannot_silently_return(self):
        r=run();r['messages']=decode_scenes(['001|坐席|0|大家好\n002|客户1|1|我先离开了。\n003|坐席|0|稍后再说'],r)
        r['messages'].append({**r['messages'][1],'seq':4,'content':'我的也提交好了。'})
        self.assertTrue(any('已离开' in e for e in contract_errors(source(),r)))

    def test_internal_review_language_is_rejected(self):
        r=run();r['messages']=decode_scenes(['001|坐席|0|大家好\n002|客户1|1|我明白了\n003|坐席|0|本轮目标已完成'],r)
        self.assertTrue(any('制作/审阅口吻' in e for e in contract_errors(source(),r)))

if __name__=='__main__':unittest.main()
